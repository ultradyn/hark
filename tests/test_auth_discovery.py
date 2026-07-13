"""CLI credential discovery for OpenAI / MiniMax (B076).

All paths use tmp_path fixtures — never read real secrets from the operator home.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hark.providers import auth as auth_mod
from hark.providers.auth import (
    extract_codex_openai_token,
    extract_legacy_minimax_token,
    extract_mmx_token,
    extract_opencode_provider_token,
    extract_pi_provider_token,
    minimax_auth,
    openai_auth,
    resolve_minimax_token,
    resolve_openai_token,
)


def _write_json(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def no_env_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("MMX_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


# ---------------------------------------------------------------------------
# Codex CLI
# ---------------------------------------------------------------------------


def test_codex_prefers_api_key_over_oauth(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "auth.json",
        {
            "OPENAI_API_KEY": "sk-codex-api-key",
            "auth_mode": "apikey",
            "tokens": {"access_token": "eyJ-oauth-should-not-win"},
        },
    )
    token, detail = extract_codex_openai_token(path)
    assert token == "sk-codex-api-key"
    assert "OPENAI_API_KEY" in detail


def test_codex_falls_back_to_oauth_access_token(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "auth.json",
        {
            "OPENAI_API_KEY": None,
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "eyJhbGciOi-fake-access"},
        },
    )
    token, detail = extract_codex_openai_token(path)
    assert token == "eyJhbGciOi-fake-access"
    assert "tokens.access_token" in detail


def test_codex_missing_file(tmp_path: Path) -> None:
    token, detail = extract_codex_openai_token(tmp_path / "nope.json")
    assert token is None
    assert "missing" in detail


# ---------------------------------------------------------------------------
# OpenCode
# ---------------------------------------------------------------------------


def test_opencode_openai_oauth(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "auth.json",
        {
            "openai": {
                "type": "oauth",
                "access": "eyJ-opencode-openai",
                "expires": 9_999_999_999_999,  # far future (ms)
            }
        },
    )
    token, detail = extract_opencode_provider_token(("openai",), auth_path=path)
    assert token == "eyJ-opencode-openai"
    assert "opencode" in detail


def test_opencode_minimax_prefix_key(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "auth.json",
        {
            "minimax-coding-plan": {
                "type": "api",
                "key": "sk-minimax-from-opencode",
            }
        },
    )
    token, detail = extract_opencode_provider_token(
        ("minimax", "minimax-coding-plan"), auth_path=path
    )
    assert token == "sk-minimax-from-opencode"
    assert "minimax-coding-plan" in detail


def test_opencode_skips_expired(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "auth.json",
        {
            "openai": {
                "type": "oauth",
                "access": "eyJ-expired",
                "expires": 1_000,  # ancient (ms)
            }
        },
    )
    token, _ = extract_opencode_provider_token(("openai",), auth_path=path)
    assert token is None


# ---------------------------------------------------------------------------
# Pi agent
# ---------------------------------------------------------------------------


def test_pi_minimax_api_key(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "auth.json",
        {"minimax": {"type": "api_key", "key": "sk-pi-minimax"}},
    )
    token, detail = extract_pi_provider_token(("minimax",), auth_path=path)
    assert token == "sk-pi-minimax"
    assert "pi" in detail


def test_pi_openai_codex_oauth(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "auth.json",
        {
            "openai-codex": {
                "type": "oauth",
                "access": "eyJ-pi-codex",
                "expires": 9_999_999_999_999,
            }
        },
    )
    token, detail = extract_pi_provider_token(
        ("openai", "openai-codex"), auth_path=path
    )
    assert token == "eyJ-pi-codex"
    assert "openai-codex" in detail


# ---------------------------------------------------------------------------
# mmx CLI
# ---------------------------------------------------------------------------


def test_mmx_prefers_api_key_over_oauth(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "config.json",
        {
            "api_key": "sk-mmx-api",
            "oauth": {
                "access_token": "oat-should-not-win",
                "refresh_token": "r",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        },
    )
    token, detail = extract_mmx_token(path)
    assert token == "sk-mmx-api"
    assert "api_key" in detail


def test_mmx_oauth_access_token(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "config.json",
        {
            "region": "global",
            "oauth": {
                "access_token": "oat-from-mmx",
                "refresh_token": "dfrt-x",
                "expires_at": "2099-07-15T15:45:05.978Z",
            },
        },
    )
    token, detail = extract_mmx_token(path)
    assert token == "oat-from-mmx"
    assert "oauth.access_token" in detail


def test_mmx_skips_expired_oauth(tmp_path: Path) -> None:
    path = _write_json(
        tmp_path / "config.json",
        {
            "oauth": {
                "access_token": "oat-expired",
                "refresh_token": "r",
                "expires_at": "2020-01-01T00:00:00Z",
            }
        },
    )
    token, _ = extract_mmx_token(path)
    assert token is None


# ---------------------------------------------------------------------------
# Legacy ~/.minimax
# ---------------------------------------------------------------------------


def test_legacy_minimax_plain_key_file(tmp_path: Path) -> None:
    path = tmp_path / ".minimax"
    path.write_text("sk-legacy-key\n", encoding="utf-8")
    token, detail = extract_legacy_minimax_token(path)
    assert token == "sk-legacy-key"
    assert "legacy" in detail


def test_legacy_minimax_dir_config_json(tmp_path: Path) -> None:
    root = tmp_path / ".minimax"
    _write_json(root / "config.json", {"api_key": "sk-legacy-dir"})
    token, detail = extract_legacy_minimax_token(root)
    assert token == "sk-legacy-dir"
    assert "legacy" in detail


# ---------------------------------------------------------------------------
# resolve_* precedence (env wins; then CLI chain)
# ---------------------------------------------------------------------------


def test_openai_resolve_env_wins_over_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env_keys: None
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-openai")
    codex = _write_json(
        tmp_path / "codex" / "auth.json",
        {"OPENAI_API_KEY": "sk-codex-should-lose"},
    )
    monkeypatch.setattr(auth_mod, "extract_codex_openai_token", lambda: (None, "skip"))
    # even if extract were called, env short-circuits
    token, detail = resolve_openai_token()
    assert token == "sk-env-openai"
    assert detail.startswith("OPENAI_API_KEY")
    assert codex.is_file()  # fixture exists; unused due to env


def test_openai_resolve_codex_then_opencode_then_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_env_keys: None
) -> None:
    calls: list[str] = []

    def codex() -> tuple[str | None, str]:
        calls.append("codex")
        return None, "no codex"

    def opencode(
        prefixes: tuple[str, ...], *, auth_path: Path | None = None
    ) -> tuple[str | None, str]:
        calls.append("opencode")
        return "sk-from-opencode", "opencode (openai api_key from mock)"

    def pi(
        keys: tuple[str, ...], *, auth_path: Path | None = None
    ) -> tuple[str | None, str]:
        calls.append("pi")
        return "sk-from-pi", "pi (openai api_key from mock)"

    monkeypatch.setattr(auth_mod, "extract_codex_openai_token", codex)
    monkeypatch.setattr(auth_mod, "extract_opencode_provider_token", opencode)
    monkeypatch.setattr(auth_mod, "extract_pi_provider_token", pi)

    token, detail = resolve_openai_token()
    assert token == "sk-from-opencode"
    assert calls == ["codex", "opencode"]  # pi not needed
    assert "opencode" in detail


def test_minimax_resolve_mmx_before_pi(
    monkeypatch: pytest.MonkeyPatch, no_env_keys: None
) -> None:
    monkeypatch.setattr(
        auth_mod,
        "extract_mmx_token",
        lambda: ("oat-mmx", "mmx (oauth.access_token from mock)"),
    )
    monkeypatch.setattr(
        auth_mod,
        "extract_pi_provider_token",
        lambda *a, **k: ("sk-pi", "pi"),
    )
    token, detail = resolve_minimax_token()
    assert token == "oat-mmx"
    assert detail.startswith("mmx")


def test_openai_auth_status_source_codex(
    monkeypatch: pytest.MonkeyPatch, no_env_keys: None
) -> None:
    monkeypatch.setattr(
        auth_mod,
        "resolve_openai_token",
        lambda: ("sk-x", "codex (OPENAI_API_KEY from /tmp/auth.json)"),
    )
    st = openai_auth()
    assert st.available is True
    assert st.source == "codex"


def test_minimax_auth_status_source_mmx(
    monkeypatch: pytest.MonkeyPatch, no_env_keys: None
) -> None:
    monkeypatch.setattr(
        auth_mod,
        "resolve_minimax_token",
        lambda: ("oat-x", "mmx (oauth.access_token from /tmp/config.json)"),
    )
    st = minimax_auth()
    assert st.available is True
    assert st.source == "mmx"


def test_openai_auth_unavailable_lists_sources(
    monkeypatch: pytest.MonkeyPatch, no_env_keys: None
) -> None:
    monkeypatch.setattr(
        auth_mod, "extract_codex_openai_token", lambda: (None, "missing")
    )
    monkeypatch.setattr(
        auth_mod,
        "extract_opencode_provider_token",
        lambda *a, **k: (None, "missing"),
    )
    monkeypatch.setattr(
        auth_mod,
        "extract_pi_provider_token",
        lambda *a, **k: (None, "missing"),
    )
    st = openai_auth()
    assert st.available is False
    assert st.source is None
    assert "OPENAI_API_KEY" in st.detail
    assert "codex" in st.detail.lower() or "pi" in st.detail


def test_providers_use_resolved_keys(
    monkeypatch: pytest.MonkeyPatch, no_env_keys: None
) -> None:
    """openai_p / minimax call resolvers, not only os.environ."""
    from hark.providers import minimax as minimax_mod
    from hark.providers import openai_p as openai_mod

    monkeypatch.setattr(openai_mod, "resolve_openai_api_key", lambda: "sk-resolved-oai")
    monkeypatch.setattr(minimax_mod, "resolve_minimax_api_key", lambda: "sk-resolved-mm")
    assert openai_mod._key() == "sk-resolved-oai"
    assert minimax_mod._key() == "sk-resolved-mm"


def test_path_helpers_honor_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hark.paths import codex_auth_path, mmx_config_path, opencode_auth_path

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("MMX_CONFIG_DIR", str(tmp_path / "m"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert codex_auth_path() == tmp_path / "c" / "auth.json"
    assert mmx_config_path() == tmp_path / "m" / "config.json"
    assert opencode_auth_path() == tmp_path / "xdg" / "opencode" / "auth.json"

"""Provider credential discovery (Grok OAuth preferred for xAI).

Also reuses operator CLI stores for OpenAI / MiniMax when env keys are unset:

- Codex CLI ``~/.codex/auth.json`` (or ``$CODEX_HOME/auth.json``)
- OpenCode ``$XDG_DATA_HOME/opencode/auth.json`` (default ``~/.local/share/…``)
- Pi agent ``~/.pi/agent/auth.json``
- MiniMax CLI ``mmx``: ``~/.mmx/config.json`` (or ``$MMX_CONFIG_DIR/config.json``)
- Legacy ``~/.minimax`` file (API key text), if present

Env vars still work (fail-open). Tokens are never logged.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from hark.paths import (
    codex_auth_path,
    grok_auth_path,
    legacy_minimax_path,
    mmx_config_path,
    opencode_auth_path,
    pi_agent_auth_path,
)


@dataclass
class AuthStatus:
    name: str
    available: bool
    source: str | None = None  # grok_oauth | env | codex | opencode | pi | mmx | legacy | …
    detail: str = ""
    # token is never included in doctor JSON by default


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _str_secret(value: Any) -> str | None:
    if isinstance(value, str):
        s = value.strip()
        if s:
            return s
    return None


def _not_expired(entry: dict[str, Any]) -> bool:
    """Return False when a credential entry has a clearly past expiry.

    Accepts unix seconds, unix milliseconds, or ISO-8601 strings. Missing /
    unparsable expiry → treat as usable (fail-open).
    """
    exp = (
        entry.get("expires_at")
        or entry.get("expires")
        or entry.get("expiry")
        or entry.get("exp")
    )
    if exp is None:
        return True
    now = time.time()
    if isinstance(exp, (int, float)):
        # Heuristic: values > year ~2001 in ms are milliseconds
        ts = float(exp) / 1000.0 if exp > 1e12 else float(exp)
        return ts >= now - 60
    if isinstance(exp, str):
        s = exp.strip()
        if not s:
            return True
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).timestamp() >= now - 60
        except ValueError:
            return True
    return True


def _entry_token(entry: Any, *, prefer_api_key: bool = True) -> str | None:
    """Extract a usable secret from a provider auth entry dict."""
    if not isinstance(entry, dict):
        return None
    if not _not_expired(entry):
        return None
    key = _str_secret(entry.get("key")) or _str_secret(entry.get("api_key"))
    access = (
        _str_secret(entry.get("access"))
        or _str_secret(entry.get("access_token"))
        or _str_secret(entry.get("token"))
    )
    if prefer_api_key:
        return key or access
    return access or key


def extract_grok_access_token(auth_path: Path | None = None) -> tuple[str | None, str]:
    """Return (token, source_detail) from ~/.grok/auth.json if usable.

    Prefer session/access tokens over static API keys when both exist.
    Never log the token.
    """
    path = auth_path or grok_auth_path()
    if not path.is_file():
        return None, "missing ~/.grok/auth.json (run: grok login)"

    data = _read_json(path)
    if not isinstance(data, dict):
        return None, "auth.json is not a JSON object"

    now = time.time()
    best_token: str | None = None
    best_kind = ""

    # Common shapes: map of issuer::client_id -> {key/access_token/token, expires...}
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue
        token = (
            entry.get("access_token")
            or entry.get("token")
            or entry.get("key")
            or entry.get("session_token")
        )
        if not token or not isinstance(token, str):
            continue
        exp = entry.get("expires_at") or entry.get("expiry") or entry.get("exp")
        if isinstance(exp, (int, float)) and exp < now - 60:
            continue
        # Prefer JWT-looking access tokens (eyJ...) over short API keys when both present
        kind = "access_token" if token.startswith("eyJ") else "api_key"
        if best_token is None or (kind == "access_token" and best_kind != "access_token"):
            best_token = token
            best_kind = kind

    if best_token:
        return best_token, f"grok_oauth ({best_kind} from {path})"

    return None, "auth.json present but no usable token (try: grok login)"


def extract_codex_openai_token(auth_path: Path | None = None) -> tuple[str | None, str]:
    """Codex CLI auth.json → OpenAI-compatible credential.

    Prefer ``OPENAI_API_KEY`` (API key mode) over ChatGPT OAuth access token.
    """
    path = auth_path or codex_auth_path()
    if not path.is_file():
        return None, "missing Codex auth.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        return None, "Codex auth.json is not a JSON object"

    api_key = _str_secret(data.get("OPENAI_API_KEY"))
    if api_key:
        return api_key, f"codex (OPENAI_API_KEY from {path})"

    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        access = _str_secret(tokens.get("access_token"))
        if access:
            return access, f"codex (tokens.access_token from {path})"

    return None, "Codex auth.json present but no usable OpenAI credential"


def extract_opencode_provider_token(
    provider_prefixes: tuple[str, ...],
    *,
    auth_path: Path | None = None,
) -> tuple[str | None, str]:
    """OpenCode auth.json → first matching provider credential."""
    path = auth_path or opencode_auth_path()
    if not path.is_file():
        return None, "missing OpenCode auth.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        return None, "OpenCode auth.json is not a JSON object"

    # Exact key match first, then prefix (e.g. minimax-coding-plan)
    candidates: list[tuple[str, Any]] = []
    lower_map = {str(k).lower(): (str(k), v) for k, v in data.items()}
    for prefix in provider_prefixes:
        p = prefix.lower()
        if p in lower_map:
            candidates.append(lower_map[p])
    for prefix in provider_prefixes:
        p = prefix.lower()
        for lk, pair in lower_map.items():
            if lk.startswith(p) and pair not in candidates:
                candidates.append(pair)

    for name, entry in candidates:
        token = _entry_token(entry)
        if token:
            kind = "api_key" if not token.startswith("eyJ") else "oauth"
            return token, f"opencode ({name} {kind} from {path})"

    return None, f"OpenCode auth.json has no usable entry for {','.join(provider_prefixes)}"


def extract_pi_provider_token(
    provider_keys: tuple[str, ...],
    *,
    auth_path: Path | None = None,
) -> tuple[str | None, str]:
    """Pi agent auth.json → first matching provider credential."""
    path = auth_path or pi_agent_auth_path()
    if not path.is_file():
        return None, "missing Pi agent auth.json"
    data = _read_json(path)
    if not isinstance(data, dict):
        return None, "Pi agent auth.json is not a JSON object"

    lower_map = {str(k).lower(): (str(k), v) for k, v in data.items()}
    for want in provider_keys:
        pair = lower_map.get(want.lower())
        if not pair:
            continue
        name, entry = pair
        token = _entry_token(entry)
        if token:
            entry_type = entry.get("type") if isinstance(entry, dict) else None
            if entry_type in ("api_key", "api", "key") or not token.startswith("eyJ"):
                kind = "api_key"
            else:
                kind = "oauth"
            return token, f"pi ({name} {kind} from {path})"

    return None, f"Pi auth.json has no usable entry for {','.join(provider_keys)}"


def extract_mmx_token(config_path: Path | None = None) -> tuple[str | None, str]:
    """MiniMax CLI (`mmx`) config.json → api_key or oauth access_token.

    Prefer static ``api_key`` over OAuth when both exist (matches mmx flag/file
    precedence for non-interactive use of a saved key).
    """
    path = config_path or mmx_config_path()
    if not path.is_file():
        return None, "missing ~/.mmx/config.json (run: mmx auth login)"
    data = _read_json(path)
    if not isinstance(data, dict):
        return None, "mmx config.json is not a JSON object"

    api_key = _str_secret(data.get("api_key"))
    if api_key:
        return api_key, f"mmx (api_key from {path})"

    oauth = data.get("oauth")
    if isinstance(oauth, dict) and _not_expired(oauth):
        access = _str_secret(oauth.get("access_token"))
        if access:
            return access, f"mmx (oauth.access_token from {path})"

    return None, "mmx config.json present but no usable credential (try: mmx auth login)"


def extract_legacy_minimax_token(path: Path | None = None) -> tuple[str | None, str]:
    """Legacy ``~/.minimax`` — file with raw API key, or dir with common filenames."""
    p = path or legacy_minimax_path()
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return None, "legacy ~/.minimax unreadable"
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                return s, f"legacy ({p})"
        s = text.strip()
        if s:
            return s, f"legacy ({p})"
        return None, "legacy ~/.minimax empty"

    if p.is_dir():
        for name in ("api_key", "api_key.txt", "auth.json", "config.json", "credentials"):
            child = p / name
            if not child.is_file():
                continue
            if child.suffix == ".json" or name.endswith(".json"):
                data = _read_json(child)
                if isinstance(data, dict):
                    tok = (
                        _str_secret(data.get("api_key"))
                        or _str_secret(data.get("key"))
                        or _str_secret(data.get("MINIMAX_API_KEY"))
                    )
                    if tok:
                        return tok, f"legacy ({child})"
                    oauth = data.get("oauth")
                    if isinstance(oauth, dict):
                        tok = _str_secret(oauth.get("access_token"))
                        if tok:
                            return tok, f"legacy ({child})"
            else:
                try:
                    text = child.read_text(encoding="utf-8")
                except OSError:
                    continue
                for line in text.splitlines():
                    s = line.strip()
                    if s and not s.startswith("#"):
                        return s, f"legacy ({child})"
        return None, "legacy ~/.minimax dir has no known credential file"

    return None, "missing legacy ~/.minimax"


def xai_auth() -> AuthStatus:
    token, detail = extract_grok_access_token()
    if token:
        return AuthStatus(name="xai", available=True, source="grok_oauth", detail=detail)
    env = os.environ.get("XAI_API_KEY")
    if env:
        return AuthStatus(
            name="xai",
            available=True,
            source="env",
            detail="XAI_API_KEY set",
        )
    return AuthStatus(
        name="xai",
        available=False,
        source=None,
        detail=f"{detail}; or set XAI_API_KEY",
    )


def resolve_openai_token() -> tuple[str | None, str]:
    """Resolve OpenAI credential: env → Codex → OpenCode → Pi.

    Returns (token, detail). Prefer env so explicit keys always win (fail-open).
    """
    env = _str_secret(os.environ.get("OPENAI_API_KEY"))
    if env:
        return env, "OPENAI_API_KEY set"

    token, detail = extract_codex_openai_token()
    if token:
        return token, detail

    token, detail = extract_opencode_provider_token(("openai",))
    if token:
        return token, detail

    token, detail = extract_pi_provider_token(("openai", "openai-codex"))
    if token:
        return token, detail

    return None, (
        "set OPENAI_API_KEY; or login via codex / opencode auth / pi "
        "(~/.codex/auth.json, OpenCode auth.json, ~/.pi/agent/auth.json)"
    )


def openai_auth() -> AuthStatus:
    token, detail = resolve_openai_token()
    if not token:
        return AuthStatus(name="openai", available=False, source=None, detail=detail)
    if detail.startswith("OPENAI_API_KEY"):
        source = "env"
    elif detail.startswith("codex"):
        source = "codex"
    elif detail.startswith("opencode"):
        source = "opencode"
    elif detail.startswith("pi"):
        source = "pi"
    else:
        source = "cli"
    return AuthStatus(name="openai", available=True, source=source, detail=detail)


def google_auth() -> AuthStatus:
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        which = "GEMINI_API_KEY" if os.environ.get("GEMINI_API_KEY") else "GOOGLE_API_KEY"
        return AuthStatus(
            name="google", available=True, source="env", detail=f"{which} set"
        )
    return AuthStatus(
        name="google",
        available=False,
        source=None,
        detail="set GEMINI_API_KEY or GOOGLE_API_KEY",
    )


def resolve_minimax_token() -> tuple[str | None, str]:
    """Resolve MiniMax credential: env → mmx → Pi → OpenCode → legacy ~/.minimax."""
    env = _str_secret(os.environ.get("MINIMAX_API_KEY"))
    if env:
        return env, "MINIMAX_API_KEY set"

    token, detail = extract_mmx_token()
    if token:
        return token, detail

    token, detail = extract_pi_provider_token(("minimax",))
    if token:
        return token, detail

    token, detail = extract_opencode_provider_token(("minimax", "minimax-coding-plan"))
    if token:
        return token, detail

    token, detail = extract_legacy_minimax_token()
    if token:
        return token, detail

    return None, (
        "set MINIMAX_API_KEY; or mmx auth login (~/.mmx/config.json); "
        "or Pi/OpenCode minimax keys; or legacy ~/.minimax"
    )


def minimax_auth() -> AuthStatus:
    token, detail = resolve_minimax_token()
    if not token:
        return AuthStatus(name="minimax", available=False, source=None, detail=detail)
    if detail.startswith("MINIMAX_API_KEY"):
        source = "env"
    elif detail.startswith("mmx"):
        source = "mmx"
    elif detail.startswith("pi"):
        source = "pi"
    elif detail.startswith("opencode"):
        source = "opencode"
    elif detail.startswith("legacy"):
        source = "legacy"
    else:
        source = "cli"
    return AuthStatus(name="minimax", available=True, source=source, detail=detail)


def anthropic_auth() -> AuthStatus:
    # No public STT; still report API key for orchestrator awareness
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AuthStatus(
            name="anthropic",
            available=False,
            source="env",
            detail="key set but public STT/TTS unsupported for hark",
        )
    return AuthStatus(
        name="anthropic",
        available=False,
        source=None,
        detail="unsupported STT/TTS (use as orchestrator only)",
    )


def all_provider_status() -> list[AuthStatus]:
    return [
        xai_auth(),
        openai_auth(),
        google_auth(),
        minimax_auth(),
        anthropic_auth(),
    ]


def resolve_xai_token() -> str | None:
    token, _ = extract_grok_access_token()
    if token:
        return token
    return os.environ.get("XAI_API_KEY")


def resolve_openai_api_key() -> str | None:
    """Token only (for providers)."""
    token, _ = resolve_openai_token()
    return token


def resolve_minimax_api_key() -> str | None:
    """Token only (for providers)."""
    token, _ = resolve_minimax_token()
    return token

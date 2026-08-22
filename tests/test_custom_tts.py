"""Custom TTS provider — OpenAI-compatible /audio/speech (+ optional /tts)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from hark.config import TtsConfig, config_to_dict, load_config
from hark.providers.base import ProviderError, SynthResult
from hark.providers.custom_tts import CustomTts, custom_tts_status
from hark.providers.resolve import resolve_tts


class _FakeResponse:
    def __init__(self, status_code: int, payload: bytes | str, headers: dict | None = None) -> None:
        self.status_code = status_code
        self.content = payload if isinstance(payload, bytes) else payload.encode()
        self.text = payload if isinstance(payload, str) else payload.decode(errors="replace")
        self.headers = headers or {}

    def json(self) -> dict:
        raise ValueError("not json")


class _FakeClient:
    def __init__(self, *a, **k) -> None:
        pass

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *a) -> None:
        return None


def test_custom_tts_synthesize_openai_path(monkeypatch) -> None:
    captured: dict = {}

    class _Client(_FakeClient):
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["json"] = dict(json or {})
            return _FakeResponse(
                200, b"\x00\x01\x02mp3", {"content-type": "audio/mpeg"}
            )

    monkeypatch.setattr(httpx, "Client", _Client)
    tts = CustomTts(
        base_url="https://gw.example.com/v1/",
        api_key="tok-abc",
        model="gpt-4o-mini-tts",
    )
    res = tts.synthesize("hello world")
    assert isinstance(res, SynthResult)
    assert res.provider == "custom"
    assert res.audio == b"\x00\x01\x02mp3"
    assert res.content_type == "audio/mpeg"
    assert res.voice == "alloy"
    assert captured["url"] == "https://gw.example.com/v1/audio/speech"
    assert captured["headers"]["Authorization"] == "Bearer tok-abc"
    assert captured["json"] == {
        "input": "hello world",
        "voice": "alloy",
        "model": "gpt-4o-mini-tts",
    }


def test_custom_tts_voice_precedence(monkeypatch) -> None:
    captured: dict = {}

    class _Client(_FakeClient):
        def post(self, url, headers=None, json=None):
            captured["json"] = dict(json or {})
            return _FakeResponse(200, b"au", {"content-type": "audio/mpeg"})

    monkeypatch.setattr(httpx, "Client", _Client)
    # explicit call-site voice wins over ctor voice
    tts = CustomTts(
        base_url="https://gw.example.com/v1",
        api_key="tok",
        model="m",
        voice="coral",
    )
    assert tts.synthesize("x", voice="eve").voice == "eve"
    # ctor voice next
    tts2 = CustomTts(
        base_url="https://gw.example.com/v1",
        api_key="tok",
        model="m",
        voice="coral",
    )
    assert tts2.synthesize("x").voice == "coral"
    # then default alloy
    tts3 = CustomTts(base_url="https://gw.example.com/v1", api_key="tok", model="m")
    assert tts3.synthesize("x").voice == "alloy"


def test_custom_tts_native_tts_path(monkeypatch) -> None:
    captured: dict = {}

    class _Client(_FakeClient):
        def post(self, url, headers=None, json=None):
            captured["url"] = url
            captured["json"] = dict(json or {})
            return _FakeResponse(200, b"au2", {"content-type": "audio/mpeg"})

    monkeypatch.setattr(httpx, "Client", _Client)
    tts = CustomTts(
        base_url="https://gw.example.com/v1",
        api_key="tok",
        model=None,
        voice="eve",
        path="/tts",
    )
    res = tts.synthesize("ping")
    assert res.audio == b"au2"
    assert captured["url"] == "https://gw.example.com/v1/tts"
    # native dual-mount: model omitted when not configured
    assert "model" not in captured["json"]
    assert captured["json"]["voice"] == "eve"


def test_custom_tts_path_must_start_with_slash() -> None:
    with pytest.raises(ProviderError, match="custom_path"):
        CustomTts(
            base_url="https://gw.example.com/v1",
            api_key="tok",
            model="m",
            path="audio/speech",
        )


def test_custom_tts_openai_path_requires_model() -> None:
    with pytest.raises(ProviderError, match="custom_model"):
        CustomTts(
            base_url="https://gw.example.com/v1",
            api_key="tok",
            model="",
            path="/audio/speech",
        )


def test_custom_tts_http_error(monkeypatch) -> None:
    class _Client(_FakeClient):
        def post(self, url, headers=None, json=None):
            return _FakeResponse(501, "not_supported")

    monkeypatch.setattr(httpx, "Client", _Client)
    tts = CustomTts(base_url="https://gw.example.com/v1", api_key="tok", model="m1")
    with pytest.raises(ProviderError, match="HTTP 501"):
        tts.synthesize("x")


def test_custom_tts_401_gets_hint(monkeypatch) -> None:
    class _Client(_FakeClient):
        def post(self, url, headers=None, json=None):
            return _FakeResponse(401, "unauthorized")

    monkeypatch.setattr(httpx, "Client", _Client)
    tts = CustomTts(base_url="https://gw.example.com/v1", api_key="tok", model="m1")
    with pytest.raises(ProviderError, match="401"):
        tts.synthesize("x")


def test_custom_tts_requires_api_key() -> None:
    with pytest.raises(ProviderError, match="api key|API key|custom_api_key"):
        CustomTts(base_url="https://gw.example.com/v1", api_key="", model="m1")


def test_custom_tts_requires_base_url() -> None:
    with pytest.raises(ProviderError, match="custom_base_url"):
        CustomTts(base_url="", api_key="tok", model="m1")


def test_custom_tts_status_reports_readiness() -> None:
    st = custom_tts_status(
        base_url="https://gw.example.com/v1",
        api_key="tok",
        model="m1",
        voice="eve",
        path="/audio/speech",
    )
    assert st.available is True
    assert "gw.example.com" in st.detail
    assert "m1" in st.detail
    assert "eve" in st.detail

    st2 = custom_tts_status(base_url=None, api_key=None, model=None)
    assert st2.available is False
    assert "base_url" in st2.detail.lower() or "not configured" in st2.detail.lower()

    # base_url + key but missing model on OpenAI path → not ready
    st3 = custom_tts_status(
        base_url="https://gw.example.com/v1", api_key="tok", model=None
    )
    assert st3.available is False
    assert "custom_model" in st3.detail

    # native path: model not required
    st4 = custom_tts_status(
        base_url="https://gw.example.com/v1", api_key="tok", model=None, path="/tts"
    )
    assert st4.available is True


def test_resolve_custom_tts(monkeypatch) -> None:
    cfg = TtsConfig(
        provider="custom",
        custom_base_url="https://gw.example.com/v1",
        custom_api_key="tok",
        custom_model="gpt-4o-mini-tts",
        custom_voice="coral",
        custom_path="/audio/speech",
    )
    tts = resolve_tts("custom", tts_cfg=cfg)
    assert tts.name == "custom"
    assert isinstance(tts, CustomTts)
    assert tts.base_url == "https://gw.example.com/v1"
    assert tts.model == "gpt-4o-mini-tts"
    assert tts.voice == "coral"


def test_resolve_custom_tts_aliases() -> None:
    cfg = TtsConfig(
        provider="custom",
        custom_base_url="https://gw.example.com/v1",
        custom_api_key="tok",
        custom_model="m",
    )
    for alias in ("custom", "custom_tts", "custom-tts"):
        tts = resolve_tts(alias, tts_cfg=cfg)
        assert isinstance(tts, CustomTts)


def test_resolve_custom_tts_missing_base_url() -> None:
    with pytest.raises(ProviderError, match="custom_base_url"):
        resolve_tts(
            "custom",
            tts_cfg=TtsConfig(provider="custom", custom_api_key="tok", custom_model="m"),
        )


def test_resolve_custom_tts_disabled() -> None:
    cfg = TtsConfig(
        provider="custom",
        disabled=["custom"],
        custom_base_url="https://gw.example.com/v1",
        custom_api_key="tok",
        custom_model="m",
    )
    with pytest.raises(ProviderError, match="disabled"):
        resolve_tts("custom", tts_cfg=cfg)


def test_auto_does_not_select_custom_tts(monkeypatch) -> None:
    monkeypatch.setattr(
        "hark.providers.resolve.xai_auth",
        lambda: MagicMock(available=False),
    )
    monkeypatch.setattr(
        "hark.providers.resolve.openai_auth",
        lambda: MagicMock(available=True),
    )
    tts = resolve_tts(
        "auto",
        tts_cfg=TtsConfig(
            custom_base_url="https://gw.example.com/v1",
            custom_api_key="tok",
            custom_model="m",
        ),
    )
    assert tts.name == "openai"


def test_config_loads_custom_tts_fields(tmp_path: Path, monkeypatch) -> None:
    for var in (
        "HARK_TTS_PROVIDER",
        "HARK_TTS_CUSTOM_BASE_URL",
        "HARK_TTS_CUSTOM_API_KEY",
        "HARK_TTS_CUSTOM_MODEL",
        "HARK_TTS_CUSTOM_VOICE",
        "HARK_TTS_CUSTOM_PATH",
    ):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "config.toml"
    path.write_text(
        """
[tts]
provider = "custom"
custom_base_url = "https://gw.example.com/v1"
custom_api_key = "cfg-key"
custom_model = "gpt-4o-mini-tts"
custom_voice = "coral"
custom_path = "/audio/speech"
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.tts.provider == "custom"
    assert cfg.tts.custom_base_url == "https://gw.example.com/v1"
    assert cfg.tts.custom_api_key == "cfg-key"
    assert cfg.tts.custom_model == "gpt-4o-mini-tts"
    assert cfg.tts.custom_voice == "coral"
    assert cfg.tts.custom_path == "/audio/speech"
    d = config_to_dict(cfg)
    assert d["tts"]["provider"] == "custom"
    assert d["tts"]["custom_base_url"] == "https://gw.example.com/v1"
    assert d["tts"]["custom_model"] == "gpt-4o-mini-tts"
    assert d["tts"]["custom_voice"] == "coral"
    # Never leak the secret into redacted config dumps.
    assert "custom_api_key" not in d["tts"] or d["tts"].get("custom_api_key") in (
        None,
        True,
        False,
        "***",
    )
    assert d["tts"].get("custom_api_key_configured") is True


def test_env_overrides_custom_tts(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[tts]
provider = "auto"
custom_base_url = "https://cfg.example/v1"
custom_model = "from-cfg"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARK_TTS_PROVIDER", "custom")
    monkeypatch.setenv("HARK_TTS_CUSTOM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("HARK_TTS_CUSTOM_API_KEY", "env-key")
    monkeypatch.setenv("HARK_TTS_CUSTOM_MODEL", "from-env")
    monkeypatch.setenv("HARK_TTS_CUSTOM_VOICE", "eve")
    monkeypatch.setenv("HARK_TTS_CUSTOM_PATH", "/tts")
    cfg = load_config(path)
    assert cfg.tts.provider == "custom"
    assert cfg.tts.custom_base_url == "https://env.example/v1"
    assert cfg.tts.custom_api_key == "env-key"
    assert cfg.tts.custom_model == "from-env"
    assert cfg.tts.custom_voice == "eve"
    assert cfg.tts.custom_path == "/tts"


def test_custom_tts_key_file_and_command(tmp_path: Path, monkeypatch) -> None:
    keyfile = tmp_path / "key"
    keyfile.write_text("file-key\n", encoding="utf-8")
    monkeypatch.setenv("HARK_TTS_CUSTOM_BASE_URL", "https://gw.example.com/v1")
    monkeypatch.setenv("HARK_TTS_CUSTOM_MODEL", "m")
    # file beats command
    monkeypatch.setenv("HARK_TTS_CUSTOM_API_KEY_FILE", str(keyfile))
    monkeypatch.setenv("HARK_TTS_CUSTOM_API_KEY_COMMAND", "echo cmd-key")
    cfg = load_config(tmp_path / "empty.toml")
    assert cfg.tts.custom_api_key_file == str(keyfile)
    from hark.providers.custom_stt import resolve_custom_api_key

    key = resolve_custom_api_key(
        None,
        getattr(cfg.tts, "custom_api_key_file", None),
        getattr(cfg.tts, "custom_api_key_command", None),
        label="custom TTS",
    )
    assert key == "file-key"
    # literal beats file
    assert resolve_custom_api_key("lit", str(keyfile), None) == "lit"
    # command fallback when no literal/file
    assert resolve_custom_api_key(None, None, "echo cmd-key") == "cmd-key"
    # TTS label on command failure
    with pytest.raises(ProviderError, match="custom TTS"):
        resolve_custom_api_key(None, None, "false", label="custom TTS")

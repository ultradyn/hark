"""Provider disable lists, MiniMax TTS consent, OpenAI STT model."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from hark.config import SttConfig, TtsConfig, load_config
from hark.providers.base import ProviderError
from hark.providers.openai_p import OpenAIStt
from hark.providers.resolve import (
    persist_tts_minimax_ok,
    resolve_stt,
    resolve_tts,
)


def test_config_loads_disabled_and_minimax_ok(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[stt]
provider = "auto"
disabled = ["google", "gemini"]

[tts]
provider = "auto"
disabled = ["openai"]
minimax_ok = true
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_STT_DISABLED", raising=False)
    monkeypatch.delenv("HARK_TTS_DISABLED", raising=False)
    monkeypatch.delenv("HARK_TTS_MINIMAX_OK", raising=False)
    cfg = load_config(path)
    assert cfg.stt.disabled == ["google", "gemini"]
    assert cfg.tts.disabled == ["openai"]
    assert cfg.tts.minimax_ok is True


def test_env_overrides_disabled_and_minimax_ok(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[tts]\nminimax_ok = false\n", encoding="utf-8")
    monkeypatch.setenv("HARK_STT_DISABLED", "xai,openai")
    monkeypatch.setenv("HARK_TTS_DISABLED", "minimax")
    monkeypatch.setenv("HARK_TTS_MINIMAX_OK", "1")
    cfg = load_config(path)
    assert cfg.stt.disabled == ["xai", "openai"]
    assert cfg.tts.disabled == ["minimax"]
    assert cfg.tts.minimax_ok is True


def test_stt_auto_skips_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        "hark.providers.resolve.xai_auth",
        lambda: MagicMock(available=True),
    )
    monkeypatch.setattr(
        "hark.providers.resolve.openai_auth",
        lambda: MagicMock(available=True),
    )
    monkeypatch.setattr(
        "hark.providers.resolve.google_auth",
        lambda: MagicMock(available=False),
    )
    stt = resolve_stt("auto", stt_cfg=SttConfig(disabled=["xai"]))
    assert stt.name == "openai"


def test_stt_pin_disabled_raises() -> None:
    with pytest.raises(ProviderError, match="disabled"):
        resolve_stt("google", stt_cfg=SttConfig(disabled=["gemini"]))


def test_tts_auto_skips_disabled_and_minimax_without_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        "hark.providers.resolve.xai_auth",
        lambda: MagicMock(available=False),
    )
    monkeypatch.setattr(
        "hark.providers.resolve.openai_auth",
        lambda: MagicMock(available=False),
    )
    monkeypatch.setattr(
        "hark.providers.resolve.minimax_auth",
        lambda: MagicMock(available=True),
    )
    monkeypatch.setattr(
        "hark.providers.resolve.google_auth",
        lambda: MagicMock(available=True),
    )
    monkeypatch.delenv("HARK_TTS_MINIMAX_OK", raising=False)
    tts = resolve_tts(
        "auto",
        tts_cfg=TtsConfig(minimax_ok=False),
        allow_prompt=False,
    )
    assert tts.name == "google"


def test_tts_minimax_pin_requires_ok(monkeypatch) -> None:
    monkeypatch.delenv("HARK_TTS_MINIMAX_OK", raising=False)
    with pytest.raises(ProviderError, match="minimax_ok"):
        resolve_tts(
            "minimax",
            tts_cfg=TtsConfig(minimax_ok=False),
            allow_prompt=False,
        )


def test_tts_minimax_ok_allows(monkeypatch) -> None:
    monkeypatch.setattr(
        "hark.providers.minimax.resolve_minimax_api_key",
        lambda: "sk-test",
    )
    tts = resolve_tts(
        "minimax",
        tts_cfg=TtsConfig(minimax_ok=True),
        allow_prompt=False,
    )
    assert tts.name == "minimax"


def test_tts_minimax_prompt_yes_persists(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[tts]\nprovider = \"auto\"\nminimax_ok = false\n", encoding="utf-8")
    monkeypatch.setattr(
        "hark.providers.minimax.resolve_minimax_api_key",
        lambda: "sk-test",
    )
    monkeypatch.setattr("hark.providers.resolve._prompt_minimax_ok", lambda: True)
    monkeypatch.delenv("HARK_TTS_MINIMAX_OK", raising=False)
    tts = resolve_tts(
        "minimax",
        tts_cfg=TtsConfig(minimax_ok=False),
        allow_prompt=True,
        config_path=path,
    )
    assert tts.name == "minimax"
    text = path.read_text(encoding="utf-8")
    assert "minimax_ok = true" in text


def test_persist_tts_minimax_ok(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[tts]\nprovider = \"auto\"\n", encoding="utf-8")
    persist_tts_minimax_ok(path)
    assert "minimax_ok = true" in path.read_text(encoding="utf-8")


def test_tts_disabled_minimax_pin_raises() -> None:
    with pytest.raises(ProviderError, match="disabled"):
        resolve_tts(
            "minimax",
            tts_cfg=TtsConfig(disabled=["minimax"], minimax_ok=True),
        )


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | str) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else str(payload)

    def json(self) -> dict:
        assert isinstance(self._payload, dict)
        return self._payload


def test_openai_stt_prefers_gpt4o_transcribe(monkeypatch) -> None:
    calls: list[str] = []

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a) -> None:
            return None

        def post(self, url, headers=None, files=None, data=None):
            calls.append(str(data.get("model")))
            return _FakeResponse(200, {"text": "hi"})

    monkeypatch.setattr(
        "hark.providers.openai_p.resolve_openai_api_key",
        lambda: "sk-test",
    )
    monkeypatch.delenv("OPENAI_STT_MODEL", raising=False)
    monkeypatch.setattr(httpx, "Client", _Client)
    tr = OpenAIStt().transcribe(b"RIFF")
    assert tr.text == "hi"
    assert calls == ["gpt-4o-mini-transcribe"]


def test_openai_stt_falls_back_to_whisper(monkeypatch) -> None:
    calls: list[str] = []

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a) -> None:
            return None

        def post(self, url, headers=None, files=None, data=None):
            model = str(data.get("model"))
            calls.append(model)
            if model == "gpt-4o-mini-transcribe":
                return _FakeResponse(401, "missing_scope")
            return _FakeResponse(200, {"text": "ok"})

    monkeypatch.setattr(
        "hark.providers.openai_p.resolve_openai_api_key",
        lambda: "sk-test",
    )
    monkeypatch.delenv("OPENAI_STT_MODEL", raising=False)
    monkeypatch.setattr(httpx, "Client", _Client)
    tr = OpenAIStt().transcribe(b"RIFF")
    assert tr.text == "ok"
    assert calls == ["gpt-4o-mini-transcribe", "whisper-1"]

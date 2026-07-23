"""Optional local full-STT provider (B072) — mocked, no model download."""

from __future__ import annotations

import io
import struct
import wave
from types import SimpleNamespace

import pytest

from hark.config import SttConfig
from hark.providers.base import ProviderError, Transcript
from hark.providers import local_stt
from hark.providers import resolve as resolve_mod
from hark.providers.local_stt import (
    FasterWhisperStt,
    MoonshineStt,
    faster_whisper_status,
    moonshine_status,
    wav_bytes_to_float32,
)
from hark.providers.resolve import resolve_stt


def _sine_wav(duration_s: float = 0.25, sr: int = 16000, freq: float = 440.0) -> bytes:
    import math

    n = int(duration_s * sr)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        frames = bytearray()
        for i in range(n):
            sample = int(16000 * math.sin(2 * math.pi * freq * i / sr))
            frames += struct.pack("<h", max(-32767, min(32767, sample)))
        wf.writeframes(bytes(frames))
    return buf.getvalue()


def test_wav_bytes_to_float32_mono():
    wav = _sine_wav()
    audio, sr = wav_bytes_to_float32(wav)
    assert sr == 16000
    assert audio.ndim == 1
    assert audio.size > 0
    assert audio.dtype.kind == "f"


def test_wav_bytes_empty_raises():
    with pytest.raises(ProviderError, match="empty"):
        wav_bytes_to_float32(b"")


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeModel:
    def __init__(self, texts: list[str] | None = None) -> None:
        self.texts = texts or [" hello world "]
        self.calls: list[dict] = []

    def transcribe(self, audio, **kwargs):
        self.calls.append({"audio_len": len(audio), **kwargs})
        segs = [_FakeSegment(t) for t in self.texts]
        info = SimpleNamespace(duration=0.25, language="en")
        return segs, info


def test_faster_whisper_transcribe_with_injected_model():
    model = _FakeModel(["  hey ", " there "])
    stt = FasterWhisperStt(model_instance=model)
    tr = stt.transcribe(_sine_wav())
    assert isinstance(tr, Transcript)
    assert tr.provider == "faster_whisper"
    assert tr.text == "hey there"
    assert tr.duration_ms > 0
    assert model.calls
    assert model.calls[0].get("beam_size") == 1


def test_faster_whisper_missing_package(monkeypatch):
    def boom():
        raise ProviderError("faster-whisper not installed — pip install 'hark[local-stt]'")

    monkeypatch.setattr(local_stt, "import_faster_whisper", boom)
    st = faster_whisper_status()
    assert st.available is False
    assert "local-stt" in st.detail
    assert "0.10" in st.rtf_note or "RTF" in st.rtf_note or "0.1" in st.rtf_note


def test_moonshine_transcribe_injected():
    def fake_fn(audio, sr=None, *args, **kwargs):
        return "offline phrase"

    stt = MoonshineStt(transcribe_fn=fake_fn)
    tr = stt.transcribe(_sine_wav())
    assert tr.provider == "moonshine"
    assert tr.text == "offline phrase"


def test_moonshine_status_missing(monkeypatch):
    def boom():
        raise ProviderError("Moonshine not installed")

    monkeypatch.setattr(local_stt, "import_moonshine", boom)
    st = moonshine_status()
    assert st.available is False


def test_resolve_faster_whisper_success(monkeypatch):
    fake = FasterWhisperStt(model_instance=_FakeModel(["ok"]))

    def fake_try(name, opts):
        assert name == "faster_whisper"
        return fake

    monkeypatch.setattr(resolve_mod, "_try_local_stt", fake_try)
    cfg = SttConfig(provider="faster_whisper", local_fail_open=False)
    got = resolve_stt("faster_whisper", stt_cfg=cfg)
    assert got is fake


def test_resolve_aliases_to_faster_whisper(monkeypatch):
    seen: list[str] = []

    def fake_try(name, opts):
        seen.append(name)
        return FasterWhisperStt(model_instance=_FakeModel(["x"]))

    monkeypatch.setattr(resolve_mod, "_try_local_stt", fake_try)
    for alias in ("local", "whisper", "faster-whisper", "faster_whisper"):
        resolve_stt(alias, stt_cfg=SttConfig(local_fail_open=False))
    assert all(n == "faster_whisper" for n in seen)
    assert len(seen) == 4


def test_resolve_local_fail_open_to_cloud(monkeypatch):
    def fail_local(name, opts):
        raise ProviderError("model missing")

    class _Cloud:
        name = "xai"

        def transcribe(self, *a, **k):
            return Transcript(text="cloud", provider="xai")

    monkeypatch.setattr(resolve_mod, "_try_local_stt", fail_local)
    monkeypatch.setattr(
        resolve_mod, "_resolve_cloud_stt", lambda name="auto", **_k: _Cloud()
    )
    cfg = SttConfig(provider="faster_whisper", local_fail_open=True)
    stt = resolve_stt("faster_whisper", stt_cfg=cfg)
    assert stt.name == "xai"


def test_resolve_local_fail_closed(monkeypatch):
    def fail_local(name, opts):
        raise ProviderError("model missing")

    monkeypatch.setattr(resolve_mod, "_try_local_stt", fail_local)
    cfg = SttConfig(provider="faster_whisper", local_fail_open=False)
    with pytest.raises(ProviderError, match="model missing"):
        resolve_stt("faster_whisper", stt_cfg=cfg)


def test_resolve_auto_never_picks_local(monkeypatch):
    """Cloud remains default (ADR-004) — auto must not load local engines."""
    called = {"local": False}

    def boom_local(name, opts):
        called["local"] = True
        raise AssertionError("local must not run for auto")

    class _Cloud:
        name = "openai"

        def transcribe(self, *a, **k):
            return Transcript(text="c", provider="openai")

    monkeypatch.setattr(resolve_mod, "_try_local_stt", boom_local)
    monkeypatch.setattr(
        resolve_mod, "_resolve_cloud_stt", lambda name="auto", **_k: _Cloud()
    )
    stt = resolve_stt("auto")
    assert stt.name == "openai"
    assert called["local"] is False


def test_resolve_moonshine_path(monkeypatch):
    seen: list[str] = []

    def fake_try(name, opts):
        seen.append(name)
        return MoonshineStt(transcribe_fn=lambda *a, **k: "m")

    monkeypatch.setattr(resolve_mod, "_try_local_stt", fake_try)
    resolve_stt("moonshine", stt_cfg=SttConfig(local_fail_open=False))
    assert seen == ["moonshine"]


def test_stt_config_loads_local_keys(tmp_path, monkeypatch):
    from hark.config import load_config

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[stt]
provider = "faster_whisper"
local_model = "base.en"
local_device = "cpu"
local_compute_type = "int8"
local_fail_open = false
local_download = false
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_STT_PROVIDER", raising=False)
    monkeypatch.delenv("HARK_STT_LOCAL_MODEL", raising=False)
    monkeypatch.delenv("HARK_STT_LOCAL_FAIL_OPEN", raising=False)
    monkeypatch.delenv("HARK_STT_LOCAL_DOWNLOAD", raising=False)
    cfg = load_config(cfg_path)
    assert cfg.stt.provider == "faster_whisper"
    assert cfg.stt.local_model == "base.en"
    assert cfg.stt.local_fail_open is False
    assert cfg.stt.local_download is False


def test_stt_config_env_overrides(tmp_path, monkeypatch):
    from hark.config import load_config

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text('[stt]\nprovider = "auto"\n', encoding="utf-8")
    monkeypatch.setenv("HARK_STT_PROVIDER", "local")
    monkeypatch.setenv("HARK_STT_LOCAL_MODEL", "base.en")
    monkeypatch.setenv("HARK_STT_LOCAL_FAIL_OPEN", "0")
    cfg = load_config(cfg_path)
    assert cfg.stt.provider == "local"
    assert cfg.stt.local_model == "base.en"
    assert cfg.stt.local_fail_open is False


def test_faster_whisper_loader_failure_surfaces():
    def bad_loader(*a, **k):
        raise ProviderError("model load failed")

    stt = FasterWhisperStt(model_loader=bad_loader)
    with pytest.raises(ProviderError, match="model load failed"):
        stt.transcribe(_sine_wav())

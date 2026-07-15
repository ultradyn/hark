"""B113: ambient wake / radio capture plays start + stop recording cues."""

from __future__ import annotations

from dataclasses import dataclass

from hark.audio.capture import CaptureResult
from hark.config import AmbientConfig, HarkConfig, ListenConfig
from hark.speech import run_listen
from hark.usage import UsageStore


def _cap(*, duration_ms: int = 400) -> CaptureResult:
    return CaptureResult(
        pcm16=b"\x00\x00" * 800,
        sample_rate=16000,
        duration_ms=duration_ms,
        speech_ms=duration_ms,
        wait_speech_ms=10,
        peak_rms=0.02,
        peak_db=-34.0,
    )


@dataclass
class _FakeStt:
    name: str = "fake"
    texts: list[str] | None = None
    calls: int = 0

    def __post_init__(self) -> None:
        self.texts = list(self.texts or [])

    def transcribe(self, wav_bytes: bytes, *, language: str | None = None):
        from hark.providers.base import Transcript

        del wav_bytes, language
        self.calls += 1
        text = self.texts.pop(0) if self.texts else ""
        return Transcript(text=text, provider=self.name)


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_listen(monkeypatch, stt: _FakeStt, caps: list, *, invoke_on_opened: bool = True):
    monkeypatch.setattr("hark.speech.resolve_stt", lambda *a, **k: stt)
    it = iter(caps)
    capture_kwargs: list[dict] = []

    def fake_capture(**kwargs):
        capture_kwargs.append(kwargs)
        if invoke_on_opened:
            on_opened = kwargs.get("on_opened")
            if callable(on_opened):
                on_opened()
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr("hark.speech.capture_utterance", fake_capture)
    monkeypatch.setattr("hark.speech.pause_ambient_for_mic", lambda **k: _NullCtx())
    monkeypatch.setattr("hark.speech.MicLease", lambda *a, **k: _NullCtx())
    monkeypatch.setattr("hark.speech.BusySection", lambda *a, **k: _NullCtx())
    monkeypatch.setattr("hark.speech.register_active_listen", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.clear_active_listen", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.consume_listen_action", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.poll_listen_action", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.configure_cues_from_config", lambda cfg: None)
    monkeypatch.setattr("hark.speech.time.sleep", lambda s: None)
    return capture_kwargs


def test_radio_post_wake_arm_start_and_stop_on_end_phrase(monkeypatch, tmp_path):
    """Ambient-style radio: arm start once, stop once on end phrase (not mid-partial)."""
    stt = _FakeStt(texts=["working on it", "ship it okay hark send"])
    _patch_listen(monkeypatch, stt, [_cap(), _cap()], invoke_on_opened=True)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    cues: list[str] = []
    monkeypatch.setattr(
        "hark.speech.play_record_start",
        lambda: cues.append("start"),
    )
    monkeypatch.setattr(
        "hark.speech.play_record_stop",
        lambda: cues.append("stop"),
    )
    logs: list[str] = []
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append(event),
    )

    # Mirrors complete_after_wake(arm_cue=post_wake_arm_cue)
    result = run_listen(
        HarkConfig(
            listen=ListenConfig(end_mode="radio", stream_partials=False),
            ambient=AmbientConfig(streaming=False),
        ),
        end_mode="radio",
        post_tts_guard_s=0,
        lead_in_ms=150,
        arm_cue=True,
    )

    assert result.end_phrase  # product end
    assert cues == ["start", "stop"]
    assert "listen.armed_cue" in logs
    assert "listen.stop_cue" in logs
    assert logs.count("listen.speech_opened") == 2  # two segments, no re-start


def test_silence_post_wake_arm_start_and_stop(monkeypatch, tmp_path):
    stt = _FakeStt(texts=["deploy now"])
    _patch_listen(monkeypatch, stt, [_cap()], invoke_on_opened=True)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    cues: list[str] = []
    monkeypatch.setattr(
        "hark.speech.play_record_start",
        lambda: cues.append("start"),
    )
    monkeypatch.setattr(
        "hark.speech.play_record_stop",
        lambda: cues.append("stop"),
    )

    result = run_listen(
        HarkConfig(listen=ListenConfig(end_mode="silence")),
        end_mode="silence",
        post_tts_guard_s=0,
        arm_cue=True,
    )

    assert result.text == "deploy now"
    assert cues == ["start", "stop"]


def test_streaming_suppresses_stop_keeps_start(monkeypatch, tmp_path):
    """B110 coordination: streaming keeps arm start; suppress misleading stop."""
    stt = _FakeStt(texts=["ship it okay hark send"])
    _patch_listen(monkeypatch, stt, [_cap()], invoke_on_opened=True)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    cues: list[str] = []
    monkeypatch.setattr(
        "hark.speech.play_record_start",
        lambda: cues.append("start"),
    )
    monkeypatch.setattr(
        "hark.speech.play_record_stop",
        lambda: cues.append("stop"),
    )
    logs: list[str] = []
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append(event),
    )

    result = run_listen(
        HarkConfig(
            listen=ListenConfig(end_mode="radio", stream_partials=False),
            ambient=AmbientConfig(streaming=True),
        ),
        end_mode="radio",
        post_tts_guard_s=0,
        arm_cue=True,
    )

    assert result.end_phrase
    assert cues == ["start"]  # no stop
    assert "listen.stop_cue_suppressed" in logs
    assert "listen.stop_cue" not in logs


def test_radio_lead_in_before_arm_cue(monkeypatch, tmp_path):
    """Post-wake lead_in_ms applies on radio path before arm (B113)."""
    stt = _FakeStt(texts=["done okay hark send"])
    _patch_listen(monkeypatch, stt, [_cap()], invoke_on_opened=True)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    sleeps: list[float] = []
    monkeypatch.setattr("hark.speech.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("hark.speech.play_record_start", lambda: None)
    monkeypatch.setattr("hark.speech.play_record_stop", lambda: None)
    monkeypatch.setattr("hark.speech.syslog", lambda *a, **k: None)

    run_listen(
        HarkConfig(listen=ListenConfig(end_mode="radio")),
        end_mode="radio",
        post_tts_guard_s=0,
        lead_in_ms=200,
        arm_cue=True,
    )
    assert 0.2 in sleeps


def test_play_cue_defaults_non_exclusive(monkeypatch):
    """Record cues must not block on the TTS exclusive FIFO (B113)."""
    import hark.audio.cues as cues

    calls: list[dict] = []
    monkeypatch.setattr(cues, "CUES_DIR", __import__("pathlib").Path("/nonexistent"))
    monkeypatch.setattr(
        cues,
        "resolve_cue_path",
        lambda name: None,
    )
    monkeypatch.setattr(
        cues,
        "play_audio",
        lambda data, **kw: calls.append(kw) or None,
    )
    monkeypatch.setattr(cues, "syslog", lambda *a, **k: None)

    cues.play_record_stop()
    assert calls
    assert calls[0].get("exclusive") is False

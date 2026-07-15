"""B112: streaming mode prompt-delivery latency helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hark.config import AmbientConfig, HarkConfig, ListenConfig
from hark.speech import effective_radio_idle_end_s


def test_effective_radio_idle_classic_default():
    cfg = HarkConfig()
    assert cfg.ambient.streaming is False
    assert effective_radio_idle_end_s(cfg) == pytest.approx(6.3)
    assert effective_radio_idle_end_s(cfg, streaming=False) == pytest.approx(6.3)


def test_effective_radio_idle_streaming_clamps_to_quiet_window():
    """Streaming should finalize after ~end_silence / ack quiet, not 3× hang."""
    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True, streaming_ack_min_quiet_s=2.0),
        listen=ListenConfig(
            end_silence_s=2.1,
            radio_idle_end_silence_s=6.3,
        ),
    )
    # P1.M6: omit kwargs → bound_answer (no ambient leak) → classic 6.3
    assert effective_radio_idle_end_s(cfg) == pytest.approx(6.3)
    assert effective_radio_idle_end_s(
        cfg, streaming=True, streaming_ack_min_quiet_s=2.0
    ) == pytest.approx(2.1)


def test_effective_radio_idle_streaming_uses_ack_when_higher():
    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True, streaming_ack_min_quiet_s=3.0),
        listen=ListenConfig(end_silence_s=2.1, radio_idle_end_silence_s=6.3),
    )
    assert effective_radio_idle_end_s(
        cfg, streaming=True, streaming_ack_min_quiet_s=3.0
    ) == pytest.approx(3.0)


def test_effective_radio_idle_respects_faster_explicit_idle():
    """If operator configured a tighter idle than the streaming clamp, keep it."""
    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True, streaming_ack_min_quiet_s=2.0),
        listen=ListenConfig(end_silence_s=2.1, radio_idle_end_silence_s=1.0),
    )
    assert effective_radio_idle_end_s(
        cfg, streaming=True, streaming_ack_min_quiet_s=2.0
    ) == pytest.approx(1.0)


def test_radio_streaming_uses_clamped_idle_timeout(monkeypatch):
    """run_listen radio path passes streaming-clamped idle as segment timeout."""
    import hark.speech as speech
    from hark.audio.capture import CaptureResult

    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True, streaming_ack_min_quiet_s=2.0),
        listen=ListenConfig(
            end_mode="radio",
            end_silence_s=2.1,
            radio_partial_silence_s=0.05,
            radio_idle_end_silence_s=6.3,
            stream_partials=True,
            soft_end_phrases_enabled=False,
        ),
    )

    class NullContext:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeStore:
        def record_stt(self, **kwargs):
            pass

    calls: list[dict] = []
    outcomes = ["speech", "idle"]
    logs: list[tuple[str, dict]] = []

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        step = outcomes.pop(0) if outcomes else "idle"
        if step == "speech":
            on_opened = kwargs.get("on_opened")
            if on_opened is not None:
                on_opened()
            return CaptureResult(
                pcm16=b"\0\0" * 640,
                sample_rate=16000,
                duration_ms=40,
                speech_ms=40,
                wait_speech_ms=20,
                peak_rms=0.02,
                peak_db=-34.0,
            )
        raise TimeoutError("no speech detected (peak_db=-60.0)")

    monkeypatch.setattr(
        speech,
        "resolve_stt",
        lambda *a, **k: SimpleNamespace(
            name="fake",
            transcribe=lambda _w: SimpleNamespace(text="ship it", provider="fake"),
        ),
    )
    monkeypatch.setattr(speech, "pause_ambient_for_mic", lambda **k: NullContext())
    monkeypatch.setattr(speech, "MicLease", lambda *a: NullContext())
    monkeypatch.setattr(speech, "BusySection", lambda *a: NullContext())
    monkeypatch.setattr(speech, "UsageStore", FakeStore)
    monkeypatch.setattr(speech, "configure_cues_from_config", lambda c: None)
    monkeypatch.setattr(speech, "register_active_listen", lambda *a, **k: None)
    monkeypatch.setattr(speech, "clear_active_listen", lambda *a, **k: None)
    monkeypatch.setattr(speech, "poll_listen_action", lambda *a: None)
    monkeypatch.setattr(speech, "consume_listen_action", lambda *a: None)
    monkeypatch.setattr(speech, "play_record_start", lambda: None)
    monkeypatch.setattr(speech, "play_record_stop", lambda: None)
    monkeypatch.setattr(speech.time, "sleep", lambda s: None)
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)
    monkeypatch.setattr(
        speech, "syslog", lambda event, **data: logs.append((event, data))
    )
    monkeypatch.setattr(speech, "duck_media", lambda *a, **k: NullContext())

    result = speech.run_listen(
        cfg, end_mode="radio", post_tts_guard_s=0, streaming=True
    )
    assert result.end_phrase == "radio_idle"
    assert len(calls) >= 2
    # Second segment (post-open) must use clamped ~2.1s, not 6.3s
    assert calls[1]["initial_timeout_s"] == pytest.approx(2.1)
    clamp_logs = [d for e, d in logs if e == "listen.streaming_idle_clamp"]
    assert clamp_logs
    assert clamp_logs[0]["idle_s"] == pytest.approx(2.1)
    assert clamp_logs[0]["classic_idle_s"] == pytest.approx(6.3)

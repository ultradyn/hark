"""B110: suppress end-of-recording beep when [ambient].streaming is true."""

from __future__ import annotations

from types import SimpleNamespace

from hark.audio.capture import CaptureResult
from hark.config import AmbientConfig, HarkConfig, ListenConfig


def _null_ctx_factory():
    class NullContext:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return NullContext


def _cap(pcm_ms: int = 40) -> CaptureResult:
    return CaptureResult(
        pcm16=b"\0\0" * max(1, int(16 * pcm_ms)),
        sample_rate=16000,
        duration_ms=pcm_ms,
        speech_ms=pcm_ms,
        wait_speech_ms=20,
        peak_rms=0.02,
        peak_db=-34.0,
    )


def _stub_listen(
    monkeypatch,
    speech,
    *,
    transcripts: list[str] | str = "hello",
    stop_calls: list[str] | None = None,
    start_calls: list[str] | None = None,
    logs: list[tuple[str, dict]] | None = None,
):
    NullContext = _null_ctx_factory()

    class FakeStore:
        def record_stt(self, **kwargs):
            pass

    texts = [transcripts] if isinstance(transcripts, str) else list(transcripts)
    idx = {"n": 0}

    def fake_transcribe(_wav):
        i = min(idx["n"], len(texts) - 1)
        idx["n"] += 1
        return SimpleNamespace(text=texts[i], provider="fake")

    monkeypatch.setattr(
        speech,
        "resolve_stt",
        lambda *args, **kwargs: SimpleNamespace(
            name="fake", transcribe=fake_transcribe
        ),
    )
    monkeypatch.setattr(speech, "pause_ambient_for_mic", lambda **kwargs: NullContext())
    monkeypatch.setattr(speech, "MicLease", lambda *args: NullContext())
    monkeypatch.setattr(speech, "BusySection", lambda *args: NullContext())
    monkeypatch.setattr(speech, "UsageStore", FakeStore)
    monkeypatch.setattr(speech, "configure_cues_from_config", lambda cfg: None)
    monkeypatch.setattr(speech, "register_active_listen", lambda *args, **kwargs: None)
    monkeypatch.setattr(speech, "clear_active_listen", lambda *args, **kwargs: None)
    monkeypatch.setattr(speech, "poll_listen_action", lambda *args: None)
    monkeypatch.setattr(speech, "consume_listen_action", lambda *args: None)
    monkeypatch.setattr(
        speech,
        "play_record_start",
        lambda: (start_calls.append("start") if start_calls is not None else None),
    )
    monkeypatch.setattr(
        speech,
        "play_record_stop",
        lambda: (stop_calls.append("stop") if stop_calls is not None else None),
    )
    monkeypatch.setattr(speech.time, "sleep", lambda s: None)
    if logs is not None:
        monkeypatch.setattr(
            speech,
            "syslog",
            lambda event, **data: logs.append((event, data)),
        )
    return idx


def _speech_then_idle_capture(outcomes: list[str]):
    """Factory: speech segments open the gate; then TimeoutError for idle."""

    steps = list(outcomes)

    def fake_capture(**kwargs):
        step = steps.pop(0) if steps else "idle"
        if step == "speech":
            on_opened = kwargs.get("on_opened")
            if on_opened is not None:
                on_opened()
            return _cap(80)
        raise TimeoutError("no speech detected (peak_db=-60.0)")

    return fake_capture


def test_radio_end_phrase_plays_stop_when_not_streaming(monkeypatch):
    import hark.speech as speech

    stop_calls: list[str] = []
    start_calls: list[str] = []
    _stub_listen(
        monkeypatch,
        speech,
        transcripts=["ship the auth fix over"],
        stop_calls=stop_calls,
        start_calls=start_calls,
    )
    monkeypatch.setattr(
        speech,
        "capture_utterance",
        _speech_then_idle_capture(["speech"]),
    )

    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=False),
        listen=ListenConfig(
            end_mode="radio",
            soft_end_phrases_enabled=True,
            stream_partials=False,
        ),
    )
    result = speech.run_listen(cfg, end_mode="radio", post_tts_guard_s=0)
    assert result.end_phrase == "over"
    assert start_calls == ["start"]
    assert stop_calls == ["stop"]


def test_radio_end_phrase_suppresses_stop_when_streaming(monkeypatch):
    import hark.speech as speech

    stop_calls: list[str] = []
    start_calls: list[str] = []
    logs: list[tuple[str, dict]] = []
    _stub_listen(
        monkeypatch,
        speech,
        transcripts=["ship the auth fix over"],
        stop_calls=stop_calls,
        start_calls=start_calls,
        logs=logs,
    )
    monkeypatch.setattr(
        speech,
        "capture_utterance",
        _speech_then_idle_capture(["speech"]),
    )

    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True),
        listen=ListenConfig(
            end_mode="radio",
            soft_end_phrases_enabled=True,
            stream_partials=False,
        ),
    )
    # Explicit streaming (or profile=post_wake): bound default no longer inherits ambient TOML (P1.M6).
    result = speech.run_listen(
        cfg, end_mode="radio", post_tts_guard_s=0, streaming=True
    )
    assert result.end_phrase == "over"
    assert result.cancelled is False
    # Start cue still plays; end beep suppressed (B110)
    assert start_calls == ["start"]
    assert stop_calls == []
    suppressed = [d for e, d in logs if e == "listen.stop_cue_suppressed"]
    assert suppressed
    assert suppressed[0]["reason"] == "ambient.streaming"


def test_radio_idle_end_suppresses_stop_when_streaming(monkeypatch):
    import hark.speech as speech

    stop_calls: list[str] = []
    logs: list[tuple[str, dict]] = []
    idle_s = 0.15
    _stub_listen(
        monkeypatch,
        speech,
        transcripts="ship the auth fix",
        stop_calls=stop_calls,
        logs=logs,
    )
    monkeypatch.setattr(
        speech,
        "capture_utterance",
        _speech_then_idle_capture(["speech", "idle"]),
    )

    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True),
        listen=ListenConfig(
            end_mode="radio",
            end_silence_s=2.1,
            radio_partial_silence_s=0.05,
            radio_idle_end_silence_s=idle_s,
            stream_partials=True,
            soft_end_phrases_enabled=False,
        ),
    )
    result = speech.run_listen(
        cfg, end_mode="radio", post_tts_guard_s=0, streaming=True
    )
    assert result.end_phrase == "radio_idle"
    assert result.cancelled is False
    assert stop_calls == []
    assert any(e == "listen.stop_cue_suppressed" for e, _ in logs)
    assert any(e == "listen.radio_idle_end" for e, _ in logs)


def test_radio_idle_end_plays_stop_when_not_streaming(monkeypatch):
    import hark.speech as speech

    stop_calls: list[str] = []
    idle_s = 0.15
    _stub_listen(
        monkeypatch,
        speech,
        transcripts="ship the auth fix",
        stop_calls=stop_calls,
    )
    monkeypatch.setattr(
        speech,
        "capture_utterance",
        _speech_then_idle_capture(["speech", "idle"]),
    )

    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=False),
        listen=ListenConfig(
            end_mode="radio",
            end_silence_s=2.1,
            radio_partial_silence_s=0.05,
            radio_idle_end_silence_s=idle_s,
            stream_partials=True,
            soft_end_phrases_enabled=False,
        ),
    )
    result = speech.run_listen(cfg, end_mode="radio", post_tts_guard_s=0)
    assert result.end_phrase == "radio_idle"
    assert stop_calls == ["stop"]


def test_silence_mode_suppresses_stop_when_streaming(monkeypatch):
    import hark.speech as speech

    stop_calls: list[str] = []
    logs: list[tuple[str, dict]] = []
    _stub_listen(
        monkeypatch,
        speech,
        transcripts="option two",
        stop_calls=stop_calls,
        logs=logs,
    )

    def fake_capture(**kwargs):
        on_opened = kwargs.get("on_opened")
        if on_opened is not None:
            on_opened()
        return _cap(100)

    monkeypatch.setattr(speech, "capture_utterance", fake_capture)

    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True),
        listen=ListenConfig(end_mode="silence", empty_stt_retry=False, empty_stt_nudge=False),
    )
    result = speech.run_listen(
        cfg, end_mode="silence", post_tts_guard_s=0, streaming=True
    )
    assert result.text == "option two"
    assert stop_calls == []
    assert any(e == "listen.stop_cue_suppressed" for e, _ in logs)


def test_silence_mode_plays_stop_when_not_streaming(monkeypatch):
    import hark.speech as speech

    stop_calls: list[str] = []
    _stub_listen(
        monkeypatch,
        speech,
        transcripts="option two",
        stop_calls=stop_calls,
    )

    def fake_capture(**kwargs):
        on_opened = kwargs.get("on_opened")
        if on_opened is not None:
            on_opened()
        return _cap(100)

    monkeypatch.setattr(speech, "capture_utterance", fake_capture)

    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=False),
        listen=ListenConfig(end_mode="silence", empty_stt_retry=False, empty_stt_nudge=False),
    )
    result = speech.run_listen(cfg, end_mode="silence", post_tts_guard_s=0)
    assert result.text == "option two"
    assert stop_calls == ["stop"]

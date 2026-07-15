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

    # Mirrors complete_after_wake: post_wake profile encodes lead_in + arm cue
    result = run_listen(
        HarkConfig(
            listen=ListenConfig(end_mode="radio", stream_partials=False),
            ambient=AmbientConfig(
                streaming=False,
                post_wake_lead_in_ms=150,
                post_wake_arm_cue=True,
            ),
        ),
        profile="post_wake",
        end_mode="radio",
        post_tts_guard_s=0,
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
        HarkConfig(
            listen=ListenConfig(end_mode="silence"),
            ambient=AmbientConfig(post_wake_arm_cue=True),
        ),
        profile="post_wake",
        end_mode="silence",
        post_tts_guard_s=0,
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
            ambient=AmbientConfig(streaming=True, post_wake_arm_cue=True),
        ),
        profile="post_wake",
        end_mode="radio",
        post_tts_guard_s=0,
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
        HarkConfig(
            listen=ListenConfig(end_mode="radio"),
            ambient=AmbientConfig(
                post_wake_lead_in_ms=200,
                post_wake_arm_cue=True,
            ),
        ),
        profile="post_wake",
        end_mode="radio",
        post_tts_guard_s=0,
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


def test_reload_mid_radio_plays_stop_when_streaming(monkeypatch, tmp_path):
    """B124: reload-abort of active radio listen always plays stop (override B110)."""
    from hark.lifecycle import clear_reload_request, request_reload

    clear_reload_request()
    stt = _FakeStt(texts=["still talking about the design"])
    _patch_listen(monkeypatch, stt, [_cap()], invoke_on_opened=True)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )

    def fake_capture(**kwargs):
        on_opened = kwargs.get("on_opened")
        if callable(on_opened):
            on_opened()
        # Simulate file-watch/SIGHUP while the segment is open
        request_reload(source="config_watch")
        should_stop = kwargs.get("should_stop")
        if callable(should_stop):
            assert should_stop(b"", 0.1) is True
        return _cap()

    monkeypatch.setattr("hark.speech.capture_utterance", fake_capture)

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
            listen=ListenConfig(end_mode="radio", stream_partials=True),
            ambient=AmbientConfig(
                streaming=True,
                post_wake_arm_cue=True,
                post_wake_lead_in_ms=0,
            ),
        ),
        profile="post_wake",
        end_mode="radio",
        post_tts_guard_s=0,
        streaming=True,
    )

    assert result.cancelled is True
    assert result.end_phrase == "reload"
    assert cues == ["start", "stop"], cues
    assert "listen.stop_cue" in logs
    assert "listen.reload_abort" in logs
    assert "listen.stop_cue_suppressed" not in logs
    clear_reload_request()


def test_reload_mid_silence_plays_stop_when_streaming(monkeypatch, tmp_path):
    """B124: silence-mode reload abort also forces stop cue under streaming."""
    from hark.lifecycle import clear_reload_request, request_reload

    clear_reload_request()
    stt = _FakeStt(texts=["hello"])
    _patch_listen(monkeypatch, stt, [_cap()], invoke_on_opened=True)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )

    def fake_capture(**kwargs):
        on_opened = kwargs.get("on_opened")
        if callable(on_opened):
            on_opened()
        request_reload(source="sighup")
        return _cap()

    monkeypatch.setattr("hark.speech.capture_utterance", fake_capture)

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
        HarkConfig(
            listen=ListenConfig(end_mode="silence"),
            ambient=AmbientConfig(streaming=True, post_wake_arm_cue=True),
        ),
        profile="post_wake",
        end_mode="silence",
        post_tts_guard_s=0,
        streaming=True,
    )

    assert result.cancelled is True
    assert result.end_phrase == "reload"
    assert cues == ["start", "stop"]
    clear_reload_request()


def test_reload_before_speech_with_arm_cue_plays_stop(monkeypatch, tmp_path):
    """B124: arm cue already played, reload before open → still stop beep."""
    from hark.lifecycle import clear_reload_request, request_reload

    clear_reload_request()
    stt = _FakeStt(texts=[])
    _patch_listen(monkeypatch, stt, [], invoke_on_opened=False)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )

    def fake_capture(**kwargs):
        request_reload(source="config_watch")
        raise TimeoutError("no speech detected (peak_db=-60.0)")

    monkeypatch.setattr("hark.speech.capture_utterance", fake_capture)

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
        HarkConfig(
            listen=ListenConfig(end_mode="radio"),
            ambient=AmbientConfig(
                streaming=True,
                post_wake_arm_cue=True,
                post_wake_lead_in_ms=0,
            ),
        ),
        profile="post_wake",
        end_mode="radio",
        post_tts_guard_s=0,
        streaming=True,
    )

    assert result.cancelled is True
    assert result.end_phrase == "reload"
    assert cues == ["start", "stop"]
    clear_reload_request()

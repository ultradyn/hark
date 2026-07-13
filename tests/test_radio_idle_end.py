"""B074: radio answer windows auto-finish after long post-speech idle quiet."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hark.config import HarkConfig, ListenConfig, config_to_dict, load_config


def _null_ctx_factory():
    class NullContext:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return NullContext


def _stub_listen_deps(monkeypatch, speech, *, transcripts: list[str] | str = "hello"):
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
        lambda *args: SimpleNamespace(name="fake", transcribe=fake_transcribe),
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
    monkeypatch.setattr(speech, "play_record_start", lambda: None)
    monkeypatch.setattr(speech, "play_record_stop", lambda: None)
    monkeypatch.setattr(speech.time, "sleep", lambda s: None)
    return idx


def _cap(pcm_ms: int = 40):
    from hark.audio.capture import CaptureResult

    return CaptureResult(
        pcm16=b"\0\0" * max(1, int(16 * pcm_ms)),
        sample_rate=16000,
        duration_ms=pcm_ms,
        speech_ms=pcm_ms,
        wait_speech_ms=20,
        peak_rms=0.02,
        peak_db=-34.0,
    )


def test_default_radio_idle_is_three_times_end_silence():
    cfg = HarkConfig()
    assert cfg.listen.end_silence_s == 2.1
    assert cfg.listen.radio_idle_end_silence_s == pytest.approx(3.0 * 2.1)
    assert cfg.listen.radio_idle_end_silence_s == pytest.approx(6.3)
    # Partial cadence stays short and non-terminal
    assert cfg.listen.radio_partial_silence_s == 0.6
    assert cfg.listen.radio_partial_silence_s < cfg.listen.radio_idle_end_silence_s


def test_config_default_scales_with_end_silence(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[listen]
end_mode = "radio"
end_silence_s = 3.0
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.listen.end_silence_s == 3.0
    assert cfg.listen.radio_idle_end_silence_s == pytest.approx(9.0)


def test_config_override_radio_idle_end(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[listen]
end_mode = "radio"
end_silence_s = 2.1
radio_idle_end_silence_s = 5.0
radio_partial_silence_s = 0.5
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.listen.radio_idle_end_silence_s == 5.0
    assert cfg.listen.radio_partial_silence_s == 0.5
    dumped = config_to_dict(cfg)["listen"]
    assert dumped["radio_idle_end_silence_s"] == 5.0


def test_radio_post_speech_idle_auto_finishes(monkeypatch):
    """Speak once, then no reopen within radio_idle → finish (not cancel)."""
    import hark.speech as speech

    idle_s = 0.15  # short fake clock for the test
    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="radio",
            end_silence_s=2.1,
            radio_partial_silence_s=0.05,
            radio_idle_end_silence_s=idle_s,
            stream_partials=True,
            soft_end_phrases_enabled=False,
        )
    )
    calls: list[dict] = []
    outcomes = ["speech", "idle"]

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        step = outcomes.pop(0) if outcomes else "idle"
        if step == "speech":
            # Simulate gate open callback
            on_opened = kwargs.get("on_opened")
            if on_opened is not None:
                on_opened()
            return _cap(80)
        raise TimeoutError("no speech detected (peak_db=-60.0)")

    logs: list[tuple[str, dict]] = []
    _stub_listen_deps(monkeypatch, speech, transcripts="ship the auth fix")
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)
    monkeypatch.setattr(
        speech,
        "syslog",
        lambda event, **data: logs.append((event, data)),
    )

    result = speech.run_listen(cfg, end_mode="radio", post_tts_guard_s=0)
    assert result.cancelled is False
    assert result.end_mode == "radio"
    assert result.end_phrase == "radio_idle"
    assert "auth" in (result.text or "").lower()
    # First segment: initial wait (pre-open); second: idle timeout
    assert len(calls) >= 2
    assert calls[0]["initial_timeout_s"] == pytest.approx(
        cfg.listen.initial_timeout_s
    )
    assert calls[0]["end_silence_s"] == pytest.approx(0.05)
    assert calls[1]["initial_timeout_s"] == pytest.approx(idle_s)
    assert calls[1]["end_silence_s"] == pytest.approx(0.05)
    idle_logs = [d for e, d in logs if e == "listen.radio_idle_end"]
    assert idle_logs
    assert idle_logs[0]["idle_s"] == pytest.approx(idle_s)


def test_radio_short_pause_still_records(monkeypatch):
    """~2s thinking pause: next segment reopens within idle window → keep going."""
    import hark.speech as speech

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="radio",
            end_silence_s=2.1,
            radio_partial_silence_s=0.6,
            radio_idle_end_silence_s=6.3,
            stream_partials=True,
            soft_end_phrases_enabled=False,
        )
    )
    calls: list[dict] = []
    # Two speech segments then end phrase on third STT (after third capture)
    n = {"i": 0}

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        n["i"] += 1
        on_opened = kwargs.get("on_opened")
        if on_opened is not None:
            on_opened()
        return _cap(40)

    transcripts = [
        "please open the pull request",
        "please open the pull request for auth",
        "please open the pull request for auth okay hark send",
    ]
    _stub_listen_deps(monkeypatch, speech, transcripts=transcripts)
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)

    result = speech.run_listen(cfg, end_mode="radio", post_tts_guard_s=0)
    assert result.cancelled is False
    assert result.end_phrase == "okay hark send"
    assert "pull request" in (result.text or "").lower()
    # After first open, subsequent segments use idle timeout (> end_silence / short pause)
    assert len(calls) >= 2
    assert calls[1]["initial_timeout_s"] == pytest.approx(6.3)
    # Partial cadence stays short — not the idle hang
    assert all(c["end_silence_s"] == pytest.approx(0.6) for c in calls)
    # Idle window is longer than a normal ~2s thinking pause
    assert calls[1]["initial_timeout_s"] > 2.0


def test_radio_no_speech_yet_does_not_idle_finish(monkeypatch):
    """Before speech opens, long quiet uses initial timeout — raise, not radio_idle."""
    import hark.speech as speech

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="radio",
            initial_timeout_s=0.2,
            radio_idle_end_silence_s=0.05,  # would finish if mis-applied pre-open
            soft_end_phrases_enabled=False,
        )
    )
    calls: list[dict] = []

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        raise TimeoutError("no speech detected (peak_db=-60.0)")

    _stub_listen_deps(monkeypatch, speech, transcripts="")
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)

    with pytest.raises(TimeoutError, match="no speech detected"):
        speech.run_listen(cfg, end_mode="radio", post_tts_guard_s=0)
    assert calls
    # Pre-open uses initial_timeout_s, not the short idle hang
    assert calls[0]["initial_timeout_s"] == pytest.approx(0.2)


def test_radio_soft_end_still_finishes_sooner(monkeypatch):
    """Soft/product end phrases still finalize without waiting for idle."""
    import hark.speech as speech

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="radio",
            radio_idle_end_silence_s=30.0,
            soft_end_phrases_enabled=True,
            stream_partials=True,
        )
    )
    calls: list[dict] = []

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        on_opened = kwargs.get("on_opened")
        if on_opened is not None:
            on_opened()
        return _cap(40)

    _stub_listen_deps(
        monkeypatch, speech, transcripts="refactor the module send it"
    )
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)

    result = speech.run_listen(cfg, end_mode="radio", post_tts_guard_s=0)
    assert result.cancelled is False
    assert result.end_phrase == "send it"
    assert result.end_phrase != "radio_idle"
    assert len(calls) == 1


def test_silence_mode_ignores_radio_idle(monkeypatch):
    """Silence end_mode still uses end_silence_s only."""
    import hark.speech as speech

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="silence",
            end_silence_s=2.1,
            radio_partial_silence_s=0.5,
            radio_idle_end_silence_s=0.1,
        )
    )
    calls: list[dict] = []

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        return _cap(40)

    _stub_listen_deps(monkeypatch, speech, transcripts="one two three")
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)

    result = speech.run_listen(cfg, end_mode="silence", post_tts_guard_s=0)
    assert result.end_mode == "silence"
    assert result.text == "one two three"
    assert calls[0]["end_silence_s"] == pytest.approx(2.1)
    # Silence path does not use radio idle as capture hang
    assert calls[0]["end_silence_s"] != pytest.approx(0.1)

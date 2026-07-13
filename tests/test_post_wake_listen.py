"""B031: post-wake listen — softer energy gate + no-open retry/nudge."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from hark.audio.capture import CaptureResult
from hark.ambient import ambient_event_line, complete_after_wake
from hark.config import AmbientConfig, HarkConfig, ListenConfig, load_config
from hark.speech import (
    NO_OPEN_NUDGE_TEXT,
    _is_no_open_timeout,
    _log_no_open,
    run_listen,
)
from hark.usage import UsageStore
from hark.wake import WakeHit


def _cap(*, duration_ms: int = 800, peak_rms: float = 0.02) -> CaptureResult:
    pcm = b"\x00\x00" * 1600
    return CaptureResult(
        pcm16=pcm,
        sample_rate=16000,
        duration_ms=duration_ms,
        speech_ms=duration_ms,
        wait_speech_ms=40,
        peak_rms=peak_rms,
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


def _patch_listen_infra(monkeypatch, stt: _FakeStt, caps_or_errors: list):
    """Stub mic/lease/cues/STT. caps_or_errors items are CaptureResult or Exception."""
    monkeypatch.setattr("hark.speech.resolve_stt", lambda *a, **k: stt)
    it = iter(caps_or_errors)
    capture_kwargs: list[dict] = []

    def fake_capture(**kwargs):
        capture_kwargs.append(kwargs)
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
    monkeypatch.setattr("hark.speech.play_record_start", lambda: None)
    monkeypatch.setattr("hark.speech.play_record_stop", lambda: None)
    monkeypatch.setattr("hark.speech.configure_cues_from_config", lambda cfg: None)
    monkeypatch.setattr("hark.speech.time.sleep", lambda s: None)
    return capture_kwargs


def test_is_no_open_timeout_detects_gate_message():
    assert _is_no_open_timeout(
        TimeoutError(
            "no speech detected (peak_db=-45.4 peak_rms=0.00537 open_thresh≈-38.0dB)"
        )
    )
    assert _is_no_open_timeout(TimeoutError("no speech captured (peak_db=-50.0)"))
    assert not _is_no_open_timeout(TimeoutError("heard audio but STT returned empty text"))


def test_log_no_open_parses_error_fields(monkeypatch):
    logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append((event, data)),
    )
    _log_no_open(
        after_tts=False,
        attempt=0,
        stream_id="s1",
        phase="initial",
        error="no speech detected (peak_db=-45.4 peak_rms=0.00537 open_thresh≈-38.0dB)",
        abs_open_db=-48.0,
    )
    assert logs[0][0] == "speech.no_open"
    payload = logs[0][1]
    assert payload["peak_db"] == pytest.approx(-45.4)
    assert payload["rms"] == pytest.approx(0.00537)
    assert payload["open_thresh"] == pytest.approx(-38.0)
    assert payload["abs_open_db"] == -48.0
    assert payload["phase"] == "initial"


def test_no_open_retry_then_success(monkeypatch, tmp_path):
    stt = _FakeStt(texts=["ship the feature"])
    no_open = TimeoutError(
        "no speech detected (peak_db=-45.4 peak_rms=0.00537 open_thresh≈-48.0dB)"
    )
    caps = _patch_listen_infra(monkeypatch, stt, [no_open, _cap()])
    logs: list[str] = []
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append(event),
    )
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )

    cfg = HarkConfig(
        listen=ListenConfig(no_open_retry=True, no_open_nudge=False, abs_open_db=-48.0),
    )
    result = run_listen(cfg, post_tts_guard_s=0)

    assert result.text == "ship the feature"
    assert stt.calls == 1
    assert "speech.no_open" in logs
    assert "speech.no_open_retry" in logs
    assert caps[0]["abs_open_db"] == -48.0
    assert caps[1]["abs_open_db"] == -48.0


def test_no_open_nudge_then_success(monkeypatch, tmp_path):
    stt = _FakeStt(texts=["yes"])
    no_open = TimeoutError("no speech detected (peak_db=-46.0 peak_rms=0.005 open_thresh≈-48.0dB)")
    _patch_listen_infra(monkeypatch, stt, [no_open, _cap()])
    tts_calls: list[str] = []
    logs: list[str] = []

    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda cfg, text, **k: tts_calls.append(text) or {"ok": True},
    )
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append(event),
    )
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )

    cfg = HarkConfig(
        listen=ListenConfig(no_open_retry=False, no_open_nudge=True),
    )
    result = run_listen(
        cfg,
        post_tts_guard_s=0,
        no_open_nudge_text="I heard the wake but not your prompt.",
    )

    assert result.text == "yes"
    assert tts_calls == ["I heard the wake but not your prompt."]
    assert "speech.no_open_nudge" in logs


def test_no_open_retry_and_nudge_exhausted(monkeypatch, tmp_path):
    stt = _FakeStt(texts=[])
    err = TimeoutError("no speech detected (peak_db=-45.4 peak_rms=0.005 open_thresh≈-48.0dB)")
    _patch_listen_infra(monkeypatch, stt, [err, err, err])
    tts_calls: list[str] = []
    logs: list[tuple[str, dict]] = []

    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda cfg, text, **k: tts_calls.append(text) or {"ok": True},
    )
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append((event, data)),
    )
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )

    cfg = HarkConfig(
        listen=ListenConfig(no_open_retry=True, no_open_nudge=True),
    )
    with pytest.raises(TimeoutError, match="no speech detected"):
        run_listen(cfg, post_tts_guard_s=0)

    assert stt.calls == 0
    assert tts_calls == [NO_OPEN_NUDGE_TEXT]
    no_open_logs = [d for e, d in logs if e == "speech.no_open"]
    assert len(no_open_logs) == 3
    assert no_open_logs[0]["phase"] == "initial"
    assert no_open_logs[1]["phase"] == "retry"
    assert no_open_logs[2]["phase"] == "nudge"


def test_no_open_disabled_raises_immediately(monkeypatch, tmp_path):
    stt = _FakeStt()
    err = TimeoutError("no speech detected (peak_db=-45.4 peak_rms=0.005 open_thresh≈-48.0dB)")
    _patch_listen_infra(monkeypatch, stt, [err])
    tts_calls: list[str] = []
    monkeypatch.setattr("hark.speech.syslog", lambda *a, **k: None)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *a, **k: tts_calls.append("x") or {"ok": True},
    )

    cfg = HarkConfig(
        listen=ListenConfig(no_open_retry=False, no_open_nudge=False),
    )
    with pytest.raises(TimeoutError, match="no speech detected"):
        run_listen(cfg, post_tts_guard_s=0)
    assert tts_calls == []
    assert stt.calls == 0


def test_run_listen_passes_gate_overrides(monkeypatch, tmp_path):
    stt = _FakeStt(texts=["hi"])
    caps = _patch_listen_infra(monkeypatch, stt, [_cap()])
    monkeypatch.setattr("hark.speech.syslog", lambda *a, **k: None)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    sleeps: list[float] = []
    monkeypatch.setattr("hark.speech.time.sleep", lambda s: sleeps.append(s))
    arm_cues: list[str] = []
    monkeypatch.setattr(
        "hark.speech.play_record_start",
        lambda: arm_cues.append("start"),
    )

    cfg = HarkConfig(listen=ListenConfig(abs_open_db=-40.0, open_margin_db=10.0))
    run_listen(
        cfg,
        post_tts_guard_s=0,
        abs_open_db=-50.0,
        open_margin_db=6.0,
        initial_timeout_s=12.0,
        lead_in_ms=150,
        arm_cue=True,
    )
    assert caps[0]["abs_open_db"] == -50.0
    assert caps[0]["open_margin_db"] == 6.0
    assert caps[0]["initial_timeout_s"] == 12.0
    assert 0.15 in sleeps
    assert arm_cues == ["start"]


def test_complete_after_wake_passes_post_wake_knobs(monkeypatch):
    calls: list[dict] = []
    listened = SimpleNamespace(
        text="deploy now",
        provider="xai",
        duration_ms=100,
        end_phrase=None,
        cancelled=False,
        stream_id="s-pw",
        partials_emitted=0,
    )

    def fake_listen(cfg, **kwargs):
        calls.append(kwargs)
        return listened

    monkeypatch.setattr("hark.ambient.run_listen", fake_listen)
    logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "hark.ambient.syslog",
        lambda event, **data: logs.append((event, data)),
    )

    cfg = HarkConfig(
        ambient=AmbientConfig(
            post_wake_lead_in_ms=200,
            post_wake_arm_cue=True,
            post_wake_abs_open_db=-50.0,
            post_wake_timeout_s=12.0,
            post_wake_no_open_nudge=True,
            post_wake_no_open_tts="I heard the wake but not your prompt.",
        ),
        listen=ListenConfig(abs_open_db=-48.0),
    )
    result = complete_after_wake(
        cfg,
        WakeHit(phrase="hey herald", remainder="", raw="hey harold", backend="vosk"),
        announce=False,
    )

    assert result.text == "deploy now"
    assert len(calls) == 1
    kw = calls[0]
    assert kw["abs_open_db"] == -50.0
    assert kw["initial_timeout_s"] == 12.0
    assert kw["lead_in_ms"] == 200
    assert kw["arm_cue"] is True
    assert kw["no_open_nudge"] is True
    assert kw["no_open_nudge_text"] == "I heard the wake but not your prompt."
    assert any(e == "ambient.post_wake_listen" for e, _ in logs)


def test_complete_after_wake_no_open_error_metrics(monkeypatch):
    def boom(cfg, **kwargs):
        raise TimeoutError(
            "no speech detected (peak_db=-45.4 peak_rms=0.00537 open_thresh≈-48.0dB)"
        )

    monkeypatch.setattr("hark.ambient.run_listen", boom)
    logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "hark.ambient.syslog",
        lambda event, **data: logs.append((event, data)),
    )

    cfg = HarkConfig(
        ambient=AmbientConfig(post_wake_abs_open_db=-48.0, post_wake_timeout_s=15.0),
    )
    result = complete_after_wake(
        cfg,
        WakeHit(phrase="hey herald", remainder="", raw="hey harold", backend="vosk"),
        announce=False,
    )

    assert result.activated is True
    assert result.text is None
    assert result.listen is not None
    assert result.listen["reason"] == "no_open"
    assert "no speech detected" in result.listen["error"]
    err_logs = [d for e, d in logs if e == "ambient.error"]
    assert len(err_logs) == 1
    assert err_logs[0]["reason"] == "no_open"
    assert err_logs[0]["phrase"] == "hey herald"
    assert err_logs[0]["abs_open_db"] == -48.0

    line = ambient_event_line(result)
    assert line["kind"] == "ambient.error"


def test_config_b031_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    path = tmp_path / "c.toml"
    path.write_text(
        """
[listen]
abs_open_db = -50.0
open_margin_db = 6.0
initial_timeout_s = 20
no_open_retry = false
no_open_nudge = false

[ambient]
post_wake_lead_in_ms = 250
post_wake_arm_cue = false
post_wake_abs_open_db = -52.0
post_wake_timeout_s = 10
post_wake_no_open_nudge = false
post_wake_no_open_tts = "custom nudge"
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.listen.abs_open_db == -50.0
    assert cfg.listen.open_margin_db == 6.0
    assert cfg.listen.initial_timeout_s == 20.0
    assert cfg.listen.no_open_retry is False
    assert cfg.listen.no_open_nudge is False
    assert cfg.ambient.post_wake_lead_in_ms == 250
    assert cfg.ambient.post_wake_arm_cue is False
    assert cfg.ambient.post_wake_abs_open_db == -52.0
    assert cfg.ambient.post_wake_timeout_s == 10.0
    assert cfg.ambient.post_wake_no_open_nudge is False
    assert cfg.ambient.post_wake_no_open_tts == "custom nudge"
    assert not any("abs_open" in w or "post_wake" in w for w in cfg.warnings)


def test_default_abs_open_softer_than_legacy():
    """Dogfood peak≈-45.4 never opened at -38; default must be below that."""
    cfg = HarkConfig()
    assert cfg.listen.abs_open_db <= -45.0
    assert cfg.listen.abs_open_db == -48.0
    assert cfg.ambient.post_wake_timeout_s == 15.0
    assert cfg.ambient.post_wake_lead_in_ms == 150
    assert "prompt" in cfg.ambient.post_wake_no_open_tts.lower()


def test_capture_default_abs_open_matches_listen():
    import inspect
    from hark.audio.capture import capture_utterance

    sig = inspect.signature(capture_utterance)
    assert sig.parameters["abs_open_db"].default == -48.0

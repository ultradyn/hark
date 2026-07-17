"""B012: empty STT after TTS — log, retry, nudge."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hark.audio.capture import CaptureResult
from hark.config import HarkConfig, ListenConfig
from hark.providers.base import Transcript
from hark.speech import EMPTY_STT_NUDGE_TEXT, run_listen
from hark.usage import UsageStore


def _cap(*, duration_ms: int = 2540, peak_rms: float = 0.02) -> CaptureResult:
    # ~0.1s of silence PCM16 mono 16kHz (enough for wav header path)
    pcm = b"\x00\x00" * 1600
    return CaptureResult(
        pcm16=pcm,
        sample_rate=16000,
        duration_ms=duration_ms,
        speech_ms=duration_ms,
        wait_speech_ms=80,
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

    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        del wav_bytes, language
        self.calls += 1
        text = self.texts.pop(0) if self.texts else ""
        return Transcript(text=text, provider=self.name)


def _patch_listen_infra(monkeypatch, stt: _FakeStt, caps: list[CaptureResult]):
    """Stub mic/lease/cues/STT so run_listen can exercise empty recovery."""
    monkeypatch.setattr("hark.speech.resolve_stt", lambda *a, **k: stt)
    cap_iter = iter(caps)

    def fake_capture(**kwargs):
        del kwargs
        try:
            return next(cap_iter)
        except StopIteration as exc:
            raise TimeoutError("no more capture fixtures") from exc

    monkeypatch.setattr("hark.speech.capture_utterance", fake_capture)
    monkeypatch.setattr("hark.speech.pause_ambient_for_mic", lambda **k: _NullCtx())
    monkeypatch.setattr("hark.speech.MicLease", lambda *a, **k: _NullCtx())
    monkeypatch.setattr("hark.speech.BusySection", lambda *a, **k: _NullCtx())
    monkeypatch.setattr("hark.speech.duck_media", lambda *a, **k: _NullCtx())
    monkeypatch.setattr("hark.speech.register_active_listen", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.clear_active_listen", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.consume_listen_action", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.poll_listen_action", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.play_record_start", lambda: None)
    monkeypatch.setattr("hark.speech.play_record_stop", lambda: None)
    monkeypatch.setattr("hark.speech.configure_cues_from_config", lambda cfg: None)
    monkeypatch.setattr("hark.speech.time.sleep", lambda s: None)


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_empty_stt_retry_then_success(monkeypatch, tmp_path):
    stt = _FakeStt(texts=["", "option two"])
    _patch_listen_infra(
        monkeypatch,
        stt,
        [_cap(duration_ms=2540), _cap(duration_ms=1200)],
    )
    logs: list[tuple[str, dict]] = []

    def fake_log(event, **data):
        logs.append((event, data))

    monkeypatch.setattr("hark.speech.syslog", fake_log)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )

    cfg = HarkConfig(
        listen=ListenConfig(empty_stt_retry=True, empty_stt_nudge=False),
    )
    result = run_listen(cfg, last_tts="Pick one or two", post_tts_guard_s=0.1)

    assert result.text == "option two"
    assert stt.calls == 2
    empty_events = [e for e, _ in logs if e == "speech.empty_stt"]
    assert len(empty_events) == 1
    payload = next(d for e, d in logs if e == "speech.empty_stt")
    assert payload["after_tts"] is True
    assert payload["duration_ms"] == 2540
    assert payload["rms"] == pytest.approx(0.02)
    assert payload["phase"] == "initial"
    assert any(e == "speech.empty_stt_retry" for e, _ in logs)


def test_empty_stt_nudge_then_success(monkeypatch, tmp_path):
    stt = _FakeStt(texts=["", "yes please"])
    _patch_listen_infra(monkeypatch, stt, [_cap(), _cap()])
    tts_calls: list[str] = []
    logs: list[str] = []

    def fake_tts(cfg, text, **kwargs):
        tts_calls.append(text)
        return {"ok": True, "provider": "cache"}

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append(event),
    )
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )

    cfg = HarkConfig(
        listen=ListenConfig(empty_stt_retry=False, empty_stt_nudge=True),
    )
    result = run_listen(cfg, last_tts="menu text here", post_tts_guard_s=0)

    assert result.text == "yes please"
    assert tts_calls == [EMPTY_STT_NUDGE_TEXT]
    assert "speech.empty_stt" in logs
    assert "speech.empty_stt_nudge" in logs
    assert stt.calls == 2


def test_empty_stt_retry_and_nudge_exhausted(monkeypatch, tmp_path):
    stt = _FakeStt(texts=["", "", ""])
    _patch_listen_infra(monkeypatch, stt, [_cap(), _cap(), _cap()])
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
        listen=ListenConfig(empty_stt_retry=True, empty_stt_nudge=True),
    )
    with pytest.raises(TimeoutError, match="empty text"):
        run_listen(cfg, last_tts="long menu", post_tts_guard_s=0.05)

    assert stt.calls == 3
    assert tts_calls == [EMPTY_STT_NUDGE_TEXT]
    empty = [d for e, d in logs if e == "speech.empty_stt"]
    assert len(empty) == 3
    assert empty[0]["phase"] == "initial"
    assert empty[1]["phase"] == "retry"
    assert empty[2]["phase"] == "nudge"
    assert empty[0]["after_tts"] is True


def test_empty_stt_disabled_raises_immediately(monkeypatch, tmp_path):
    stt = _FakeStt(texts=[""])
    _patch_listen_infra(monkeypatch, stt, [_cap()])
    monkeypatch.setattr("hark.speech.syslog", lambda *a, **k: None)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    tts_calls: list[str] = []
    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *a, **k: tts_calls.append("x") or {"ok": True},
    )

    cfg = HarkConfig(
        listen=ListenConfig(empty_stt_retry=False, empty_stt_nudge=False),
    )
    with pytest.raises(TimeoutError, match="empty text"):
        run_listen(cfg, last_tts=None, post_tts_guard_s=0)

    assert stt.calls == 1
    assert tts_calls == []


def test_already_armed_still_applies_post_tts_guard(monkeypatch, tmp_path):
    """Pre-arm must not zero settle delay (mute unmute / residual race)."""
    stt = _FakeStt(texts=["hello"])
    _patch_listen_infra(monkeypatch, stt, [_cap(duration_ms=500)])
    sleeps: list[float] = []
    monkeypatch.setattr("hark.speech.time.sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr("hark.speech.syslog", lambda *a, **k: None)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )

    cfg = HarkConfig()
    run_listen(
        cfg,
        last_tts="question",
        post_tts_guard_s=0.12,
        already_armed=True,
    )
    assert sleeps and sleeps[0] == pytest.approx(0.12)


def test_usage_empty_stt_rate(tmp_path):
    store = UsageStore(tmp_path / "u.jsonl")
    store.record_stt(text="hi", provider="xai", audio_ms=500, ok=True)
    store.record_stt(
        text="",
        provider="xai",
        audio_ms=2540,
        ok=False,
        error="empty transcript",
    )
    store.record_stt(
        text="",
        provider="xai",
        audio_ms=2000,
        ok=False,
        error="empty transcript",
    )
    s = store.summary()["stt"]
    assert s["empty_transcript"] == 2
    assert s["empty_stt_rate"] == 0.6667
    assert s["errors"] == 2


def test_config_empty_stt_keys(tmp_path):
    from hark.config import load_config

    path = tmp_path / "c.toml"
    path.write_text(
        "[listen]\nempty_stt_retry = false\nempty_stt_nudge = false\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.listen.empty_stt_retry is False
    assert cfg.listen.empty_stt_nudge is False
    assert not any("empty_stt" in w for w in cfg.warnings)

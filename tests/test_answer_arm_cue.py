"""B078: answer-window arm cue beeps when listen is ready (not only when speech opens)."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from hark.audio.capture import CaptureResult
from hark.config import AudioConfig, HarkConfig, ListenConfig
from hark.speech import run_listen, speak_and_listen
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
    """Stub mic/lease/cues/STT. Optionally fire on_opened like the energy gate."""
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
    monkeypatch.setattr("hark.speech.play_record_stop", lambda: None)
    return capture_kwargs


def test_silence_arm_cue_before_speech_no_double_beep(monkeypatch, tmp_path):
    """Silence: arm_cue beeps once at arm; speech-open must not beep again."""
    stt = _FakeStt(texts=["hello"])
    caps = _patch_listen(monkeypatch, stt, [_cap()], invoke_on_opened=True)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    starts: list[str] = []
    monkeypatch.setattr(
        "hark.speech.play_record_start",
        lambda: starts.append("start"),
    )
    logs: list[str] = []
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append(event),
    )

    run_listen(
        HarkConfig(),
        post_tts_guard_s=0,
        arm_cue=True,
    )

    assert starts == ["start"]
    assert "listen.armed_cue" in logs
    assert "listen.speech_opened" in logs
    # on_opened present but must not re-cue
    assert caps[0]["on_opened"] is not None


def test_radio_arm_cue_before_speech_no_double_beep(monkeypatch, tmp_path):
    """Radio: early arm cue once; multi-segment speech open does not re-beep."""
    stt = _FakeStt(texts=["working on it", "ship it okay hark send"])
    caps = _patch_listen(
        monkeypatch,
        stt,
        [_cap(), _cap()],
        invoke_on_opened=True,
    )
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    starts: list[str] = []
    monkeypatch.setattr(
        "hark.speech.play_record_start",
        lambda: starts.append("start"),
    )
    logs: list[str] = []
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append(event),
    )

    result = run_listen(
        HarkConfig(listen=ListenConfig(end_mode="radio", stream_partials=False)),
        end_mode="radio",
        post_tts_guard_s=0,
        arm_cue=True,
    )

    assert result.end_mode == "radio"
    assert result.end_phrase  # finalized by product end phrase
    assert starts == ["start"]
    assert logs.count("listen.armed_cue") == 1
    # speech opened for each segment, but no second start beep
    assert logs.count("listen.speech_opened") == 2
    assert len(caps) == 2


def test_radio_without_arm_cue_beeps_on_speech_open(monkeypatch, tmp_path):
    """Without arm_cue, first speech open still plays record-start once."""
    stt = _FakeStt(texts=["done okay hark send"])
    _patch_listen(monkeypatch, stt, [_cap()], invoke_on_opened=True)
    monkeypatch.setattr(
        "hark.speech.UsageStore",
        lambda: UsageStore(tmp_path / "usage.jsonl"),
    )
    starts: list[str] = []
    monkeypatch.setattr(
        "hark.speech.play_record_start",
        lambda: starts.append("start"),
    )
    logs: list[str] = []
    monkeypatch.setattr(
        "hark.speech.syslog",
        lambda event, **data: logs.append(event),
    )

    run_listen(
        HarkConfig(listen=ListenConfig(end_mode="radio")),
        end_mode="radio",
        post_tts_guard_s=0,
        arm_cue=False,
    )

    assert starts == ["start"]
    assert "listen.armed_cue" not in logs
    assert "listen.speech_opened" in logs


def test_speak_and_listen_passes_answer_arm_cue(monkeypatch):
    """ask/tts --listen wiring: answer_arm_cue flows into run_listen.arm_cue."""
    seen: list[dict] = []

    def fake_tts(cfg, text, **kwargs):
        return {"ok": True, "provider": "mock", "voice": "eve", "mic_muted": True}

    def fake_listen(cfg, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(
            text="yes",
            provider="mock",
            duration_ms=50,
            end_mode="silence",
            end_phrase=None,
            cancelled=False,
            stream_id="s1",
            partials_emitted=0,
            meta_command=None,
        )

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    cfg = HarkConfig()
    assert cfg.audio.answer_arm_cue is True
    speak_and_listen(cfg, "question?")
    assert seen[-1]["arm_cue"] is True

    seen.clear()
    cfg_off = HarkConfig(audio=AudioConfig(answer_arm_cue=False))
    speak_and_listen(cfg_off, "question?")
    assert seen[-1]["arm_cue"] is False

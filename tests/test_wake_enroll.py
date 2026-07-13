"""I006 wake enrollment — beep-paced sample loop (no real mic)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hark.config import AmbientConfig, HarkConfig, ListenConfig
from hark.wake_enroll import (
    DEFAULT_COUNT,
    EnrollResult,
    default_phrase,
    run_wake_enroll,
    _pcm_peak_rms,
)


def _tone_pcm(ms: int = 400, peak: int = 8000, sr: int = 16000) -> bytes:
    """Simple 440 Hz-ish PCM16 for accept threshold tests."""
    import array
    import math

    n = int(sr * ms / 1000)
    out = array.array("h")
    for i in range(n):
        s = int(peak * math.sin(2 * math.pi * 440 * i / sr))
        out.append(max(-32767, min(32767, s)))
    return out.tobytes()


def test_pcm_peak_rms_detects_energy():
    quiet = b"\x00\x00" * 800
    loud = _tone_pcm(200, peak=12000)
    assert _pcm_peak_rms(quiet) < 0.001
    assert _pcm_peak_rms(loud) > 0.2


def test_default_phrase_from_names():
    cfg = HarkConfig(
        ambient=AmbientConfig(names=["iris", "mercury"], activation_phrases=[])
    )
    # activation_phrases often auto-derived — if empty, hey + first name
    p = default_phrase(cfg)
    assert "iris" in p.lower() or p.startswith("hey")


def test_dry_run_beeps_and_succeeds(tmp_path, monkeypatch):
    cues: list[str] = []

    def track(name: str):
        def _fn():
            cues.append(name)

        return _fn

    cfg = HarkConfig(ambient=AmbientConfig(names=["iris"]))
    monkeypatch.setattr("hark.wake_enroll.enroll_root", lambda: tmp_path / "enroll")
    # dry-run still uses pause path — stub mic lease / pause
    monkeypatch.setattr(
        "hark.wake_enroll.pause_ambient_for_mic",
        lambda **kw: _null_cm(),
    )
    monkeypatch.setattr("hark.wake_enroll.MicLease", lambda *a, **k: _null_cm())

    result = run_wake_enroll(
        cfg,
        phrase="hey iris",
        count=3,
        min_count=3,
        dry_run=True,
        beeps=True,
        score=False,
        learn=False,
        play_ready=track("ready"),
        play_accept=track("accept"),
        play_reject=track("reject"),
        play_end=track("end"),
    )
    assert result.ok
    assert result.accepted == 3
    assert cues.count("ready") == 3
    assert cues.count("accept") == 3
    assert cues.count("end") == 1
    assert "reject" not in cues


def test_capture_loop_writes_manifest(tmp_path, monkeypatch):
    pcm = _tone_pcm(500, peak=10000)
    calls = {"n": 0}

    def fake_capture(**kwargs):
        calls["n"] += 1
        return SimpleNamespace(pcm16=pcm, sample_rate=16000)

    monkeypatch.setattr("hark.wake_enroll.enroll_root", lambda: tmp_path / "enroll")
    monkeypatch.setattr(
        "hark.wake_enroll.pause_ambient_for_mic",
        lambda **kw: _null_cm(),
    )
    monkeypatch.setattr("hark.wake_enroll.MicLease", lambda *a, **k: _null_cm())

    cues: list[str] = []
    cfg = HarkConfig(ambient=AmbientConfig(names=["iris"]))
    result = run_wake_enroll(
        cfg,
        phrase="hey iris",
        count=2,
        min_count=2,
        dry_run=False,
        score=False,
        learn=False,
        capture_fn=fake_capture,
        play_ready=lambda: cues.append("r"),
        play_accept=lambda: cues.append("a"),
        play_reject=lambda: cues.append("x"),
        play_end=lambda: cues.append("e"),
    )
    assert result.ok
    assert result.accepted == 2
    assert calls["n"] == 2
    assert result.manifest_path is not None
    assert result.manifest_path.is_file()
    body = result.manifest_path.read_text(encoding="utf-8")
    assert "hey iris" in body
    assert "cloud_upload" in body
    wavs = list(result.out_dir.glob("*.wav"))
    assert len(wavs) == 2
    assert cues == ["r", "a", "r", "a", "e"]


def test_reject_too_quiet_retries(tmp_path, monkeypatch):
    quiet = b"\x00\x00" * 8000
    loud = _tone_pcm(400, peak=12000)
    seq = [quiet, loud]

    def fake_capture(**kwargs):
        pcm = seq.pop(0) if seq else loud
        return SimpleNamespace(pcm16=pcm, sample_rate=16000)

    monkeypatch.setattr("hark.wake_enroll.enroll_root", lambda: tmp_path / "enroll")
    monkeypatch.setattr(
        "hark.wake_enroll.pause_ambient_for_mic",
        lambda **kw: _null_cm(),
    )
    monkeypatch.setattr("hark.wake_enroll.MicLease", lambda *a, **k: _null_cm())

    rejects: list[str] = []
    cfg = HarkConfig()
    result = run_wake_enroll(
        cfg,
        phrase="hey iris",
        count=1,
        min_count=1,
        score=False,
        learn=False,
        capture_fn=fake_capture,
        play_ready=lambda: None,
        play_accept=lambda: None,
        play_reject=lambda: rejects.append("x"),
        play_end=lambda: None,
    )
    assert result.ok
    assert result.accepted == 1
    assert result.rejected >= 1
    assert rejects


class _null_cm:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

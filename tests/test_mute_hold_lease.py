"""B084 mute hold is a bounded lease: a leaked hold cannot pin the mic open.

The half-duplex TTS mute freezes every listen clock (B084/B112). A hold whose
``run_tts`` died before its ``finally`` therefore froze capture forever: the
frozen branch advanced no counter and paused the wall-clock watchdog, so
``capture_utterance`` never returned. The lease bounds that freeze.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from hark.audio import capture as cap
from hark.audio import mic_mute as mm


class _FreezeNeverEnded(RuntimeError):
    """Test guard: capture kept reading long past any sane freeze budget."""


def _drop_hold(monkeypatch, reason: str) -> None:
    """Clear any hold without shelling out to pactl/amixer on the host."""
    with monkeypatch.context() as m:
        m.setattr(mm, "_which", lambda n: False)
        m.setattr(mm, "find_wave_alsa_card", lambda: None)
        mm.force_clear_tts_mute_hold(reason=reason)


@pytest.fixture(autouse=True)
def _reset_hold(monkeypatch):
    """A leaked hold would freeze every later capture test — clear both ways."""
    _drop_hold(monkeypatch, "test_setup")
    yield
    _drop_hold(monkeypatch, "test_teardown")


def _fake_pulse(monkeypatch) -> None:
    """Make mute acquisition work headless (no pactl/amixer on CI)."""
    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(mm, "default_source", lambda: "src0")
    monkeypatch.setattr(mm, "source_is_muted", lambda s: False)
    monkeypatch.setattr(mm, "set_source_mute", lambda *a, **k: True)
    monkeypatch.setattr(mm, "set_alsa_mic_capture", lambda *a, **k: True)
    monkeypatch.setattr(mm, "find_wave_alsa_card", lambda: None)


def _silent_stream(*, guard_s: float = 5.0):
    """Stream of silence that refuses to feed an unbounded freeze forever."""
    block = np.zeros(320, dtype=np.float32).reshape(-1, 1)

    class FakeStream:
        def __init__(self) -> None:
            self.t0 = time.monotonic()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            if time.monotonic() - self.t0 > guard_s:
                raise _FreezeNeverEnded(
                    f"capture still reading {guard_s:g}s into a held TTS mute"
                )
            return block.copy(), False

    return FakeStream


def test_capture_stops_freezing_when_hold_outlives_its_budget(monkeypatch):
    """A never-released hold must stop freezing clocks, so capture terminates."""
    _fake_pulse(monkeypatch)
    # Keep the test fast: shrink the freeze budget floor (raising=False so the
    # pre-lease red run still exercises the unbounded freeze it is proving).
    monkeypatch.setattr(mm, "MUTE_FREEZE_BUDGET_MIN_S", 0.25, raising=False)
    monkeypatch.setattr(cap, "_require_sd", lambda: None)
    monkeypatch.setattr(
        cap, "sd", SimpleNamespace(InputStream=lambda **k: _silent_stream()())
    )

    # Crashed TTS: the hold is entered and its finally never runs.
    held = mm.mic_muted_during_tts(enabled=True)
    held.__enter__()

    with pytest.raises(TimeoutError):
        cap.capture_utterance(
            sample_rate=16000,
            max_s=5.0,
            end_silence_s=0.15,
            min_speech_s=0.08,
            initial_timeout_s=0.2,
            mute_edge_pad_ms=40,
            preroll_ms=0,
            open_confirm_blocks=2,
            abs_open_db=-40.0,
            open_margin_db=1.0,
        )


def test_capped_freeze_is_logged(monkeypatch):
    """The cap must be visible in the field, not a silent behaviour change."""
    _fake_pulse(monkeypatch)
    monkeypatch.setattr(mm, "MUTE_FREEZE_BUDGET_MIN_S", 0.25, raising=False)
    monkeypatch.setattr(cap, "_require_sd", lambda: None)
    monkeypatch.setattr(
        cap, "sd", SimpleNamespace(InputStream=lambda **k: _silent_stream()())
    )
    logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "hark.syslog.log", lambda event, **data: logs.append((event, data))
    )

    held = mm.mic_muted_during_tts(enabled=True)
    held.__enter__()

    with pytest.raises(TimeoutError):
        cap.capture_utterance(
            sample_rate=16000,
            max_s=5.0,
            end_silence_s=0.15,
            min_speech_s=0.08,
            initial_timeout_s=0.2,
            mute_edge_pad_ms=0,
            preroll_ms=0,
            open_confirm_blocks=2,
            abs_open_db=-40.0,
            open_margin_db=1.0,
        )

    capped = [d for e, d in logs if e == "listen.mute_freeze_capped"]
    assert len(capped) == 1, [e for e, _ in logs]
    assert capped[0]["budget_s"] == pytest.approx(0.25)
    assert capped[0]["depth"] == 1


def test_hold_freezes_while_fresh_and_not_once_stale(monkeypatch):
    """The lease itself is the single authority for "may clocks stay frozen"."""
    _fake_pulse(monkeypatch)
    with mm.mic_muted_during_tts(enabled=True) as hold:
        assert hold.held() is True
        assert hold.freezing(budget_s=30.0) is True
        assert mm.current_tts_mute_hold() is hold
        hold.acquired_mono -= 120.0
        assert hold.held() is True  # still muted: the OS mute is unchanged
        assert hold.freezing(budget_s=30.0) is False
    assert mm.current_tts_mute_hold() is None


def test_nested_holds_share_one_lease(monkeypatch):
    """Nesting keeps depth semantics and does not extend the lease (B084)."""
    _fake_pulse(monkeypatch)
    with mm.mic_muted_during_tts(enabled=True) as outer:
        started = outer.acquired_mono
        with mm.mic_muted_during_tts(enabled=True) as inner:
            assert inner is outer
            assert inner.depth == 2
            assert mm.tts_mute_depth() == 2
            assert inner.acquired_mono == started
        assert outer.depth == 1
        assert mm.tts_mute_depth() == 1
    assert mm.tts_mute_depth() == 0
    assert mm.current_tts_mute_hold() is None


def test_disabled_hold_never_freezes():
    with mm.mic_muted_during_tts(enabled=False) as hold:
        assert hold.held() is False
        assert hold.freezing(budget_s=30.0) is False
        assert mm.current_tts_mute_hold() is None


def test_freeze_budget_mirrors_discard_shape():
    """Floor of 30 s, or the gate budget when that is larger (capture.py)."""
    assert mm.mute_freeze_budget_s(0.5) == 30.0
    assert mm.mute_freeze_budget_s(45.0) == 45.0

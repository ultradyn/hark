"""One frame source under both mic read loops (B084 freeze + B087 telemetry).

``capture_utterance`` and ``ContinuousMicStream.read_for`` used to re-implement
frame acquisition, the read deadline, the TTS mute freeze and the spectrum
publish inline, with two different throttles and a synchronous state-file write
on the audio thread. These pin the shared seam.
"""

from __future__ import annotations

import inspect
import threading
import time

import numpy as np
import pytest

BLOCK = 320  # 20 ms @ 16 kHz


class _FakeStream:
    """Non-blocking input stream: one distinct constant per 20 ms block."""

    def __init__(self, *, fail_at: int | None = None) -> None:
        self.reads = 0
        self.fail_at = fail_at

    def read(self, block: int):
        self.reads += 1
        if self.fail_at is not None and self.reads >= self.fail_at:
            raise OSError("native read failed")
        samples = np.full(block, 0.01 * self.reads, dtype=np.float32)
        return samples.reshape(-1, 1), False


class _ScriptedGate:
    """Freeze authority stand-in: one scripted answer per frame."""

    def __init__(self, script) -> None:
        self.script = list(script)
        self.asked = 0

    def freezing(self) -> bool:
        i, self.asked = self.asked, self.asked + 1
        return bool(self.script[i]) if i < len(self.script) else False


class _RecordingDeadline:
    """Read deadline stand-in that records the frame source's calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fired: str | None = None

    def check(self) -> None:
        if self.fired is not None:
            raise TimeoutError(self.fired)

    def map_error(self, exc: BaseException) -> BaseException:
        return TimeoutError(self.fired) if self.fired is not None else exc

    def arm(self, timeout_s: float, message: str) -> None:
        self.calls.append(("arm", message))

    def disarm(self) -> None:
        self.calls.append(("disarm",))

    def pause(self) -> None:
        self.calls.append(("pause",))

    def resume(self) -> None:
        self.calls.append(("resume",))

    def close(self) -> None:
        self.calls.append(("close",))

    def fired_message(self) -> str | None:
        return self.fired


# ---------------------------------------------------------------------------
# Telemetry is a tap, and it runs off the audio thread
# ---------------------------------------------------------------------------

def test_spectrum_tap_never_blocks_the_audio_thread(monkeypatch):
    """A wedged ``spectrum.latest`` write must not stall a read loop."""
    from hark.audio import spectrum as spec_mod
    from hark.audio.spectrum import SpectrumTap

    release = threading.Event()
    writes = 0

    def blocking_write(path, payload):
        nonlocal writes
        writes += 1
        release.wait(5.0)

    monkeypatch.setattr(spec_mod, "_write_latest", blocking_write)

    tap = SpectrumTap(sample_rate=16000, source="listen", interval_s=0.0)
    try:
        started = time.monotonic()
        for i in range(60):
            tap(np.full(BLOCK, 0.01 * i, dtype=np.float32))
        elapsed = time.monotonic() - started
    finally:
        release.set()
        tap.close()

    assert elapsed < 1.0, f"tap blocked the audio thread for {elapsed:.2f}s"
    # Bounded queue: a wedged writer costs frames, never latency.
    assert tap.dropped > 0
    assert writes >= 1


def test_spectrum_tap_publishes_a_window_off_thread(monkeypatch):
    """The FFT window is concatenated on the worker, never on the audio thread."""
    from hark.audio import spectrum as spec_mod
    from hark.audio.spectrum import SpectrumTap

    seen: list[dict] = []
    monkeypatch.setattr(spec_mod, "_write_latest", lambda path, payload: seen.append(payload))

    tap = SpectrumTap(sample_rate=16000, source="ambient", recording=False, interval_s=0.0)
    for i in range(8):
        tap(np.full(BLOCK, 0.2, dtype=np.float32))
    tap.close()

    assert seen, "tap published nothing"
    assert seen[-1]["kind"] == "serve.spectrum"
    assert seen[-1]["recording"] is False
    assert seen[-1]["source"] == "ambient"
    assert len(seen[-1]["bands"]) == spec_mod.DEFAULT_N_BANDS


def test_closed_tap_drops_frames_and_joins_its_worker(monkeypatch):
    from hark.audio import spectrum as spec_mod
    from hark.audio.spectrum import SpectrumTap

    seen: list[dict] = []
    monkeypatch.setattr(spec_mod, "_write_latest", lambda path, payload: seen.append(payload))

    tap = SpectrumTap(sample_rate=16000, source="listen", interval_s=0.0)
    tap(np.full(BLOCK, 0.3, dtype=np.float32))
    tap.close()
    published = len(seen)
    tap(np.full(BLOCK, 0.3, dtype=np.float32))
    tap.close()  # idempotent
    assert len(seen) == published
    assert not tap.alive


# ---------------------------------------------------------------------------
# One frame source, two adapters
# ---------------------------------------------------------------------------

def test_frame_source_yields_20ms_frames_and_taps_each_one():
    from hark.audio.frames import FramePhase, MicFrameSource

    tapped: list[float] = []
    stream = _FakeStream()
    src = MicFrameSource(stream, block=BLOCK, tap=lambda s: tapped.append(float(s[0])))

    frames = [src.read() for _ in range(3)]
    assert [f.samples.shape for f in frames] == [(BLOCK,), (BLOCK,), (BLOCK,)]
    assert all(f.phase is FramePhase.LIVE for f in frames)
    assert tapped == pytest.approx([0.01, 0.02, 0.03], abs=1e-6)
    assert stream.reads == 3


def test_frame_source_keeps_yielding_while_the_tap_write_blocks(monkeypatch):
    """The read loop must outrun a wedged disk (the whole point of the tap)."""
    from hark.audio import spectrum as spec_mod
    from hark.audio.frames import MicFrameSource
    from hark.audio.spectrum import SpectrumTap

    release = threading.Event()
    monkeypatch.setattr(
        spec_mod, "_write_latest", lambda path, payload: release.wait(5.0)
    )

    stream = _FakeStream()
    tap = SpectrumTap(sample_rate=16000, source="listen", interval_s=0.0)
    src = MicFrameSource(stream, block=BLOCK, tap=tap)
    try:
        started = time.monotonic()
        for _ in range(60):
            src.read()
        elapsed = time.monotonic() - started
    finally:
        release.set()
        src.close()

    assert stream.reads == 60
    assert elapsed < 1.0, f"read loop stalled behind telemetry for {elapsed:.2f}s"


def test_tap_fires_on_frozen_frames_too():
    """B087: the live webui keeps moving while TTS holds the mic muted."""
    from hark.audio.frames import FramePhase, MicFrameSource

    tapped = 0

    def _tap(_samples):
        nonlocal tapped
        tapped += 1

    gate = _ScriptedGate([True, True, True])
    src = MicFrameSource(_FakeStream(), block=BLOCK, tap=_tap, mute_gate=gate)
    assert [src.read().phase for _ in range(3)] == [FramePhase.FROZEN] * 3
    assert tapped == 3


def test_frame_phases_follow_the_mute_lease_and_its_edge_pad():
    """B084: frozen while the hold stands, then ``mute_edge_pad_ms`` of pad."""
    from hark.audio.frames import FramePhase, MicFrameSource

    gate = _ScriptedGate([False, True, True, False, False, False])
    src = MicFrameSource(
        _FakeStream(), block=BLOCK, mute_gate=gate, edge_pad_frames=2
    )
    got = [(f.phase, f.mute_released) for f in (src.read() for _ in range(6))]
    assert got == [
        (FramePhase.LIVE, False),
        (FramePhase.FROZEN, False),
        (FramePhase.FROZEN, False),
        # The release frame is itself the first pad frame (legacy block maths).
        (FramePhase.EDGE_PAD, True),
        (FramePhase.EDGE_PAD, False),
        (FramePhase.LIVE, False),
    ]


def test_zero_edge_pad_releases_straight_to_a_live_frame():
    from hark.audio.frames import FramePhase, MicFrameSource

    gate = _ScriptedGate([True, False])
    src = MicFrameSource(
        _FakeStream(), block=BLOCK, mute_gate=gate, edge_pad_frames=0
    )
    assert src.read().phase is FramePhase.FROZEN
    released = src.read()
    assert released.phase is FramePhase.LIVE
    assert released.mute_released is True


def test_re_muting_mid_pad_restarts_the_full_pad():
    from hark.audio.frames import FramePhase, MicFrameSource

    gate = _ScriptedGate([True, False, True, False, False, False])
    src = MicFrameSource(
        _FakeStream(), block=BLOCK, mute_gate=gate, edge_pad_frames=2
    )
    assert [src.read().phase for _ in range(6)] == [
        FramePhase.FROZEN,
        FramePhase.EDGE_PAD,
        FramePhase.FROZEN,
        FramePhase.EDGE_PAD,
        FramePhase.EDGE_PAD,
        FramePhase.LIVE,
    ]


def test_frozen_and_padded_frames_pause_the_read_deadline():
    """B084: the wall-clock gate budget freezes with the clocks it bounds."""
    from hark.audio.frames import MicFrameSource

    deadline = _RecordingDeadline()
    gate = _ScriptedGate([True, False, False])
    src = MicFrameSource(
        _FakeStream(),
        block=BLOCK,
        deadline=deadline,
        mute_gate=gate,
        edge_pad_frames=1,
    )
    src.read()  # frozen
    src.read()  # edge pad
    src.read()  # live
    assert deadline.calls == [("pause",), ("pause",), ("resume",)]


def test_frame_source_is_the_only_deadline_contact_point():
    from hark.audio.frames import MicFrameSource

    deadline = _RecordingDeadline()
    src = MicFrameSource(_FakeStream(), block=BLOCK, deadline=deadline)
    src.arm_deadline(1.5, "gate")
    src.read()
    src.disarm_deadline()
    assert src.deadline_fired() is None
    src.close()
    assert deadline.calls == [("arm", "gate"), ("resume",), ("disarm",), ("close",)]


def test_fired_deadline_surfaces_as_timeout_error():
    from hark.audio.frames import MicFrameSource

    deadline = _RecordingDeadline()
    deadline.fired = "no speech detected"
    src = MicFrameSource(_FakeStream(), block=BLOCK, deadline=deadline)
    with pytest.raises(TimeoutError, match="no speech detected"):
        src.read()


def test_native_read_failure_propagates_when_no_deadline_fired():
    from hark.audio.frames import MicFrameSource

    src = MicFrameSource(
        _FakeStream(fail_at=1), block=BLOCK, deadline=_RecordingDeadline()
    )
    with pytest.raises(OSError, match="native read failed"):
        src.read()


def test_native_read_failure_maps_to_timeout_when_the_deadline_fired_mid_read():
    """B145: the watchdog aborts the stream, so the abort error must not surface."""
    from hark.audio.frames import MicFrameSource

    class _FiresDuringRead(_RecordingDeadline):
        def check(self) -> None:
            # Not fired yet when the read starts; the watchdog fires during it.
            return None

    deadline = _FiresDuringRead()
    deadline.fired = "capture deadline exceeded"
    src = MicFrameSource(_FakeStream(fail_at=1), block=BLOCK, deadline=deadline)
    with pytest.raises(TimeoutError, match="capture deadline exceeded"):
        src.read()


def test_attach_switches_on_telemetry_and_freeze_after_the_discard_phase():
    """Phase 0 discard publishes nothing and burns no freeze budget."""
    from hark.audio.frames import FramePhase, MicFrameSource

    tapped: list[float] = []
    gate = _ScriptedGate([True, True, True])
    src = MicFrameSource(_FakeStream(), block=BLOCK)
    assert src.read().phase is FramePhase.LIVE  # discard read: gate not consulted
    assert gate.asked == 0
    assert tapped == []
    src.attach(tap=lambda s: tapped.append(float(s[0])), mute_gate=gate)
    assert src.read().phase is FramePhase.FROZEN
    assert gate.asked == 1
    assert len(tapped) == 1


# ---------------------------------------------------------------------------
# One throttle, not two
# ---------------------------------------------------------------------------

def test_both_read_loops_share_one_spectrum_throttle():
    from hark.audio import capture as cap_mod
    from hark.audio import spectrum as spec_mod

    src = inspect.getsource(cap_mod)
    # The per-loop wall-clock throttles (16 ms listen / 32 ms ambient) are gone.
    assert "spec_interval_s" not in src
    assert "last_spec" not in src
    assert "_publish_spec" not in src
    # Both loops build their publisher through the one helper.
    assert "_make_spectrum_tap" in inspect.getsource(cap_mod.capture_utterance)
    assert "_make_spectrum_tap" in inspect.getsource(cap_mod.ContinuousMicStream.open)
    assert spec_mod.TAP_INTERVAL_S > 0


def test_make_spectrum_tap_survives_a_missing_spectrum_module(monkeypatch):
    """Telemetry stays optional: capture must not hard-depend on it."""
    import sys

    from hark.audio import capture as cap_mod

    monkeypatch.setitem(sys.modules, "hark.audio.spectrum", None)
    assert cap_mod._make_spectrum_tap(
        sample_rate=16000, source="listen", recording=True
    ) is None


# ---------------------------------------------------------------------------
# The capped freeze budget moved with the freeze decision (B084 / B086)
# ---------------------------------------------------------------------------

def _held_hold(*, age_s: float = 0.0):
    """A lease held by one owner, optionally aged past its freeze budget."""
    from hark.audio import mic_mute

    hold = mic_mute.MuteHold(
        state=mic_mute.MuteState(source="src0", was_muted=False, applied=True),
        depth=1,
    )
    hold.acquired_mono -= age_s
    return hold


def test_mute_freeze_gate_stops_freezing_past_its_budget(monkeypatch):
    from hark.audio import mic_mute
    from hark.audio.frames import MuteFreezeGate

    hold = _held_hold(age_s=100.0)
    monkeypatch.setattr(mic_mute, "current_tts_mute_hold", lambda: hold)

    logs: list[tuple[str, dict]] = []
    import hark.syslog as syslog_mod

    monkeypatch.setattr(
        syslog_mod, "log", lambda event, **kw: logs.append((event, kw))
    )

    gate = MuteFreezeGate(budget_s=30.0)
    assert gate.freezing() is False
    assert gate.capped is True
    # Logged once per capture, not once per frame (B084).
    assert gate.freezing() is False
    assert [e for e, _ in logs] == ["listen.mute_freeze_capped"]
    assert hold.held() is True  # the OS mute itself still stands


def test_mute_freeze_gate_freezes_a_fresh_hold(monkeypatch):
    from hark.audio import mic_mute
    from hark.audio.frames import MuteFreezeGate

    hold = _held_hold()
    monkeypatch.setattr(mic_mute, "current_tts_mute_hold", lambda: hold)

    gate = MuteFreezeGate(budget_s=30.0)
    assert gate.freezing() is True
    assert gate.capped is False


def test_mute_freeze_gate_ignores_an_absent_hold(monkeypatch):
    from hark.audio import mic_mute
    from hark.audio.frames import MuteFreezeGate

    monkeypatch.setattr(mic_mute, "current_tts_mute_hold", lambda: None)
    gate = MuteFreezeGate(budget_s=30.0)
    assert gate.freezing() is False
    assert gate.capped is False

"""One 20 ms frame source under both microphone read loops.

``capture_utterance`` (the one-shot answer window) and
``ContinuousMicStream.read_for`` (ambient wake) both pull 20 ms frames from a
PortAudio input stream, and both used to re-implement the same three side
concerns inline: the wall-clock read deadline, the B084 TTS mute freeze, and
the B087 spectrum telemetry — the last with two different throttles and a
synchronous state-file write on the audio thread.

:class:`MicFrameSource` owns all three, so each loop keeps only the policy that
actually differs: the energy gate / endpointing decision on one side, ring
filling on the other. A frame arrives already classified (:class:`FramePhase`)
and the deadline has already been paused or resumed to match.

The read deadline class itself stays in :mod:`hark.audio.capture`: it aborts a
hung ``Pa_ReadStream`` through the process-global stream-cancel registry that
B143 / B145 cancellation ownership depends on. This module drives it through
:class:`ReadDeadline` and never reaches into that machinery.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

import numpy as np

# 20 ms capture frame (matches hark.endpointing.BLOCK_S).
FRAME_S = 0.02


@runtime_checkable
class ReadDeadline(Protocol):
    """Wall-clock abort for a blocked native read (``_CaptureReadDeadline``)."""

    def arm(self, timeout_s: float, message: str) -> None: ...
    def disarm(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def check(self) -> None: ...
    def map_error(self, exc: BaseException) -> BaseException: ...
    def fired_message(self) -> str | None: ...
    def close(self) -> None: ...


@runtime_checkable
class FreezeGate(Protocol):
    """"May listen clocks stay frozen right now?" (see :class:`MuteFreezeGate`)."""

    def freezing(self) -> bool: ...


class FramePhase(enum.Enum):
    """How a read loop must treat one frame."""

    # Normal frame: clocks advance, energy counts.
    LIVE = "live"
    # Hark holds the mic muted for half-duplex TTS (B084): clocks freeze.
    FROZEN = "frozen"
    # ``mute_edge_pad_ms`` settle window after the hold released; still frozen.
    EDGE_PAD = "edge_pad"


@dataclass(frozen=True)
class Frame:
    """One 20 ms mono float32 frame plus its phase."""

    samples: np.ndarray
    phase: FramePhase = FramePhase.LIVE
    # True on the first frame after a TTS mute hold released (B084 edge). The
    # release frame is itself the first edge-pad frame when a pad is configured.
    mute_released: bool = False

    @property
    def frozen(self) -> bool:
        """True while this frame must not advance any listen clock."""
        return self.phase is not FramePhase.LIVE


class MuteFreezeGate:
    """B084 / B086: the capped freeze consultation, once.

    The authority is the TTS mute **lease** (:class:`~hark.audio.mic_mute.MuteHold`),
    never a process-global depth count: a hold whose ``run_tts`` died never
    decrements, and every frozen frame advances nothing, so an uncapped freeze
    holds the mic open forever. Past ``budget_s`` the freeze stops — the mute
    itself stands, since only the hold's owner may unmute — so the capture ends
    as a bounded listen timeout. Capped once per gate, logged once.
    """

    def __init__(self, budget_s: float) -> None:
        self.budget_s = float(budget_s)
        self.capped = False

    def freezing(self) -> bool:
        try:
            from hark.audio.mic_mute import current_tts_mute_hold

            hold = current_tts_mute_hold()
        except Exception:
            return False
        if hold is None:
            return False
        if hold.freezing(budget_s=self.budget_s):
            return True
        if hold.held() and not self.capped:
            # Mute still stands (only its owner may unmute); we just stop
            # freezing, so this ends as a bounded listen timeout.
            self.capped = True
            self._log_capped(hold)
        return False

    def _log_capped(self, hold: Any) -> None:
        try:
            from hark.syslog import log as _syslog

            _syslog(
                "listen.mute_freeze_capped",
                component="stt",
                level="warn",
                hold_age_s=round(hold.age_s(), 1),
                budget_s=round(self.budget_s, 2),
                depth=hold.depth,
                source=hold.source,
                message=(
                    "TTS mute hold outlived its freeze budget — "
                    "resuming listen clocks (run `hark mute-sync` "
                    "if the mic stays muted)"
                ),
            )
        except Exception:
            pass


class MicFrameSource:
    """Yield classified 20 ms frames from one open input stream.

    :meth:`read` performs exactly one ``stream.read`` and, in this order:

    1. surfaces a fired read deadline as ``TimeoutError`` — before and after the
       native read, so an aborted hung ``Pa_ReadStream`` maps cleanly (B145);
    2. hands the frame to the tap, **every** frame including frozen ones, so
       the live webui keeps moving while TTS holds the mic (B087);
    3. classifies it against the mute freeze and the post-unmute edge pad,
       pausing / resuming the deadline to match (B084).

    A source starts bare (acquisition + deadline only) so a discard phase can
    read frames without publishing telemetry or burning freeze budget;
    :meth:`attach` switches both on.
    """

    def __init__(
        self,
        stream: Any,
        *,
        block: int,
        deadline: ReadDeadline | None = None,
        tap: Callable[[np.ndarray], None] | None = None,
        mute_gate: FreezeGate | None = None,
        edge_pad_frames: int = 0,
    ) -> None:
        self._stream = stream
        self._block = int(block)
        self._deadline = deadline
        self._tap = tap
        self._mute_gate = mute_gate
        self._edge_pad_frames = max(0, int(edge_pad_frames))
        self._was_frozen = False
        self._pad_left = 0

    def attach(
        self,
        *,
        tap: Callable[[np.ndarray], None] | None = None,
        mute_gate: FreezeGate | None = None,
        edge_pad_frames: int = 0,
    ) -> None:
        """Switch on telemetry and the TTS freeze (after the discard phase)."""
        self._tap = tap
        self._mute_gate = mute_gate
        self._edge_pad_frames = max(0, int(edge_pad_frames))

    # -- deadline: this source is its only contact point -------------------

    def arm_deadline(self, timeout_s: float, message: str) -> None:
        if self._deadline is not None:
            self._deadline.arm(timeout_s, message)

    def disarm_deadline(self) -> None:
        if self._deadline is not None:
            self._deadline.disarm()

    def deadline_fired(self) -> str | None:
        return None if self._deadline is None else self._deadline.fired_message()

    def close(self) -> None:
        """Release the deadline watchdog and the tap worker."""
        tap = self._tap
        self._tap = None
        close = getattr(tap, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        if self._deadline is not None:
            self._deadline.close()

    # -- frames ------------------------------------------------------------

    def read(self) -> Frame:
        samples = self._read_samples()
        if self._tap is not None:
            try:
                self._tap(samples)
            except Exception:
                pass
        return self._classify(samples)

    def _read_samples(self) -> np.ndarray:
        deadline = self._deadline
        if deadline is not None:
            deadline.check()
        try:
            data, overflowed = self._stream.read(self._block)
        except BaseException as exc:
            if deadline is not None:
                mapped = deadline.map_error(exc)
                if mapped is not exc:
                    raise mapped from exc
            raise
        del overflowed
        if deadline is not None:
            deadline.check()
        return data.reshape(-1)

    def _classify(self, samples: np.ndarray) -> Frame:
        gate = self._mute_gate
        if gate is not None and gate.freezing():
            self._was_frozen = True
            self._pause()
            return Frame(samples=samples, phase=FramePhase.FROZEN)
        released = self._was_frozen
        if released:
            # A hold that re-engaged mid-pad restarts the full pad on release.
            self._was_frozen = False
            self._pad_left = self._edge_pad_frames
        if self._pad_left > 0:
            # The edge pad is part of the mute hold freeze (B084).
            self._pad_left -= 1
            self._pause()
            return Frame(
                samples=samples,
                phase=FramePhase.EDGE_PAD,
                mute_released=released,
            )
        self._resume()
        return Frame(
            samples=samples, phase=FramePhase.LIVE, mute_released=released
        )

    def _pause(self) -> None:
        if self._deadline is not None:
            self._deadline.pause()

    def _resume(self) -> None:
        if self._deadline is not None:
            self._deadline.resume()

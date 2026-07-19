"""Speak-then-listen handoff: half-duplex default or optional overlap pre-arm.

Owns near-end arm, overlap discard window (``audio_ok_after``), and attaching
``tts_info`` to listen errors. Calls :func:`hark.speech.run_tts` (play stack:
conference → mute → duck) and :func:`hark.speech.run_listen` (Answer Window).
"""

from __future__ import annotations

import threading
import time
from enum import Enum, auto
from pathlib import Path
from typing import Any

from hark.answer_window.result import ListenResult
from hark.audio.capture import capture_interrupt_signals
from hark.config import HarkConfig
from hark.syslog import log as syslog

_OVERLAP_SETTLE_TIMEOUT_S = 0.25
_OVERLAP_CANCEL_JOIN_S = 2.0


class _AttemptPhase(Enum):
    """Lifecycle of the one overlap-listen attempt owned by a handoff."""

    PUBLISHED = auto()
    RUNNING = auto()
    CANCELLED_BEFORE_RUN = auto()
    QUARANTINED = auto()
    TERMINAL = auto()


class _OverlapAttempt:
    """Own a possibly launched overlap target until it acknowledges terminality.

    The attempt is published before ``Thread`` construction. Cancellation and
    target entry serialize on ``_lock``: either the target becomes ``RUNNING``
    or a late target is refused before it can call ``run_listen``. A running,
    non-cooperative target transfers to process-wide quarantine rather than
    deadlocking the handoff.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.phase = _AttemptPhase.PUBLISHED
        self.cancel_event = threading.Event()
        self.start_finished = threading.Event()
        self.target_entered = threading.Event()
        self.terminal = threading.Event()
        self.start_error: BaseException | None = None
        self.result: ListenResult | None = None
        self.error: BaseException | None = None
        self._capture_state: Any | None = None
        self._capture_state_owned = False

    def bind_capture_state(self, state: Any | None) -> None:
        """Attach the B143 capture lease registered before thread startup."""
        with self._lock:
            self._capture_state = state
            self._capture_state_owned = True
            settle_now = self.phase in (
                _AttemptPhase.CANCELLED_BEFORE_RUN,
                _AttemptPhase.TERMINAL,
            )
        if settle_now:
            release_error = self._settle_capture_state()
            if release_error is not None:
                raise release_error

    def capture_state(self) -> Any | None:
        with self._lock:
            return self._capture_state

    def _settle_capture_state(self) -> BaseException | None:
        """Idempotently release the capture lease before terminal publication."""
        from hark.audio.capture import release_capture_attempt

        first_error: BaseException | None = None
        while True:
            with self._lock:
                if not self._capture_state_owned:
                    return first_error
                state = self._capture_state
            try:
                release_capture_attempt(state)
            except BaseException as exc:  # noqa: BLE001 - lease release must settle
                if first_error is None:
                    first_error = exc
                continue
            with self._lock:
                # Capture leases are tokenised and release is idempotent, so a
                # retry after interruption cannot consume another registration.
                if self._capture_state is state:
                    self._capture_state_owned = False
            return first_error

    def _publish_terminal(
        self,
        *,
        result: ListenResult | None = None,
        error: BaseException | None = None,
    ) -> BaseException | None:
        release_error = self._settle_capture_state()
        final_error = error if error is not None else release_error
        with _QUARANTINE_LOCK:
            with self._lock:
                if self.phase is not _AttemptPhase.TERMINAL:
                    self.result = result
                    self.error = final_error
                    self.phase = _AttemptPhase.TERMINAL
                    self.terminal.set()
                elif self.error is None and final_error is not None:
                    # Concurrent idempotent finalisers never erase the first
                    # observed cleanup failure.
                    self.error = final_error
            _QUARANTINED_ATTEMPTS.discard(self)
        return release_error

    def enter_target(self) -> bool:
        """Acknowledge target entry, or refuse a target cancelled before entry."""
        with self._lock:
            cancelled = self.cancel_event.is_set()
            if not cancelled:
                self.phase = _AttemptPhase.RUNNING
        if cancelled:
            self._publish_terminal()
        # ``Thread.start`` only acknowledges scheduling. Publish target entry
        # separately so the owner never waits forever for a thread paused before
        # its first instruction.
        self.target_entered.set()
        return not cancelled

    def finish(
        self,
        *,
        result: ListenResult | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Release capture ownership, then publish terminal result exactly once."""
        self._publish_terminal(result=result, error=error)

    def finish_cancelled_before_run(self) -> None:
        """Finalize a cancelled target that is forbidden from entering capture."""
        self.cancel_event.set()
        release_error = self._publish_terminal()
        if release_error is not None:
            raise release_error

    def finish_start(self, error: BaseException | None = None) -> None:
        """Publish the outcome of the interruptible ``Thread.start`` call."""
        with self._lock:
            self.start_error = error
            self.start_finished.set()

    def cancel(self) -> _AttemptPhase:
        """Publish durable cancellation and return the observed target phase."""
        with self._lock:
            self.cancel_event.set()
            if self.phase is _AttemptPhase.PUBLISHED:
                self.phase = _AttemptPhase.CANCELLED_BEFORE_RUN
            return self.phase

    def quarantine(self) -> bool:
        """Transfer a non-terminal running target to durable process ownership."""
        with _QUARANTINE_LOCK:
            with self._lock:
                self.cancel_event.set()
                if self.phase is _AttemptPhase.TERMINAL:
                    return False
                if self.phase in (
                    _AttemptPhase.PUBLISHED,
                    _AttemptPhase.CANCELLED_BEFORE_RUN,
                ):
                    self.phase = _AttemptPhase.CANCELLED_BEFORE_RUN
                    return False
                self.phase = _AttemptPhase.QUARANTINED
                _QUARANTINED_ATTEMPTS.add(self)
                return True


_QUARANTINE_LOCK = threading.Lock()
_QUARANTINED_ATTEMPTS: set[_OverlapAttempt] = set()


def _remember_first(
    primary: tuple[BaseException, Any] | None,
    candidate: BaseException,
) -> tuple[BaseException, Any]:
    """Keep the first BaseException and its original traceback."""
    return primary if primary is not None else (candidate, candidate.__traceback__)


def _cancel_active_attempt(
    primary: tuple[BaseException, Any] | None,
) -> tuple[BaseException, Any] | None:
    """Abort a running native capture without replacing an earlier failure."""
    from hark.audio.capture import cancel_active_capture

    exc = primary[0] if primary is not None else None
    signum = getattr(exc, "signum", None)
    try:
        if isinstance(signum, int):
            cancel_active_capture(signum)
        else:
            cancel_active_capture(reason="handoff_cancel")
    except BaseException as cancel_exc:  # noqa: BLE001 - retain first failure
        primary = _remember_first(primary, cancel_exc)
    return primary


def _cancel_and_settle_bounded(
    attempt: _OverlapAttempt,
    primary: tuple[BaseException, Any] | None,
) -> tuple[tuple[BaseException, Any] | None, _AttemptPhase]:
    """Cancel once, then observe terminal acknowledgement or transfer ownership."""
    # Cancellation is the safety-critical ownership transition. An injected
    # BaseException may land before the attempt lock is acquired, so retry until
    # cancellation is durably observable while retaining the first exception.
    while True:
        try:
            phase = attempt.cancel()
            break
        except BaseException as exc:  # noqa: BLE001 - retry ownership transition
            primary = _remember_first(primary, exc)

    if phase is _AttemptPhase.CANCELLED_BEFORE_RUN:
        while True:
            try:
                attempt.finish_cancelled_before_run()
                break
            except BaseException as exc:  # noqa: BLE001 - terminality is mandatory
                primary = _remember_first(primary, exc)
                if attempt.terminal.is_set():
                    break
        return primary, phase
    if phase is _AttemptPhase.TERMINAL:
        return primary, phase

    primary = _cancel_active_attempt(primary)
    try:
        terminal = attempt.terminal.wait(timeout=_OVERLAP_CANCEL_JOIN_S)
    except BaseException as exc:  # noqa: BLE001 - retain first failure
        primary = _remember_first(primary, exc)
        terminal = attempt.terminal.is_set()
    if terminal:
        return primary, _AttemptPhase.TERMINAL

    # Quarantine is the durable owner of a target that ignored cancellation.
    # Retry its idempotent publication rather than returning with no owner.
    while True:
        try:
            quarantined = attempt.quarantine()
            break
        except BaseException as exc:  # noqa: BLE001 - ownership cannot be dropped
            primary = _remember_first(primary, exc)
            if attempt.terminal.is_set():
                return primary, _AttemptPhase.TERMINAL
    if quarantined:
        return primary, _AttemptPhase.QUARANTINED
    return primary, _AttemptPhase.TERMINAL


def attach_tts_info(exc: BaseException, tts_info: dict[str, Any]) -> BaseException:
    """Attach TTS result dict to a listen/provider error (run_ask / CLI)."""
    try:
        setattr(exc, "tts_info", tts_info)
    except BaseException:
        pass
    return exc


@capture_interrupt_signals()
def speak_and_listen(
    cfg: HarkConfig,
    text: str,
    *,
    provider: str | None = None,
    voice: str | None = None,
    end_mode: str | None = None,
    out: Path | None = None,
    mute_mic: bool | None = None,
    on_partial: Any | None = None,
    partial_kind: str = "ambient.partial",
) -> tuple[dict[str, Any], ListenResult]:
    """TTS then listen with half-duplex default or optional overlap pre-arm.

    Default (``overlap_prearm=false``): half-duplex — capture starts after TTS
    exits the mute context. ``listen_pre_arm_ms`` only signals near-end so the
    sequential listen can skip / tighten the post-TTS guard.

    Optional (``overlap_prearm=true``): start the capture thread near TTS end
    while mute may still be held. Frames are discarded until TTS finishes plus
    ``overlap_discard_ms`` so residual echo is not fed to STT (ADR-009 no
    barge-in).
    """
    from hark import speech as speech_mod

    pre_arm_ms = int(cfg.audio.listen_pre_arm_ms)
    overlap = bool(cfg.audio.overlap_prearm) and pre_arm_ms > 0
    discard_ms = max(0, int(cfg.audio.overlap_discard_ms))
    arm_event = threading.Event()
    handoff: dict[str, float | None] = {"tts_done_at": None}
    attempt: _OverlapAttempt | None = None
    publication_lock = threading.Lock()
    publication_closed = False

    def audio_ok_after() -> float | None:
        done = handoff["tts_done_at"]
        if done is None:
            return None
        return float(done) + discard_ms / 1000.0

    def _listen_worker(owned: _OverlapAttempt) -> None:
        if not owned.enter_target():
            return
        try:
            from hark.audio.capture import (
                bind_capture_state,
                raise_if_capture_cancelled,
            )

            with bind_capture_state(owned.capture_state()):
                raise_if_capture_cancelled(owned.capture_state())
                result = speech_mod.run_listen(
                    cfg,
                    profile="bound_answer",
                    provider=provider,
                    end_mode=end_mode,
                    last_tts=text,
                    already_armed=True,
                    post_tts_guard_s=0.0,
                    on_partial=on_partial,
                    partial_kind=partial_kind,
                    audio_ok_after=audio_ok_after,
                )
        except BaseException as exc:  # noqa: BLE001 - surface to owner
            owned.finish(error=exc)
        else:
            owned.finish(result=result)

    def _on_near_end() -> None:
        nonlocal attempt
        owned: _OverlapAttempt | None = None
        start_error: BaseException | None = None
        try:
            with publication_lock:
                if publication_closed:
                    return
                arm_event.set()
                if not overlap or attempt is not None:
                    return
                owned = _OverlapAttempt()
                # Publish ownership before capture registration, Thread
                # construction, or Thread.start can stall/interleave.
                attempt = owned

            from hark.audio.capture import register_capture_attempt

            capture_state = register_capture_attempt()
            owned.bind_capture_state(capture_state)
            worker = threading.Thread(
                target=lambda: _listen_worker(owned),
                name="hark-overlap-listen",
                daemon=True,
            )
            worker.start()
        except BaseException as exc:  # noqa: BLE001 - start decision is observed below
            start_error = exc
        finally:
            if owned is not None:
                # Do not permit interruption to leave ownership unacknowledged.
                while not owned.start_finished.is_set():
                    try:
                        owned.finish_start(start_error)
                    except BaseException as exc:  # noqa: BLE001
                        if start_error is None:
                            start_error = exc
        if start_error is not None:
            return
        try:
            syslog(
                "listen.overlap_prearm",
                component="stt",
                level="info",
                discard_ms=discard_ms,
                pre_arm_ms=pre_arm_ms,
            )
        except BaseException:
            pass

    def _close_publication() -> _OverlapAttempt | None:
        nonlocal publication_closed
        with publication_lock:
            publication_closed = True
            return attempt

    speech_mod.maybe_print_tts_question(cfg, text)

    primary: tuple[BaseException, Any] | None = None
    tts_info: dict[str, Any] | None = None
    try:
        tts_info = speech_mod.run_tts(
            cfg,
            text,
            provider=provider,
            voice=voice,
            play=True,
            out=out,
            mute_mic=cfg.audio.mute_mic_during_tts if mute_mic is None else mute_mic,
            on_near_end=_on_near_end if pre_arm_ms > 0 else None,
            near_end_ms=pre_arm_ms if pre_arm_ms > 0 else 0,
        )
    except BaseException as exc:  # noqa: BLE001 - settle before propagating
        primary = _remember_first(primary, exc)
    finally:
        # Closing publication is the handoff's linearization point. Once this
        # succeeds, no callback can publish a new capture attempt.
        while True:
            try:
                settled_attempt = _close_publication()
                break
            except BaseException as exc:  # noqa: BLE001 - retry ownership close
                primary = _remember_first(primary, exc)

    try:
        handoff["tts_done_at"] = time.monotonic()
    except BaseException as exc:  # noqa: BLE001 - gate is already closed
        primary = _remember_first(primary, exc)
        handoff["tts_done_at"] = 0.0

    if settled_attempt is not None:
        if primary is not None:
            primary, _phase = _cancel_and_settle_bounded(settled_attempt, primary)
        else:
            try:
                start_decided = settled_attempt.start_finished.wait(
                    timeout=_OVERLAP_SETTLE_TIMEOUT_S
                )
            except BaseException as exc:  # noqa: BLE001
                primary = _remember_first(primary, exc)
                start_decided = False

            if not start_decided:
                primary, phase = _cancel_and_settle_bounded(settled_attempt, primary)
                if phase is _AttemptPhase.CANCELLED_BEFORE_RUN:
                    settled_attempt = None
                elif primary is None and phase is _AttemptPhase.QUARANTINED:
                    primary = _remember_first(
                        primary,
                        TimeoutError(
                            "overlap listener did not acknowledge cancellation"
                        ),
                    )
            elif settled_attempt.start_error is not None:
                primary = _remember_first(primary, settled_attempt.start_error)
                primary, _phase = _cancel_and_settle_bounded(settled_attempt, primary)
            else:
                try:
                    target_entered = settled_attempt.target_entered.wait(
                        timeout=_OVERLAP_SETTLE_TIMEOUT_S
                    )
                except BaseException as exc:  # noqa: BLE001 - cancel on interrupt
                    primary = _remember_first(primary, exc)
                    target_entered = False

                if not target_entered:
                    primary, phase = _cancel_and_settle_bounded(
                        settled_attempt, primary
                    )
                    if phase is _AttemptPhase.CANCELLED_BEFORE_RUN:
                        settled_attempt = None
                    elif primary is None and phase is _AttemptPhase.QUARANTINED:
                        primary = _remember_first(
                            primary,
                            TimeoutError("overlap target did not acknowledge entry"),
                        )
                else:
                    try:
                        settled_attempt.terminal.wait()
                    except BaseException as exc:  # noqa: BLE001 - cancel on interrupt
                        primary = _remember_first(primary, exc)
                        primary, _phase = _cancel_and_settle_bounded(
                            settled_attempt, primary
                        )

    if primary is not None:
        exc, traceback = primary
        if tts_info is not None:
            attach_tts_info(exc, tts_info)
        raise exc.with_traceback(traceback)

    assert tts_info is not None
    if settled_attempt is not None:
        if settled_attempt.error is not None:
            raise attach_tts_info(settled_attempt.error, tts_info)
        listened = settled_attempt.result
        assert isinstance(listened, ListenResult)
        speech_mod._tag_meta_command(listened)
        return tts_info, listened

    try:
        listened = speech_mod.run_listen(
            cfg,
            profile="bound_answer",
            provider=provider,
            end_mode=end_mode,
            last_tts=text,
            post_tts_guard_s=cfg.audio.post_tts_guard_ms / 1000.0,
            already_armed=arm_event.is_set(),
            on_partial=on_partial,
            partial_kind=partial_kind,
        )
    except BaseException as exc:
        raise attach_tts_info(exc, tts_info) from exc
    speech_mod._tag_meta_command(listened)
    return tts_info, listened

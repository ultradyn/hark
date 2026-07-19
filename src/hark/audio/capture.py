"""Microphone capture with adaptive energy gate and single-mic lease."""

from __future__ import annotations

import io
import fcntl
import os
import select
import signal
import threading
import time
import wave
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import numpy as np

from hark.endpointing import EndpointFrame, EndpointStrategy, SilenceEndpointer
from hark.paths import state_dir

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None  # type: ignore


class MicBusyError(RuntimeError):
    pass


class CaptureInterrupted(KeyboardInterrupt):
    """Process signal interrupted an active one-shot microphone capture."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:
            signal_name = str(signum)
        self.signal_name = signal_name
        super().__init__(f"capture interrupted by {signal_name}")


class CaptureCancelled(KeyboardInterrupt):
    """A non-signal lifecycle event cancelled one-shot microphone capture."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.signal_name = None
        super().__init__(f"capture cancelled: {reason}")


class _CaptureSignalState:
    """Cancellation shared by the ask thread and an overlap capture worker."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cancelled = False
        self._signum: int | None = None
        self._reason: str | None = None
        self._wake_signal_reason: str | None = None
        self._interruption_surfaced = False
        self._interruption_deferred = False
        self._attempts: set[object] = set()
        self._cleanup_depth = 0

    def begin_attempt(self) -> object:
        token = object()
        with self._lock:
            self._attempts.add(token)
        return token

    def finish_attempt(self, token: object) -> None:
        """Release one registration idempotently, even after interruption."""
        with self._lock:
            self._attempts.discard(token)

    def has_attempt(self) -> bool:
        with self._lock:
            return bool(self._attempts)

    def request(
        self,
        signum: int | None = None,
        *,
        reason: str | None = None,
    ) -> bool:
        """Publish cancellation atomically; preserve its first known identity."""
        with self._lock:
            first = not self._cancelled
            self._cancelled = True
            if self._signum is None and self._reason is None:
                if reason:
                    self._reason = reason
                elif signum is not None:
                    # The first delivered signal is the cancellation's identity.
                    # A repeat reinforces cleanup but never rewrites the result.
                    self._signum = signum
            return first

    def prepare_wake_signal(self, reason: str) -> bool:
        """Stage an internal SIGTERM that wakes the main thread from native read."""
        with self._lock:
            if self._signum is not None:
                return False
            self._cancelled = True
            if self._reason is None:
                self._reason = reason
            self._wake_signal_reason = self._reason
            return True

    def consume_wake_signal(self, signum: int) -> str | None:
        """Return a staged lifecycle reason instead of treating SIGTERM as external."""
        with self._lock:
            if signum != signal.SIGTERM or self._wake_signal_reason is None:
                return None
            reason = self._wake_signal_reason
            self._wake_signal_reason = None
            return reason

    def _interruption_locked(self) -> KeyboardInterrupt | None:
        if not self._cancelled:
            return None
        if self._reason is not None:
            return CaptureCancelled(self._reason)
        if self._signum is None:
            return KeyboardInterrupt("capture cancelled")
        return CaptureInterrupted(self._signum)

    def interruption(self) -> KeyboardInterrupt | None:
        with self._lock:
            return self._interruption_locked()

    def take_interruption(self) -> KeyboardInterrupt | None:
        """Mark the pending interruption surfaced and return its exception."""
        with self._lock:
            interruption = self._interruption_locked()
            if interruption is not None:
                self._interruption_surfaced = True
            return interruption

    def interruption_surfaced(self) -> bool:
        with self._lock:
            return self._interruption_surfaced

    def defer_interruption(self) -> None:
        """Remember that a signal handler suppressed cancellation for cleanup."""
        with self._lock:
            if not self._interruption_surfaced:
                self._interruption_deferred = True

    def interruption_deferred(self) -> bool:
        with self._lock:
            return self._interruption_deferred

    def begin_cleanup(self) -> None:
        with self._lock:
            self._cleanup_depth += 1

    def finish_cleanup(self) -> None:
        with self._lock:
            self._cleanup_depth = max(0, self._cleanup_depth - 1)

    def cleaning_up(self) -> bool:
        with self._lock:
            return self._cleanup_depth > 0


@dataclass(frozen=True)
class _CaptureAttemptLease:
    """Idempotent token for one early capture-attempt registration."""

    state: _CaptureSignalState
    token: object

    def release(self) -> None:
        self.state.finish_attempt(self.token)


_capture_state_lock = threading.RLock()
_capture_signal_state: _CaptureSignalState | None = None
_capture_thread_state = threading.local()
_active_stream_lock = threading.RLock()
_active_input_stream: Any | None = None
_stream_cancel_lock = threading.RLock()
_STREAM_CLEANUP_JOIN_S = 2.0


@dataclass
class _StreamCleanupOwner:
    stream: Any
    entered: threading.Event
    cancel_requested: bool
    fallback_exit: Callable[[], Any] | None = None
    worker: threading.Thread | None = None
    start_claimed: bool = False


_stream_cancel_workers: dict[int, _StreamCleanupOwner] = {}
_PARENT_EXIT_REASON = "orchestrator_disappeared"
_PARENT_WATCH_POLL_S = 0.05
# Keep parent-lifetime monitoring independent of tests that replace the native
# cleanup thread factory to exercise Thread.start publication races.
_ParentWatchThread = threading.Thread
# Same for the capture deadline watchdog: B143 tests replace threading.Thread
# to inject signals at cleanup-worker start and must not trip on this helper.
_CaptureDeadlineThread = threading.Thread
# Wall-clock abort for Pa_ReadStream hangs that never advance block counters
# (B145). Poll granularity bounds worst-case overrun on short test deadlines.
_CAPTURE_DEADLINE_POLL_S = 0.02


class _CaptureReadDeadline:
    """Abort a blocked PortAudio read when a wall-clock capture budget elapses.

    Gate / discard timeouts normally count completed 20 ms frames. When
    ``stream.read`` never returns (hung ``Pa_ReadStream``), those counters
    stall forever and ``hark ask`` never produces a structured result. This
    watchdog arms an absolute deadline, asynchronously aborts the active
    stream (same native path as B143 cancellation), and converts the resulting
    read failure into ``TimeoutError`` — without sticky KeyboardInterrupt, so
    ask still maps the failure to exit code TIMEOUT.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._deadline_mono: float | None = None
        self._message: str | None = None
        self._fired_message: str | None = None
        self._paused_remaining: float | None = None
        self._stop = threading.Event()
        self._thread = _CaptureDeadlineThread(
            target=self._run,
            name="hark-capture-deadline",
            daemon=True,
        )
        self._thread.start()

    def arm(self, timeout_s: float, message: str) -> None:
        """Start or replace the active wall-clock budget."""
        with self._lock:
            self._deadline_mono = time.monotonic() + max(0.0, float(timeout_s))
            self._message = message
            self._fired_message = None
            self._paused_remaining = None

    def disarm(self) -> None:
        """Cancel the active budget without firing (e.g. after speech opens)."""
        with self._lock:
            self._deadline_mono = None
            self._message = None
            self._paused_remaining = None

    def pause(self) -> None:
        """Freeze remaining budget (TTS mute hold; B084 clock freeze)."""
        with self._lock:
            if self._deadline_mono is None or self._fired_message is not None:
                return
            if self._paused_remaining is not None:
                return
            self._paused_remaining = max(0.0, self._deadline_mono - time.monotonic())
            self._deadline_mono = None

    def resume(self) -> None:
        """Resume a budget previously frozen by :meth:`pause`."""
        with self._lock:
            if self._paused_remaining is None or self._fired_message is not None:
                return
            self._deadline_mono = time.monotonic() + self._paused_remaining
            self._paused_remaining = None

    def close(self) -> None:
        """Stop the watchdog thread; safe to call more than once."""
        self._stop.set()
        thread = self._thread
        if thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def fired_message(self) -> str | None:
        with self._lock:
            return self._fired_message

    def check(self) -> None:
        """Raise ``TimeoutError`` if the wall-clock budget already fired."""
        message = self.fired_message()
        if message is not None:
            raise TimeoutError(message)

    def map_error(self, exc: BaseException) -> BaseException:
        """Prefer deadline timeout over native abort noise after fire."""
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return exc
        message = self.fired_message()
        if message is not None:
            return TimeoutError(message)
        return exc

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                deadline = self._deadline_mono
                message = self._message
                already = self._fired_message is not None
                paused = self._paused_remaining is not None
            if already or deadline is None or paused:
                if self._stop.wait(_CAPTURE_DEADLINE_POLL_S):
                    return
                continue
            remaining = deadline - time.monotonic()
            if remaining > 0:
                if self._stop.wait(min(remaining, _CAPTURE_DEADLINE_POLL_S)):
                    return
                continue
            with self._lock:
                # A re-arm/pause between the wait and this claim cancels the fire.
                if (
                    self._deadline_mono != deadline
                    or self._fired_message is not None
                    or self._paused_remaining is not None
                ):
                    continue
                self._fired_message = message or "capture deadline exceeded"
                self._deadline_mono = None
            try:
                # Abort only — do not sticky-cancel the ask signal scope, or
                # TimeoutError would be rewritten into KeyboardInterrupt.
                _request_stream_cancel(self._stream)
            except BaseException:
                pass


def _read_input_block(
    stream: Any,
    block_size: int,
    deadline: _CaptureReadDeadline | None,
) -> tuple[Any, Any]:
    """Read one capture frame, surfacing wall-clock gate/discard timeouts."""
    if deadline is not None:
        deadline.check()
    try:
        data, overflowed = stream.read(block_size)
    except BaseException as exc:
        if deadline is not None:
            mapped = deadline.map_error(exc)
            if mapped is not exc:
                raise mapped from exc
        raise
    if deadline is not None:
        deadline.check()
    return data, overflowed


def _proc_parent_and_start(pid: int) -> tuple[int, str]:
    """Return Linux procfs parent PID and start ticks for one process."""
    with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
        raw = handle.read()
    fields = raw[raw.rfind(")") + 2 :].split()
    try:
        return int(fields[1]), fields[19]
    except (IndexError, ValueError) as exc:
        raise OSError(f"malformed /proc/{pid}/stat") from exc


def _ancestor_identities() -> tuple[tuple[int, str | None], ...]:
    """Snapshot the launch chain so surviving shell wrappers cannot mask exit."""
    identities: list[tuple[int, str | None]] = []
    seen: set[int] = set()
    pid = os.getppid()
    while pid > 1 and pid not in seen and len(identities) < 64:
        seen.add(pid)
        try:
            parent, start = _proc_parent_and_start(pid)
        except OSError:
            identities.append((pid, None))
            break
        identities.append((pid, start))
        pid = parent
    return tuple(identities)


def _ancestor_still_alive(identity: tuple[int, str | None]) -> bool:
    pid, expected_start = identity
    try:
        _parent, current_start = _proc_parent_and_start(pid)
    except (FileNotFoundError, ProcessLookupError):
        return False
    except OSError:
        # Transient procfs failures are not evidence that the orchestrator died.
        return True
    return expected_start is None or current_start == expected_start


class _ParentLifetimeGuard:
    """Wake a one-shot ask when any original launcher ancestor disappears."""

    def __init__(self, state: _CaptureSignalState) -> None:
        self._state = state
        self._ancestors = _ancestor_identities()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self._ancestors:
            return
        thread: threading.Thread | None = None
        try:
            # Thread.start waits on an internal Event/Condition. A pending
            # process signal must not raise until that lock state is restored.
            with cancellation_cleanup(self._state):
                thread = _ParentWatchThread(
                    target=self._watch,
                    name="hark-ask-parent-watch",
                    daemon=True,
                )
                self._thread = thread
                thread.start()
        except BaseException:
            launched = False
            if thread is not None:
                try:
                    launched = thread.ident is not None or thread.is_alive()
                except BaseException:
                    launched = True
            if not launched:
                self._thread = None
            raise

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, _PARENT_WATCH_POLL_S * 3))

    def _watch(self) -> None:
        parent_gone = False
        pidfds: list[int] = []
        fallback: list[tuple[int, str | None]] = []
        try:
            pidfd_open = getattr(os, "pidfd_open", None)
            for identity in self._ancestors:
                if not callable(pidfd_open):
                    fallback.append(identity)
                    continue
                try:
                    pidfd = pidfd_open(identity[0], 0)
                    pidfds.append(pidfd)
                    if identity[1] is not None:
                        try:
                            _parent, opened_start = _proc_parent_and_start(identity[0])
                        except OSError:
                            parent_gone = True
                            break
                        if opened_start != identity[1]:
                            parent_gone = True
                            break
                except (FileNotFoundError, ProcessLookupError):
                    parent_gone = True
                    break
                except OSError:
                    fallback.append(identity)
            while not parent_gone and not self._stop.is_set():
                if pidfds:
                    readable, _writable, _errors = select.select(
                        pidfds, [], [], _PARENT_WATCH_POLL_S
                    )
                    parent_gone = bool(readable)
                else:
                    self._stop.wait(_PARENT_WATCH_POLL_S)
                if not parent_gone and fallback:
                    parent_gone = any(
                        not _ancestor_still_alive(identity)
                        for identity in fallback
                    )
        finally:
            for pidfd in pidfds:
                try:
                    os.close(pidfd)
                except OSError:
                    pass

        if not parent_gone or self._stop.is_set():
            return
        # The orchestrator supplied no signal. Stage a reason-bearing internal
        # wake so a main thread blocked in Pa_ReadStream exits promptly even if
        # PortAudio abort/close itself never returns.
        if not self._state.prepare_wake_signal(_PARENT_EXIT_REASON):
            return
        # Start native abort even when SIGTERM delivery is unavailable or
        # blocked. The signal is an additional bounded wake for a Pa_ReadStream
        # whose abort/close path itself never returns.
        cancel_active_capture(reason=_PARENT_EXIT_REASON)
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except OSError:
            pass


def _abort_input_stream(stream: Any) -> bool:
    try:
        stream.abort()
    except BaseException:
        return False
    return True


def _close_input_stream(stream: Any) -> bool:
    try:
        stream.close()
    except BaseException:
        return False
    return True


def _stop_input_stream(stream: Any) -> bool:
    stop = getattr(stream, "stop", None)
    if not callable(stop):
        return False
    try:
        stop()
    except BaseException:
        return False
    return True


def _perform_stream_cleanup(owner: _StreamCleanupOwner) -> None:
    """Choose normal versus cancel teardown at the last safe pre-native point."""
    with _stream_cancel_lock:
        cancel_now = owner.cancel_requested
        fallback_now = owner.fallback_exit
    if cancel_now:
        _abort_input_stream(owner.stream)
        closed = _close_input_stream(owner.stream)
        # Legacy context-manager streams expose neither stop nor close; still
        # run __exit__ so leases and test doubles release deterministically
        # after a deadline/interrupt abort (B145).
        if not closed and fallback_now is not None:
            try:
                fallback_now()
            except BaseException:
                pass
        return
    stopped = _stop_input_stream(owner.stream)
    closed = _close_input_stream(owner.stream)
    if not stopped and not closed and fallback_now is not None:
        try:
            fallback_now()
        except BaseException:
            pass


def _reserve_stream_cleanup(
    stream: Any,
    *,
    cancel: bool,
    fallback_exit: Callable[[], Any] | None = None,
) -> _StreamCleanupOwner:
    """Publish or upgrade the exact stream's sole native cleanup owner."""
    key = id(stream)
    owner = _StreamCleanupOwner(
        stream=stream,
        entered=threading.Event(),
        cancel_requested=cancel,
        fallback_exit=fallback_exit,
    )

    def _cleanup() -> None:
        owner.entered.set()
        with _stream_cancel_lock:
            # If start publication looked pre-launch and was reconciled away,
            # reclaim only an empty slot. A repeated cancel may already own it.
            current = _stream_cancel_workers.setdefault(key, owner)
            if current is not owner:
                return
        try:
            _perform_stream_cleanup(owner)
        finally:
            with _stream_cancel_lock:
                current = _stream_cancel_workers.get(key)
                if current is owner:
                    _stream_cancel_workers.pop(key, None)

    owner.worker = threading.Thread(
        target=_cleanup,
        name="hark-capture-cancel",
        daemon=True,
    )
    with _stream_cancel_lock:
        existing = _stream_cancel_workers.setdefault(key, owner)
        existing.cancel_requested = existing.cancel_requested or cancel
        if existing.fallback_exit is None:
            existing.fallback_exit = fallback_exit
        return existing


def _start_stream_cleanup(owner: _StreamCleanupOwner) -> threading.Thread:
    """Claim and start an owner without holding cancellation registry locks."""
    key = id(owner.stream)
    previous_mask: set[signal.Signals] | None = None
    mask_signals = (
        threading.current_thread() is threading.main_thread()
        and hasattr(signal, "pthread_sigmask")
    )

    worker = owner.worker
    assert worker is not None
    try:
        if mask_signals:
            # Query the old mask without changing it first. If Python raises a
            # pending signal after the subsequent SIG_BLOCK C call, the outer
            # finally already knows exactly which mask to restore.
            previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
            signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGINT, signal.SIGTERM},
            )
        try:
            # Cover both the start-claim publication and Thread.start's internal
            # Event/Condition handshake. Main-thread pthread_sigmask alone is
            # insufficient when another thread receives a process signal and
            # Python later runs the pending handler here.
            with cancellation_cleanup():
                with _stream_cancel_lock:
                    if owner.start_claimed:
                        return worker
                    owner.start_claimed = True
                worker.start()
        except BaseException:
            # Reconcile definite pre-launch failures, while retaining an owner
            # whose target entered or whose underlying thread became observable.
            launched = owner.entered.is_set()
            if not launched:
                try:
                    launched = worker.ident is not None or worker.is_alive()
                except BaseException:
                    launched = True
            if not launched:
                with _stream_cancel_lock:
                    current = _stream_cancel_workers.get(key)
                    if current is owner:
                        _stream_cancel_workers.pop(key, None)
            raise
        return worker
    finally:
        if previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def _request_stream_cleanup(
    stream: Any,
    *,
    cancel: bool,
    fallback_exit: Callable[[], Any] | None = None,
) -> threading.Thread:
    owner = _reserve_stream_cleanup(
        stream,
        cancel=cancel,
        fallback_exit=fallback_exit,
    )
    return _start_stream_cleanup(owner)


def _request_stream_cancel(stream: Any) -> bool:
    """Schedule best-effort native abort without blocking the signal path."""
    try:
        _request_stream_cleanup(stream, cancel=True)
    except BaseException:
        # The signal handler still raises the original interruption.  A
        # post-launch interruption remains represented by the published owner;
        # a definite pre-launch failure was reconciled above.
        with _stream_cancel_lock:
            return id(stream) in _stream_cancel_workers
    return True


def _wait_stream_cleanup(worker: threading.Thread) -> None:
    """Give normal native teardown a bounded wait; daemon ownership stays truthful."""
    deadline = time.monotonic() + _STREAM_CLEANUP_JOIN_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            worker.join(timeout=remaining)
        except RuntimeError:
            # Post-launch publication can briefly precede Thread._started.
            time.sleep(min(0.001, remaining))
            continue
        if not worker.is_alive():
            return


def _current_capture_state() -> _CaptureSignalState | None:
    bound = getattr(_capture_thread_state, "state", None)
    if bound is not None:
        return bound
    with _capture_state_lock:
        return _capture_signal_state


def capture_in_progress() -> bool:
    """Whether the installed ask signal scope owns capture work."""
    state = _current_capture_state()
    if state is not None and state.has_attempt():
        return True
    with _active_stream_lock:
        if _active_input_stream is not None:
            return True
    with _stream_cancel_lock:
        return bool(_stream_cancel_workers)


def request_capture_cancel(
    signum: int | None = None,
    *,
    reason: str | None = None,
) -> None:
    """Make cancellation sticky for current and not-yet-registered capture."""
    state = _current_capture_state()
    if state is not None:
        state.request(signum, reason=reason)


def _lease_state(
    owner: _CaptureSignalState | _CaptureAttemptLease | None,
) -> _CaptureSignalState | None:
    return owner.state if isinstance(owner, _CaptureAttemptLease) else owner


def raise_if_capture_cancelled(
    state: _CaptureSignalState | _CaptureAttemptLease | None = None,
) -> None:
    """Raise cancellation from an explicit attempt or the current scope."""
    owner = _lease_state(state) if state is not None else _current_capture_state()
    interruption = owner.take_interruption() if owner is not None else None
    if interruption is not None:
        raise interruption


def register_capture_attempt() -> _CaptureAttemptLease | None:
    """Register capture ownership and return its idempotent lease token."""
    state = _current_capture_state()
    if state is None:
        return None
    return _CaptureAttemptLease(state=state, token=state.begin_attempt())


def release_capture_attempt(lease: _CaptureAttemptLease | None) -> None:
    """Idempotently release a lease returned by ``register_capture_attempt``."""
    if lease is not None:
        lease.release()


@contextmanager
def capture_attempt() -> Iterator[None]:
    """Register capture work early enough for pre-stream cancellation."""
    state = register_capture_attempt()
    try:
        raise_if_capture_cancelled(state)
        yield
    finally:
        release_capture_attempt(state)


@contextmanager
def bind_capture_state(
    state: _CaptureSignalState | _CaptureAttemptLease | None,
) -> Iterator[None]:
    """Keep an explicit turn token discoverable throughout a worker stack."""
    previous = getattr(_capture_thread_state, "state", None)
    bound = _lease_state(state)
    if bound is not None:
        _capture_thread_state.state = bound
    try:
        yield
    finally:
        if previous is None:
            try:
                del _capture_thread_state.state
            except AttributeError:
                pass
        else:
            _capture_thread_state.state = previous


@contextmanager
def cancellation_cleanup(
    state: _CaptureSignalState | None = None,
    *,
    primary: BaseException | None = None,
) -> Iterator[None]:
    """Finish cleanup before surfacing a signal delivered during cleanup."""
    owner = state if state is not None else _current_capture_state()
    if owner is None:
        yield
        return
    owner.begin_cleanup()
    try:
        try:
            yield
        except BaseException:
            if primary is None:
                raise
    finally:
        owner.finish_cleanup()
    if (
        primary is None
        and owner.interruption_deferred()
        and not owner.interruption_surfaced()
    ):
        raise_if_capture_cancelled(owner)


def cancel_active_capture(
    signum: int | None = None,
    *,
    reason: str | None = None,
) -> bool:
    """Detach and asynchronously abort this process's active input stream.

    No PID lookup or process signalling is performed: this is deliberately
    scoped to the PortAudio stream registered by this Python process. Native
    ``abort``/``close`` may themselves hang, so neither the signal handler nor
    bounded parent cleanup calls them synchronously.
    """
    global _active_input_stream
    request_capture_cancel(signum, reason=reason)
    owner: _StreamCleanupOwner | None = None
    with _active_stream_lock:
        stream = _active_input_stream
        if stream is not None:
            owner = _reserve_stream_cleanup(stream, cancel=True)
            _active_input_stream = None
    if stream is None:
        with _stream_cancel_lock:
            owners = list(_stream_cancel_workers.values())
            for pending in owners:
                pending.cancel_requested = True
        found = bool(owners)
        for pending in owners:
            try:
                _start_stream_cleanup(pending)
            except BaseException:
                # Continue reinforcing every published owner; the primary
                # signal remains authoritative at the handler boundary.
                pass
        return found
    try:
        assert owner is not None
        _start_stream_cleanup(owner)
        return True
    except BaseException:
        # The handler must still raise the original signal promptly even if
        # cancellation worker startup itself fails or is interrupted.
        with _stream_cancel_lock:
            return id(stream) in _stream_cancel_workers


@contextmanager
def _registered_input_stream(stream: Any) -> Iterator[None]:
    global _active_input_stream
    state = _current_capture_state()
    with _active_stream_lock:
        previous = _active_input_stream
        _active_input_stream = stream
    primary: BaseException | None = None
    try:
        raise_if_capture_cancelled(state)
        yield
    except BaseException as exc:
        primary = exc
        # PortAudio's blocking Pa_ReadStream may not make progress on its own.
        # Its tracked daemon owns abort/close; the interrupted parent must not
        # enter InputStream.__exit__ or any native teardown synchronously.
        owner: _StreamCleanupOwner | None = None
        with _active_stream_lock:
            if _active_input_stream is stream:
                owner = _reserve_stream_cleanup(stream, cancel=True)
                _active_input_stream = previous
        if owner is not None:
            try:
                _start_stream_cleanup(owner)
            except KeyboardInterrupt:
                # A first signal delivered during the ownership handoff is the
                # command-level outcome, not cleanup noise from the backend.
                raise
            except BaseException:
                # Preserve the capture/provider exception; the owner registry
                # already reflects post-launch uncertainty or was reconciled.
                pass
        interruption = state.interruption() if state is not None else None
        if interruption is not None and not isinstance(exc, KeyboardInterrupt):
            primary = interruption
            raise interruption from exc
        raise
    finally:
        with cancellation_cleanup(state, primary=primary):
            with _active_stream_lock:
                if _active_input_stream is stream:
                    _active_input_stream = previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    """Restore every handler even if one restoration is interrupted."""
    primary: BaseException | None = None
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except BaseException as exc:
            if primary is None:
                primary = exc
    if primary is not None:
        raise primary


@contextmanager
def capture_interrupt_signals() -> Iterator[None]:
    """Scope SIGINT/SIGTERM to graceful cancellation of one-shot capture."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    global _capture_signal_state
    with _capture_state_lock:
        inherited_state = _capture_signal_state
    if inherited_state is not None:
        # run_ask owns a scope for direct callers; cmd_ask supplies the outer
        # command scope.  Reuse it so nested ownership never clobbers state.
        yield
        return

    watched = (signal.SIGINT, signal.SIGTERM)
    previous = {signum: signal.getsignal(signum) for signum in watched}
    state = _CaptureSignalState()
    previous_state: _CaptureSignalState | None = None

    def _interrupt(signum: int, _frame: object) -> None:
        wake_reason = state.consume_wake_signal(signum)
        if wake_reason is not None:
            cancel_active_capture(reason=wake_reason)
            if state.cleaning_up():
                state.defer_interruption()
                return
            interruption = state.take_interruption()
            assert interruption is not None
            raise interruption
        first = state.request(signum)
        cancel_active_capture(signum)
        if state.cleaning_up():
            # Finish teardown, then surface only a first cancellation that the
            # signal handler could not safely raise in-place. A repeat merely
            # reinforces an already-published programmatic cancellation.
            if first:
                state.defer_interruption()
            return
        if not first:
            # Repeated signals reinforce stream abort but never replace cause.
            return
        interruption = state.take_interruption()
        assert interruption is not None
        raise interruption

    primary: BaseException | None = None
    parent_guard: _ParentLifetimeGuard | None = None
    cleanup_errors: list[BaseException] = []
    try:
        # Publication and ancestry construction can themselves be interrupted;
        # keep both inside the ownership-restoring try/finally.
        with _capture_state_lock:
            previous_state = _capture_signal_state
            _capture_signal_state = state
        parent_guard = _ParentLifetimeGuard(state)
        for signum in watched:
            signal.signal(signum, _interrupt)
        parent_guard.start()
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        with cancellation_cleanup(state, primary=primary):
            if parent_guard is not None:
                try:
                    parent_guard.stop()
                except BaseException as exc:
                    cleanup_errors.append(exc)
            try:
                with _capture_state_lock:
                    if _capture_signal_state is state:
                        _capture_signal_state = previous_state
            except BaseException as exc:
                cleanup_errors.append(exc)
            try:
                _restore_signal_handlers(previous)
            except BaseException as exc:
                cleanup_errors.append(exc)
        # Cleanup failures never replace the primary interruption, but with no
        # primary they remain visible after every restoration step was tried.
        if primary is None and cleanup_errors:
            raise cleanup_errors[0]


class MicLease:
    """Process- and system-wide single mic lease."""

    _lock = threading.Lock()
    _holder: str | None = None

    def __init__(self, name: str = "hark") -> None:
        self.name = name
        self._held = False
        self._lock_fd: int | None = None

    def __enter__(self) -> MicLease:
        fd: int | None = None
        try:
            with MicLease._lock:
                if MicLease._holder is not None:
                    raise MicBusyError(f"mic busy ({MicLease._holder})")
                lock_path = state_dir() / "mic.lock"
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                self._lock_fd = fd
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                MicLease._holder = self.name
                self._held = True
            return self
        except BaseException as exc:
            if fd is not None:
                with cancellation_cleanup(primary=exc):
                    with MicLease._lock:
                        owns_fd = self._lock_fd == fd
                        if owns_fd:
                            self._held = False
                            self._lock_fd = None
                            if MicLease._holder == self.name:
                                MicLease._holder = None
                    if owns_fd:
                        self._release_fd(fd, primary=exc)
            if isinstance(exc, BlockingIOError):
                raise MicBusyError("mic busy (held by another Hark process)") from None
            raise

    def __exit__(self, *args: object) -> None:
        primary = (
            args[1] if len(args) > 1 and isinstance(args[1], BaseException) else None
        )
        with cancellation_cleanup(primary=primary):
            with MicLease._lock:
                fd = self._lock_fd
                self._lock_fd = None
                if self._held and MicLease._holder == self.name:
                    MicLease._holder = None
                self._held = False
            if fd is not None:
                self._release_fd(fd, primary=primary)

    @staticmethod
    def _release_fd(fd: int, *, primary: BaseException | None) -> None:
        """Release a detached fd; cleanup never replaces an existing error."""
        cleanup_error: BaseException | None = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BaseException as exc:
            cleanup_error = exc
        try:
            os.close(fd)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        if primary is None and cleanup_error is not None:
            raise cleanup_error


def _require_sd() -> None:
    if sd is None:
        raise RuntimeError(
            "sounddevice not installed — run: uv sync  (needs PortAudio)"
        )


def pcm16_mono_bytes(samples: np.ndarray) -> bytes:
    samples = np.clip(samples, -1.0, 1.0)
    ints = (samples * 32767.0).astype(np.int16)
    return ints.tobytes()


def write_wav_bytes(pcm16: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    return buf.getvalue()


def pad_pcm16_silence(
    pcm16: bytes,
    *,
    pad_ms: int = 0,
    sample_rate: int = 16000,
    lead_ms: int | None = None,
    trail_ms: int | None = None,
) -> bytes:
    """Expand mono PCM16 bounds with zero samples (silence).

    Mid-buffer samples are preserved byte-identical. Used for radio segment
    STT so edge phonemes are not hard-cut at the energy-gate boundary (B075).
    Silence-only pad does not invent speech for STT.
    """
    lead = int(pad_ms if lead_ms is None else lead_ms)
    trail = int(pad_ms if trail_ms is None else trail_ms)
    if lead <= 0 and trail <= 0:
        return pcm16
    n_lead = max(0, int(sample_rate * lead / 1000.0))
    n_trail = max(0, int(sample_rate * trail / 1000.0))
    # 16-bit mono: 2 bytes per sample
    return (b"\x00\x00" * n_lead) + pcm16 + (b"\x00\x00" * n_trail)


def radio_stt_window_pcm(
    seg_pcm: bytes,
    overlap_tail: bytes,
    *,
    overlap_ms: int,
    sample_rate: int = 16000,
) -> tuple[bytes, bytes]:
    """Build an STT window with real PCM lookback (B085) and next-segment tail.

    Returns ``(stt_pcm, new_overlap_tail)``. Prefers real previous-segment PCM
    over synthetic silence; empty ``overlap_tail`` yields the segment alone.
    """
    ov_ms = max(0, int(overlap_ms))
    if ov_ms <= 0:
        return seg_pcm, b""
    stt_pcm = (overlap_tail + seg_pcm) if overlap_tail else seg_pcm
    ov_bytes = int(sample_rate * (ov_ms / 1000.0)) * 2
    if ov_bytes > 0 and len(seg_pcm) >= ov_bytes:
        new_tail = seg_pcm[-ov_bytes:]
    elif seg_pcm:
        new_tail = seg_pcm
    else:
        new_tail = b""
    return stt_pcm, new_tail


def effective_radio_segment_pad_ms(
    pad_ms: int | float,
    radio_partial_silence_s: float,
    *,
    absolute_max_ms: int = 300,
) -> int:
    """Clamp radio boundary pad so it stays well under inter-segment quiet.

    Budget: ``min(absolute_max_ms, radio_partial_silence_s * 1000 * 0.4)``.
    Pad ≪ segment silence → STT sees hush, not prior speech / phantom words.
    """
    raw = int(pad_ms)
    if raw <= 0:
        return 0
    silence_budget = max(0, int(float(radio_partial_silence_s) * 1000.0 * 0.4))
    return max(0, min(raw, int(absolute_max_ms), silence_budget))


def record_seconds(
    seconds: float,
    *,
    sample_rate: int = 16000,
    device: int | str | None = None,
) -> bytes:
    """Record fixed duration mono float→PCM16."""
    _require_sd()
    frames = int(seconds * sample_rate)
    audio = sd.rec(
        frames,
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=device,
    )
    sd.wait()
    return pcm16_mono_bytes(audio.reshape(-1))


class PcmRingBuffer:
    """Fixed-capacity mono PCM16 ring (sample-interleaved int16).

    Used by continuous ambient capture so wake scoring and pre-roll read
    from a sliding window without reopening the device.
    """

    BYTES_PER_SAMPLE = 2

    def __init__(self, capacity_s: float, sample_rate: int = 16000) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        capacity_s = max(0.05, float(capacity_s))
        self.sample_rate = int(sample_rate)
        self.capacity = max(1, int(capacity_s * self.sample_rate))
        self._buf = np.zeros(self.capacity, dtype=np.int16)
        self._write = 0  # next write index
        self._available = 0  # samples currently held (≤ capacity)

    @property
    def available_samples(self) -> int:
        return self._available

    @property
    def available_s(self) -> float:
        return self._available / float(self.sample_rate)

    @property
    def capacity_s(self) -> float:
        return self.capacity / float(self.sample_rate)

    def clear(self) -> None:
        self._write = 0
        self._available = 0
        self._buf.fill(0)

    def write_samples(self, samples: np.ndarray) -> None:
        """Append int16 mono samples (overwrites oldest when full)."""
        if samples.size == 0:
            return
        flat = np.ascontiguousarray(samples.reshape(-1), dtype=np.int16)
        n = int(flat.shape[0])
        if n >= self.capacity:
            # Keep only the newest capacity samples
            self._buf[:] = flat[-self.capacity :]
            self._write = 0
            self._available = self.capacity
            return
        end = self._write + n
        if end <= self.capacity:
            self._buf[self._write : end] = flat
        else:
            first = self.capacity - self._write
            self._buf[self._write :] = flat[:first]
            self._buf[: n - first] = flat[first:]
        self._write = (self._write + n) % self.capacity
        self._available = min(self.capacity, self._available + n)

    def write_pcm16(self, data: bytes) -> None:
        if not data:
            return
        self.write_samples(np.frombuffer(data, dtype=np.int16))

    def write_float32(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        clipped = np.clip(samples.reshape(-1), -1.0, 1.0)
        self.write_samples((clipped * 32767.0).astype(np.int16))

    def tail_samples(self, n: int) -> np.ndarray:
        """Return the last *n* samples in chronological order (oldest→newest)."""
        if n <= 0 or self._available <= 0:
            return np.zeros(0, dtype=np.int16)
        n = min(int(n), self._available)
        end = self._write  # next write = one past newest
        # Full capacity span: oldest sits at write index
        if n == self.capacity:
            return np.concatenate((self._buf[end:], self._buf[:end]))
        start = (end - n) % self.capacity
        if start < end:
            return self._buf[start:end].copy()
        # Wrapped: [start:] + [:end]
        return np.concatenate((self._buf[start:], self._buf[:end]))

    def tail(self, duration_s: float) -> bytes:
        """Last ``duration_s`` of audio as PCM16 bytes."""
        n = int(max(0.0, float(duration_s)) * self.sample_rate)
        return self.tail_samples(n).tobytes()

    def tail_ms(self, ms: int) -> bytes:
        return self.tail(max(0, int(ms)) / 1000.0)

    def window(self, duration_s: float, *, end_offset_s: float = 0.0) -> bytes:
        """Score window of ``duration_s`` ending ``end_offset_s`` before the tip.

        ``end_offset_s=0`` is the newest audio (same as :meth:`tail`).
        """
        end_off = max(0, int(float(end_offset_s) * self.sample_rate))
        n = max(0, int(float(duration_s) * self.sample_rate))
        if n <= 0 or self._available <= 0:
            return b""
        # Drop end_off newest samples, then take n before that
        total_from_tip = end_off + n
        if total_from_tip > self._available:
            # Not enough history: take what we can before end_off
            avail_before = max(0, self._available - end_off)
            n = min(n, avail_before)
            if n <= 0:
                return b""
        samples = self.tail_samples(end_off + n)
        if end_off > 0:
            samples = samples[: len(samples) - end_off]
        return samples.tobytes()


def clamp_pre_roll_ms(ms: int | float | None, *, default: int = 300) -> int:
    """Clamp pre-roll to the B079 target range (250–500 ms)."""
    if ms is None:
        return default
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return default
    return max(250, min(500, v))


def score_window_plan(
    snippet_s: float,
    hop_s: float | None = None,
    *,
    min_snippet_s: float = 0.8,
    max_snippet_s: float = 2.5,
    default_hop_ratio: float = 0.3,
) -> tuple[float, float]:
    """Normalize wake window + hop so hop is always strictly less than snippet.

    Default hop ≈ 30% of snippet (e.g. 2.5 s → 0.75 s) for overlapping cuts so
    a greeting+name rarely straddles non-overlapping boundaries.
    """
    snippet = max(min_snippet_s, min(float(snippet_s), max_snippet_s))
    if hop_s is None:
        hop = snippet * default_hop_ratio
    else:
        hop = float(hop_s)
    # Keep hop in (0, snippet): at least 100 ms, at most 75% of snippet
    hop = max(0.1, min(hop, snippet * 0.75))
    if hop >= snippet:
        hop = max(0.1, snippet * 0.5)
    return snippet, hop


class ContinuousMicStream:
    """Hold MicLease + InputStream open; fill a :class:`PcmRingBuffer`.

    Ambient wake keeps one of these for the whole arm (or until pause/yield).
    Score overlapping windows via :meth:`window_pcm16` without open/close thrash.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        ring_s: float = 5.0,
        device: int | str | None = None,
        lease_name: str = "ambient",
        block_ms: float = 20.0,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.ring = PcmRingBuffer(ring_s, self.sample_rate)
        self.device = device
        self.lease_name = lease_name
        self._block = max(1, int(self.sample_rate * (block_ms / 1000.0)))
        self._lease: MicLease | None = None
        self._stream: Any = None
        self._open = False

    @property
    def available_s(self) -> float:
        return self.ring.available_s

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> ContinuousMicStream:
        if self._open:
            return self
        _require_sd()
        lease = MicLease(self.lease_name)
        lease.__enter__()
        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self._block,
                device=self.device,
            )
            stream.start()
        except Exception:
            lease.__exit__(None, None, None)
            raise
        self._lease = lease
        self._stream = stream
        self._open = True
        return self

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        lease = self._lease
        self._lease = None
        self._open = False
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if lease is not None:
            try:
                lease.__exit__(None, None, None)
            except Exception:
                pass

    def __enter__(self) -> ContinuousMicStream:
        return self.open()

    def __exit__(self, *args: object) -> None:
        self.close()

    def read_for(
        self,
        duration_s: float,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> bool:
        """Block while reading ``duration_s`` of audio into the ring.

        Returns False if ``should_stop`` became true before the full duration
        (caller should check pause/shutdown). Raises if the stream is closed.
        """
        if not self._open or self._stream is None:
            raise RuntimeError("ContinuousMicStream is not open")
        duration_s = max(0.0, float(duration_s))
        if duration_s <= 0:
            return True
        deadline = time.monotonic() + duration_s
        # Ambient spectrum: non-recording feed so the webui can stay live (B087)
        last_spec = 0.0
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                return False
            data, overflowed = self._stream.read(self._block)
            del overflowed
            samples = data.reshape(-1)
            self.ring.write_float32(samples)
            now = time.monotonic()
            if now - last_spec >= 0.032:  # ~30 fps ambient (cheaper than listen)
                last_spec = now
                try:
                    from hark.audio.spectrum import publish_spectrum

                    publish_spectrum(
                        samples,
                        sample_rate=self.sample_rate,
                        recording=False,
                        source="ambient",
                    )
                except Exception:
                    pass
        return True

    def window_pcm16(self, duration_s: float, *, end_offset_s: float = 0.0) -> bytes:
        return self.ring.window(duration_s, end_offset_s=end_offset_s)

    def tail_ms(self, ms: int) -> bytes:
        return self.ring.tail_ms(ms)

    def tail(self, duration_s: float) -> bytes:
        return self.ring.tail(duration_s)


@dataclass
class CaptureResult:
    pcm16: bytes
    sample_rate: int
    duration_ms: int
    speech_ms: int
    # Time spent waiting for speech open (leading silence not in pcm16)
    wait_speech_ms: int = 0
    # Peak energy while capture was armed (helps diagnose residual TTS / mute races)
    peak_rms: float = 0.0
    peak_db: float = -120.0

    @property
    def wav(self) -> bytes:
        return write_wav_bytes(self.pcm16, self.sample_rate)


def _still_discarding(
    *,
    open_mono: float,
    discard_leading_ms: int,
    audio_ok_after: Callable[[], float | None] | None,
) -> bool:
    """True while leading audio should be dropped (overlap pre-arm echo guard).

    ``audio_ok_after`` returns a monotonic deadline (or None while TTS is still
    playing). Fixed ``discard_leading_ms`` applies from stream open.
    """
    now = time.monotonic()
    if audio_ok_after is not None:
        ok_at = audio_ok_after()
        if ok_at is None or now < ok_at:
            return True
    if discard_leading_ms > 0 and now < open_mono + discard_leading_ms / 1000.0:
        return True
    return False


@capture_attempt()
def capture_utterance(
    *,
    sample_rate: int = 16000,
    max_s: float = 120.0,
    end_silence_s: float = 2.1,
    min_speech_s: float = 0.25,
    open_margin_db: float = 8.0,
    # Absolute floor: speech louder than this opens even if relative margin fails
    abs_open_db: float = -48.0,
    open_confirm_blocks: int = 4,  # ~80 ms
    # Keep this much audio immediately before speech open (trims long leading silence).
    # B079: default ≥250 ms so word onsets are not clipped when the gate lags.
    # Values outside 250–500 are clamped (except 0 which disables pre-roll).
    preroll_ms: int = 300,
    # B084: after TTS mute releases, discard this many ms and freeze silence clocks
    mute_edge_pad_ms: int = 300,
    initial_timeout_s: float = 45.0,
    device: int | str | None = None,
    should_stop: Callable[[bytes, float], bool] | None = None,
    on_opened: Callable[[], None] | None = None,
    # B105: called (throttled ≈10 Hz) while speech energy is present so TTS can
    # measure operator quiet for streaming acks. Optional; ignore if None.
    on_voice: Callable[[], None] | None = None,
    post_tts_guard_s: float = 0.0,
    # Drop leading frames (fixed window from open and/or until audio_ok_after)
    discard_leading_ms: int = 0,
    audio_ok_after: Callable[[], float | None] | None = None,
    # Pluggable endpointing (B007). None strategy == legacy fixed-silence gate.
    endpoint_strategy: EndpointStrategy | None = None,
    endpoint_probe_silence_s: float | None = None,
    endpoint_max_silence_s: float | None = None,
    on_endpoint_event: Callable[[str, dict], None] | None = None,
    # B108: hang hysteresis / relative-to-peak drop for high-gain robustness
    hang_margin_db: float = 4.0,
    speech_drop_db: float = 18.0,
    peak_gate_slack_db: float = 12.0,
) -> CaptureResult:
    """Energy-gated capture until turn end or should_stop or max.

    The turn-end decision is delegated to :class:`~hark.endpointing.SilenceEndpointer`.
    With ``endpoint_strategy=None`` this is exactly the legacy fixed-silence gate
    (end after ``end_silence_s`` of quiet once ``min_speech_s`` speech seen). A
    smarter strategy may end earlier (reducing long waits) or wait longer up to
    ``endpoint_max_silence_s`` (reducing mid-thought cutoffs).

    Leading silence / background noise is **not** kept: the gate waits until
    speech is confirmed, then starts the capture buffer with a short pre-roll
    only (so word onsets are not clipped). ``on_opened`` fires once when speech
    is confirmed — use it for the record-start cue / stream arming.

    should_stop(pcm_so_far, elapsed_s) → True to end (e.g. agent listen-end).

    Overlap pre-arm: open the stream early and discard frames while
    ``audio_ok_after()`` is None or before its deadline (TTS still ending /
    residual echo). Gate timeout clocks start only after discard completes.

    Hang decision (B108): classic hysteresis uses ``open_thresh - hang_margin_db``.
    When the utterance peak sits well above the open threshold (high input gain /
    loud close-talk), frames must also stay within ``speech_drop_db`` of that peak
    to count as continued speech — otherwise elevated room noise that never falls
    below a frozen low ``abs_open_db`` hang floor would keep the stream open forever.
    """
    _require_sd()
    if post_tts_guard_s > 0:
        time.sleep(post_tts_guard_s)

    block = int(sample_rate * 0.02)  # 20 ms
    noise_floor = 1e-4
    open_thresh = None
    opened = False
    speech_blocks = 0
    silent_blocks = 0
    end_silence_blocks = max(1, int(end_silence_s / 0.02))
    min_speech_blocks = max(1, int(min_speech_s / 0.02))
    endpointer = (
        SilenceEndpointer(
            end_silence_s=end_silence_s,
            min_speech_s=min_speech_s,
            strategy=endpoint_strategy,
            probe_silence_s=endpoint_probe_silence_s,
            max_silence_s=endpoint_max_silence_s,
            on_event=on_endpoint_event,
        )
        if endpoint_strategy is not None
        else None
    )
    max_blocks = int(max_s / 0.02)
    timeout_blocks = int(initial_timeout_s / 0.02)
    # 0 disables; otherwise clamp to B079 range so config mistakes stay safe
    effective_preroll = 0 if preroll_ms <= 0 else clamp_pre_roll_ms(preroll_ms)
    preroll_blocks = max(1, int(effective_preroll / 20.0)) if effective_preroll > 0 else 0
    peak_db = -120.0
    peak_rms = 0.0

    chunks: list[np.ndarray] = []
    # Short ring of recent frames while waiting for speech (discarded if timeout)
    preroll: deque[np.ndarray] = deque(maxlen=max(1, preroll_blocks))
    wait_speech_ms = 0
    # Safety cap for discard phase (TTS tail + residual + long mute)
    discard_max_s = max(30.0, initial_timeout_s)
    # Spectrum window: ~40 ms of recent blocks for FFT (B087 live webui)
    spec_blocks = max(1, int(0.04 / 0.02))
    spec_ring: deque[np.ndarray] = deque(maxlen=spec_blocks)
    # Throttle file writes a bit when local publisher is absent (still ~50 fps)
    last_spec_pub = 0.0
    spec_interval_s = 0.016

    def _publish_spec(samples_block: np.ndarray) -> None:
        nonlocal last_spec_pub
        now = time.monotonic()
        if now - last_spec_pub < spec_interval_s:
            return
        last_spec_pub = now
        try:
            from hark.audio.spectrum import publish_spectrum

            spec_ring.append(samples_block)
            window = (
                np.concatenate(list(spec_ring))
                if len(spec_ring) > 1
                else samples_block
            )
            publish_spectrum(
                window,
                sample_rate=sample_rate,
                recording=True,
                source="listen",
            )
        except Exception:
            pass

    try:
        stream_owner = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=block,
            device=device,
        )
        stream = stream_owner
        fallback_exit: Callable[[], Any] | None = None
        read_deadline: _CaptureReadDeadline | None = None
        try:
            start = getattr(stream_owner, "start", None)
            if callable(start):
                start()
            else:
                # Small test doubles and legacy backends may only expose the
                # context protocol.  Real sounddevice streams use start/stop.
                entered_stream = stream_owner.__enter__()
                if entered_stream is not None:
                    stream = entered_stream

                def _legacy_exit() -> Any:
                    return stream_owner.__exit__(None, None, None)

                fallback_exit = _legacy_exit
        except BaseException:
            _request_stream_cancel(stream_owner)
            raise

        read_deadline = _CaptureReadDeadline(stream)
        discard_timeout_msg = (
            "overlap discard window exceeded before audio became usable"
        )
        # Wall-clock fire message is fixed at arm time (peaks may still be
        # updating). Block-count timeout below includes live peak diagnostics.
        gate_deadline_msg = (
            f"no speech detected within initial_timeout_s={initial_timeout_s:g}"
        )
        # Set when the wall-clock watchdog (not an external signal) ends capture,
        # so legacy context-manager streams still run __exit__ (B145 lifecycle).
        deadline_forced_exit = False

        try:
            with _registered_input_stream(stream):
                open_mono = time.monotonic()
                # Phase 0: drop leading audio (overlap pre-arm / fixed discard window)
                if discard_leading_ms > 0 or audio_ok_after is not None:
                    read_deadline.arm(discard_max_s, discard_timeout_msg)
                    while _still_discarding(
                        open_mono=open_mono,
                        discard_leading_ms=discard_leading_ms,
                        audio_ok_after=audio_ok_after,
                    ):
                        if time.monotonic() - open_mono > discard_max_s:
                            raise TimeoutError(discard_timeout_msg)
                        data, overflowed = _read_input_block(
                            stream, block, read_deadline
                        )
                        del overflowed, data

                # Gate clock starts only after discard so TTS tail does not burn timeout
                start = time.monotonic()
                read_deadline.arm(initial_timeout_s, gate_deadline_msg)
                wait_blocks = 0  # only counts when not muted (B084)
                blocks_used = 0  # non-mute blocks against max_s (B084 freezes max too)
                mute_pad_blocks = 0
                was_tts_muted = False
                edge_pad_blocks = max(0, int(float(mute_edge_pad_ms) / 20.0))
                last_voice_cb = 0.0  # monotonic; throttle on_voice (B105)

                def _emit_voice() -> None:
                    nonlocal last_voice_cb
                    if on_voice is None:
                        return
                    t = time.monotonic()
                    if t - last_voice_cb < 0.1:
                        return
                    last_voice_cb = t
                    try:
                        on_voice()
                    except Exception:
                        pass

                def _tts_muted() -> bool:
                    try:
                        from hark.audio.mic_mute import tts_mute_depth

                        return tts_mute_depth() > 0
                    except Exception:
                        return False

                # While-loop so TTS mute / edge-pad do not burn max_s or initial_timeout
                speech_during_mute = False
                speech_during_mute_peak_db = -120.0

                def _block_energy(samples_block: np.ndarray) -> tuple[float, float]:
                    r = float(np.sqrt(np.mean(samples_block**2)) + 1e-12)
                    d = 20.0 * np.log10(r)
                    return r, d

                def _note_speech_during_hold(db_val: float, *, phase: str) -> None:
                    """Operator energy while TTS mute / edge-pad is held (B112).

                    OS-level mute often zeros PCM (undetectable). When residual path
                    still carries energy, log once per hold and force silence
                    progress reset so we do not finalize as if the operator stayed
                    quiet.
                    """
                    nonlocal speech_during_mute, speech_during_mute_peak_db
                    thresh = open_thresh if open_thresh is not None else abs_open_db
                    if db_val < thresh - 2.0:
                        return
                    first = not speech_during_mute
                    speech_during_mute = True
                    if db_val > speech_during_mute_peak_db:
                        speech_during_mute_peak_db = db_val
                    if not first:
                        return
                    try:
                        from hark.syslog import log as _syslog

                        _syslog(
                            "listen.speech_during_mute",
                            component="stt",
                            level="warn",
                            phase=phase,
                            peak_db=round(db_val, 1),
                            open_thresh=round(float(thresh), 1),
                            message=(
                                "operator energy while mic muted/padded for TTS — "
                                "half-duplex may drop speech; silence endpoint reset"
                            ),
                        )
                    except Exception:
                        pass

                def _gate_timeout_message() -> str:
                    thresh = (
                        float(open_thresh)
                        if open_thresh is not None
                        else float(abs_open_db)
                    )
                    return (
                        f"no speech detected within "
                        f"initial_timeout_s={initial_timeout_s:g} "
                        f"(peak_db={peak_db:.1f} peak_rms={peak_rms:.5f} "
                        f"open_thresh≈{thresh:.1f}dB — try speaking louder "
                        f"or set a different input device)"
                    )

                while blocks_used < max_blocks:
                    data, overflowed = _read_input_block(
                        stream, block, read_deadline
                    )
                    del overflowed
                    samples = data.reshape(-1)
                    _publish_spec(samples)

                    # B084 / B112: while Hark holds TTS mute, *freeze* open/silence/max
                    # clocks (do not advance). Do **not** unconditionally reset the
                    # silence counter — streaming TTS acks mid-listen used to wipe
                    # silence progress and delay ambient.prompt until max/agent end.
                    # Only reset when operator energy is observed during the hold.
                    muted_now = _tts_muted()
                    if muted_now:
                        was_tts_muted = True
                        # B084: freeze wall-clock gate budget while TTS mute holds.
                        read_deadline.pause()
                        _rms_m, db_m = _block_energy(samples)
                        if opened:
                            _note_speech_during_hold(db_m, phase="mute")
                        elif not opened and preroll_blocks > 0:
                            # Keep preroll warm if OS mute does not zero the stream
                            if db_m > (
                                open_thresh if open_thresh is not None else abs_open_db
                            ) - 6:
                                preroll.append(samples.copy())
                        continue
                    if was_tts_muted:
                        was_tts_muted = False
                        mute_pad_blocks = edge_pad_blocks
                        if opened and speech_during_mute:
                            silent_blocks = 0
                            if endpointer is not None:
                                endpointer.on_speech()
                    if mute_pad_blocks > 0:
                        # Edge pad is part of the mute hold freeze (B084).
                        read_deadline.pause()
                        mute_pad_blocks -= 1
                        _rms_p, db_p = _block_energy(samples)
                        if opened:
                            _note_speech_during_hold(db_p, phase="mute_edge_pad")
                            if speech_during_mute:
                                silent_blocks = 0
                        # Still seed preroll while waiting so post-pad open has history
                        if not opened and preroll_blocks > 0:
                            preroll.append(samples.copy())
                        continue
                    # Hold ended cleanly: keep prior silent_blocks (true freeze).
                    # speech_during_mute already forced a reset above when needed.
                    read_deadline.resume()
                    if speech_during_mute and opened:
                        # Fresh speech after hold — treat as active talk turn
                        silent_blocks = 0
                        speech_during_mute = False

                    blocks_used += 1
                    rms, db = _block_energy(samples)
                    if db > peak_db:
                        peak_db = db
                        peak_rms = rms

                    if not opened:
                        if preroll_blocks > 0:
                            preroll.append(samples.copy())
                        # adapt noise floor while closed (slow attack)
                        noise_floor = 0.98 * noise_floor + 0.02 * rms
                        rel_thresh = (
                            20.0 * np.log10(noise_floor + 1e-12) + open_margin_db
                        )
                        open_thresh = max(rel_thresh, abs_open_db)
                        if db >= open_thresh:
                            speech_blocks += 1
                            if speech_blocks >= open_confirm_blocks:
                                opened = True
                                silent_blocks = 0
                                # Gate satisfied — stop the no-speech wall clock.
                                # max_s is still enforced by blocks_used below.
                                read_deadline.disarm()
                                # Seed buffer with short pre-roll only (not full leading silence)
                                if preroll_blocks > 0:
                                    chunks.extend(preroll)
                                    preroll.clear()
                                wait_speech_ms = int(
                                    1000 * (time.monotonic() - start)
                                )
                                _emit_voice()
                                if on_opened is not None:
                                    try:
                                        on_opened()
                                    except Exception:
                                        pass
                        else:
                            speech_blocks = max(0, speech_blocks - 1)
                        wait_blocks += 1
                        if wait_blocks >= timeout_blocks and not opened:
                            raise TimeoutError(_gate_timeout_message())
                    else:
                        chunks.append(samples.copy())
                        # Classic hysteresis hang floor (open_thresh freezes at open).
                        hang_floor = (
                            float(open_thresh) - float(hang_margin_db)
                            if open_thresh is not None
                            else float("-inf")
                        )
                        # B108: when peak is far above open_thresh, also require a
                        # relative drop from peak — high mic gain can leave room noise
                        # forever above a low frozen abs_open hang floor.
                        if (
                            open_thresh is not None
                            and peak_db
                            > float(open_thresh) + float(peak_gate_slack_db)
                        ):
                            hang_floor = max(
                                hang_floor, float(peak_db) - float(speech_drop_db)
                            )
                        still_speech = open_thresh is not None and db >= hang_floor
                        if still_speech:
                            silent_blocks = 0
                            speech_blocks += 1
                            _emit_voice()
                            if endpointer is not None:
                                endpointer.on_speech()
                        else:
                            silent_blocks += 1
                            if endpointer is None:
                                if (
                                    silent_blocks >= end_silence_blocks
                                    and speech_blocks >= min_speech_blocks
                                ):
                                    break
                            else:
                                def _endpoint_frame() -> EndpointFrame:
                                    pcm = (
                                        pcm16_mono_bytes(np.concatenate(chunks))
                                        if chunks
                                        else b""
                                    )
                                    return EndpointFrame(
                                        pcm16=pcm,
                                        sample_rate=sample_rate,
                                        trailing_silence_s=silent_blocks * 0.02,
                                        speech_s=speech_blocks * 0.02,
                                    )

                                if endpointer.should_end(
                                    silent_blocks=silent_blocks,
                                    speech_blocks=speech_blocks,
                                    audio_fn=_endpoint_frame,
                                ):
                                    break

                    if should_stop is not None:
                        pcm = (
                            pcm16_mono_bytes(np.concatenate(chunks)) if chunks else b""
                        )
                        if should_stop(pcm, time.monotonic() - start):
                            break

                # Publish normal teardown ownership before the registered stream is
                # detached. A signal at this boundary sees either the active stream
                # or this exact owner and upgrades it to cancellation; never neither.
                normal_cleanup_owner = _reserve_stream_cleanup(
                    stream,
                    cancel=False,
                    fallback_exit=fallback_exit,
                )

            cleanup_worker = _start_stream_cleanup(normal_cleanup_owner)
            _wait_stream_cleanup(cleanup_worker)

            if not chunks:
                raise TimeoutError(
                    f"no speech captured (peak_db={peak_db:.1f} peak_rms={peak_rms:.5f})"
                )

            all_s = np.concatenate(chunks)
            pcm = pcm16_mono_bytes(all_s)
            dur_ms = int(1000 * len(all_s) / sample_rate)
            speech_ms = int(1000 * speech_blocks * 0.02)
            return CaptureResult(
                pcm16=pcm,
                sample_rate=sample_rate,
                duration_ms=dur_ms,
                speech_ms=speech_ms,
                wait_speech_ms=wait_speech_ms,
                peak_rms=float(peak_rms),
                peak_db=float(peak_db),
            )
        except BaseException:
            if read_deadline is not None and read_deadline.fired_message() is not None:
                deadline_forced_exit = True
            raise
        finally:
            if read_deadline is not None:
                if read_deadline.fired_message() is not None:
                    deadline_forced_exit = True
                read_deadline.close()
            # Cancel teardown aborts/closes native streams but skips legacy
            # context-manager __exit__ unless fallback was published on the
            # cleanup owner. Deadline timeouts never publish that owner before
            # raising, so run fallback here without affecting signal cancel
            # ownership races covered by B143.
            if deadline_forced_exit and fallback_exit is not None:
                try:
                    fallback_exit()
                except BaseException:
                    pass
    finally:
        try:
            from hark.audio.spectrum import clear_spectrum

            clear_spectrum(source="listen")
        except Exception:
            pass


def list_input_devices() -> list[dict]:
    _require_sd()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append(
                {
                    "id": i,
                    "name": d.get("name"),
                    "channels": d.get("max_input_channels"),
                    "default_sr": d.get("default_samplerate"),
                }
            )
    return out

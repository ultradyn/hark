"""Clean-interpreter process transport for TTS provider synthesis."""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import weakref
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum, auto
from typing import Any, Protocol

from hark.providers.base import ProviderError
from hark.signal_safety import SigintMaskGuard


_MAX_METADATA_SIZE = 64 * 1024
_MAX_AUDIO_SIZE = 64 * 1024 * 1024
_BORROW_BUSY = object()
_OS_CLOSE = os.close
_PIPE_CLOSE = os.close
_PIPE_FDOPEN = os.fdopen
_PIPE_RAW_CLOSE = os.close
_PTHREAD_SIGMASK = getattr(signal, "pthread_sigmask", None)


@dataclass(frozen=True)
class SynthRequest:
    provider: str
    voice: str
    language: str | None
    text: str


@dataclass(frozen=True)
class SynthResponse:
    audio: bytes
    provider: str
    content_type: str
    voice: str


class SynthWorkerError(RuntimeError):
    """A synth worker failed without a reconstructable provider exception."""


def _pipe_fd_identity(fd: int) -> tuple[int, int, int, int]:
    stat = os.fstat(fd)
    return (stat.st_dev, stat.st_ino, stat.st_mode, stat.st_rdev)


class _RawPipeFdGuard:
    """Own one pipe fd across fallible close/fdopen ownership transfers."""

    __slots__ = ("_fd", "_identity")

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._identity = _pipe_fd_identity(fd)

    @property
    def fd(self) -> int:
        return self._fd

    def _reconcile_failed_transfer(self, fd: int) -> None:
        """Reclaim ownership only while *fd* still names our original pipe."""
        try:
            current = _pipe_fd_identity(fd)
        except OSError:
            # The transfer may have consumed the descriptor before raising.
            return
        if current == self._identity:
            # The operation failed before consuming the pipe. Reclaim it only
            # after live identity proves that no foreign fd reused the number.
            self._fd = fd

    def close(self) -> None:
        fd = self._fd
        # Relinquish before close can consume the descriptor. This closes the
        # post-return bytecode window: an asynchronous BaseException after the
        # kernel effect can never leave a reused descriptor owned by this guard.
        self._fd = -1
        try:
            _PIPE_CLOSE(fd)
        except BaseException:
            primary = sys.exception()
            try:
                self._reconcile_failed_transfer(fd)
            except BaseException:
                # Transfer reconciliation is cleanup; preserve the exact
                # close failure that established this unwind.
                assert primary is not None
            raise

    def adopt(self, *args: Any, **kwargs: Any) -> Any:
        fd = self._fd
        # fdopen owns the resource if it returns. Publish relinquishment before
        # entering that fallible transfer so a post-return signal/trace hook
        # cannot make later cleanup close a same-number replacement.
        self._fd = -1
        try:
            adopted = _PIPE_FDOPEN(fd, *args, **kwargs)
        except BaseException:
            primary = sys.exception()
            try:
                self._reconcile_failed_transfer(fd)
            except BaseException:
                assert primary is not None
            raise
        return adopted

    def close_if_owned(self) -> None:
        fd = self._fd
        if fd < 0:
            return
        # Relinquish before the raw close. A post-effect failure must not make
        # later cleanup close a same-number replacement.
        self._fd = -1
        _PIPE_RAW_CLOSE(fd)


class _BorrowLease:
    __slots__ = ("_on_release", "__weakref__")

    def __init__(self) -> None:
        self._on_release: Callable[[], None] | None = None

    def arm(self, on_release: Callable[[], None] | None) -> None:
        self._on_release = on_release

    def __del__(self) -> None:
        if self._on_release is None:
            return
        try:
            self._on_release()
        except BaseException:
            pass


class _BorrowAuthority:
    """Keep authority reachable while one operation borrows it.

    The authority object itself is never moved or destructively popped. A
    temporary marker only excludes concurrent borrowers. Its cleanup is inside
    the same exception region as publication, including an asynchronous
    exception delivered immediately after ``setdefault`` inserts the marker.
    """

    __slots__ = ("_borrower_slot", "retired")

    def __init__(self) -> None:
        self._borrower_slot: dict[int, weakref.ReferenceType[_BorrowLease]] = {}
        self.retired = False

    @property
    def borrowed(self) -> bool:
        borrower = dict.get(self._borrower_slot, 0)
        return borrower is not None and borrower() is not None

    def use(
        self,
        operation: Callable[..., Any],
        *args: Any,
        on_release: Callable[[], None] | None = None,
    ) -> Any:
        lease = _BorrowLease()
        lease_ref = weakref.ref(lease)
        while True:
            if self.retired:
                return _BORROW_BUSY
            existing = dict.get(self._borrower_slot, 0)
            if existing is not None:
                if existing() is not None:
                    return _BORROW_BUSY
                # A cleanup interruption can leave only a dead weakref. It has
                # no authority and is safe for any future borrower to discard.
                dict.pop(self._borrower_slot, 0, None)
                continue
            existing = dict.setdefault(self._borrower_slot, 0, lease_ref)
            if existing is lease_ref:
                break
            if existing() is not None:
                return _BORROW_BUSY
        if self.retired:
            return _BORROW_BUSY
        # Only the successfully published authoritative borrower may perform
        # release work. Busy, retired, rejected, and pre-publication leases are
        # deliberately inert when their temporary objects are destroyed.
        lease.arm(on_release)
        try:
            result = operation(*args)
        except BaseException:
            # Do not execute fallible eager cleanup while a primary is active.
            # Frame unwind drops the only strong lease reference, which is the
            # authoritative cleanup and leaves at most a stale weakref.
            raise
        else:
            # This pop is only eager hygiene. If any BaseException interrupts
            # it, frame unwind destroys ``lease`` and the stored weakref becomes
            # recognizably stale instead of wedging the authority.
            if dict.get(self._borrower_slot, 0) is lease_ref:
                dict.pop(self._borrower_slot, 0, None)
            return result


class _OwnedPidfd:
    """RAII owner for a pidfd, including temporary signalling borrows."""

    __slots__ = (
        "_borrow",
        "_close_authority",
        "_committed",
        "_fd",
    )

    @classmethod
    def from_raw(cls, fd: int) -> _OwnedPidfd:
        """Adopt a raw descriptor with exactly one bootstrap owner.

        Destruction is suppressed until commit. Before the scalar ``_fd``
        assignment the raw local is responsible; afterwards only ``owner`` is.
        The exception path inspects that single field and never closes both.
        """
        owner: _OwnedPidfd | None = None
        try:
            owner = object.__new__(cls)
            owner._committed = False
            owner._fd = None
            owner._borrow = _BorrowAuthority()
            owner._close_authority = _BorrowAuthority()
            owner._fd = fd
            owner._committed = True
            owner._after_commit()
            return owner
        except BaseException:
            if owner is not None and getattr(owner, "_fd", None) == fd:
                owner.request_close()
            else:
                cls._close_raw(fd)
            raise

    def _after_commit(self) -> None:
        """Injection seam for the post-initialization ownership boundary."""

    @staticmethod
    def _close_raw(fd: int) -> None:
        # Construction cleanup is best-effort and cannot replace its primary.
        # A one-shot injected mask failure is retried after the guard restores
        # the known prior state.
        guard = None
        for _ in range(2):
            try:
                guard = SigintMaskGuard.acquire(_PTHREAD_SIGMASK)
                break
            except BaseException:
                continue
        if guard is None:
            try:
                _OS_CLOSE(fd)
            except BaseException:
                pass
            return
        trace = sys.gettrace()
        try:
            if trace is not None:
                sys.settrace(None)
            try:
                _OS_CLOSE(fd)
            except BaseException:
                pass
        finally:
            if trace is not None:
                try:
                    sys.settrace(trace)
                except BaseException:
                    pass
            guard.restore_suppressing()

    @property
    def fd(self) -> int | None:
        return self._fd

    def use(self, operation: Callable[[int, int], Any], signum: int) -> Any:
        try:
            return self._borrow.use(
                self._use_owned,
                operation,
                signum,
                on_release=self._after_signal_borrow_release,
            )
        finally:
            if self._borrow.retired and not self._borrow.borrowed:
                self._finish_close()

    def _after_signal_borrow_release(self) -> None:
        if self._borrow.retired:
            self._finish_close()

    def _use_owned(
        self,
        operation: Callable[[int, int], Any],
        signum: int,
    ) -> Any:
        fd = self._fd
        if fd is None:
            return _BORROW_BUSY
        return operation(fd, signum)

    def request_close(self) -> None:
        guard: SigintMaskGuard | None = None
        try:
            guard = SigintMaskGuard.acquire(_PTHREAD_SIGMASK)
        except BaseException:
            # Retirement itself is the fail-closed primitive. If masking is
            # unavailable, continue rather than leaving descriptor admission
            # open merely because defensive masking failed.
            pass
        trace = sys.gettrace()
        try:
            if trace is not None:
                sys.settrace(None)
            try:
                # This one scalar is both close intent and the borrow rejection
                # gate. There is no state in which close has begun while a later
                # borrower can still be admitted to the descriptor.
                self._borrow.retired = True
            finally:
                if self._borrow.retired and not self._borrow.borrowed:
                    self._finish_close()
        finally:
            primary = sys.exception()
            if trace is not None:
                try:
                    sys.settrace(trace)
                except BaseException:
                    if primary is None:
                        raise
            if guard is not None:
                guard.restore_preserving_primary()

    def _finish_close(self) -> None:
        for _ in range(2):
            try:
                result = self._close_authority.use(self._finish_close_exclusive)
            except BaseException:
                # The weak lease makes an interrupted attempt retryable.
                continue
            if result is _BORROW_BUSY:
                return
            return

    def _finish_close_exclusive(self) -> None:
        fd = self._fd
        if fd is None:
            return
        guard = SigintMaskGuard.acquire(_PTHREAD_SIGMASK)
        trace = sys.gettrace()
        try:
            if trace is not None:
                sys.settrace(None)
            # Invalidate ownership only while SIGINT is blocked, then close via
            # the captured OS primitive with Python tracing disabled. Nested or
            # concurrent cleanup cannot win the close authority or observe a
            # descriptor integer after the kernel consumes it.
            self._fd = None
            try:
                _OS_CLOSE(fd)
            except BaseException:
                # Ownership is already invalidated under the signal mask. A
                # hostile cleanup hook must not replace the primary exception
                # or make this object close the same integer again.
                pass
        finally:
            primary = sys.exception()
            if trace is not None:
                try:
                    sys.settrace(trace)
                except BaseException:
                    if primary is None:
                        raise
            guard.restore()

    def __del__(self) -> None:
        if not getattr(self, "_committed", False):
            return
        try:
            self.request_close()
        except BaseException:
            pass


@dataclass
class _IdentityToken:
    pidfd: _OwnedPidfd | None

    @property
    def pidfd_mode(self) -> bool:
        return self.pidfd is not None

    def request_close(self) -> None:
        if self.pidfd is not None:
            self.pidfd.request_close()


class _SpawnState(Enum):
    """How much child authority a partially initialized ``Popen`` exposes."""

    CLAIMED_NOT_ENTERED = auto()
    CHILD_CREATION_UNCERTAIN = auto()
    CHILD = auto()
    FAILED_WITHOUT_CHILD = auto()


@dataclass
class _ProcessAuthority:
    process: subprocess.Popen[bytes]
    published: bool
    spawn_state: _SpawnState = _SpawnState.CHILD
    identity: _IdentityToken | None = None
    reap: _BorrowAuthority = field(default_factory=_BorrowAuthority)
    termination_level: int = 0
    reaped: bool = False
    numeric_send_fenced: bool = False


class SynthProcessLifecycle:
    """Synchronized ownership and bounded termination for one supervisor.

    Once terminalization begins, this lifecycle never accepts another spawn.
    Authority stays published until the direct child is confirmed reaped, so a
    nested signal can resume the same TERM-and-reap transaction safely.
    """

    _TERM_GRACE_S = 0.65

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._authority: _ProcessAuthority | None = None
        # Monotonic scalar publication is re-entrant from nested Python signal
        # handlers: unlike Event.set, this assignment takes no Python lock.
        self._terminal_requested = False

    @property
    def active(self) -> bool:
        with self._lock:
            return self._authority is not None

    @property
    def terminalizing(self) -> bool:
        return self._terminal_requested

    @staticmethod
    def _pid(process: subprocess.Popen[bytes]) -> int | None:
        pid = getattr(process, "pid", None)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            return pid
        return None

    @staticmethod
    def _returncode(process: subprocess.Popen[bytes]) -> int | None:
        returncode = getattr(process, "returncode", None)
        if isinstance(returncode, int) and not isinstance(returncode, bool):
            return returncode
        return None

    def _before_popen_init(self) -> None:
        """Injection seam before child creation becomes possible."""

    def preclaim(
        self,
        process: subprocess.Popen[bytes],
    ) -> None:
        with self._lock:
            if self._terminal_requested:
                raise SynthWorkerError("TTS synth lifecycle is terminalizing")
            if self._authority is not None:
                raise RuntimeError("synth process ownership overlap")
            state = (
                _SpawnState.CHILD
                if self._pid(process) is not None
                else _SpawnState.CLAIMED_NOT_ENTERED
            )
            self._authority = _ProcessAuthority(
                process,
                False,
                state,
            )

    def spawn(
        self,
        process: subprocess.Popen[bytes],
        command: list[str],
        **kwargs: Any,
    ) -> None:
        """Atomically claim the spawn gate and initialize the direct child."""
        with self._lock:
            self.preclaim(process)
            authority = self._authority
            assert authority is not None and authority.process is process
            if self._terminal_requested:
                authority.spawn_state = _SpawnState.FAILED_WITHOUT_CHILD
                authority.reaped = True
                authority.reap.retired = True
                self._finish_reaped(authority)
                raise SynthWorkerError("TTS synth lifecycle terminated before spawn")
            try:
                self._before_popen_init()
            except BaseException:
                authority.spawn_state = _SpawnState.FAILED_WITHOUT_CHILD
                authority.reaped = True
                authority.reap.retired = True
                self._finish_reaped(authority)
                raise
            # From this publication until a real PID appears, Popen may have
            # created a kernel child without yet exposing its identity. A
            # missing ``process.pid`` is no longer proof that no child exists.
            authority.spawn_state = _SpawnState.CHILD_CREATION_UNCERTAIN
            try:
                # Cancellation never waits on this spawn-owned lock. If Popen
                # has published a real PID but an outer wrapper stalls before
                # returning, the signal path can terminate that direct child
                # through the preclaimed authority object.
                subprocess.Popen.__init__(process, command, **kwargs)
            finally:
                primary = sys.exception()
                if self._pid(process) is not None:
                    authority.spawn_state = _SpawnState.CHILD
                if self._terminal_requested:
                    try:
                        self.cancel()
                    except BaseException:
                        if primary is None:
                            raise
            if self._terminal_requested:
                raise SynthWorkerError("TTS synth lifecycle terminated during spawn")
            if self._pid(process) is None:
                raise SynthWorkerError("TTS synth supervisor has no process id")

    def publish(
        self,
        process: subprocess.Popen[bytes],
        identity: _IdentityToken,
    ) -> bool:
        with self._lock:
            authority = self._authority
            if (
                authority is None
                or authority.process is not process
                or authority.published
            ):
                return False
            existing = authority.identity
            if existing is not None and existing is not identity:
                return False
            # One scalar publication transfers the complete identity object:
            # pidfd-vs-portable mode and descriptor ownership cannot diverge.
            authority.identity = identity
            if self._authority is not authority or authority.reaped:
                return False
            if self._terminal_requested:
                # Retain the newly published pidfd so the already-requested
                # cancellation can finish without an unsafe numeric fallback.
                return False
            authority.published = True
            return True

    def release(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            authority = self._authority
            if authority is None or authority.process is not process:
                return
            if not authority.reaped:
                pid = self._pid(process)
                if self._returncode(process) is not None:
                    if authority.reap.borrowed:
                        return
                    authority.reaped = True
                    authority.reap.retired = True
                elif pid is not None:
                    self._wait_direct_child(authority, 0)
                else:
                    if authority.spawn_state in {
                        _SpawnState.CLAIMED_NOT_ENTERED,
                        _SpawnState.CHILD_CREATION_UNCERTAIN,
                    }:
                        # Only spawn() can prove a pre-entry cancellation. Once
                        # Popen was entered, missing PID publication is not
                        # enough to release possible kernel-child authority.
                        return
                    if authority.reap.borrowed:
                        return
                    authority.spawn_state = _SpawnState.FAILED_WITHOUT_CHILD
                    authority.reaped = True
                    authority.reap.retired = True
                if not authority.reaped:
                    return
            self._finish_reaped(authority)

    def close_identity_if_unowned(
        self,
        process: subprocess.Popen[bytes],
        identity: _IdentityToken,
    ) -> None:
        """Close a caller token only when it was never transferred here."""
        authority = self._authority
        if (
            authority is not None
            and authority.process is process
            and self._identity(authority) is identity
        ):
            return
        identity.request_close()

    def wait_and_release(self, process: subprocess.Popen[bytes]) -> int:
        """Wait for supervisor exit/reap while retaining cancellation authority."""
        authority = self._authority
        if authority is None or authority.process is not process:
            returncode = self._returncode(process)
            if returncode is None:
                raise SynthWorkerError("TTS synth supervisor authority was lost")
            return returncode
        pid = self._pid(process)
        if pid is None:
            raise SynthWorkerError("TTS synth supervisor has no process id")

        while not authority.reaped:
            self._wait_direct_child(authority, 0.05)
        returncode = self._returncode(process)
        self._finish_reaped(authority)
        return returncode if returncode is not None else 0

    @staticmethod
    def _identity(authority: _ProcessAuthority) -> _IdentityToken | None:
        return authority.identity

    @staticmethod
    def _wait_direct_child(authority: _ProcessAuthority, timeout: float) -> bool:
        pid = SynthProcessLifecycle._pid(authority.process)
        if pid is None:
            return False
        deadline = time.monotonic() + timeout
        while True:
            result = authority.reap.use(
                SynthProcessLifecycle._waitpid_once,
                authority,
                pid,
            )
            if result is _BORROW_BUSY:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))
                continue
            if result:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _waitpid_once(authority: _ProcessAuthority, pid: int) -> bool:
        # Fence numeric signalling before waitpid can consume the child. If a
        # wrapper raises after the kernel reap, the fence remains permanent;
        # a later wait reconciles ChildProcessError without ever reopening PID
        # reuse to os.kill.
        authority.numeric_send_fenced = True
        try:
            waited, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            authority.reaped = True
            authority.reap.retired = True
            return True
        except OSError:
            return False
        if waited == 0:
            # A completed return of zero proves this attempt consumed nothing.
            # Reopening is safe even if an injected exception arrives after
            # this scalar publication: the observed kernel result is durable.
            authority.numeric_send_fenced = False
            return False
        if waited != pid:
            return False
        setattr(authority.process, "returncode", os.waitstatus_to_exitcode(status))
        authority.reaped = True
        authority.reap.retired = True
        return True

    @staticmethod
    def _send_pid(pid: int, signum: int) -> None:
        try:
            os.kill(pid, signum)
        except (ProcessLookupError, OSError):
            pass

    @staticmethod
    def _send_pid_if_unfenced(
        authority: _ProcessAuthority,
        pid: int,
        signum: int,
    ) -> None:
        if authority.numeric_send_fenced:
            return
        SynthProcessLifecycle._send_pid(pid, signum)

    def _send(self, authority: _ProcessAuthority, signum: int) -> None:
        pid = self._pid(authority.process)
        if pid is None:
            return
        identity = self._identity(authority)
        if identity is not None and identity.pidfd_mode:
            assert identity.pidfd is not None
            try:
                result = identity.pidfd.use(signal.pidfd_send_signal, signum)
            except (ProcessLookupError, OSError):
                # Never fall back to a numeric PID after a pidfd syscall
                # failure: a concurrent waiter may already have released the
                # last PID-reuse fence.
                return
            if result is not _BORROW_BUSY:
                return
            if authority.published:
                return

            # Publication aborted before becoming durable. The RAII identity
            # remains responsible for closing its pidfd; the unreaped child is
            # still safely cancellable through the portable authority below.

        try:
            # The durable reap authority prevents concurrent wait/reap and PID
            # reuse for the full duration of this numeric send. It is never
            # popped from the process authority, even while borrowed.
            authority.reap.use(
                SynthProcessLifecycle._send_pid_if_unfenced,
                authority,
                pid,
                signum,
            )
        except (ProcessLookupError, OSError):
            pass

    def _finish_reaped(self, authority: _ProcessAuthority) -> None:
        if not authority.reaped:
            return
        if self._authority is authority:
            self._authority = None
        identity = authority.identity
        authority.identity = None
        if identity is not None:
            identity.request_close()
        authority.reap.retired = True

    def cancel(self) -> bool:
        """Begin terminalization and confirm the owned direct child is reaped.

        The method is deliberately re-entrant from a Python signal handler. A
        nested call observes the same authority and continues waiting without
        killing the cleanup supervisor. Unexpected cleanup failures leave
        authority intact for the next call.
        """
        # This path is used directly by signal handlers and watchdog timers.
        # It never acquires the spawn-owned lock: terminal intent is published
        # first, then cancellation operates on the atomically visible authority.
        self._terminal_requested = True
        authority = self._authority
        if authority is None:
            return True
        if authority.reaped:
            self._finish_reaped(authority)
            return True
        if self._returncode(authority.process) is not None:
            try:
                self._wait_direct_child(authority, 0)
            except BaseException:
                pass
            self._finish_reaped(authority)
            return authority.reaped

        if self._pid(authority.process) is None:
            if authority.spawn_state is _SpawnState.CLAIMED_NOT_ENTERED:
                # Popen entry has not been published, so no kernel child can
                # exist. CLI hard-exit is safe; a surviving library caller's
                # spawn path observes terminal_requested before entering.
                return True
            if authority.spawn_state is _SpawnState.FAILED_WITHOUT_CHILD:
                authority.reaped = True
                authority.reap.retired = True
                self._finish_reaped(authority)
                return True
            # Once Popen entry is published, pid=None is child-creation
            # uncertainty, not absence. Python argv, object identity, and code
            # that runs after exec cannot prove cleanup for a child that may
            # still be stalled before that code. Fail closed until a kernel
            # identity is published.
            return False

        # The direct child is a cleanup supervisor. Repeated/nested interrupts
        # may resend TERM through the same reuse-safe authority, but must never
        # SIGKILL that supervisor before it proves its provider tree and pipes
        # are gone. If cleanup cannot finish, retain authority and fail closed.
        authority.termination_level = 1
        try:
            self._send(authority, signal.SIGTERM)
            self._wait_direct_child(authority, self._TERM_GRACE_S)
        except BaseException:
            pass
        self._finish_reaped(authority)
        return authority.reaped


class ProcessOwner(Protocol):
    def spawn_synth_process(
        self,
        process: subprocess.Popen[bytes],
        command: list[str],
        **kwargs: Any,
    ) -> None: ...

    def publish_synth_process_pidfd(
        self,
        process: subprocess.Popen[bytes],
        identity: _IdentityToken,
    ) -> bool: ...

    def unregister_synth_process(self, process: subprocess.Popen[bytes]) -> None: ...

    def wait_and_unregister_synth_process(
        self,
        process: subprocess.Popen[bytes],
    ) -> int: ...

    def cancel_synth_process(self, process: subprocess.Popen[bytes]) -> bool: ...

    def close_synth_identity_if_unowned(
        self,
        process: subprocess.Popen[bytes],
        identity: _IdentityToken,
    ) -> None: ...


class SynthTransport(Protocol):
    def synthesize(self, request: SynthRequest) -> SynthResponse: ...


def synth_worker_command() -> list[str]:
    return [sys.executable, "-m", "hark.tts_worker"]


class SubprocessSynthTransport:
    """Execute one provider call in a clean child interpreter."""

    def __init__(
        self,
        owner: ProcessOwner,
        *,
        command_factory: Callable[[], list[str]] = synth_worker_command,
    ) -> None:
        self._owner = owner
        self._command_factory = command_factory

    @staticmethod
    def _open_pidfd(process: subprocess.Popen[bytes]) -> _OwnedPidfd | None:
        """Claim a reuse-safe parent handle before the worker protocol begins."""
        if sys.platform != "linux":
            return None
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if pidfd_open is None or pidfd_send_signal is None:
            return None
        pid = SynthProcessLifecycle._pid(process)
        if pid is None:
            raise SynthWorkerError("TTS synth supervisor has no process id")
        try:
            raw_pidfd = pidfd_open(pid, 0)
        except OSError as exc:
            raise SynthWorkerError("could not claim TTS synth worker pidfd") from exc
        try:
            return _OwnedPidfd.from_raw(raw_pidfd)
        except BaseException:
            # from_raw has already consumed the raw descriptor on failure.
            raise

    @staticmethod
    def _require_protocol_eof(result_file: Any) -> None:
        if result_file.read(1):
            raise SynthWorkerError("trailing TTS synth worker result data")

    @staticmethod
    def _decode(result_file: Any, returncode: int) -> SynthResponse:
        header = result_file.read(4)
        if len(header) != 4:
            raise SynthWorkerError(
                f"TTS synth worker exited {returncode} without a result"
            )
        metadata_size = struct.unpack("!I", header)[0]
        if metadata_size > _MAX_METADATA_SIZE:
            raise SynthWorkerError("oversize TTS synth worker metadata")
        metadata_payload = result_file.read(metadata_size)
        if len(metadata_payload) != metadata_size:
            raise SynthWorkerError("truncated TTS synth worker metadata")
        try:
            message = json.loads(metadata_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SynthWorkerError("invalid TTS synth worker result") from exc
        if not isinstance(message, dict):
            raise SynthWorkerError("invalid TTS synth worker message")

        status = message.get("status")
        if not isinstance(status, str) or status not in {"ok", "error"}:
            raise SynthWorkerError("invalid TTS synth worker status")

        if status == "ok":
            audio_size = message.get("audio_size")
            if not isinstance(audio_size, int) or isinstance(audio_size, bool):
                raise SynthWorkerError("invalid TTS synth worker audio size")
            if audio_size < 0 or audio_size > _MAX_AUDIO_SIZE:
                raise SynthWorkerError("invalid TTS synth worker audio size")
            provider = message.get("provider")
            content_type = message.get("content_type")
            voice = message.get("voice")
            if not all(
                isinstance(value, str) for value in (provider, content_type, voice)
            ):
                raise SynthWorkerError("invalid TTS synth worker response fields")
            audio = result_file.read(audio_size)
            if len(audio) != audio_size:
                raise SynthWorkerError("truncated TTS synth worker audio")
            SubprocessSynthTransport._require_protocol_eof(result_file)
            return SynthResponse(
                audio=audio,
                provider=provider,
                content_type=content_type,
                voice=voice,
            )

        kind = message.get("kind")
        if kind == "provider":
            remote_message = message.get("message")
            code = message.get("code")
            if not isinstance(remote_message, str):
                raise SynthWorkerError("invalid TTS provider error message")
            if (
                not isinstance(code, int)
                or isinstance(code, bool)
                or code < 0
                or code > 255
            ):
                raise SynthWorkerError("invalid TTS provider error code")
            SubprocessSynthTransport._require_protocol_eof(result_file)
            raise ProviderError(remote_message, code=code)
        if kind != "exception":
            raise SynthWorkerError("invalid TTS synth worker error kind")
        remote_type = message.get("type")
        remote_message = message.get("message")
        if not isinstance(remote_type, str) or not isinstance(remote_message, str):
            raise SynthWorkerError("invalid TTS synth worker exception fields")
        SubprocessSynthTransport._require_protocol_eof(result_file)
        raise SynthWorkerError(f"TTS synth worker {remote_type}: {remote_message}")

    def synthesize(self, request: SynthRequest) -> SynthResponse:
        read_fd, write_fd = os.pipe()
        read_guard: _RawPipeFdGuard | None = None
        write_guard: _RawPipeFdGuard | None = None
        process: subprocess.Popen[bytes] | None = None
        identity: _IdentityToken | None = None
        read_file = None
        try:
            read_guard = _RawPipeFdGuard(read_fd)
            read_fd = -1
            write_guard = _RawPipeFdGuard(write_fd)
            write_fd = -1
            env = os.environ.copy()
            assert write_guard.fd >= 0
            env["HARK_TTS_RESULT_FD"] = str(write_guard.fd)
            import_path = os.pathsep.join(path for path in sys.path if path)
            if import_path:
                current_path = env.get("PYTHONPATH")
                env["PYTHONPATH"] = import_path + (
                    os.pathsep + current_path if current_path else ""
                )
            process = subprocess.Popen.__new__(subprocess.Popen)
            self._owner.spawn_synth_process(
                process,
                self._command_factory(),
                stdin=subprocess.PIPE,
                pass_fds=(write_guard.fd,),
                env=env,
                start_new_session=True,
            )
            pidfd = self._open_pidfd(process)
            identity = _IdentityToken(pidfd)
            published = self._owner.publish_synth_process_pidfd(process, identity)
            if not published:
                raise SynthWorkerError("TTS synth worker ownership was withdrawn")
            write_guard.close()

            assert process.stdin is not None
            request_payload = json.dumps(asdict(request), separators=(",", ":")).encode(
                "utf-8"
            )
            if len(request_payload) > _MAX_METADATA_SIZE:
                raise SynthWorkerError("oversize TTS synth worker request")
            process.stdin.write(struct.pack("!I", len(request_payload)))
            process.stdin.write(request_payload)
            process.stdin.close()
            read_file = read_guard.adopt("rb", closefd=True)
            response = self._decode(read_file, -1)
            # Result EOF does not prove that supervisor cleanup/atexit has
            # finished. Retain cancellation authority through confirmed exit
            # and reap on every platform.
            returncode = self._owner.wait_and_unregister_synth_process(process)
            if returncode != 0:
                raise SynthWorkerError(f"TTS synth worker exited {returncode}")
            return response
        except BaseException:
            if process is not None:
                try:
                    if not self._owner.cancel_synth_process(process):
                        self._owner.cancel_synth_process(process)
                except BaseException:
                    pass
            raise
        finally:
            if process is not None:
                try:
                    process_stdin = getattr(process, "stdin", None)
                    if process_stdin is not None and not process_stdin.closed:
                        process_stdin.close()
                except BaseException:
                    pass
            if read_file is not None:
                try:
                    read_file.close()
                except BaseException:
                    pass
            if read_guard is not None:
                try:
                    read_guard.close_if_owned()
                except BaseException:
                    pass
            if write_guard is not None:
                try:
                    write_guard.close_if_owned()
                except BaseException:
                    pass
            if read_fd >= 0:
                try:
                    closing_fd = read_fd
                    read_fd = -1
                    _PIPE_RAW_CLOSE(closing_fd)
                except BaseException:
                    pass
            if write_fd >= 0:
                try:
                    closing_fd = write_fd
                    write_fd = -1
                    _PIPE_RAW_CLOSE(closing_fd)
                except BaseException:
                    pass
            if process is not None:
                try:
                    self._owner.unregister_synth_process(process)
                except BaseException:
                    # Ownership bookkeeping must not replace the primary or
                    # prevent cleanup of the transport resources above.
                    pass
            if process is not None and identity is not None:
                try:
                    self._owner.close_synth_identity_if_unowned(process, identity)
                except BaseException:
                    pass


class InProcessSynthTransport:
    """Injectable deterministic transport for unit tests."""

    def __init__(self, resolver: Callable[..., Any]) -> None:
        self._resolver = resolver

    def synthesize(self, request: SynthRequest) -> SynthResponse:
        provider = self._resolver(
            request.provider,
            voice=request.voice,
            language=request.language,
        )
        result = provider.synthesize(request.text, voice=request.voice)
        return SynthResponse(
            audio=result.audio,
            provider=result.provider,
            content_type=result.content_type,
            voice=result.voice or request.voice,
        )

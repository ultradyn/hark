"""Exec target for one isolated TTS provider synthesis call."""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any

from hark.providers.base import ProviderError
from hark.signal_safety import SigintMaskGuard


_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36


class _SupervisorTermination(BaseException):
    """Interrupt a supervisor that has not created provider state yet."""


_MAX_METADATA_SIZE = 64 * 1024
_MAX_AUDIO_SIZE = 64 * 1024 * 1024
_OUTPUT_CHUNK_SIZE = 64 * 1024
_PORTABLE_OUTPUT_DRAIN_SECONDS = 0.05
_BLOCKING_RESTORE_ATTEMPTS = 2
_PAYLOAD_POPEN_CLASS = subprocess.Popen
_NATIVE_PAYLOAD_POPEN_INIT = subprocess.Popen.__init__
_PIDFD_OPEN = getattr(os, "pidfd_open", None)
_PIDFD_SEND_SIGNAL = getattr(signal, "pidfd_send_signal", None)
_OS_CLOSE = os.close
_PTHREAD_SIGMASK = getattr(signal, "pthread_sigmask", None)
_DIRECT_CHILDREN_AUTHORITY_FD = -1
_DIRECT_CHILDREN_AUTHORITY_PID = -1
_NO_RESULT_OWNER = object()
_UNSUPPORTED_DESCENDANT_AUTHORITY = (
    "isolated TTS requires exact descendant cleanup authority on this host"
)


class _FdTransferState:
    """Record whether an fd operation may have crossed its effect boundary."""

    __slots__ = ("effect_started", "result")

    def __init__(self) -> None:
        self.effect_started = False
        self.result: Any = None


def _restore_runtime_callback(
    setter: Callable[[Any], None],
    callback: Any,
) -> None:
    """Restore one runtime callback without replacing an active exception."""
    primary = sys.exception()
    try:
        setter(callback)
    except BaseException:
        if primary is None:
            raise


def _run_fd_transfer(
    state: _FdTransferState,
    operation: Callable[..., Any],
    *args: Any,
    on_success: Callable[[], None] | None = None,
    **kwargs: Any,
) -> Any:
    """Run marker and fd effect without a SIGINT/trace/profile gap."""
    mask = SigintMaskGuard.acquire(_PTHREAD_SIGMASK)
    trace = sys.gettrace()
    profile = sys.getprofile()
    caught: BaseException | None = None
    try:
        if trace is not None:
            sys.settrace(None)
        if profile is not None:
            sys.setprofile(None)
        state.effect_started = True
        state.result = operation(*args, **kwargs)
        if on_success is not None:
            on_success()
    except BaseException:
        caught = sys.exception()
        raise
    finally:
        try:
            try:
                mask.restore()
            except BaseException:
                restore_failure = sys.exception()
                mask.restore_suppressing()
                if caught is None:
                    assert restore_failure is not None
                    raise restore_failure
        finally:
            try:
                if profile is not None:
                    _restore_runtime_callback(sys.setprofile, profile)
            finally:
                if trace is not None:
                    _restore_runtime_callback(sys.settrace, trace)
    return state.result


def _protected_payload_popen_init(
    process: subprocess.Popen[bytes],
    command: list[str],
    **kwargs: Any,
) -> None:
    """Protect real payload creation while keeping injected wrappers stoppable."""
    _run_fd_transfer(
        _FdTransferState(),
        _NATIVE_PAYLOAD_POPEN_INIT,
        process,
        command,
        **kwargs,
    )


_PAYLOAD_POPEN_INIT = _protected_payload_popen_init


def _result_close(fd: int, state: _FdTransferState) -> None:
    _run_fd_transfer(state, os.close, fd)


def _result_fdopen(
    fd: int,
    state: _FdTransferState,
    *args: Any,
    **kwargs: Any,
) -> Any:
    return _run_fd_transfer(state, os.fdopen, fd, *args, **kwargs)


_RESULT_CLOSE = _result_close
_RESULT_FDOPEN = _result_fdopen


def _pidfd_close(
    fd: int,
    state: _FdTransferState,
    retire: Callable[[], None],
) -> None:
    """Close and retire one pidfd in the same protected transaction."""
    _run_fd_transfer(state, _OS_CLOSE, fd, on_success=retire)


def _direct_children_close(
    fd: int,
    state: _FdTransferState,
    retire: Callable[[], None],
) -> None:
    """Close and retire procfs authority without a callback-visible gap."""
    _run_fd_transfer(state, _OS_CLOSE, fd, on_success=retire)


_PIDFD_CLOSE = _pidfd_close
_DIRECT_CHILDREN_CLOSE = _direct_children_close


def _close_pidfd_owner(owner: list[int | None]) -> tuple[BaseException, Any] | None:
    """Close one published pidfd, retaining its exact owner until effect."""
    first_error: tuple[BaseException, Any] | None = None
    while isinstance(owner[0], int):
        fd = owner[0]
        state = _FdTransferState()

        def retire() -> None:
            owner[0] = None

        try:
            _PIDFD_CLOSE(fd, state, retire)
        except BaseException as exc:
            if first_error is None:
                first_error = (exc, exc.__traceback__)
            if not state.effect_started:
                continue
            retire()
        break
    return first_error


def _install_parent_death_signal() -> bool:
    """Install Linux supervisor ownership; return false when unsupported."""
    _close_direct_children_authority()
    if sys.platform != "linux":
        return False
    parent_pid = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None:
        return False
    if prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    if parent_pid == 1 or os.getppid() != parent_pid:
        os._exit(1)
    if prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    # Detached-tree authority depends on enumerating only our current direct
    # children. Fail closed into a structured worker error if procfs cannot
    # provide that reuse-safe set.
    _linux_direct_children()
    authority_state = _FdTransferState()

    def publish_authority() -> None:
        authority_fd = authority_state.result
        if not isinstance(authority_fd, int):
            raise RuntimeError("invalid direct-child authority descriptor")
        global _DIRECT_CHILDREN_AUTHORITY_FD, _DIRECT_CHILDREN_AUTHORITY_PID
        _DIRECT_CHILDREN_AUTHORITY_FD = authority_fd
        _DIRECT_CHILDREN_AUTHORITY_PID = os.getpid()

    try:
        _run_fd_transfer(
            authority_state,
            os.open,
            f"/proc/{os.getpid()}/task/{os.getpid()}/children",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0),
            on_success=publish_authority,
        )
        authority_fd = _DIRECT_CHILDREN_AUTHORITY_FD
        _read_direct_children_authority(authority_fd)
    except BaseException:
        # The fd was published before callbacks resumed. Preserve the setup
        # primary while mandatory cleanup consumes that exact global owner.
        while _DIRECT_CHILDREN_AUTHORITY_FD >= 0:
            try:
                _close_direct_children_authority()
            except BaseException:
                continue
        raise
    # The parent owns cancellation and terminates this process through a
    # pidfd.  Preserve an inherited blocked SIGINT mask: a terminal killpg can
    # otherwise queue SIGINT during interpreter startup, then inject a raw
    # KeyboardInterrupt here when unblocked outside the protected protocol.
    return True


def _linux_direct_children() -> set[int]:
    children = f"/proc/{os.getpid()}/task/{os.getpid()}/children"
    with open(children, encoding="ascii") as child_file:
        return {int(value) for value in child_file.read().split()}


def _read_direct_children_authority(fd: int) -> set[int]:
    """Read the pre-opened live children view retained before provider spawn."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(fd, 4096):
        chunks.append(chunk)
    return {int(value) for value in b"".join(chunks).split()}


def _direct_children_from_authority() -> set[int]:
    fd = _DIRECT_CHILDREN_AUTHORITY_FD
    if fd < 0 or _DIRECT_CHILDREN_AUTHORITY_PID != os.getpid():
        raise OSError("direct-child fallback authority is unavailable")
    return _read_direct_children_authority(fd)


def _close_direct_children_authority() -> None:
    global _DIRECT_CHILDREN_AUTHORITY_FD, _DIRECT_CHILDREN_AUTHORITY_PID
    fd = _DIRECT_CHILDREN_AUTHORITY_FD
    if fd < 0:
        _DIRECT_CHILDREN_AUTHORITY_PID = -1
        return

    first_error: tuple[BaseException, Any] | None = None
    while _DIRECT_CHILDREN_AUTHORITY_FD == fd:
        state = _FdTransferState()

        def retire() -> None:
            global _DIRECT_CHILDREN_AUTHORITY_FD, _DIRECT_CHILDREN_AUTHORITY_PID
            _DIRECT_CHILDREN_AUTHORITY_FD = -1
            _DIRECT_CHILDREN_AUTHORITY_PID = -1

        try:
            _DIRECT_CHILDREN_CLOSE(fd, state, retire)
        except BaseException as exc:
            if first_error is None:
                first_error = (exc, exc.__traceback__)
            # A pre-effect interruption leaves the exact descriptor published
            # and is safe to retry. Once the callback may have entered, its
            # scalar retirement is authoritative and the integer is never
            # reused for cleanup.
            if not state.effect_started:
                continue
            retire()
        break
    if first_error is not None:
        exc, traceback = first_error
        raise exc.with_traceback(traceback)


def _owned_direct_children() -> set[int]:
    """Enumerate exact unreaped children through path or retained authority."""
    try:
        return _linux_direct_children()
    except (OSError, ValueError) as path_error:
        try:
            return _direct_children_from_authority()
        except (OSError, ValueError):
            raise path_error


def _signal_children(pids: set[int], signum: int) -> None:
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signum)
        except (ProcessLookupError, OSError):
            pass


def _record_payload_status(
    payload: subprocess.Popen[bytes] | None,
    pid: int,
    status: int,
) -> None:
    if payload is None or pid != getattr(payload, "pid", None):
        return
    if payload.returncode is None:
        payload.returncode = os.waitstatus_to_exitcode(status)


def _reap_available(payload: subprocess.Popen[bytes] | None = None) -> bool | None:
    """Reap ready children and report whether no child remains.

    ``True`` is an exact ``ECHILD`` proof, ``False`` means at least one live
    child remains, and ``None`` means the kernel state could not be queried.
    """
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        except OSError:
            return None
        if pid == 0:
            return False
        _record_payload_status(payload, pid, status)


def _open_payload_pidfd(
    payload: subprocess.Popen[bytes],
    owner: list[int | None] | None = None,
) -> int | None:
    """Open and publish exact signalling authority before callbacks resume."""
    pid = getattr(payload, "pid", None)
    if _PIDFD_OPEN is None or not isinstance(pid, int) or pid <= 0:
        return None
    published = owner if owner is not None else [None]
    state = _FdTransferState()

    def publish() -> None:
        result = state.result
        if not isinstance(result, int):
            raise RuntimeError("pidfd_open returned invalid authority")
        published[0] = result

    try:
        _run_fd_transfer(state, _PIDFD_OPEN, pid, 0, on_success=publish)
    except (OSError, ValueError):
        return None
    return published[0]


def _signal_payload(
    payload: subprocess.Popen[bytes],
    signum: int,
    *,
    payload_pidfd: int | None,
) -> None:
    """Signal only the exact unreaped payload incarnation."""
    if payload.returncode is not None:
        return
    try:
        if payload_pidfd is not None and _PIDFD_SEND_SIGNAL is not None:
            _PIDFD_SEND_SIGNAL(payload_pidfd, signum)
        else:
            # An unreaped child PID cannot be reused.  This fallback is used
            # only by the single supervisor thread, which records every reap.
            payload.send_signal(signum)
    except (ProcessLookupError, ChildProcessError):
        pass


def _require_descendant_cleanup_authority(*, subreaper: bool) -> None:
    """Reject isolation before provider spawn unless full-tree cleanup is owned.

    A direct-child PID cannot authorize signalling a daemonized descendant, and
    a later procfs/pidfd failure cannot be repaired after the provider has
    spawned.  The Linux subreaper contract is therefore the capability gate:
    unsupported hosts fail once, structurally, before any provider side effect.
    """
    if not subreaper:
        raise RuntimeError(_UNSUPPORTED_DESCENDANT_AUTHORITY)


def _claim_descendant_cleanup_authority() -> bool:
    """Acquire the complete cleanup capability before entering supervision."""
    try:
        subreaper = _install_parent_death_signal()
    except (OSError, ValueError) as exc:
        # prctl setup and the initial procfs proof are one public capability.
        # Their platform-specific diagnostics must not leak as distinct worker
        # protocols or allow payload creation after only partial acquisition.
        raise RuntimeError(_UNSUPPORTED_DESCENDANT_AUTHORITY) from exc
    _require_descendant_cleanup_authority(subreaper=subreaper)
    return subreaper


def _cleanup_process_tree(
    payload: subprocess.Popen[bytes],
    *,
    subreaper: bool,
    payload_pidfd: int | None = None,
) -> bool:
    """Bounded cleanup under the supervisor's established authority."""
    if not subreaper:
        # This primitive can safely reap only the unreaped direct child. The
        # supervisor capability gate forbids using it after provider spawn,
        # because it cannot prove that daemonized descendants are gone.
        if payload.poll() is None:
            payload.terminate()
            try:
                payload.wait(timeout=0.15)
            except subprocess.TimeoutExpired:
                payload.kill()
                try:
                    payload.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    return False
        return True

    # PR_SET_CHILD_SUBREAPER reparents daemonized grandchildren to this
    # supervisor. /proc lists only current unreaped direct children, so every
    # numeric PID signalled below remains reuse-safe.
    for signum, grace in ((signal.SIGTERM, 0.15), (signal.SIGKILL, 0.3)):
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                current = _owned_direct_children()
            except (OSError, ValueError):
                # Procfs can disappear after the capability was established.
                # Retain exact control of the known unreaped payload through
                # its pidfd (or its non-reusable child PID) and use ECHILD as
                # the only fallback proof that the entire subreaper tree is
                # gone.  A live unknown adopted child remains fail-closed.
                _signal_payload(
                    payload,
                    signum,
                    payload_pidfd=payload_pidfd,
                )
                if _reap_available(payload) is True:
                    return True
                time.sleep(0.01)
                continue
            if not current:
                return True
            _signal_children(current, signum)
            _reap_available(payload)
            time.sleep(0.01)
    no_children = _reap_available(payload)
    try:
        return not _owned_direct_children()
    except (OSError, ValueError):
        return no_children is True


def _settle_process_tree(
    payload: subprocess.Popen[bytes],
    *,
    subreaper: bool,
    payload_pidfd: int | None,
) -> tuple[BaseException, Any] | None:
    """Retain cleanup authority until absence is proved.

    Transient cleanup exceptions are remembered but never let the supervisor
    abandon a live tree.  The first one is returned only after cleanup proves
    success, allowing the caller to preserve an earlier body exception.
    """
    first_error: tuple[BaseException, Any] | None = None
    while True:
        try:
            if _cleanup_process_tree(
                payload,
                subreaper=subreaper,
                payload_pidfd=payload_pidfd,
            ):
                return first_error
        except BaseException as exc:
            if first_error is None:
                first_error = (exc, exc.__traceback__)
        time.sleep(0.01)


def _write_result_frame(result_file: Any, message: dict[str, Any]) -> None:
    """Write one bounded worker result to an already-owned stream."""
    audio = message.pop("audio", b"")
    if not isinstance(audio, bytes):
        audio = bytes(audio)
    if len(audio) > _MAX_AUDIO_SIZE:
        raise RuntimeError("TTS synth audio exceeds 64 MiB")
    message["audio_size"] = len(audio)
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_METADATA_SIZE:
        raise RuntimeError("TTS synth metadata exceeds 64 KiB")
    result_file.write(struct.pack("!I", len(payload)))
    result_file.write(payload)
    result_file.write(audio)


def _write_result(message: dict[str, Any]) -> None:
    fd_text = os.environ.get("HARK_TTS_RESULT_FD")
    if not fd_text:
        raise RuntimeError("missing HARK_TTS_RESULT_FD")
    with os.fdopen(int(fd_text), "wb", closefd=True) as result_file:
        _write_result_frame(result_file, message)


def _exception_message(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ProviderError):
        return {
            "status": "error",
            "kind": "provider",
            "message": str(exc),
            "code": exc.code,
        }
    message: dict[str, Any] = {
        "status": "error",
        "kind": "exception",
        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": str(exc),
    }
    return message


def _read_request(request_file: Any) -> dict[str, Any]:
    header = request_file.read(4)
    if len(header) != 4:
        raise ValueError("missing TTS synth request header")
    payload_size = struct.unpack("!I", header)[0]
    if payload_size > _MAX_METADATA_SIZE:
        raise ValueError("oversize TTS synth request")
    payload = request_file.read(payload_size)
    if len(payload) != payload_size:
        raise ValueError("truncated TTS synth request")
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid TTS synth request") from exc
    if not isinstance(request, dict):
        raise ValueError("invalid TTS synth request message")

    provider = request.get("provider")
    voice = request.get("voice")
    language = request.get("language")
    text = request.get("text")
    if not isinstance(provider, str):
        raise ValueError("invalid TTS synth request provider")
    if not isinstance(voice, str):
        raise ValueError("invalid TTS synth request voice")
    if language is not None and not isinstance(language, str):
        raise ValueError("invalid TTS synth request language")
    if not isinstance(text, str):
        raise ValueError("invalid TTS synth request text")
    return {
        "provider": provider,
        "voice": voice,
        "language": language,
        "text": text,
    }


def _run_test_mode(mode: str) -> int:
    if mode == "--test-success":
        _write_result(
            {
                "status": "ok",
                "audio": b"test-audio",
                "provider": "test-worker",
                "content_type": "audio/mpeg",
                "voice": "test-voice",
            }
        )
        return 0
    if mode == "--test-provider-error":
        _write_result(
            {
                "status": "error",
                "kind": "provider",
                "message": "worker provider failed",
                "code": 42,
            }
        )
        return 1
    if mode == "--test-unknown-error":
        _write_result(
            {
                "status": "error",
                "kind": "exception",
                "type": "test.UnknownFailure",
                "message": "unknown worker failure",
            }
        )
        return 1
    if mode == "--test-gil-hang":
        print(f"GIL_READY {os.getpid()}", flush=True)
        ctypes.PyDLL(None).sleep(10)
        return 1
    if mode == "--test-hang":
        print("APPEND_READY", flush=True)
        threading.Event().wait()
        return 1
    return 2


def _payload_main(args: list[str]) -> int:
    if args:
        return _run_test_mode(args[0])
    try:
        from hark.providers.resolve import resolve_tts

        request = _read_request(sys.stdin.buffer)
        provider = resolve_tts(
            request["provider"],
            voice=request["voice"],
            language=request.get("language"),
        )
        result = provider.synthesize(
            request["text"],
            voice=request["voice"],
        )
        _write_result(
            {
                "status": "ok",
                "audio": result.audio,
                "provider": result.provider,
                "content_type": result.content_type,
                "voice": result.voice or request["voice"],
            }
        )
        return 0
    except BaseException as exc:
        try:
            _write_result(_exception_message(exc))
        except BaseException:
            pass
        return 1


def _close_relay_stream(
    selector: selectors.BaseSelector,
    fileobj: Any,
) -> None:
    try:
        selector.unregister(fileobj)
    except (KeyError, ValueError):
        pass
    try:
        fileobj.close()
    except (AttributeError, OSError):
        pass


def _open_output_relay_fd(target_fd: int) -> int:
    """Open a private nonblocking writer for one inherited output endpoint.

    ``dup`` would share file-status flags with the caller's inherited open-file
    description. Reopening through procfs gives this supervisor independent
    ``O_NONBLOCK`` state, so provider backpressure cannot mutate the parent.
    """
    state = _FdTransferState()
    flags = (
        os.O_WRONLY
        | os.O_NONBLOCK
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        return _run_fd_transfer(
            state,
            os.open,
            f"/proc/self/fd/{target_fd}",
            flags,
        )
    except OSError:
        return -1
    except BaseException:
        relay_fd = state.result
        if isinstance(relay_fd, int) and relay_fd >= 0:
            try:
                os.close(relay_fd)
            except OSError:
                pass
        raise


def _write_output_nowait(target_fd: int, data: bytes) -> None:
    """Best-effort relay without letting caller backpressure own cleanup."""
    try:
        was_blocking = os.get_blocking(target_fd)
    except OSError:
        return
    try:
        if was_blocking:
            # Publish the restoration obligation before the fallible state
            # transition: set_blocking may apply O_NONBLOCK and then raise.
            os.set_blocking(target_fd, False)
        # A partial write is intentionally discardable. Provider logs are
        # diagnostic; process-tree cleanup and protocol EOF are mandatory.
        os.write(target_fd, data)
    except OSError:
        pass
    finally:
        if was_blocking:
            primary = sys.exception()
            try:
                _restore_blocking_mode(target_fd)
            except BaseException:
                # A transition/write failure is the first active primary.
                # Restoration must still finish, but its failure cannot mask
                # the exception whose unwind created the obligation.
                if primary is None:
                    raise


def _restore_blocking_mode(target_fd: int) -> None:
    """Reconcile one originally-blocking descriptor with bounded recovery."""
    first_error: BaseException | None = None
    verified_blocking = False

    for _ in range(_BLOCKING_RESTORE_ATTEMPTS):
        try:
            os.set_blocking(target_fd, True)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        try:
            verified_blocking = os.get_blocking(target_fd)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
            verified_blocking = False
        if verified_blocking:
            break

    # set_blocking can fail before applying its change, and get_blocking can
    # itself be unavailable during cleanup. Reconcile O_NONBLOCK through the
    # underlying flags as a final bounded fail-safe, then verify the live mode.
    try:
        flags = fcntl.fcntl(target_fd, fcntl.F_GETFL)
        if flags & os.O_NONBLOCK:
            fcntl.fcntl(target_fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
        verified_blocking = not bool(
            fcntl.fcntl(target_fd, fcntl.F_GETFL) & os.O_NONBLOCK
        )
    except BaseException as exc:
        if first_error is None:
            first_error = exc

    if not verified_blocking:
        if first_error is not None:
            raise first_error
        raise RuntimeError("could not restore blocking output descriptor")
    if first_error is not None:
        # Restoration did fail even though reconciliation repaired the mode.
        # Surface that truthful primary only after the invariant is restored.
        raise first_error


def _relay_payload_output(
    selector: selectors.BaseSelector,
    *,
    timeout: float = 0.01,
) -> None:
    for key, _ in selector.select(timeout=timeout):
        try:
            data = os.read(key.fd, _OUTPUT_CHUNK_SIZE)
        except OSError:
            data = b""
        if not data:
            _close_relay_stream(selector, key.fileobj)
            continue
        _write_output_nowait(key.data, data)


def _drain_payload_output(
    selector: selectors.BaseSelector,
    *,
    subreaper: bool,
) -> None:
    if subreaper:
        # Full-tree cleanup proves no descendant retains a writer, so EOF is
        # finite and completes the diagnostic relay.
        while selector.get_map():
            _relay_payload_output(selector)
        return

    # Portable hosts only own the direct child. A detached descendant may
    # retain the internal pipe writers forever, so bound the useful tail and
    # then sever the supervisor's private readers to release caller EOF.
    deadline = time.monotonic() + _PORTABLE_OUTPUT_DRAIN_SECONDS
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _relay_payload_output(selector, timeout=min(0.01, remaining))
    for key in list(selector.get_map().values()):
        _close_relay_stream(selector, key.fileobj)


def _result_fd_identity(fd: int) -> tuple[int, int, int, int]:
    stat = os.fstat(fd)
    return (stat.st_dev, stat.st_ino, stat.st_mode, stat.st_rdev)


class _RawResultFdGuard:
    """Own a raw result fd across explicit close/fdopen effect boundaries."""

    __slots__ = ("_committed", "_fd")

    def __init__(self, fd: int) -> None:
        _result_fd_identity(fd)
        self._fd = fd
        self._committed = True

    @classmethod
    def claim(cls, slots: list[Any], index: int) -> _RawResultFdGuard:
        """Move one mutable raw-fd slot into one reachable guard owner."""
        guard = object.__new__(cls)
        guard._committed = False
        fd = slots[index]
        if not isinstance(fd, int) or fd < 0:
            raise ValueError("raw result descriptor is already claimed")
        guard._fd = fd

        def publish() -> None:
            slots[index] = guard
            guard._committed = True

        try:
            _result_fd_identity(fd)
            _run_fd_transfer(_FdTransferState(), publish)
            return guard
        except BaseException:
            raise

    def __del__(self) -> None:
        if getattr(self, "_committed", False):
            try:
                self.close_if_owned()
            except BaseException:
                pass

    @property
    def fd(self) -> int:
        return self._fd

    def _reconcile_failed_transfer(self, fd: int, state: _FdTransferState) -> None:
        """Reclaim ownership only after a proven pre-effect failure."""
        if not state.effect_started:
            self._fd = fd

    def adopt(self) -> Any:
        fd = self._fd
        # Publish relinquishment before fdopen can adopt the resource. An
        # asynchronous exception after fdopen returns therefore cannot leave a
        # same-number foreign descriptor reachable through this guard.
        state = _FdTransferState()
        adopted = None
        try:
            self._fd = -1
            adopted = _RESULT_FDOPEN(fd, state, "wb", closefd=True)
            state.result = None
        except BaseException:
            primary = sys.exception()
            try:
                transfer_result = state.result
                state.result = None
                if transfer_result is not None:
                    transfer_result.close()
                if adopted is not None and adopted is not transfer_result:
                    adopted.close()
                self._reconcile_failed_transfer(fd, state)
            except BaseException:
                assert primary is not None
            raise
        return adopted

    def close_if_owned(self) -> None:
        fd = self._fd
        if fd < 0:
            return
        # Relinquish before close so a post-effect close failure cannot make a
        # later cleanup close a same-number replacement.
        state = _FdTransferState()
        try:
            self._fd = -1
            _RESULT_CLOSE(fd, state)
        except BaseException:
            primary = sys.exception()
            try:
                self._reconcile_failed_transfer(fd, state)
            except BaseException:
                assert primary is not None
            raise


class _ResultOwnerHandoff:
    """One mutable ownership slot shared by supervisor and entrypoint."""

    __slots__ = ("slot",)

    def __init__(self) -> None:
        self.slot: list[Any] = [_NO_RESULT_OWNER]


def _write_result_to_owner(
    message: dict[str, Any],
    owner: int | _RawResultFdGuard,
) -> None:
    """Serialize through the exact owner transferred out of the supervisor."""
    owners: list[Any] = [owner]
    guard: _RawResultFdGuard | None = None
    try:
        if isinstance(owner, _RawResultFdGuard):
            guard = owner
        else:
            guard = _RawResultFdGuard.claim(owners, 0)
            owners[0] = -1
        with guard.adopt() as result_file:
            _write_result_frame(result_file, message)
    finally:
        primary = sys.exception()
        try:
            if guard is not None:
                guard.close_if_owned()
            else:
                raw_owner = owners[0]
                if isinstance(raw_owner, _RawResultFdGuard):
                    owners[0] = -1
                    raw_owner.close_if_owned()
                elif isinstance(raw_owner, int) and raw_owner >= 0:
                    _RESULT_CLOSE(raw_owner, _FdTransferState())
        except BaseException:
            if primary is None:
                raise


def _forward_payload_result(
    payload_result: Any,
    parent_result_owner: int | _RawResultFdGuard,
) -> None:
    guard: _RawResultFdGuard | None = None
    raw_owners: list[Any] = [parent_result_owner]
    try:
        if isinstance(parent_result_owner, _RawResultFdGuard):
            guard = parent_result_owner
        else:
            guard = _RawResultFdGuard.claim(raw_owners, 0)
            raw_owners[0] = -1
        payload_result.flush()
        payload_result.seek(0)
        with guard.adopt() as parent_result:
            while chunk := payload_result.read(64 * 1024):
                parent_result.write(chunk)
    finally:
        primary = sys.exception()
        try:
            if guard is None:
                raw_owner = raw_owners[0]
                if isinstance(raw_owner, _RawResultFdGuard):
                    raw_owners[0] = -1
                    raw_owner.close_if_owned()
                elif isinstance(raw_owner, int) and raw_owner >= 0:
                    state = _FdTransferState()
                    _RESULT_CLOSE(raw_owner, state)
            else:
                guard.close_if_owned()
        except BaseException:
            if primary is None:
                raise


def _stage_payload_request(payload_request: Any) -> None:
    """Copy one bounded request off the parent-facing stdin descriptor."""
    header = sys.stdin.buffer.read(4)
    if len(header) != 4:
        raise ValueError("missing TTS synth request header")
    payload_size = struct.unpack("!I", header)[0]
    if payload_size > _MAX_METADATA_SIZE:
        raise ValueError("oversize TTS synth request")
    payload = sys.stdin.buffer.read(payload_size)
    if len(payload) != payload_size:
        raise ValueError("truncated TTS synth request")
    payload_request.write(header)
    payload_request.write(payload)
    payload_request.flush()
    payload_request.seek(0)


def _restore_signal_handler(
    signum: int,
    previous: Any,
) -> tuple[BaseException, Any] | None:
    """Restore process-global signal state and retain the first fault.

    A wrapper may raise either before changing the handler or after the kernel
    transition.  A live getsignal check resolves the latter ambiguity; proven
    pre-effect failures retry with the same desired handler.  The caller can
    preserve an existing body primary while still surfacing a cleanup-only
    failure after the invariant is restored.
    """
    first_error: tuple[BaseException, Any] | None = None
    while True:
        try:
            signal.signal(signum, previous)
        except BaseException as exc:
            if first_error is None:
                first_error = (exc, exc.__traceback__)
            try:
                current = signal.getsignal(signum)
            except BaseException as verify_exc:
                if first_error is None:
                    first_error = (verify_exc, verify_exc.__traceback__)
                continue
            try:
                restored = current is previous or current == previous
            except BaseException:
                restored = current is previous
            if restored:
                return first_error
            continue
        return first_error


def _supervise_payload(
    args: list[str],
    *,
    subreaper: bool,
    result_handoff: _ResultOwnerHandoff | None = None,
) -> int:
    """Exec provider work behind supervisor-only parent-facing descriptors."""
    # Establish honest detached-tree authority before installing handlers or
    # entering any provider-facing resource transition. Unsupported hosts
    # produce one stable structured worker failure with no payload side effect.
    _require_descendant_cleanup_authority(subreaper=subreaper)
    terminating = False
    payload: subprocess.Popen[bytes] | None = None
    payload_spawn_entered = False
    payload_pidfd_owner: list[int | None] = [None]
    output_relay_fds = [-1, -1]

    def request_termination(signum: int, frame: Any) -> None:
        nonlocal terminating
        terminating = True
        if payload is None and not payload_spawn_entered:
            # Before provider creation there is no tree cleanup to preserve.
            # Raising here breaks a blocking request read so the known-PID
            # supervisor can exit without requiring an unsafe SIGKILL.
            raise _SupervisorTermination

    previous_term = signal.getsignal(signal.SIGTERM)
    parent_result_owners = (
        result_handoff.slot if result_handoff is not None else [_NO_RESULT_OWNER]
    )
    parent_result_guard: _RawResultFdGuard | None = None
    selector: selectors.BaseSelector | None = None
    handler_setup_entered = False
    try:
        # Record the prior handler before the fallible installation call. A
        # trace exception after signal.signal returns can otherwise lose the
        # only value capable of restoring process-global signal state.
        handler_setup_entered = True
        signal.signal(signal.SIGTERM, request_termination)
        parent_result_text = os.environ.get("HARK_TTS_RESULT_FD")
        if not parent_result_text:
            raise RuntimeError("missing HARK_TTS_RESULT_FD")
        parent_result_owners[0] = int(parent_result_text)
        parent_result_guard = _RawResultFdGuard.claim(parent_result_owners, 0)
        if result_handoff is None:
            parent_result_owners[0] = -1
        selector = selectors.DefaultSelector()
    except BaseException:
        setup_primary = sys.exception()
        if selector is not None:
            try:
                selector.close()
            except BaseException:
                pass
        if result_handoff is None:
            raw_owner = parent_result_owners[0]
            try:
                if parent_result_guard is not None:
                    parent_result_guard.close_if_owned()
                elif isinstance(raw_owner, _RawResultFdGuard):
                    parent_result_owners[0] = -1
                    raw_owner.close_if_owned()
                elif isinstance(raw_owner, int) and raw_owner >= 0:
                    _RESULT_CLOSE(raw_owner, _FdTransferState())
            except BaseException:
                pass
        if handler_setup_entered:
            _restore_signal_handler(signal.SIGTERM, previous_term)
        assert setup_primary is not None
        raise

    assert selector is not None
    try:
        if terminating:
            return 128 + signal.SIGTERM
        with (
            tempfile.TemporaryFile() as payload_request,
            tempfile.TemporaryFile() as payload_result,
        ):
            _stage_payload_request(payload_request)
            if terminating:
                return 128 + signal.SIGTERM
            env = os.environ.copy()
            env["HARK_TTS_RESULT_FD"] = str(payload_result.fileno())
            env["HARK_TTS_PAYLOAD"] = "1"
            # Allocate the exact Popen authority before entering its fallible
            # initializer.  A wrapper may successfully create the child and
            # then raise; the preclaimed object still publishes its PID and
            # remains reachable to mandatory finalization.
            payload = _PAYLOAD_POPEN_CLASS.__new__(_PAYLOAD_POPEN_CLASS)
            payload_spawn_entered = True
            _PAYLOAD_POPEN_INIT(
                payload,
                [sys.executable, "-m", "hark.tts_worker", "--payload", *args],
                stdin=payload_request,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(payload_result.fileno(),),
                env=env,
            )
            _open_payload_pidfd(payload, payload_pidfd_owner)
            assert payload.stdout is not None
            assert payload.stderr is not None
            for index, (stream, target_fd) in enumerate(
                ((payload.stdout, 1), (payload.stderr, 2))
            ):
                os.set_blocking(stream.fileno(), False)
                output_relay_fds[index] = _open_output_relay_fd(target_fd)
                selector.register(
                    stream,
                    selectors.EVENT_READ,
                    output_relay_fds[index],
                )

            while payload.poll() is None and not terminating:
                _relay_payload_output(selector)

            cleanup_error = _settle_process_tree(
                payload,
                subreaper=subreaper,
                payload_pidfd=payload_pidfd_owner[0],
            )
            tree_gone = True
            if cleanup_error is not None:
                exc, traceback = cleanup_error
                raise exc.with_traceback(traceback)
            _drain_payload_output(selector, subreaper=subreaper)

            if not terminating and tree_gone:
                forwarding_guard = parent_result_guard
                parent_result_guard = None
                _forward_payload_result(payload_result, forwarding_guard)
                parent_result_owners[0] = -1
            payload_status = payload.returncode

        if terminating:
            return 128 + signal.SIGTERM
        if not tree_gone or payload_status is None:
            return 1
        return payload_status
    except _SupervisorTermination:
        return 128 + signal.SIGTERM
    finally:
        body_primary = sys.exception()
        cleanup_primary: tuple[BaseException, Any] | None = None

        def remember_cleanup_error(exc: BaseException) -> None:
            nonlocal cleanup_primary
            if body_primary is None and cleanup_primary is None:
                cleanup_primary = (exc, exc.__traceback__)

        try:
            selector.close()
        except BaseException as exc:
            remember_cleanup_error(exc)

        for index, relay_fd in enumerate(output_relay_fds):
            if relay_fd < 0:
                continue
            output_relay_fds[index] = -1
            try:
                os.close(relay_fd)
            except BaseException as exc:
                remember_cleanup_error(exc)

        payload_pid = getattr(payload, "pid", None) if payload is not None else None
        if (
            payload_spawn_entered
            and payload is not None
            and isinstance(payload_pid, int)
        ):
            if payload_pidfd_owner[0] is None:
                try:
                    _open_payload_pidfd(payload, payload_pidfd_owner)
                except BaseException as exc:
                    # The unreaped child PID remains exact authority when
                    # opening the stronger pidfd is interrupted.
                    remember_cleanup_error(exc)
            while True:
                try:
                    tree_error = _settle_process_tree(
                        payload,
                        subreaper=subreaper,
                        payload_pidfd=payload_pidfd_owner[0],
                    )
                except BaseException as exc:
                    # Never let a boundary fault abandon a live tree. Retain
                    # the first primary and retry with the same exact authority
                    # until absence is proved.
                    remember_cleanup_error(exc)
                    continue
                if tree_error is not None:
                    remember_cleanup_error(tree_error[0])
                break

        pidfd_close_error = _close_pidfd_owner(payload_pidfd_owner)
        if pidfd_close_error is not None:
            remember_cleanup_error(pidfd_close_error[0])
        restore_error = _restore_signal_handler(signal.SIGTERM, previous_term)
        if restore_error is not None:
            remember_cleanup_error(restore_error[0])

        if parent_result_guard is not None:
            transfer_for_error = result_handoff is not None and (
                body_primary is not None or cleanup_primary is not None
            )
            if not transfer_for_error:
                try:
                    parent_result_guard.close_if_owned()
                except BaseException as exc:
                    remember_cleanup_error(exc)
            if (
                result_handoff is not None
                and parent_result_guard.fd < 0
                and parent_result_owners[0] is parent_result_guard
            ):
                parent_result_owners[0] = -1

        if cleanup_primary is not None:
            exc, traceback = cleanup_primary
            raise exc.with_traceback(traceback)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--payload":
        return _payload_main(args[1:])
    result_handoff = _ResultOwnerHandoff()
    try:
        try:
            subreaper = _claim_descendant_cleanup_authority()
            return _supervise_payload(
                args,
                subreaper=subreaper,
                result_handoff=result_handoff,
            )
        except BaseException as exc:
            try:
                result_owner = result_handoff.slot[0]
                if result_owner is _NO_RESULT_OWNER:
                    _write_result(_exception_message(exc))
                elif (
                    isinstance(result_owner, _RawResultFdGuard) and result_owner.fd >= 0
                ) or (isinstance(result_owner, int) and result_owner >= 0):
                    _write_result_to_owner(_exception_message(exc), result_owner)
            except BaseException:
                pass
            return 1
    finally:
        while _DIRECT_CHILDREN_AUTHORITY_FD >= 0:
            try:
                _close_direct_children_authority()
            except BaseException:
                # Terminal authority cleanup is mandatory but cannot replace
                # an already serialized failure or deterministic return code.
                # Retry while the exact descriptor remains published; after
                # retirement a post-effect interrupt tail is safe to suppress.
                continue


if __name__ == "__main__":
    raise SystemExit(main())

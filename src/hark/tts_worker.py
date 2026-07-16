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
from typing import Any

from hark.providers.base import ProviderError


_PR_SET_PDEATHSIG = 1
_PR_SET_CHILD_SUBREAPER = 36


class _SupervisorTermination(BaseException):
    """Interrupt a supervisor that has not created provider state yet."""


_MAX_METADATA_SIZE = 64 * 1024
_MAX_AUDIO_SIZE = 64 * 1024 * 1024
_OUTPUT_CHUNK_SIZE = 64 * 1024
_PORTABLE_OUTPUT_DRAIN_SECONDS = 0.05
_BLOCKING_RESTORE_ATTEMPTS = 2
_RESULT_CLOSE = os.close
_RESULT_FDOPEN = os.fdopen
_UNSUPPORTED_DESCENDANT_AUTHORITY = (
    "isolated TTS requires exact descendant cleanup authority on this host"
)


def _install_parent_death_signal() -> bool:
    """Install Linux supervisor ownership; return false when unsupported."""
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
    # The parent owns cancellation and terminates this process through a
    # pidfd.  Preserve an inherited blocked SIGINT mask: a terminal killpg can
    # otherwise queue SIGINT during interpreter startup, then inject a raw
    # KeyboardInterrupt here when unblocked outside the protected protocol.
    return True


def _linux_direct_children() -> set[int]:
    children = f"/proc/{os.getpid()}/task/{os.getpid()}/children"
    with open(children, encoding="ascii") as child_file:
        return {int(value) for value in child_file.read().split()}


def _signal_children(pids: set[int], signum: int) -> None:
    for pid in pids:
        if pid == os.getpid():
            continue
        try:
            os.kill(pid, signum)
        except (ProcessLookupError, OSError):
            pass


def _reap_available() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except (ChildProcessError, OSError):
            return
        if pid <= 0:
            return


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
                current = _linux_direct_children()
            except (OSError, ValueError):
                time.sleep(0.01)
                continue
            if not current:
                return True
            _signal_children(current, signum)
            _reap_available()
            time.sleep(0.01)
    _reap_available()
    try:
        return not _linux_direct_children()
    except (OSError, ValueError):
        return False


def _write_result(message: dict[str, Any]) -> None:
    fd_text = os.environ.get("HARK_TTS_RESULT_FD")
    if not fd_text:
        raise RuntimeError("missing HARK_TTS_RESULT_FD")
    audio = message.pop("audio", b"")
    if not isinstance(audio, bytes):
        audio = bytes(audio)
    if len(audio) > _MAX_AUDIO_SIZE:
        raise RuntimeError("TTS synth audio exceeds 64 MiB")
    message["audio_size"] = len(audio)
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_METADATA_SIZE:
        raise RuntimeError("TTS synth metadata exceeds 64 KiB")
    with os.fdopen(int(fd_text), "wb", closefd=True) as result_file:
        result_file.write(struct.pack("!I", len(payload)))
        result_file.write(payload)
        result_file.write(audio)


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
    """Own a raw result fd until fdopen verifiably adopts that resource."""

    __slots__ = ("_fd", "_identity")

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._identity = _result_fd_identity(fd)

    def _reconcile_failed_adoption(self, fd: int) -> None:
        """Reclaim only an fd that still names the original resource."""
        try:
            current_identity = _result_fd_identity(fd)
        except OSError:
            # fdopen may have consumed and closed the descriptor. With no live
            # descriptor at this number there is no remaining raw ownership.
            return
        if current_identity == self._identity:
            self._fd = fd

    def adopt(self) -> Any:
        fd = self._fd
        # Publish relinquishment before fdopen can adopt the resource. An
        # asynchronous exception after fdopen returns therefore cannot leave a
        # same-number foreign descriptor reachable through this guard.
        self._fd = -1
        try:
            result = _RESULT_FDOPEN(fd, "wb", closefd=True)
        except BaseException:
            primary = sys.exception()
            try:
                self._reconcile_failed_adoption(fd)
            except BaseException:
                # Identity probing is cleanup; the fdopen failure stays first.
                assert primary is not None
            raise
        return result

    def close_if_owned(self) -> None:
        fd = self._fd
        if fd < 0:
            return
        # Relinquish before close so a post-effect close failure cannot make a
        # later cleanup close a same-number replacement.
        self._fd = -1
        _RESULT_CLOSE(fd)


def _forward_payload_result(payload_result: Any, parent_result_fd: int) -> None:
    guard: _RawResultFdGuard | None = None
    try:
        guard = _RawResultFdGuard(parent_result_fd)
        payload_result.flush()
        payload_result.seek(0)
        with guard.adopt() as parent_result:
            while chunk := payload_result.read(64 * 1024):
                parent_result.write(chunk)
    finally:
        primary = sys.exception()
        try:
            if guard is None:
                _RESULT_CLOSE(parent_result_fd)
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


def _supervise_payload(args: list[str], *, subreaper: bool) -> int:
    """Exec provider work behind supervisor-only parent-facing descriptors."""
    # Establish honest detached-tree authority before installing handlers or
    # entering any provider-facing resource transition. Unsupported hosts
    # produce one stable structured worker failure with no payload side effect.
    _require_descendant_cleanup_authority(subreaper=subreaper)
    terminating = False
    payload: subprocess.Popen[bytes] | None = None
    payload_spawn_entered = False

    def request_termination(signum: int, frame: Any) -> None:
        nonlocal terminating
        terminating = True
        if payload is None and not payload_spawn_entered:
            # Before provider creation there is no tree cleanup to preserve.
            # Raising here breaks a blocking request read so the known-PID
            # supervisor can exit without requiring an unsafe SIGKILL.
            raise _SupervisorTermination

    previous_term = signal.signal(signal.SIGTERM, request_termination)
    parent_result_text = os.environ.get("HARK_TTS_RESULT_FD")
    if not parent_result_text:
        raise RuntimeError("missing HARK_TTS_RESULT_FD")
    parent_result_fd = int(parent_result_text)
    selector = selectors.DefaultSelector()
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
            # After this publication, payload=None is child-creation
            # uncertainty. Retain the supervisor instead of claiming there is
            # no provider tree merely because Popen has not returned.
            payload_spawn_entered = True
            payload = subprocess.Popen(
                [sys.executable, "-m", "hark.tts_worker", "--payload", *args],
                stdin=payload_request,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(payload_result.fileno(),),
                env=env,
            )
            assert payload.stdout is not None
            assert payload.stderr is not None
            for stream, target_fd in ((payload.stdout, 1), (payload.stderr, 2)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, target_fd)

            while payload.poll() is None and not terminating:
                _relay_payload_output(selector)

            tree_gone = _cleanup_process_tree(
                payload,
                subreaper=subreaper,
            )
            while not tree_gone:
                # The parent treats supervisor reap as cleanup proof. Never
                # exit while a provider/descendant remains, even after rapid
                # repeated interrupts; retain the subreaper authority and
                # continue the reuse-safe TERM/KILL cleanup transaction.
                tree_gone = _cleanup_process_tree(
                    payload,
                    subreaper=subreaper,
                )
            _drain_payload_output(selector, subreaper=subreaper)

            if not terminating and tree_gone:
                forwarding_fd = parent_result_fd
                parent_result_fd = -1
                _forward_payload_result(payload_result, forwarding_fd)
            payload_status = payload.returncode

        if terminating:
            return 128 + signal.SIGTERM
        if not tree_gone or payload_status is None:
            return 1
        return payload_status
    except _SupervisorTermination:
        return 128 + signal.SIGTERM
    finally:
        selector.close()
        if payload is not None and payload.poll() is None:
            try:
                _cleanup_process_tree(
                    payload,
                    subreaper=subreaper,
                )
            except BaseException:
                pass
        if parent_result_fd >= 0:
            try:
                os.close(parent_result_fd)
            except OSError:
                pass
        signal.signal(signal.SIGTERM, previous_term)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--payload":
        return _payload_main(args[1:])
    try:
        subreaper = _claim_descendant_cleanup_authority()
        return _supervise_payload(args, subreaper=subreaper)
    except BaseException as exc:
        try:
            _write_result(_exception_message(exc))
        except BaseException:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

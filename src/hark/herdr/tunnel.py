"""Reusable SSH Unix-socket tunnels for configured remote Herdr sessions.

Crash-safe lifetime (B152)
--------------------------
A Hark-started SSH child is tracked by a durable owner marker that records the
exact child incarnation (pid + start-time + boot id) and a cleanup owner. Later
processes may borrow a live forward, and when the cleanup owner is dead a later
process may **adopt** cleanup ownership. Only a verified child incarnation is
ever signalled; foreign or unverifiable processes are left alone.

Cross-process lease files under the transport key decide when the final cleanup
owner may reap the child. In-process reference counting still collapses duplicate
leases inside one Python process.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import signal
import socket
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Any, Iterator

from hark.paths import cache_dir


_DEFAULT_REMOTE_SOCKET = "~/.config/herdr/herdr.sock"
# Darwin's sockaddr_un.sun_path is smaller than Linux's. Keep a conservative
# byte budget including room for the terminating NUL used by the kernel.
_MAX_UNIX_SOCKET_PATH_BYTES = 100
_SHORT_SOCKET_BASES = (Path("/tmp"), Path("/var/tmp"))
_OWNER_MARKER_VERSION = 1
_REAP_WAIT_S = 3.0
_REAP_POLL_S = 0.05


def _path_fits_unix_socket(path: Path) -> bool:
    return len(os.fsencode(path)) <= _MAX_UNIX_SOCKET_PATH_BYTES


def _transport_digest(session_id: str, ssh: str, remote: str) -> str:
    return hashlib.sha256(f"{session_id}\0{ssh}\0{remote}".encode()).hexdigest()[:16]


def _tunnel_socket_name(session_id: str, ssh: str, remote: str) -> str:
    safe_id = "".join(
        c if c.isascii() and (c.isalnum() or c in "-_") else "_" for c in session_id
    )
    prefix = (safe_id or "session")[:16]
    # Transport-stable name so a later process can find orphans to adopt/reap.
    return f"{prefix}-{_transport_digest(session_id, ssh, remote)}.sock"


def _short_tunnel_socket_path(
    preferred_root: Path,
    filename: str,
    *,
    session_id: str,
) -> Path:
    uid = os.getuid() if hasattr(os, "getuid") else "user"
    namespace = hashlib.sha256(os.fsencode(preferred_root)).hexdigest()[:8]
    for base in _SHORT_SOCKET_BASES:
        if not base.is_dir() or not os.access(base, os.W_OK | os.X_OK):
            continue
        candidate = base / f"hark-{uid}-{namespace}" / filename
        if _path_fits_unix_socket(candidate):
            return candidate
    raise RuntimeError(f"no AF_UNIX-safe tunnel path for Herdr session {session_id!r}")


def tunnel_socket_path(
    session_id: str,
    ssh: str,
    *,
    remote_socket: str | None = None,
) -> Path:
    """Return a deterministic, transport-stable, AF_UNIX-safe tunnel path."""
    remote = remote_socket or _DEFAULT_REMOTE_SOCKET
    filename = _tunnel_socket_name(session_id, ssh, remote)
    preferred_root = cache_dir()
    preferred = preferred_root / "tunnels" / filename
    if _path_fits_unix_socket(preferred):
        return preferred
    return _short_tunnel_socket_path(
        preferred_root,
        filename,
        session_id=session_id,
    )


def _owner_marker_path(local_socket: Path) -> Path:
    return local_socket.with_suffix(local_socket.suffix + ".owner.json")


def _lock_path(local_socket: Path) -> Path:
    return local_socket.with_suffix(local_socket.suffix + ".lock")


def _leases_dir(local_socket: Path) -> Path:
    return local_socket.with_suffix(local_socket.suffix + ".leases")


def _ensure_private_socket_parent(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlink tunnel directory: {path}")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(f"tunnel socket parent is not a directory: {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError(f"tunnel socket directory is not owned by this user: {path}")
    if info.st_mode & 0o077:
        path.chmod(0o700)


def _socket_is_live(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.2)
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# Process identity (pid + start-time + boot id) — never signal on PID alone
# ---------------------------------------------------------------------------


class _IdentityState(Enum):
    LIVE = auto()
    STALE = auto()
    UNVERIFIABLE = auto()


@dataclass(frozen=True)
class ProcessIdentity:
    """Exact process incarnation; safe authority for later signals."""

    pid: int
    start_time: str
    boot_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "boot_id": self.boot_id,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ProcessIdentity | None:
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        start_time = data.get("start_time")
        boot_id = data.get("boot_id")
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(start_time, str)
            or not start_time
            or not isinstance(boot_id, str)
            or not boot_id
        ):
            return None
        return cls(pid=pid, start_time=start_time, boot_id=boot_id)


def _boot_id() -> str | None:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8")
    except OSError:
        return None
    boot_id = value.strip()
    return boot_id or None


def _proc_start_time(pid: int) -> str | None:
    """Return Linux starttime ticks for *pid*, or None if the slot is empty."""
    if pid <= 0:
        return None
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, ProcessLookupError)) or exc.errno in {
            errno.ENOENT,
            errno.ESRCH,
        }:
            return None
        # Presence unknown — caller must treat as UNVERIFIABLE, not STALE.
        raise
    rparen = text.rfind(")")
    if rparen < 0:
        return None
    fields = text[rparen + 2 :].split()
    if len(fields) <= 19:
        return None
    if fields[0] in {"Z", "X", "x"}:
        return None
    return fields[19]


def capture_process_identity(pid: int) -> ProcessIdentity | None:
    """Capture the exact incarnation of *pid*, or None if it is gone."""
    boot_id = _boot_id()
    if boot_id is None:
        return None
    try:
        start_time = _proc_start_time(pid)
    except OSError:
        return None
    if start_time is None:
        return None
    # Re-read after boot id to shrink the swap window.
    try:
        again = _proc_start_time(pid)
    except OSError:
        return None
    if again != start_time:
        return None
    return ProcessIdentity(pid=pid, start_time=start_time, boot_id=boot_id)


def self_process_identity() -> ProcessIdentity:
    identity = capture_process_identity(os.getpid())
    if identity is None:
        raise RuntimeError("cannot capture self process identity for tunnel ownership")
    return identity


def identity_state(identity: ProcessIdentity) -> _IdentityState:
    """Classify a stored process identity without confusing absence with I/O failure."""
    boot_id = _boot_id()
    if boot_id is None:
        return _IdentityState.UNVERIFIABLE
    if boot_id != identity.boot_id:
        return _IdentityState.STALE
    try:
        start_time = _proc_start_time(identity.pid)
    except OSError:
        return _IdentityState.UNVERIFIABLE
    if start_time is None:
        return _IdentityState.STALE
    if start_time != identity.start_time:
        return _IdentityState.STALE
    return _IdentityState.LIVE


def _identity_is_live(identity: ProcessIdentity | None) -> bool:
    return identity is not None and identity_state(identity) is _IdentityState.LIVE


# ---------------------------------------------------------------------------
# Owner marker + lease registry
# ---------------------------------------------------------------------------


@dataclass
class OwnerMarker:
    version: int
    session_id: str
    ssh: str
    remote_socket: str
    local_socket: str
    child: ProcessIdentity
    cleanup_owner: ProcessIdentity

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "ssh": self.ssh,
            "remote_socket": self.remote_socket,
            "local_socket": self.local_socket,
            "child": self.child.to_dict(),
            "cleanup_owner": self.cleanup_owner.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> OwnerMarker | None:
        if not isinstance(data, dict):
            return None
        if data.get("version") != _OWNER_MARKER_VERSION:
            return None
        session_id = data.get("session_id")
        ssh = data.get("ssh")
        remote_socket = data.get("remote_socket")
        local_socket = data.get("local_socket")
        child = ProcessIdentity.from_dict(data.get("child"))
        cleanup_owner = ProcessIdentity.from_dict(data.get("cleanup_owner"))
        if (
            not isinstance(session_id, str)
            or not session_id
            or not isinstance(ssh, str)
            or not ssh
            or not isinstance(remote_socket, str)
            or not remote_socket
            or not isinstance(local_socket, str)
            or not local_socket
            or child is None
            or cleanup_owner is None
        ):
            return None
        return cls(
            version=_OWNER_MARKER_VERSION,
            session_id=session_id,
            ssh=ssh,
            remote_socket=remote_socket,
            local_socket=local_socket,
            child=child,
            cleanup_owner=cleanup_owner,
        )


def _read_owner_marker(path: Path) -> OwnerMarker | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return OwnerMarker.from_dict(data)


def _write_owner_marker(path: Path, marker: OwnerMarker) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = json.dumps(marker.to_dict(), separators=(",", ":"), sort_keys=True)
    temporary = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_owner_marker(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def _transport_lock(local_socket: Path) -> Iterator[None]:
    lock_path = _lock_path(local_socket)
    _ensure_private_socket_parent(lock_path.parent)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _lease_file_path(local_socket: Path, identity: ProcessIdentity) -> Path:
    name = f"{identity.pid}-{identity.start_time}.json"
    return _leases_dir(local_socket) / name


def _register_lease(local_socket: Path, identity: ProcessIdentity) -> Path:
    directory = _leases_dir(local_socket)
    _ensure_private_socket_parent(directory)
    path = _lease_file_path(local_socket, identity)
    payload = json.dumps(
        {"holder": identity.to_dict()},
        separators=(",", ":"),
        sort_keys=True,
    )
    path.write_text(payload, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _unregister_lease(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _live_foreign_leases(
    local_socket: Path, *, self_identity: ProcessIdentity
) -> list[ProcessIdentity]:
    """Return live lease holders other than *self_identity*."""
    directory = _leases_dir(local_socket)
    if not directory.is_dir():
        return []
    live: list[ProcessIdentity] = []
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        holder = None
        if isinstance(data, dict):
            holder = ProcessIdentity.from_dict(data.get("holder"))
        if holder is None:
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        state = identity_state(holder)
        if state is _IdentityState.LIVE:
            if (
                holder.pid == self_identity.pid
                and holder.start_time == self_identity.start_time
                and holder.boot_id == self_identity.boot_id
            ):
                continue
            live.append(holder)
            continue
        if state is _IdentityState.STALE:
            try:
                entry.unlink(missing_ok=True)
            except OSError:
                pass
        # UNVERIFIABLE: leave the lease file; fail closed (do not reap yet).
    return live


def _has_unverifiable_leases(local_socket: Path) -> bool:
    directory = _leases_dir(local_socket)
    if not directory.is_dir():
        return False
    try:
        entries = list(directory.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError):
            continue
        holder = (
            ProcessIdentity.from_dict(data.get("holder"))
            if isinstance(data, dict)
            else None
        )
        if holder is None:
            continue
        if identity_state(holder) is _IdentityState.UNVERIFIABLE:
            return True
    return False


# ---------------------------------------------------------------------------
# Verified signalling — foreign/unverifiable processes are never signalled
# ---------------------------------------------------------------------------


def _signal_verified(identity: ProcessIdentity, sig: int) -> bool:
    """Send *sig* only when *identity* still names the same incarnation.

    Returns True if the signal was delivered. Never signals when identity is
    stale or unverifiable. Prefers pidfd when available to close the PID-reuse
    race between verification and delivery.
    """
    if identity_state(identity) is not _IdentityState.LIVE:
        return False

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is not None and pidfd_send is not None:
        try:
            pidfd = pidfd_open(identity.pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            # Fall through to kill-with-recheck.
            pass
        else:
            try:
                if identity_state(identity) is not _IdentityState.LIVE:
                    return False
                try:
                    pidfd_send(pidfd, sig)
                except ProcessLookupError:
                    return False
                return True
            finally:
                try:
                    os.close(pidfd)
                except OSError:
                    pass

    # Fallback: re-verify immediately around kill(2). Without pidfd there is a
    # residual PID-reuse race between the last LIVE check and the kill; Linux
    # with pidfd is preferred and closes that window.
    if identity_state(identity) is not _IdentityState.LIVE:
        return False
    try:
        os.kill(identity.pid, sig)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    return True


def _reap_verified_child(
    identity: ProcessIdentity,
    *,
    proc: subprocess.Popen[bytes] | None = None,
) -> None:
    """Terminate and reap a verified child incarnation; never touch foreigners."""
    if identity_state(identity) is _IdentityState.UNVERIFIABLE:
        return
    if identity_state(identity) is _IdentityState.STALE:
        return

    _signal_verified(identity, signal.SIGTERM)
    deadline = time.monotonic() + _REAP_WAIT_S
    while time.monotonic() < deadline:
        if identity_state(identity) is not _IdentityState.LIVE:
            break
        if proc is not None:
            try:
                proc.wait(timeout=_REAP_POLL_S)
            except subprocess.TimeoutExpired:
                pass
            else:
                break
        else:
            time.sleep(_REAP_POLL_S)
    if identity_state(identity) is _IdentityState.LIVE:
        _signal_verified(identity, signal.SIGKILL)
        if proc is not None:
            try:
                proc.wait(timeout=_REAP_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
        else:
            deadline = time.monotonic() + _REAP_WAIT_S
            while time.monotonic() < deadline:
                if identity_state(identity) is not _IdentityState.LIVE:
                    break
                time.sleep(_REAP_POLL_S)


def _unlink_socket_if_present(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        if stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _cleanup_transport_artifacts(local_socket: Path) -> None:
    _unlink_socket_if_present(local_socket)
    _remove_owner_marker(_owner_marker_path(local_socket))
    leases = _leases_dir(local_socket)
    if leases.is_dir():
        try:
            for entry in leases.iterdir():
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass
            leases.rmdir()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Tunnel handle
# ---------------------------------------------------------------------------


@dataclass
class Tunnel:
    session_id: str
    ssh: str
    local_socket: Path
    remote_socket: str
    proc: subprocess.Popen[bytes] | None = None
    child_identity: ProcessIdentity | None = None
    cleanup_owner: ProcessIdentity | None = None
    owns_cleanup: bool = False
    lease_path: Path | None = None
    holder_identity: ProcessIdentity | None = None

    def start(self) -> Path:
        """Spawn SSH and publish an owner marker for the exact child incarnation."""
        _ensure_private_socket_parent(self.local_socket.parent)
        if os.path.lexists(self.local_socket):
            mode = self.local_socket.lstat().st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(
                    f"refusing non-socket tunnel path for {self.session_id!r}: "
                    f"{self.local_socket}"
                )
            if _socket_is_live(self.local_socket):
                raise RuntimeError(
                    f"refusing unowned live tunnel path for {self.session_id!r}: "
                    f"{self.local_socket}"
                )
            self.local_socket.unlink()

        command = [
            "ssh",
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "BatchMode=yes",
            "-L",
            f"{self.local_socket}:{self.remote_socket}",
            self.ssh,
        ]
        self.proc = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            for _ in range(50):
                if self.proc.poll() is not None:
                    raw = self.proc.stderr.read() if self.proc.stderr else b""
                    error = raw.decode(errors="replace")[:400]
                    raise RuntimeError(
                        f"SSH tunnel failed for {self.session_id!r}: "
                        f"{error or f'exit {self.proc.returncode}'}"
                    )
                if os.path.lexists(self.local_socket):
                    mode = self.local_socket.lstat().st_mode
                    if not stat.S_ISSOCK(mode):
                        raise RuntimeError(
                            f"SSH tunnel path for {self.session_id!r} is not a Unix socket"
                        )
                    child = capture_process_identity(self.proc.pid)
                    if child is None:
                        raise RuntimeError(
                            f"SSH tunnel for {self.session_id!r} lost child identity "
                            "before owner publication"
                        )
                    owner = self.holder_identity or self_process_identity()
                    marker = OwnerMarker(
                        version=_OWNER_MARKER_VERSION,
                        session_id=self.session_id,
                        ssh=self.ssh,
                        remote_socket=self.remote_socket,
                        local_socket=str(self.local_socket),
                        child=child,
                        cleanup_owner=owner,
                    )
                    _write_owner_marker(_owner_marker_path(self.local_socket), marker)
                    self.child_identity = child
                    self.cleanup_owner = owner
                    self.owns_cleanup = True
                    return self.local_socket
                time.sleep(0.1)
            raise RuntimeError(f"SSH tunnel timeout for {self.session_id!r}")
        except BaseException:
            # Crash / failure before or after marker: only reap if we still own
            # a verified child. Unverifiable processes are never signalled.
            self._abort_start()
            raise

    def _abort_start(self) -> None:
        if self.proc is not None:
            child = self.child_identity
            if child is None and self.proc.pid:
                child = capture_process_identity(self.proc.pid)
            if child is not None:
                _reap_verified_child(child, proc=self.proc)
            elif self.proc.poll() is None:
                # We parented this Popen; terminate via the handle without
                # claiming foreign-PID authority.
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    try:
                        self.proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            self.proc = None
        if self.owns_cleanup or self.child_identity is not None:
            _cleanup_transport_artifacts(self.local_socket)
        elif os.path.lexists(self.local_socket) and not _socket_is_live(
            self.local_socket
        ):
            _unlink_socket_if_present(self.local_socket)

    def is_live(self) -> bool:
        if not (
            os.path.lexists(self.local_socket)
            and stat.S_ISSOCK(self.local_socket.lstat().st_mode)
            and _socket_is_live(self.local_socket)
        ):
            return False
        if self.proc is not None and self.proc.poll() is not None:
            return False
        if self.child_identity is not None:
            return _identity_is_live(self.child_identity)
        return self.proc is not None and self.proc.poll() is None

    def adopt_cleanup(self, owner: ProcessIdentity) -> bool:
        """Become cleanup owner when the previous owner is dead.

        Returns True when this handle now owns cleanup. Foreign/unverifiable
        children are never claimed for signalling.
        """
        marker_path = _owner_marker_path(self.local_socket)
        marker = _read_owner_marker(marker_path)
        if marker is None or marker.child is None:
            return False
        child_state = identity_state(marker.child)
        if child_state is _IdentityState.UNVERIFIABLE:
            return False
        if child_state is _IdentityState.STALE:
            return False
        cleanup_state = identity_state(marker.cleanup_owner)
        if cleanup_state is _IdentityState.LIVE:
            # Another live process still owns cleanup.
            self.child_identity = marker.child
            self.cleanup_owner = marker.cleanup_owner
            self.owns_cleanup = (
                marker.cleanup_owner.pid == owner.pid
                and marker.cleanup_owner.start_time == owner.start_time
                and marker.cleanup_owner.boot_id == owner.boot_id
            )
            return self.owns_cleanup
        if cleanup_state is _IdentityState.UNVERIFIABLE:
            # Fail closed: do not steal cleanup while identity cannot be checked.
            self.child_identity = marker.child
            self.cleanup_owner = marker.cleanup_owner
            self.owns_cleanup = False
            return False

        # Cleanup owner is stale — adopt.
        adopted = OwnerMarker(
            version=_OWNER_MARKER_VERSION,
            session_id=marker.session_id,
            ssh=marker.ssh,
            remote_socket=marker.remote_socket,
            local_socket=marker.local_socket,
            child=marker.child,
            cleanup_owner=owner,
        )
        _write_owner_marker(marker_path, adopted)
        self.child_identity = marker.child
        self.cleanup_owner = owner
        self.owns_cleanup = True
        return True

    def stop(self, *, final_lease: bool = True) -> None:
        """Release this handle.

        When *final_lease* is True and this process owns cleanup, reap the
        verified child only if no other live cross-process leases remain.
        """
        holder = self.holder_identity
        if final_lease and holder is not None:
            _unregister_lease(self.lease_path)
            self.lease_path = None

        if not final_lease:
            return

        if not self.owns_cleanup:
            # Borrowers never signal. If cleanup owner died under us, a later
            # ensure/stop that adopts will reap.
            return

        marker_path = _owner_marker_path(self.local_socket)
        marker = _read_owner_marker(marker_path)
        if marker is not None and holder is not None:
            if not (
                marker.cleanup_owner.pid == holder.pid
                and marker.cleanup_owner.start_time == holder.start_time
                and marker.cleanup_owner.boot_id == holder.boot_id
            ):
                # Lost cleanup ownership to another adopter.
                self.owns_cleanup = False
                return

        if holder is not None:
            foreign = _live_foreign_leases(self.local_socket, self_identity=holder)
            if foreign:
                # Transfer cleanup ownership so a surviving borrower reaps later.
                if marker is not None:
                    transferred = OwnerMarker(
                        version=_OWNER_MARKER_VERSION,
                        session_id=marker.session_id,
                        ssh=marker.ssh,
                        remote_socket=marker.remote_socket,
                        local_socket=marker.local_socket,
                        child=marker.child,
                        cleanup_owner=foreign[0],
                    )
                    _write_owner_marker(marker_path, transferred)
                self.owns_cleanup = False
                self.cleanup_owner = foreign[0]
                return
            if _has_unverifiable_leases(self.local_socket):
                # Fail closed: do not reap while another holder might still exist.
                return

        child = marker.child if marker is not None else self.child_identity

        if child is not None:
            _reap_verified_child(child, proc=self.proc)
        elif self.proc is not None and self.proc.poll() is None:
            # Parent-only fallback for the narrow window before identity capture.
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

        _cleanup_transport_artifacts(self.local_socket)
        self.proc = None
        self.owns_cleanup = False


@dataclass
class _TunnelRecord:
    tunnel: Tunnel
    references: int = 1


@dataclass
class TunnelLease:
    """A reference-counted handle to one process-local tunnel record."""

    _key: tuple[str, str, str]
    _record: _TunnelRecord
    _released: bool = False

    @property
    def local_socket(self) -> Path:
        return self._record.tunnel.local_socket

    @property
    def owns_cleanup(self) -> bool:
        return self._record.tunnel.owns_cleanup

    def stop(self) -> None:
        with _TUNNEL_LOCK:
            if self._released:
                return
            self._released = True
            record = _TUNNELS.get(self._key)
            if record is not self._record:
                return
            record.references -= 1
            if record.references > 0:
                # Other in-process leases remain; keep the child and lease file.
                return
            try:
                with _transport_lock(record.tunnel.local_socket):
                    # Last in-process lease: attempt adoption if cleanup owner died,
                    # then reap only when we own cleanup and no foreign leases live.
                    holder = record.tunnel.holder_identity or self_process_identity()
                    if not record.tunnel.owns_cleanup:
                        record.tunnel.adopt_cleanup(holder)
                    record.tunnel.stop(final_lease=True)
            except BaseException:
                record.references = 1
                self._released = False
                raise
            _TUNNELS.pop(self._key, None)


_TUNNEL_LOCK = threading.RLock()
_TUNNELS: dict[tuple[str, str, str], _TunnelRecord] = {}


def _attach_existing_tunnel(
    *,
    session_id: str,
    ssh: str,
    remote: str,
    local_socket: Path,
    holder: ProcessIdentity,
) -> Tunnel | None:
    """Borrow or adopt a live transport under the caller's transport lock."""
    marker_path = _owner_marker_path(local_socket)
    marker = _read_owner_marker(marker_path)
    if marker is None:
        return None
    if (
        marker.session_id != session_id
        or marker.ssh != ssh
        or marker.remote_socket != remote
    ):
        return None

    child_state = identity_state(marker.child)
    if child_state is _IdentityState.UNVERIFIABLE:
        # Fail closed: leave the foreign-looking process alone.
        return None
    if child_state is _IdentityState.STALE:
        # Child gone — clear stale artifacts only (no signal).
        _cleanup_transport_artifacts(local_socket)
        return None
    if not (
        os.path.lexists(local_socket)
        and stat.S_ISSOCK(local_socket.lstat().st_mode)
        and _socket_is_live(local_socket)
    ):
        # Marker names a live SSH but the forward is not usable — reap verified child.
        _reap_verified_child(marker.child)
        _cleanup_transport_artifacts(local_socket)
        return None

    tunnel = Tunnel(
        session_id=session_id,
        ssh=ssh,
        local_socket=local_socket,
        remote_socket=remote,
        proc=None,
        child_identity=marker.child,
        cleanup_owner=marker.cleanup_owner,
        owns_cleanup=False,
        holder_identity=holder,
    )
    cleanup_state = identity_state(marker.cleanup_owner)
    if cleanup_state is _IdentityState.LIVE:
        tunnel.owns_cleanup = (
            marker.cleanup_owner.pid == holder.pid
            and marker.cleanup_owner.start_time == holder.start_time
            and marker.cleanup_owner.boot_id == holder.boot_id
        )
    elif cleanup_state is _IdentityState.STALE:
        tunnel.adopt_cleanup(holder)
    else:
        # UNVERIFIABLE cleanup owner: borrow only; do not adopt or signal.
        tunnel.owns_cleanup = False

    tunnel.lease_path = _register_lease(local_socket, holder)
    return tunnel


def ensure_tunnel(
    session_id: str,
    ssh: str,
    *,
    remote_socket: str | None = None,
) -> TunnelLease:
    """Establish, borrow, or adopt the tunnel for one exact configured transport."""
    remote = remote_socket or _DEFAULT_REMOTE_SOCKET
    key = (session_id, ssh, remote)
    with _TUNNEL_LOCK:
        record = _TUNNELS.get(key)
        if record is not None:
            if record.tunnel.is_live():
                record.references += 1
                return TunnelLease(key, record)
            # A dead cached process is never a reusable tunnel.
            _TUNNELS.pop(key, None)
            try:
                with _transport_lock(record.tunnel.local_socket):
                    holder = record.tunnel.holder_identity or self_process_identity()
                    if not record.tunnel.owns_cleanup:
                        record.tunnel.adopt_cleanup(holder)
                    record.tunnel.stop(final_lease=True)
            except BaseException:
                _TUNNELS[key] = record
                raise

        local_socket = tunnel_socket_path(
            session_id,
            ssh,
            remote_socket=remote,
        )
        holder = self_process_identity()
        with _transport_lock(local_socket):
            attached = _attach_existing_tunnel(
                session_id=session_id,
                ssh=ssh,
                remote=remote,
                local_socket=local_socket,
                holder=holder,
            )
            if attached is not None:
                record = _TunnelRecord(tunnel=attached)
                _TUNNELS[key] = record
                return TunnelLease(key, record)

            # No reusable tunnel. Drop only clearly stale, non-live paths.
            if os.path.lexists(local_socket) and not _socket_is_live(local_socket):
                marker = _read_owner_marker(_owner_marker_path(local_socket))
                if marker is not None:
                    child_state = identity_state(marker.child)
                    if child_state is _IdentityState.LIVE:
                        _reap_verified_child(marker.child)
                    elif child_state is _IdentityState.UNVERIFIABLE:
                        raise RuntimeError(
                            f"refusing to replace unverifiable tunnel for "
                            f"{session_id!r}: {local_socket}"
                        )
                _cleanup_transport_artifacts(local_socket)

            tunnel = Tunnel(
                session_id=session_id,
                ssh=ssh,
                local_socket=local_socket,
                remote_socket=remote,
                holder_identity=holder,
            )
            tunnel.start()
            tunnel.lease_path = _register_lease(local_socket, holder)
            record = _TunnelRecord(tunnel=tunnel)
            _TUNNELS[key] = record
            return TunnelLease(key, record)

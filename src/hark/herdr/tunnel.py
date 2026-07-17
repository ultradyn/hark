"""Reusable SSH Unix-socket tunnels for configured remote Herdr sessions."""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from hark.paths import cache_dir


_DEFAULT_REMOTE_SOCKET = "~/.config/herdr/herdr.sock"
# Darwin's sockaddr_un.sun_path is smaller than Linux's. Keep a conservative
# byte budget including room for the terminating NUL used by the kernel.
_MAX_UNIX_SOCKET_PATH_BYTES = 100
_SHORT_SOCKET_BASES = (Path("/tmp"), Path("/var/tmp"))


def _path_fits_unix_socket(path: Path) -> bool:
    return len(os.fsencode(path)) <= _MAX_UNIX_SOCKET_PATH_BYTES


def _tunnel_socket_name(session_id: str, ssh: str, remote: str) -> str:
    safe_id = "".join(
        c if c.isascii() and (c.isalnum() or c in "-_") else "_"
        for c in session_id
    )
    prefix = (safe_id or "session")[:16]
    identity = hashlib.sha256(
        f"{session_id}\0{ssh}\0{remote}".encode()
    ).hexdigest()[:16]
    return f"{prefix}-{os.getpid()}-{identity}.sock"


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
    raise RuntimeError(
        f"no AF_UNIX-safe tunnel path for Herdr session {session_id!r}"
    )


def tunnel_socket_path(
    session_id: str,
    ssh: str,
    *,
    remote_socket: str | None = None,
) -> Path:
    """Return a deterministic, process-scoped, AF_UNIX-safe tunnel path."""
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


@dataclass
class Tunnel:
    session_id: str
    ssh: str
    local_socket: Path
    remote_socket: str
    proc: subprocess.Popen[bytes] | None = None
    owns_socket: bool = True

    def start(self) -> Path:
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
                    return self.local_socket
                time.sleep(0.1)
            raise RuntimeError(f"SSH tunnel timeout for {self.session_id!r}")
        except BaseException:
            self.stop()
            raise

    def is_live(self) -> bool:
        return (
            self.proc is not None
            and self.proc.poll() is None
            and os.path.lexists(self.local_socket)
            and stat.S_ISSOCK(self.local_socket.lstat().st_mode)
            and _socket_is_live(self.local_socket)
        )

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        if self.owns_socket and os.path.lexists(self.local_socket):
            try:
                if stat.S_ISSOCK(self.local_socket.lstat().st_mode):
                    self.local_socket.unlink()
            except FileNotFoundError:
                pass


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
                return
            try:
                record.tunnel.stop()
            except BaseException:
                record.references = 1
                self._released = False
                raise
            _TUNNELS.pop(self._key, None)


_TUNNEL_LOCK = threading.RLock()
_TUNNELS: dict[tuple[str, str, str], _TunnelRecord] = {}


def ensure_tunnel(
    session_id: str,
    ssh: str,
    *,
    remote_socket: str | None = None,
) -> TunnelLease:
    """Establish or reuse the tunnel for one exact configured transport."""
    remote = remote_socket or _DEFAULT_REMOTE_SOCKET
    key = (session_id, ssh, remote)
    with _TUNNEL_LOCK:
        record = _TUNNELS.get(key)
        if record is not None:
            if record.tunnel.is_live():
                record.references += 1
                return TunnelLease(key, record)
            # A dead cached process is never a reusable tunnel. Remove its
            # process-scoped socket before replacing the registry record.
            _TUNNELS.pop(key, None)
            try:
                record.tunnel.stop()
            except BaseException:
                _TUNNELS[key] = record
                raise

        tunnel = Tunnel(
            session_id=session_id,
            ssh=ssh,
            local_socket=tunnel_socket_path(
                session_id,
                ssh,
                remote_socket=remote,
            ),
            remote_socket=remote,
        )
        tunnel.start()
        record = _TunnelRecord(tunnel=tunnel)
        _TUNNELS[key] = record
        return TunnelLease(key, record)

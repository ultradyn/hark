"""Durable answer-window audio recovery (B178).

Persists accepted microphone PCM continuously under a private state directory
so abnormal interruption (SIGINT/SIGTERM, harness timeout, crash, parent death)
loses at most a few seconds of audio — not an entire multi-minute answer.

Layout (mode ``0700`` dirs, ``0600`` files)::

    $XDG_STATE_HOME/hark/recover/streams/<stream_id>/
      meta.json   # identity, format, status, counters (no secrets)
      audio.pcm   # append-only s16le mono PCM

Retention
---------
- **Active / incomplete** streams stay on disk until the operator discards them
  or they exceed :data:`DEFAULT_RETENTION_DAYS` (purged by doctor / recover purge).
- **Successful completion** (clean listen finish with intentional end) removes
  the stream directory immediately.
- Partial STT text may be recorded for inspection; never store API keys or full
  unbound free-text secrets beyond short optional previews.

Privacy
-------
Recovery audio is operator speech. Keep the recover tree user-private. Do not
log paths that embed stream IDs into shared Monitor feeds beyond local syslog.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from hark.paths import state_dir


def _write_wav_bytes(pcm16: bytes, sample_rate: int = 16000) -> bytes:
    # Late import avoids capture↔recovery cycle at module import time.
    from hark.audio.capture import write_wav_bytes

    return write_wav_bytes(pcm16, sample_rate)

SCHEMA_VERSION = 1
DEFAULT_FLUSH_INTERVAL_S = 1.0
DEFAULT_RETENTION_DAYS = 7.0
# Hard cap on unflushed buffer even if wall-clock flush is delayed.
DEFAULT_MAX_BUFFER_BYTES = 16000 * 2 * 3  # ~3 s @ 16 kHz mono s16le
SAMPLE_FORMAT = "s16le"
BYTES_PER_SAMPLE = 2

_active_lock = threading.RLock()
_active_sessions: dict[str, "StreamRecoverySession"] = {}


def recover_root(*, root: Path | None = None) -> Path:
    """Private recover root: ``<state>/recover``."""
    base = root if root is not None else state_dir()
    return base / "recover"


def streams_dir(*, root: Path | None = None) -> Path:
    return recover_root(root=root) / "streams"


def stream_dir(stream_id: str, *, root: Path | None = None) -> Path:
    sid = _safe_stream_id(stream_id)
    return streams_dir(root=root) / sid


def _safe_stream_id(stream_id: str) -> str:
    sid = (stream_id or "").strip()
    if not sid or sid in (".", "..") or "/" in sid or "\\" in sid or "\x00" in sid:
        raise ValueError(f"invalid stream_id: {stream_id!r}")
    # Stream ids are hex-ish; reject path traversal leftovers.
    if any(part in (".", "..") for part in Path(sid).parts):
        raise ValueError(f"invalid stream_id: {stream_id!r}")
    return sid


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    """Finish short writes without silently dropping accepted microphone PCM."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("recovery write made no progress")
        view = view[written:]


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary: Path | None = None
    try:
        # Named temp beside target so replace is atomic on same filesystem.
        tmp_name = f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        temporary = path.parent / tmp_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(temporary), flags, 0o600)
        try:
            _write_all(fd, body.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        temporary = None
        try:
            path.chmod(0o600)
        except OSError:
            pass
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _pid_alive(pid: int | None) -> bool | None:
    if pid is None or pid <= 0:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


@dataclass(frozen=True)
class RecoveredStream:
    """Read-only view of one on-disk recovery stream."""

    stream_id: str
    path: Path
    meta: dict[str, Any]
    pcm_path: Path
    pcm_bytes: int
    duration_ms: int
    orphaned: bool

    def read_pcm(self) -> bytes:
        if not self.pcm_path.is_file():
            return b""
        return self.pcm_path.read_bytes()

    def read_wav(self) -> bytes:
        rate = int(self.meta.get("sample_rate") or 16000)
        return _write_wav_bytes(self.read_pcm(), rate)

    def to_public_dict(self) -> dict[str, Any]:
        m = self.meta
        return {
            "stream_id": self.stream_id,
            "path": str(self.path),
            "status": m.get("status") or "unknown",
            "mode": m.get("mode"),
            "sample_rate": m.get("sample_rate"),
            "sample_format": m.get("sample_format") or SAMPLE_FORMAT,
            "started_at": m.get("started_at"),
            "updated_at": m.get("updated_at"),
            "pid": m.get("pid"),
            "pid_alive": _pid_alive(m.get("pid") if isinstance(m.get("pid"), int) else None),
            "bytes": self.pcm_bytes,
            "duration_ms": self.duration_ms,
            "segment_count": m.get("segment_count") or 0,
            "partial_text": m.get("partial_text") or "",
            "end_reason": m.get("end_reason"),
            "orphaned": self.orphaned,
            "flush_interval_s": m.get("flush_interval_s"),
            "recover_cmds": {
                "show": f"hark recover show {self.stream_id}",
                "export": f"hark recover export {self.stream_id}",
                "transcribe": f"hark recover transcribe {self.stream_id}",
                "discard": f"hark recover discard {self.stream_id}",
            },
        }


class StreamRecoverySession:
    """Append-only durable sink for one answer-window capture.

    Thread-safe for the capture loop + signal-time flush. Writes are best-effort:
    I/O errors never abort listen; they are swallowed after a single syslog.
    """

    def __init__(
        self,
        stream_id: str,
        *,
        sample_rate: int = 16000,
        mode: str = "radio",
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
        root: Path | None = None,
        pid: int | None = None,
    ) -> None:
        self.stream_id = _safe_stream_id(stream_id)
        self.sample_rate = int(sample_rate) if sample_rate > 0 else 16000
        self.mode = str(mode or "radio")
        self.flush_interval_s = max(0.05, float(flush_interval_s))
        self.max_buffer_bytes = max(BYTES_PER_SAMPLE, int(max_buffer_bytes))
        self.root = root
        self.dir = stream_dir(self.stream_id, root=root)
        self.meta_path = self.dir / "meta.json"
        self.pcm_path = self.dir / "audio.pcm"
        self.pid = int(pid) if pid is not None else os.getpid()

        self._lock = threading.RLock()
        self._buf = bytearray()
        self._bytes_written = 0
        self._last_flush_mono = time.monotonic()
        self._pcm_fd: int | None = None
        self._status = "active"
        self._segment_count = 0
        self._partial_text = ""
        self._end_reason: str | None = None
        self._started_at = time.time()
        self._closed = False
        self._io_failed = False
        self._registered = False

    def __enter__(self) -> "StreamRecoverySession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Own failures entering mic/media contexts before capture's finally.
        self.close_retain(reason="abnormal_exit" if exc_type else "incomplete")

    # --- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Create private dir, open append fd, publish meta, register active."""
        with self._lock:
            if self._closed:
                raise RuntimeError("recovery session already closed")
            try:
                _ensure_private_dir(recover_root(root=self.root))
                _ensure_private_dir(streams_dir(root=self.root))
                _ensure_private_dir(self.dir)
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
                fd = os.open(str(self.pcm_path), flags, 0o600)
                try:
                    os.chmod(self.pcm_path, 0o600)
                except OSError:
                    pass
                self._pcm_fd = fd
                # Existing file (restart same id): count prior bytes.
                try:
                    self._bytes_written = int(self.pcm_path.stat().st_size)
                except OSError:
                    self._bytes_written = 0
                self._write_meta_unlocked()
                self._register_unlocked()
            except OSError:
                self._io_failed = True
                self._close_fd_unlocked()
                # Leave any partial dir; next open may heal.

    def append_pcm(self, pcm16: bytes) -> None:
        """Accept mono s16le bytes (already gate-accepted speech)."""
        if not pcm16:
            return
        with self._lock:
            if self._closed or self._pcm_fd is None or self._io_failed:
                return
            self._buf.extend(pcm16)
            now = time.monotonic()
            overdue = (now - self._last_flush_mono) >= self.flush_interval_s
            oversized = len(self._buf) >= self.max_buffer_bytes
            if overdue or oversized:
                self._flush_unlocked(force=True)

    def note_segment(
        self,
        *,
        partial_text: str | None = None,
        force_flush: bool = True,
    ) -> None:
        """Mark a completed radio/silence segment boundary."""
        with self._lock:
            if self._closed:
                return
            self._segment_count += 1
            if partial_text is not None:
                self._partial_text = _clip_text(partial_text)
            if force_flush:
                self._flush_unlocked(force=True)
            else:
                self._write_meta_unlocked()

    def set_partial_text(self, text: str | None) -> None:
        with self._lock:
            if self._closed:
                return
            self._partial_text = _clip_text(text or "")
            # Meta only — do not force audio flush on every partial.
            try:
                self._write_meta_unlocked()
            except OSError:
                pass

    def flush(self, *, force: bool = True, blocking: bool = True) -> None:
        """Push buffer to disk (signal-safe best-effort when *blocking* is False)."""
        if blocking:
            with self._lock:
                self._flush_unlocked(force=force)
            return
        # Signal path: never block on the capture thread's lock.
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._flush_unlocked(force=force)
        finally:
            self._lock.release()

    def mark_interrupted(self, *, reason: str | None = None) -> None:
        """Flush and mark incomplete so doctor/recover can surface it."""
        with self._lock:
            if self._closed:
                return
            self._status = "interrupted"
            if reason:
                self._end_reason = str(reason)[:120]
            self._flush_unlocked(force=True)
            self._close_fd_unlocked()
            self._unregister_unlocked()
            self._closed = True

    def complete_and_discard(self) -> None:
        """Successful listen path: remove private audio entirely."""
        with self._lock:
            if self._closed and self._status == "complete":
                return
            self._status = "complete"
            self._end_reason = self._end_reason or "complete"
            self._close_fd_unlocked()
            self._unregister_unlocked()
            self._closed = True
            self._rm_tree_unlocked()

    def close_retain(self, *, reason: str | None = None) -> None:
        """End session but keep files (abnormal / incomplete exit).

        Empty streams (never accepted speech) are removed so doctor is not
        cluttered by no-open timeouts.
        """
        with self._lock:
            if self._closed:
                return
            if reason and not self._end_reason:
                self._end_reason = str(reason)[:120]
            self._flush_unlocked(force=True)
            empty = self._bytes_written <= 0
            self._close_fd_unlocked()
            self._unregister_unlocked()
            self._closed = True
            if empty:
                self._status = "complete"
                self._rm_tree_unlocked()
                return
            if self._status == "active":
                self._status = "incomplete"
            self._write_meta_unlocked()

    def duration_ms(self) -> int:
        with self._lock:
            total = self._bytes_written + len(self._buf)
        return _bytes_to_ms(total, self.sample_rate)

    # --- internals ---------------------------------------------------------

    def _register_unlocked(self) -> None:
        with _active_lock:
            _active_sessions[self.stream_id] = self
        self._registered = True

    def _unregister_unlocked(self) -> None:
        if not self._registered:
            return
        with _active_lock:
            cur = _active_sessions.get(self.stream_id)
            if cur is self:
                _active_sessions.pop(self.stream_id, None)
        self._registered = False

    def _flush_unlocked(self, *, force: bool) -> None:
        if self._pcm_fd is None or self._io_failed:
            self._buf.clear()
            return
        if not self._buf and not force:
            return
        try:
            if self._buf:
                view = bytes(self._buf)
                _write_all(self._pcm_fd, view)
                self._bytes_written += len(view)
                self._buf.clear()
            os.fsync(self._pcm_fd)
            self._last_flush_mono = time.monotonic()
            self._write_meta_unlocked()
        except OSError:
            self._io_failed = True
            self._buf.clear()
            try:
                from hark.syslog import log as syslog

                syslog(
                    "recover.flush_failed",
                    component="stt",
                    level="warn",
                    stream_id=self.stream_id,
                    message="answer-window recovery flush failed; continuing without durability",
                )
            except Exception:
                pass

    def _write_meta_unlocked(self) -> None:
        if self._io_failed:
            return
        payload = {
            "schema": SCHEMA_VERSION,
            "stream_id": self.stream_id,
            "sample_rate": self.sample_rate,
            "channels": 1,
            "sample_format": SAMPLE_FORMAT,
            "mode": self.mode,
            "pid": self.pid,
            "started_at": self._started_at,
            "updated_at": time.time(),
            "status": self._status,
            "bytes_written": self._bytes_written,
            "duration_ms": _bytes_to_ms(self._bytes_written, self.sample_rate),
            "flush_interval_s": self.flush_interval_s,
            "segment_count": self._segment_count,
            "partial_text": self._partial_text,
            "end_reason": self._end_reason,
        }
        try:
            _atomic_write_json(self.meta_path, payload)
        except OSError:
            self._io_failed = True

    def _close_fd_unlocked(self) -> None:
        fd = self._pcm_fd
        self._pcm_fd = None
        if fd is None:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def _rm_tree_unlocked(self) -> None:
        for child in (self.pcm_path, self.meta_path):
            try:
                child.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            self.dir.rmdir()
        except OSError:
            # Non-empty or racing — best-effort walk.
            try:
                for p in sorted(self.dir.rglob("*"), reverse=True):
                    try:
                        if p.is_file() or p.is_symlink():
                            p.unlink(missing_ok=True)
                        elif p.is_dir():
                            p.rmdir()
                    except OSError:
                        pass
                self.dir.rmdir()
            except OSError:
                pass


def flush_all_active_recovery() -> None:
    """Best-effort fsync of every in-process recovery session (signal path).

    Non-blocking: must not deadlock if the capture thread holds a session lock
    when SIGINT/SIGTERM arrives.
    """
    if not _active_lock.acquire(blocking=False):
        return
    try:
        sessions = list(_active_sessions.values())
    finally:
        _active_lock.release()
    for session in sessions:
        try:
            session.flush(force=True, blocking=False)
        except Exception:
            pass


def active_recovery_session(stream_id: str | None = None) -> StreamRecoverySession | None:
    with _active_lock:
        if stream_id:
            return _active_sessions.get(stream_id)
        if len(_active_sessions) == 1:
            return next(iter(_active_sessions.values()))
        return None


def _ensure_capture_flush_hook() -> None:
    """Install signal-path flusher once (no-op if capture already has one)."""
    try:
        from hark.audio.capture import set_recovery_flush

        set_recovery_flush(flush_all_active_recovery)
    except Exception:
        pass


def open_stream_recovery(
    stream_id: str,
    *,
    sample_rate: int = 16000,
    mode: str = "radio",
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
    max_buffer_bytes: int = DEFAULT_MAX_BUFFER_BYTES,
    root: Path | None = None,
) -> StreamRecoverySession:
    """Create and open a recovery session for *stream_id*."""
    _ensure_capture_flush_hook()
    session = StreamRecoverySession(
        stream_id,
        sample_rate=sample_rate,
        mode=mode,
        flush_interval_s=flush_interval_s,
        max_buffer_bytes=max_buffer_bytes,
        root=root,
    )
    session.open()
    return session


def _load_meta(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def iter_recovered_streams(*, root: Path | None = None) -> Iterator[RecoveredStream]:
    """Yield on-disk recovery streams (newest updated_at first)."""
    base = streams_dir(root=root)
    if not base.is_dir():
        return
    found: list[RecoveredStream] = []
    try:
        children = list(base.iterdir())
    except OSError:
        return
    for child in children:
        if not child.is_dir():
            continue
        meta_path = child / "meta.json"
        pcm_path = child / "audio.pcm"
        meta = _load_meta(meta_path) if meta_path.is_file() else None
        if meta is None:
            meta = {
                "stream_id": child.name,
                "status": "unknown",
                "sample_rate": 16000,
            }
        sid = str(meta.get("stream_id") or child.name)
        try:
            pcm_size = int(pcm_path.stat().st_size) if pcm_path.is_file() else 0
        except OSError:
            pcm_size = 0
        rate = int(meta.get("sample_rate") or 16000)
        duration = int(meta.get("duration_ms") or _bytes_to_ms(pcm_size, rate))
        # Prefer live size if larger (crash before meta flush).
        live_ms = _bytes_to_ms(pcm_size, rate)
        if live_ms > duration:
            duration = live_ms
        status = str(meta.get("status") or "unknown")
        pid = meta.get("pid")
        pid_i = int(pid) if isinstance(pid, int) or (isinstance(pid, str) and str(pid).isdigit()) else None
        alive = _pid_alive(pid_i)
        orphaned = status in ("active", "incomplete", "interrupted", "unknown") and (
            alive is False or (alive is None and status != "complete")
        )
        # Active with live pid is in-progress, not an orphan for recovery UX.
        if status == "active" and alive is True:
            orphaned = False
        found.append(
            RecoveredStream(
                stream_id=sid,
                path=child,
                meta=meta,
                pcm_path=pcm_path,
                pcm_bytes=pcm_size,
                duration_ms=duration,
                orphaned=orphaned or status in ("interrupted", "incomplete"),
            )
        )
    found.sort(key=lambda s: float(s.meta.get("updated_at") or 0.0), reverse=True)
    yield from found


def list_recovered_streams(
    *,
    root: Path | None = None,
    include_active: bool = True,
    only_orphaned: bool = False,
) -> list[RecoveredStream]:
    out: list[RecoveredStream] = []
    for item in iter_recovered_streams(root=root):
        status = str(item.meta.get("status") or "")
        if not include_active and status == "active" and not item.orphaned:
            continue
        if only_orphaned and not item.orphaned:
            continue
        out.append(item)
    return out


def get_recovered_stream(
    stream_id: str,
    *,
    root: Path | None = None,
) -> RecoveredStream | None:
    sid = _safe_stream_id(stream_id)
    for item in iter_recovered_streams(root=root):
        if item.stream_id == sid or item.path.name == sid:
            return item
    # Direct path even if meta missing.
    d = stream_dir(sid, root=root)
    if d.is_dir():
        meta = _load_meta(d / "meta.json") or {"stream_id": sid, "status": "unknown", "sample_rate": 16000}
        pcm_path = d / "audio.pcm"
        try:
            pcm_size = int(pcm_path.stat().st_size) if pcm_path.is_file() else 0
        except OSError:
            pcm_size = 0
        rate = int(meta.get("sample_rate") or 16000)
        return RecoveredStream(
            stream_id=sid,
            path=d,
            meta=meta,
            pcm_path=pcm_path,
            pcm_bytes=pcm_size,
            duration_ms=_bytes_to_ms(pcm_size, rate),
            orphaned=True,
        )
    return None


def discard_recovered_stream(
    stream_id: str,
    *,
    root: Path | None = None,
) -> bool:
    """Delete one recovery stream. Returns True if something was removed."""
    item = get_recovered_stream(stream_id, root=root)
    if item is None:
        return False
    removed = False
    for child in (item.pcm_path, item.path / "meta.json"):
        try:
            if child.is_file():
                child.unlink()
                removed = True
        except OSError:
            pass
    try:
        if item.path.is_dir():
            for p in sorted(item.path.rglob("*"), reverse=True):
                try:
                    if p.is_file() or p.is_symlink():
                        p.unlink(missing_ok=True)
                        removed = True
                    elif p.is_dir():
                        p.rmdir()
                except OSError:
                    pass
            item.path.rmdir()
            removed = True
    except OSError:
        pass
    return removed


def purge_old_recovered_streams(
    *,
    retention_days: float = DEFAULT_RETENTION_DAYS,
    root: Path | None = None,
) -> int:
    """Delete recovery streams whose mtime/updated_at is older than retention."""
    cutoff = time.time() - max(0.0, float(retention_days)) * 86400.0
    removed = 0
    for item in list(iter_recovered_streams(root=root)):
        # Never purge a stream whose owner pid is still alive.
        pid = item.meta.get("pid")
        pid_i = int(pid) if isinstance(pid, int) else None
        if _pid_alive(pid_i) is True and str(item.meta.get("status")) == "active":
            continue
        updated = item.meta.get("updated_at") or item.meta.get("started_at")
        try:
            ts = float(updated) if updated is not None else item.path.stat().st_mtime
        except (TypeError, ValueError, OSError):
            try:
                ts = item.path.stat().st_mtime
            except OSError:
                continue
        if ts < cutoff:
            if discard_recovered_stream(item.stream_id, root=root):
                removed += 1
    return removed


def recovery_doctor_report(*, root: Path | None = None) -> dict[str, Any]:
    """Soft doctor section for recoverable / orphaned answer-window audio."""
    try:
        purged = purge_old_recovered_streams(root=root)
    except Exception:
        purged = 0
    try:
        items = list_recovered_streams(root=root, include_active=True)
    except Exception as exc:
        return {
            "status": "error",
            "ok": False,
            "count": 0,
            "orphaned": 0,
            "error": str(exc)[:200],
            "warnings": [f"recover scan failed: {exc}"],
            "streams": [],
            "purged": purged,
            "root": str(streams_dir(root=root)),
        }
    orphaned = [s for s in items if s.orphaned]
    warnings: list[str] = []
    hints: list[str] = []
    if orphaned:
        warnings.append(
            f"{len(orphaned)} recoverable answer-window recording(s) "
            f"(interrupted capture; at most a few seconds of tail may be missing)"
        )
        hints.append("hark recover list")
        hints.append("hark recover transcribe <stream_id>")
        hints.append("hark recover discard <stream_id>")
    status = "orphaned" if orphaned else ("idle" if not items else "active")
    return {
        "status": status,
        "ok": True,
        "count": len(items),
        "orphaned": len(orphaned),
        "purged": purged,
        "root": str(streams_dir(root=root)),
        "retention_days": DEFAULT_RETENTION_DAYS,
        "flush_interval_s": DEFAULT_FLUSH_INTERVAL_S,
        "warnings": warnings,
        "hints": hints,
        "streams": [s.to_public_dict() for s in items[:20]],
    }


def _bytes_to_ms(n_bytes: int, sample_rate: int) -> int:
    if sample_rate <= 0 or n_bytes <= 0:
        return 0
    samples = n_bytes // BYTES_PER_SAMPLE
    return int(1000 * samples / sample_rate)


def _clip_text(text: str, limit: int = 500) -> str:
    body = (text or "").replace("\x00", " ").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1] + "…"

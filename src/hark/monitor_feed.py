"""Unified handsfree monitor feed: all events that should wake the orchestrator.

``hark watch`` only covers Herdr agent state. Ambient writes
``ambient.wake_near_miss``, ``ambient.prompt``, etc. to state JSONL files that
were easy to miss with ad-hoc ``tail | grep`` monitors.

``hark monitor`` follows the worker state files and prints one HEP NDJSON line
per matching event (optionally compact for harness Monitors).

Singleflight: only one feed consumer should run (B102). A second
``hark monitor`` refuses unless ``--allow-multiple`` is set.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, TextIO

from hark.state_feed import SourceFollower, StateFeedFollower, present_for_monitor
from hark.exitcodes import ERROR
from hark.paths import state_dir
from hark import paths as hark_paths

# Exclusive consumer lock under XDG state (flock + pid for diagnostics).
MONITOR_PID_NAME = "monitor.pid"
EVENT_PROVENANCE_ENV = "HARK_EVENT_PROVENANCE"
TEST_PROVENANCE = "test"

# Events that MUST wake the handsfree orchestrator (persistent Monitor consumers).
MODE_A_WAKE_KINDS: frozenset[str] = frozenset(
    {
        # Herdr / watch (via watch.jsonl from `hark watch`)
        "agent.blocked",
        "agent.needs_input",
        "agent.completed",
        "agent.question_changed",
        "watch.armed",
        "target.invalidated",
        # Ambient / voice (via ambient.jsonl from `hark ambient`)
        "ambient.prompt",
        "ambient.partial",
        "ambient.turn",
        "ambient.conversation_end",
        "ambient.wake_near_miss",
        "ambient.wake_learned",
        "ambient.error",
        "ambient.cancelled",
        "ambient.reloaded",
        "ambient.armed",
        # TTS lifecycle (via ambient.jsonl side-channel from speech.run_tts)
        "tts.truncated",
        "tts.chunked",
    }
)

# Default files written by workers (run-mode-a.sh / harkd --workers)
DEFAULT_FEED_FILES: tuple[str, ...] = ("watch.jsonl", "ambient.jsonl")


def ambient_feed_path(root: Path | None = None) -> Path:
    """Canonical ambient HEP feed path (`hark monitor` / dashboard tail).

    Uses ``hark.paths.state_dir`` via the module so tests that monkeypatch
    ``hark.paths.state_dir`` still isolate the feed path.
    """
    return (root or hark_paths.state_dir()) / "ambient.jsonl"


def io_targets_path(out: TextIO | None, path: Path) -> bool:
    """True when *out* is already the open file at *path* (same resolved path).

    Used to skip dual-write when workers redirect ambient stdout → ambient.jsonl
    so each HEP line is not appended twice.
    """
    if out is None:
        return False
    try:
        name = getattr(out, "name", None)
        if not name or not isinstance(name, (str, Path)):
            return False
        name_s = str(name)
        # StringIO / TTY / pipe names are not real paths
        if name_s.startswith("<") or name_s in ("stdout", "stderr"):
            return False
        return Path(name_s).resolve() == path.resolve()
    except Exception:
        return False


def _stored_event_line(event: dict[str, Any]) -> str:
    """Return the authoritative NDJSON representation for persisted events.

    Execution provenance is reserved metadata: when the process declares it,
    it overrides caller-supplied data in a copy.  Callers may still emit their
    original object to a non-canonical machine stream unchanged.
    """
    stored_event = dict(event)
    provenance = os.environ.get(EVENT_PROVENANCE_ENV, "").strip()
    if provenance:
        stored_event["hark_provenance"] = provenance
    return json.dumps(stored_event, separators=(",", ":"), ensure_ascii=False) + "\n"


def append_ambient_jsonl(
    event: dict[str, Any],
    *,
    root: Path | None = None,
) -> bool:
    """Best-effort append of one HEP object to state ``ambient.jsonl``.

    Shared side-channel for TTS lifecycle (``surface_tts_event``) and ambient
    dual-write when process stdout is redirected elsewhere (B104).
    Returns True if a line was written.
    """
    try:
        path = ambient_feed_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_stored_event_line(event))
        return True
    except Exception:
        return False


def emit_hep(
    event: dict[str, Any],
    out: TextIO | None = None,
    *,
    root: Path | None = None,
    dual_write: bool = True,
) -> None:
    """Write one HEP NDJSON line to *out* and dual-write to ambient.jsonl (B104).

    Dual-write is skipped when *out* is already ambient.jsonl (workers redirect),
    so redirect-to-restart-log still feeds ``hark monitor`` without duplicating
    the normal path.
    """
    feed = ambient_feed_path(root)
    out_is_feed = io_targets_path(out, feed)
    if out is not None:
        try:
            line = (
                _stored_event_line(event)
                if out_is_feed
                else json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
            )
            out.write(line)
            out.flush()
        except Exception:
            pass
    if not dual_write or out is None:
        # No stream → nothing to dual-write (callers that only want the feed
        # should use append_ambient_jsonl directly, e.g. TTS lifecycle).
        return
    if out_is_feed:
        return
    append_ambient_jsonl(event, root=root)


class MonitorBusyError(RuntimeError):
    """Another ``hark monitor`` already holds the feed consumer lock."""

    def __init__(self, message: str, *, pid: int | None = None) -> None:
        super().__init__(message)
        self.pid = pid


def monitor_pid_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / MONITOR_PID_NAME


def _pid_alive(pid: int) -> bool:
    """Local check; prefer daemon.pid_alive when available."""
    if pid <= 0:
        return False
    try:
        from hark.daemon import pid_alive

        return bool(pid_alive(pid))
    except Exception:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


def read_monitor_holder_pid(root: Path | None = None) -> int | None:
    """Return live PID from monitor.pid if any, else None (stale/empty)."""
    path = monitor_pid_path(root)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("pid="):
            line = line.split("=", 1)[1].strip()
        try:
            pid = int(line)
        except ValueError:
            continue
        if _pid_alive(pid):
            return pid
    return None


def probe_monitor_consumer(root: Path | None = None) -> dict[str, Any]:
    """Status snapshot for the unified monitor feed consumer."""
    root = root or state_dir()
    path = monitor_pid_path(root)
    pid = read_monitor_holder_pid(root)
    return {
        "running": pid is not None,
        "pid": pid,
        "pidfile": str(path) if path.exists() else None,
    }


class MonitorFeedLock:
    """Process-wide singleflight for handsfree ``hark monitor`` consumers.

    Uses ``fcntl.flock`` so the lock drops if the process dies; also writes
    ``monitor.pid`` so status / error messages can name the holder.
    """

    def __init__(self, root: Path | None = None, *, pid: int | None = None) -> None:
        self.root = root or state_dir()
        self.pid = os.getpid() if pid is None else int(pid)
        self._fd: int | None = None
        self._held = False

    def acquire(self) -> Path:
        """Take exclusive lock. Raises :class:`MonitorBusyError` if held."""
        if self._held:
            return monitor_pid_path(self.root)
        path = monitor_pid_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            other = read_monitor_holder_pid(self.root)
            if other is not None:
                msg = (
                    f"hark monitor already running (pid {other}); "
                    "only one feed consumer should be armed "
                    "(skill: do not arm a second Monitor). "
                    "Debug only: --allow-multiple"
                )
            else:
                msg = (
                    "hark monitor already running (another process holds "
                    f"{path.name}); only one feed consumer should be armed. "
                    "Debug only: --allow-multiple"
                )
            raise MonitorBusyError(msg, pid=other) from None
        except OSError:
            os.close(fd)
            raise

        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{self.pid}\n".encode("utf-8"))
            try:
                os.fsync(fd)
            except OSError:
                pass
        except OSError:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
            raise

        self._fd = fd
        self._held = True
        return path

    def release(self) -> None:
        if not self._held:
            return
        fd = self._fd
        self._fd = None
        self._held = False
        if fd is None:
            return
        path = monitor_pid_path(self.root)
        try:
            # Clear pid body only if we still own the record.
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                raw = os.read(fd, 64)
                text = raw.decode("utf-8", errors="replace").strip()
                holder: int | None
                try:
                    holder = int(text.splitlines()[0]) if text else None
                except ValueError:
                    holder = None
                if holder is None or holder == self.pid:
                    os.ftruncate(fd, 0)
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        # Drop empty/stale pidfile when no live holder remains.
        if read_monitor_holder_pid(self.root) is None:
            try:
                if path.is_file() and not path.read_text(encoding="utf-8").strip():
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def __enter__(self) -> MonitorFeedLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


def compact_mode_a_event(event: dict[str, Any]) -> dict[str, Any]:
    """Compact line for harness Monitors — alias of :func:`present_for_monitor`."""
    return present_for_monitor(event)


def parse_event_line(line: str) -> dict[str, Any] | None:
    line = (line or "").strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def event_kind(obj: dict[str, Any]) -> str:
    return str(obj.get("kind") or obj.get("event") or "")


def should_surface(
    obj: dict[str, Any],
    kinds: frozenset[str],
    *,
    include_test_events: bool = False,
) -> bool:
    if not include_test_events and obj.get("hark_provenance") == TEST_PROVENANCE:
        return False
    return event_kind(obj) in kinds


def emit_line(
    obj: dict[str, Any],
    *,
    for_monitor: bool,
    out: TextIO,
    monitor_mode: str | None = None,
    source: str | None = None,
) -> None:
    if for_monitor:
        try:
            payload = present_for_monitor(obj)
        except Exception as exc:
            # Never kill the whole feed on one malformed line (dogfood: string
            # question/target crashed monitor_profile). Fall back to a minimal
            # compact object the orchestrator can still see.
            payload = {
                "schema": obj.get("schema") or "hark.event.v1",
                "kind": obj.get("kind") or obj.get("event"),
                "event_id": obj.get("event_id"),
                "observed_at": obj.get("observed_at"),
                "session_id": obj.get("session_id"),
                "compact_error": str(exc)[:200],
                "instructions": (
                    "Monitor compact failed for this event; inspect raw logs. "
                    "Do not invent an answer."
                ),
            }
    else:
        payload = dict(obj)
    if monitor_mode is not None:
        payload = dict(payload)
        payload["monitor_delivery"] = {
            "mode": monitor_mode,
            "source": source,
        }
    out.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
    out.flush()


def replay_matching(
    paths: Iterable[Path],
    *,
    kinds: frozenset[str],
    limit: int,
    for_monitor: bool,
    out: TextIO,
    include_test_events: bool = False,
) -> int:
    """Replay last *limit* matching events (chronological) from files."""
    if limit <= 0:
        return 0
    matched: list[tuple[dict[str, Any], str]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            obj = parse_event_line(line)
            if obj and should_surface(
                obj, kinds, include_test_events=include_test_events
            ):
                matched.append((obj, path.name))

    # keep last N across all files by observed_at if present else order
    def sort_key(item: tuple[dict[str, Any], str]) -> str:
        return str(item[0].get("observed_at") or "")

    matched.sort(key=sort_key)
    tail = matched[-limit:]
    for obj, source in tail:
        emit_line(
            obj,
            for_monitor=for_monitor,
            out=out,
            monitor_mode="replay",
            source=source,
        )
    return len(tail)


def follow_state_files(
    paths: list[Path],
    *,
    kinds: frozenset[str],
    for_monitor: bool = True,
    out: TextIO | None = None,
    poll_s: float = 0.05,
    include_test_events: bool = False,
) -> int:
    """Follow JSONL state files; print matching handsfree wake events forever.

    Uses :class:`~hark.state_feed.StateFeedFollower` (partial buffer, inode
    rotation, truncation) — same core as the dashboard MultiTailer adapter.
    """
    out = out or sys.stdout
    # Ensure files exist so first open works
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            path.touch()

    sources = [
        SourceFollower(path, source=path.name, cursor_key=path.stem) for path in paths
    ]
    follower = StateFeedFollower(sources)
    try:
        follower.start_live()
        while True:
            progressed = False
            for rec in follower.poll():
                progressed = True
                obj = rec.payload
                if obj and should_surface(
                    obj, kinds, include_test_events=include_test_events
                ):
                    emit_line(
                        obj,
                        for_monitor=for_monitor,
                        out=out,
                        monitor_mode="live",
                        source=rec.source,
                    )
            if not progressed:
                time.sleep(poll_s)
    except KeyboardInterrupt:
        return 0
    finally:
        follower.close()
    return 0


def default_feed_paths() -> list[Path]:
    root = state_dir()
    return [root / name for name in DEFAULT_FEED_FILES]


def run_monitor(
    *,
    for_monitor: bool = True,
    kinds: frozenset[str] | None = None,
    replay: int = 0,
    paths: list[Path] | None = None,
    out: TextIO | None = None,
    allow_multiple: bool = False,
    state_root: Path | None = None,
) -> int:
    """Entry for ``hark monitor``.

    Acquires a singleflight lock so only one feed consumer runs (B102).
    Pass ``allow_multiple=True`` (CLI ``--allow-multiple``) only for debug.
    """
    out = out or sys.stdout
    lock: MonitorFeedLock | None = None
    if not allow_multiple:
        lock = MonitorFeedLock(state_root)
        try:
            lock.acquire()
        except MonitorBusyError as exc:
            print(str(exc), file=sys.stderr)
            return ERROR
    try:
        try:
            from hark.config import load_config
            from hark.update_check import maybe_print_update_notice

            cfg = load_config()
            maybe_print_update_notice(
                enabled=bool(getattr(cfg.update, "enabled", True)),
                repo=getattr(cfg.update, "repo", None),
            )
        except Exception:  # pragma: no cover — never block monitor on update check
            pass
        kinds = kinds if kinds is not None else MODE_A_WAKE_KINDS
        paths = paths or default_feed_paths()
        if replay:
            replay_matching(
                paths, kinds=kinds, limit=replay, for_monitor=for_monitor, out=out
            )
        return follow_state_files(paths, kinds=kinds, for_monitor=for_monitor, out=out)
    finally:
        if lock is not None:
            lock.release()

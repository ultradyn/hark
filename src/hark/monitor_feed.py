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

from hark.events import monitor_profile
from hark.exitcodes import ERROR
from hark.paths import state_dir

# Exclusive consumer lock under XDG state (flock + pid for diagnostics).
MONITOR_PID_NAME = "monitor.pid"

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
    """Compact line for harness Monitors (short, actionable)."""
    kind = str(event.get("kind") or "")
    if kind.startswith("agent.") or kind in ("watch.armed", "watch.error", "target.invalidated"):
        return monitor_profile(event)

    compact: dict[str, Any] = {
        "schema": event.get("schema") or "hark.event.v1",
        "kind": kind,
        "event_id": event.get("event_id"),
        "observed_at": event.get("observed_at"),
    }

    if kind == "ambient.prompt":
        text = event.get("text")
        if isinstance(text, str) and len(text) > 400:
            text = text[:397] + "…"
        compact.update(
            {
                "phrase": event.get("phrase"),
                "text": text,
                "stream_id": event.get("stream_id"),
                "final": True,
                "partial": False,
                "instructions": (
                    "FINAL operator voice prompt. Reply with hark tts. "
                    "Not bound to a pane unless they ask. Then idle for next Monitor event."
                ),
            }
        )
    elif kind == "ambient.partial":
        from hark.partial import partial_compact_instructions

        text = event.get("text")
        full_len = len(text) if isinstance(text, str) else 0
        if isinstance(text, str) and len(text) > 400:
            text = text[:397] + "…"
        frag = event.get("fragment")
        if isinstance(frag, str) and len(frag) > 240:
            frag = frag[:237] + "…"
        streaming = bool(event.get("streaming"))
        compact.update(
            {
                "stream_id": event.get("stream_id"),
                "seq": event.get("seq"),
                # Prefer delta for the orchestrator / logs; keep full body truncated as text
                "fragment": frag if frag is not None else text,
                "text": text,
                "text_len": full_len or None,
                "partial": True,
                "final": False,
                "streaming": streaming,
                "instructions": partial_compact_instructions(streaming=streaming),
            }
        )
    elif kind == "ambient.wake_near_miss":
        attempts = event.get("attempts") or []
        texts = []
        if isinstance(attempts, list):
            for a in attempts[:5]:
                if isinstance(a, dict) and a.get("text"):
                    texts.append(str(a["text"]))
                elif isinstance(a, str):
                    texts.append(a)
        compact.update(
            {
                "count": event.get("count"),
                "total_near_misses": event.get("total_near_misses"),
                "group_index": event.get("group_index"),
                "attempts": texts or attempts,
                "priority": event.get("priority", 35),
                "instructions": (
                    "Failed wake attempt(s) — not a prompt. Review attempts; "
                    "learning may auto-expand aliases (wake_learned). "
                    "Optional: adjust ambient names / extra_trigger_phrases. See docs/CUSTOM_WAKE.md."
                ),
            }
        )
    elif kind == "ambient.wake_learned":
        compact.update(
            {
                "learn_kind": event.get("learn_kind"),
                "value": event.get("value"),
                "canonical": event.get("canonical"),
                "wake_mode": event.get("wake_mode"),
                "instructions": (
                    "Learned a new wake alternate (no restart). "
                    "Optional: pin in config (names / trigger_phrases)."
                ),
            }
        )
    elif kind == "ambient.error":
        compact.update(
            {
                "error": event.get("error") or event.get("message"),
                "reason": event.get("reason"),
                "phrase": event.get("phrase"),
                "stream_id": event.get("stream_id"),
                "instructions": "Ambient/listen error — speak briefly if useful; fix or retry.",
            }
        )
    elif kind in ("ambient.cancelled", "ambient.reloaded", "ambient.armed"):
        compact.update(
            {
                "phrase": event.get("phrase"),
                "wake_mode": event.get("wake_mode"),
                "names": event.get("names"),
                "phrases": event.get("phrases"),
                "instructions": event.get("instructions")
                or f"{kind}: informational; continue idle with monitors armed.",
            }
        )
    elif kind == "tts.truncated":
        compact.update(
            {
                "original_chars": event.get("original_chars"),
                "kept_chars": event.get("kept_chars"),
                "max_chars": event.get("max_chars"),
                "text_preview": event.get("text_preview"),
                "instructions": event.get("instructions")
                or (
                    "TTS text was truncated to tts.max_chars. Full agent text was NOT spoken. "
                    "Raise [tts].max_chars (0=unlimited) or shorten the reply."
                ),
            }
        )
    elif kind == "tts.chunked":
        compact.update(
            {
                "chars": event.get("chars"),
                "n_chunks": event.get("n_chunks"),
                "chunk_chars": event.get("chunk_chars"),
                "instructions": event.get("instructions")
                or "Long TTS multi-chunk play (informational).",
            }
        )
    else:
        # Pass through compact non-null subset
        for key in (
            "text",
            "phrase",
            "error",
            "stream_id",
            "instructions",
            "priority",
        ):
            if event.get(key) is not None:
                compact[key] = event[key]

    return {k: v for k, v in compact.items() if v is not None}


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


def should_surface(obj: dict[str, Any], kinds: frozenset[str]) -> bool:
    return event_kind(obj) in kinds


def emit_line(
    obj: dict[str, Any],
    *,
    for_monitor: bool,
    out: TextIO,
) -> None:
    if for_monitor:
        try:
            payload = compact_mode_a_event(obj)
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
        payload = obj
    out.write(json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n")
    out.flush()


def replay_matching(
    paths: Iterable[Path],
    *,
    kinds: frozenset[str],
    limit: int,
    for_monitor: bool,
    out: TextIO,
) -> int:
    """Replay last *limit* matching events (chronological) from files."""
    if limit <= 0:
        return 0
    matched: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            obj = parse_event_line(line)
            if obj and should_surface(obj, kinds):
                matched.append(obj)
    # keep last N across all files by observed_at if present else order
    def sort_key(o: dict[str, Any]) -> str:
        return str(o.get("observed_at") or "")

    matched.sort(key=sort_key)
    tail = matched[-limit:]
    for obj in tail:
        emit_line(obj, for_monitor=for_monitor, out=out)
    return len(tail)


def follow_state_files(
    paths: list[Path],
    *,
    kinds: frozenset[str],
    for_monitor: bool = True,
    out: TextIO | None = None,
    poll_s: float = 0.05,
) -> int:
    """Follow JSONL state files; print matching handsfree wake events forever.

    Expects workers (or equivalent) to be writing watch.jsonl + ambient.jsonl.
    """
    out = out or sys.stdout
    # Ensure files exist so first open works
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            path.touch()

    handles: list[tuple[Path, Any]] = []
    try:
        for path in paths:
            fh = path.open("r", encoding="utf-8", errors="replace")
            fh.seek(0, 2)  # end
            handles.append((path, fh))

        while True:
            progressed = False
            for path, fh in handles:
                # Detect truncation/rotation
                try:
                    pos = fh.tell()
                    size = path.stat().st_size
                    if size < pos:
                        fh.seek(0)
                except OSError:
                    continue
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    progressed = True
                    obj = parse_event_line(line)
                    if obj and should_surface(obj, kinds):
                        emit_line(obj, for_monitor=for_monitor, out=out)
            if not progressed:
                time.sleep(poll_s)
    except KeyboardInterrupt:
        return 0
    finally:
        for _, fh in handles:
            try:
                fh.close()
            except Exception:
                pass
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
        return follow_state_files(
            paths, kinds=kinds, for_monitor=for_monitor, out=out
        )
    finally:
        if lock is not None:
            lock.release()

"""Playback helpers — WAV/PCM via sounddevice; MP3/other via ffplay/ffmpeg."""

from __future__ import annotations

import atexit
import errno
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None  # type: ignore


@dataclass
class PlayResult:
    duration_ms: int
    format: str


# B161: user-initiated skip of in-process playback (TTS skip notification).
# Stoppers are registered for the *currently playing* chunk only; the
# generation counter lets the run_tts chunk loop notice a skip that landed
# between stoppers (or while no playback was active) and stop early.
# Scope is deliberately process-wide: a skip stops every in-process playback,
# including short cues/chimes that happen to be playing concurrently — the
# speaker FIFO serializes TTS with most cue playback, so overlap is rare.
_skip_lock = threading.Lock()
_skip_stoppers: set[Callable[[], None]] = set()
_skip_generation = 0


def request_playback_skip() -> bool:
    """Stop any in-process playback right now (B161).

    Returns True when at least one active playback stopper was invoked.
    Always bumps the skip generation so callers that snapshot it before
    playback can detect the request even if it raced playback setup.
    """
    global _skip_generation
    with _skip_lock:
        _skip_generation += 1
        stoppers = list(_skip_stoppers)
    invoked = False
    for stop in stoppers:
        try:
            stop()
            invoked = True
        except Exception:
            pass
    return invoked


def playback_skip_generation() -> int:
    """Monotonic skip counter; snapshot before play to detect a later skip."""
    with _skip_lock:
        return _skip_generation


@contextmanager
def _skip_stopper(stop: Callable[[], None]) -> Iterator[int]:
    """Register *stop* as the way to interrupt the in-flight playback.

    Yields the skip generation atomically at registration time, so a skip
    cannot land undetected between registering and snapshotting.
    """
    with _skip_lock:
        _skip_stoppers.add(stop)
        generation = _skip_generation
    try:
        yield generation
    finally:
        with _skip_lock:
            _skip_stoppers.discard(stop)


# Cross-process FIFO speaker: synth may run in parallel; play is serial (B092).
# Ticket is claimed at *launch* (before synth) so N concurrent jobs keep order.
# B099: holders track pid+claim time so dead processes cannot stall the queue.
_play_tls = threading.local()
_play_lock_name = "tts_play.lock"
_play_queue_name = "tts_play_queue.json"

# After this many seconds waiting for a head with no holder (legacy/killed
# pre-B099), treat the head as abandoned. Live PIDs are never aged out here —
# long multi-chunk TTS can hold the speaker for minutes.
_MISSING_HOLDER_GRACE_S = 8.0
# Absolute safety: abandon a holder whose claim is older than this *and* whose
# PID is dead (redundant with pid check) — kept for documentation/tests.
_STALE_HOLDER_AGE_S = 600.0

# Metadata transactions normally hold this flock for milliseconds.  Playback
# itself may hold it much longer, so every new acquisition must have a bound:
# a wedged player must surface a useful error instead of making `hark ask` look
# indistinguishable from a hung listen (B146).
_PLAY_LOCK_ACQUIRE_TIMEOUT_S = 15.0
_PLAY_LOCK_POLL_S = 0.03
_DEFERRED_ABANDON_RETRY_S = 0.25

_our_tickets: set[int] = set()
_our_tickets_lock = threading.Lock()
_cleanup_hooks_installed = False
_deferred_abandons: set[int] = set()
_deferred_abandons_lock = threading.Lock()
_deferred_abandon_thread: threading.Thread | None = None


def tts_play_lock_path() -> Path:
    from hark.paths import state_dir

    return state_dir() / _play_lock_name


def tts_play_queue_path() -> Path:
    from hark.paths import state_dir

    return state_dir() / _play_queue_name


def _pid_alive(pid: int) -> bool:
    """True if *pid* looks like a live (non-zombie) process."""
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


def _syslog_tts_queue(event: str, **fields: Any) -> None:
    try:
        from hark.syslog import log as syslog

        syslog(event, component="tts", level=fields.pop("level", "warn"), **fields)
    except Exception:
        pass


def _flock_owner_pid(fd: int) -> int | None:
    """Best-effort Linux ``/proc/locks`` lookup for the flock owning *fd*."""
    try:
        st = os.fstat(fd)
        wanted = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
        with open("/proc/locks", encoding="utf-8") as locks:
            for line in locks:
                fields = line.split()
                if len(fields) < 6 or fields[1] != "FLOCK":
                    continue
                dev_inode = fields[5].split(":", 2)
                if len(dev_inode) != 3:
                    continue
                try:
                    actual = (
                        int(dev_inode[0], 16),
                        int(dev_inode[1], 16),
                        int(dev_inode[2]),
                    )
                    owner_pid = int(fields[4])
                except ValueError:
                    continue
                if actual == wanted and owner_pid > 0:
                    return owner_pid
    except (OSError, ValueError):
        pass
    return None


class TtsPlayTimeout(TimeoutError):
    """Base for JSON-safe playback lock and FIFO-turn timeout failures."""

    description = "tts playback timed out"
    error_type = "tts_play_timeout"

    def __init__(
        self,
        *,
        operation: str,
        timeout_s: float,
        elapsed_s: float,
        ticket: int | None,
        lock_owner_pid: int | None,
        queue_state: dict[str, Any],
    ) -> None:
        self.operation = operation
        self.timeout_s = timeout_s
        self.elapsed_s = elapsed_s
        self.ticket = ticket
        self.lock_owner_pid = lock_owner_pid
        self.queue_state = queue_state
        serving = int(queue_state.get("serving", 0))
        holders = _normalize_holders(queue_state.get("holders"))
        queue_owner = holders.get(str(serving), {})
        self.queue_owner_pid = int(queue_owner.get("pid", 0) or 0) or None
        self.queue_owner_claimed_at = float(queue_owner.get("claimed_at", 0.0) or 0.0)
        details = [
            f"operation={operation}",
            f"timeout={timeout_s:g}s",
            f"serving={serving}",
            f"next={int(queue_state.get('next', 0))}",
        ]
        if ticket is not None:
            details.append(f"ticket={ticket}")
        if lock_owner_pid is not None:
            details.append(f"lock_owner_pid={lock_owner_pid}")
        if self.queue_owner_pid is not None:
            details.append(f"queue_owner_pid={self.queue_owner_pid}")
        super().__init__(self.description + " (" + ", ".join(details) + ")")

    def as_dict(self) -> dict[str, Any]:
        """Return stable, JSON-safe owner and queue diagnostics for callers."""
        serving = int(self.queue_state.get("serving", 0))
        next_ticket = int(self.queue_state.get("next", 0))
        queue = {
            "serving": serving,
            "next": next_ticket,
            "pending": max(0, next_ticket - serving),
            "cancelled": [int(x) for x in self.queue_state.get("cancelled", [])],
            "holders": _normalize_holders(self.queue_state.get("holders")),
        }
        return {
            "operation": self.operation,
            "timeout_s": self.timeout_s,
            "elapsed_s": self.elapsed_s,
            "ticket": self.ticket,
            "lock_owner_pid": self.lock_owner_pid,
            "queue_owner_pid": self.queue_owner_pid,
            "queue_owner_claimed_at": self.queue_owner_claimed_at or None,
            "queue": queue,
        }


class TtsPlayLockTimeout(TtsPlayTimeout):
    """Bounded failure to acquire the cross-process playback flock."""

    description = "tts playback lock acquisition timed out"
    error_type = "tts_play_lock_timeout"


class TtsPlayQueueTimeout(TtsPlayTimeout):
    """Bounded failure to reach a claimed ticket's FIFO playback turn."""

    description = "tts play queue wait timed out"
    error_type = "tts_play_queue_timeout"


def _acquire_play_lock(
    fd: int,
    *,
    operation: str,
    ticket: int | None = None,
    timeout_s: float | None = None,
) -> None:
    """Acquire the playback flock without an unbounded kernel wait."""
    import fcntl

    timeout = _PLAY_LOCK_ACQUIRE_TIMEOUT_S if timeout_s is None else max(0.0, timeout_s)
    started = time.monotonic()
    deadline = started + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise
        now = time.monotonic()
        if now >= deadline:
            queue_state = _queue_read(tts_play_queue_path())
            lock_owner_pid = _flock_owner_pid(fd)
            error = TtsPlayLockTimeout(
                operation=operation,
                timeout_s=timeout,
                elapsed_s=now - started,
                ticket=ticket,
                lock_owner_pid=lock_owner_pid,
                queue_state=queue_state,
            )
            _syslog_tts_queue(
                "tts.play_lock_timeout",
                operation=operation,
                ticket=ticket,
                lock_timeout_s=timeout,
                lock_owner_pid=lock_owner_pid,
                queue_owner_pid=error.queue_owner_pid,
                serving=int(queue_state.get("serving", 0)),
                next=int(queue_state.get("next", 0)),
                level="warn",
            )
            raise error
        time.sleep(min(_PLAY_LOCK_POLL_S, max(0.0, deadline - now)))


def _close_play_lock_fd(fd: int, *, locked: bool) -> None:
    """Release/close *fd* without replacing an exception already in flight."""
    import fcntl

    primary_active = sys.exc_info()[0] is not None
    cleanup_error: BaseException | None = None
    if locked:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BaseException as exc:  # cleanup must preserve the primary failure
            cleanup_error = exc
    try:
        os.close(fd)
    except BaseException as exc:  # cleanup must preserve the primary failure
        if cleanup_error is None:
            cleanup_error = exc
    if cleanup_error is not None and not primary_active:
        raise cleanup_error


def _empty_queue_state() -> dict[str, object]:
    return {"next": 0, "serving": 0, "cancelled": [], "holders": {}}


def _normalize_holders(raw: object) -> dict[str, dict[str, object]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, object]] = {}
    for k, v in raw.items():
        try:
            ticket_s = str(int(k))
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        try:
            pid = int(v.get("pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        try:
            claimed_at = float(v.get("claimed_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            claimed_at = 0.0
        out[ticket_s] = {"pid": pid, "claimed_at": claimed_at}
    return out


def _queue_read(path: Path) -> dict[str, object]:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cancelled = data.get("cancelled") or []
        return {
            "next": int(data.get("next", 0)),
            "serving": int(data.get("serving", 0)),
            "cancelled": [int(x) for x in cancelled],
            "holders": _normalize_holders(data.get("holders")),
        }
    except Exception:
        return _empty_queue_state()


def _queue_write(path: Path, state: dict[str, object]) -> None:
    import json

    cancelled = sorted({int(x) for x in (state.get("cancelled") or [])})
    # Drop cancelled tickets already behind the head
    serving = int(state["serving"])
    cancelled = [c for c in cancelled if c >= serving]
    holders = _normalize_holders(state.get("holders"))
    # Drop holders already behind the head
    holders = {k: v for k, v in holders.items() if int(k) >= serving}
    path.write_text(
        json.dumps(
            {
                "next": int(state["next"]),
                "serving": serving,
                "cancelled": cancelled,
                "holders": holders,
            }
        ),
        encoding="utf-8",
    )


def _drop_holder(st: dict[str, object], ticket: int) -> None:
    holders = _normalize_holders(st.get("holders"))
    holders.pop(str(int(ticket)), None)
    st["holders"] = holders


def _set_holder(
    st: dict[str, object], ticket: int, *, pid: int, claimed_at: float
) -> None:
    holders = _normalize_holders(st.get("holders"))
    holders[str(int(ticket))] = {"pid": int(pid), "claimed_at": float(claimed_at)}
    st["holders"] = holders


def _skip_cancelled_heads(st: dict[str, object]) -> dict[str, object]:
    """Advance serving past any tickets marked cancelled."""
    cancelled = set(int(x) for x in (st.get("cancelled") or []))
    serving = int(st["serving"])
    while serving in cancelled:
        cancelled.discard(serving)
        _drop_holder(st, serving)
        serving += 1
    st["serving"] = serving
    st["cancelled"] = sorted(cancelled)
    return st


def _advance_serving(st: dict[str, object]) -> dict[str, object]:
    """Move serving past the current head and any following cancelled tickets."""
    serving = int(st["serving"])
    _drop_holder(st, serving)
    st["serving"] = serving + 1
    return _skip_cancelled_heads(st)


def _abandon_ticket_locked(st: dict[str, object], ticket: int) -> dict[str, object]:
    """Mutate queue state to abandon *ticket*. Caller holds the flock."""
    _drop_holder(st, ticket)
    if int(st["serving"]) == ticket:
        return _advance_serving(st)
    cancelled = list(st.get("cancelled") or [])
    if ticket not in cancelled:
        cancelled.append(ticket)
    st["cancelled"] = cancelled
    return _skip_cancelled_heads(st)


def _holder_abandoned(
    holder: dict[str, object] | None,
    *,
    missing_as_abandoned: bool,
    now: float,
) -> str | None:
    """Return abandon reason or None if the holder still owns the ticket."""
    if holder is None:
        return "missing_holder" if missing_as_abandoned else None
    try:
        pid = int(holder.get("pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0 and not _pid_alive(pid):
        # Prefer dead_pid; stale_age only when claim timestamp is ancient
        try:
            claimed_at = float(holder.get("claimed_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            claimed_at = 0.0
        if claimed_at > 0 and (now - claimed_at) > _STALE_HOLDER_AGE_S:
            return "stale_age"
        return "dead_pid"
    if pid <= 0:
        return "missing_holder" if missing_as_abandoned else None
    # Live PID: never age-out (long multi-chunk play is legitimate).
    return None


def _heal_abandoned_locked(
    st: dict[str, object],
    *,
    missing_as_abandoned: bool = False,
    now: float | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Advance serving past cancelled + abandoned heads. Caller holds flock."""
    now = time.time() if now is None else now
    st = _skip_cancelled_heads(st)
    healed: list[dict[str, object]] = []
    holders = _normalize_holders(st.get("holders"))
    st["holders"] = holders

    while int(st["serving"]) < int(st["next"]):
        serving = int(st["serving"])
        holder = holders.get(str(serving))
        reason = _holder_abandoned(
            holder, missing_as_abandoned=missing_as_abandoned, now=now
        )
        if reason is None:
            break
        entry: dict[str, object] = {
            "ticket": serving,
            "reason": reason,
            "pid": int((holder or {}).get("pid", 0) or 0) if holder else 0,
        }
        healed.append(entry)
        holders.pop(str(serving), None)
        st["holders"] = holders
        st["serving"] = serving + 1
        st = _skip_cancelled_heads(st)
        holders = _normalize_holders(st.get("holders"))
        st["holders"] = holders

    # Prune holders behind head
    serving = int(st["serving"])
    st["holders"] = {k: v for k, v in holders.items() if int(k) >= serving}
    return st, healed


def _track_our_ticket(ticket: int) -> None:
    with _our_tickets_lock:
        _our_tickets.add(int(ticket))
    _ensure_cleanup_hooks()


def _untrack_our_ticket(ticket: int) -> None:
    with _our_tickets_lock:
        _our_tickets.discard(int(ticket))


def _our_ticket_is_tracked(ticket: int) -> bool:
    with _our_tickets_lock:
        return int(ticket) in _our_tickets


def _recover_deferred_abandons_locked(
    st: dict[str, object],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Apply retained cleanup requests while the caller owns the file lock."""
    with _deferred_abandons_lock:
        if _deferred_abandon_thread is not None:
            return st, []
        tickets = list(_deferred_abandons)
    recovered: list[dict[str, object]] = []
    for ticket in tickets:
        if _our_ticket_is_tracked(ticket):
            st = _abandon_ticket_locked(st, ticket)
            recovered.append(
                {"ticket": ticket, "reason": "deferred_cleanup", "pid": os.getpid()}
            )
        else:
            with _deferred_abandons_lock:
                _deferred_abandons.discard(ticket)
    return st, recovered


def _finalize_deferred_abandons(recovered: list[dict[str, object]]) -> None:
    """Drop in-memory ownership only after recovered queue state is durable."""
    for entry in recovered:
        ticket = int(entry["ticket"])
        _untrack_our_ticket(ticket)
        with _deferred_abandons_lock:
            _deferred_abandons.discard(ticket)


def _abandon_our_tickets() -> None:
    """Best-effort abandon of tickets still claimed by this process (B099)."""
    with _our_tickets_lock:
        tickets = list(_our_tickets)
    for ticket in tickets:
        try:
            # atexit/SIGTERM cleanup must not spend the normal acquisition bound
            # per ticket. A surviving queue record is safely healed by dead PID.
            abandon_tts_play_ticket(ticket, lock_timeout_s=0.0)
        except Exception:
            pass


def _deferred_abandon_reaper() -> None:
    """Drain all delegated tickets with at most one retrying daemon thread."""
    global _deferred_abandon_thread
    try:
        while True:
            with _deferred_abandons_lock:
                tickets = list(_deferred_abandons)
                if not tickets:
                    _deferred_abandon_thread = None
                    return
            retry_after_error = False
            for ticket in tickets:
                if not _our_ticket_is_tracked(ticket):
                    with _deferred_abandons_lock:
                        _deferred_abandons.discard(ticket)
                    continue
                try:
                    _abandon_tts_play_ticket_now(ticket)
                except TtsPlayLockTimeout:
                    # The normal finite acquisition bound is the retry cadence.
                    continue
                except Exception as exc:
                    # Retain ownership and cleanup intent across transient I/O
                    # failures; one process-wide reaper retries with backoff.
                    retry_after_error = True
                    _syslog_tts_queue(
                        "tts.play_ticket_deferred_abandon_failed",
                        ticket=ticket,
                        error=str(exc)[:200],
                        level="warn",
                    )
                else:
                    with _deferred_abandons_lock:
                        _deferred_abandons.discard(ticket)
            if retry_after_error:
                time.sleep(_DEFERRED_ABANDON_RETRY_S)
    finally:
        with _deferred_abandons_lock:
            if _deferred_abandon_thread is threading.current_thread():
                _deferred_abandon_thread = None


def defer_tts_play_ticket_abandon(ticket: int) -> bool:
    """Delegate *ticket* to the process-wide bounded lock reaper."""
    global _deferred_abandon_thread
    ticket = int(ticket)
    with _deferred_abandons_lock:
        _deferred_abandons.add(ticket)
        if _deferred_abandon_thread is not None:
            return True
        worker = threading.Thread(
            target=_deferred_abandon_reaper,
            name="hark-tts-abandon-reaper",
            daemon=True,
        )
        _deferred_abandon_thread = worker
        try:
            worker.start()
        except BaseException:
            # Retain the request: a later publication attempt or successful
            # playback-lock transaction can apply it under the same flock.
            _deferred_abandon_thread = None
            _syslog_tts_queue(
                "tts.play_ticket_deferred_abandon_failed",
                ticket=ticket,
                error="could not start deferred abandonment worker",
                level="warn",
            )
            return False
    return True


def _ensure_cleanup_hooks() -> None:
    """Install atexit + SIGTERM chain so claimed tickets are abandoned on exit."""
    global _cleanup_hooks_installed
    if _cleanup_hooks_installed:
        return
    _cleanup_hooks_installed = True
    atexit.register(_abandon_our_tickets)
    try:
        prev = signal.getsignal(signal.SIGTERM)

        def _on_sigterm(signum: int, frame: object) -> None:
            _abandon_our_tickets()
            if callable(prev) and prev not in (signal.SIG_DFL, signal.SIG_IGN):
                prev(signum, frame)  # type: ignore[operator]
            else:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTERM)

        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception:
        # Restricted environments / non-main thread — atexit + PID heal remain.
        pass


def claim_tts_play_ticket(*, lock_timeout_s: float | None = None) -> int:
    """Reserve FIFO place *before* synth so launch order is preserved (B092).

    Call once per outer utterance (not per multi-chunk). Then
    :func:`exclusive_playback` with that ticket. On failure before play, call
    :func:`abandon_tts_play_ticket` so the queue cannot stall.

    Records ``pid`` + ``claimed_at`` on the ticket (B099) so other waiters can
    advance past a dead holder.
    """
    lock_path = tts_play_lock_path()
    queue_path = tts_play_queue_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    locked = False
    try:
        _acquire_play_lock(fd, operation="claim", timeout_s=lock_timeout_s)
        locked = True
        st = _queue_read(queue_path)
        # Recover retained same-process cleanup first, then heal dead heads.
        st, recovered = _recover_deferred_abandons_locked(st)
        st, healed = _heal_abandoned_locked(st, missing_as_abandoned=False)
        healed = [*recovered, *healed]
        ticket = int(st["next"])
        st["next"] = ticket + 1
        if int(st["serving"]) > int(st["next"]):
            st["serving"] = ticket
        _set_holder(st, ticket, pid=os.getpid(), claimed_at=time.time())
        _queue_write(queue_path, st)
        _finalize_deferred_abandons(recovered)
        _track_our_ticket(ticket)
        for h in healed:
            _syslog_tts_queue(
                "tts.play_queue_healed",
                ticket=h.get("ticket"),
                reason=h.get("reason"),
                pid=h.get("pid"),
                where="claim",
            )
        return ticket
    finally:
        _close_play_lock_fd(fd, locked=locked)


def abandon_tts_play_ticket(
    ticket: int,
    *,
    lock_timeout_s: float | None = None,
) -> None:
    """Drop a locally claimed ticket, or join its delegated abandonment."""
    ticket = int(ticket)
    with _deferred_abandons_lock:
        delegated = (
            ticket in _deferred_abandons and _deferred_abandon_thread is not None
        )
    if delegated or not _our_ticket_is_tracked(ticket):
        return
    _abandon_tts_play_ticket_now(ticket, lock_timeout_s=lock_timeout_s)
    with _deferred_abandons_lock:
        _deferred_abandons.discard(ticket)


def _abandon_tts_play_ticket_now(
    ticket: int,
    *,
    lock_timeout_s: float | None = None,
) -> None:
    """Drop a claimed ticket without playing (synth error / early return / exit)."""
    ticket = int(ticket)
    lock_path = tts_play_lock_path()
    queue_path = tts_play_queue_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    locked = False
    abandoned = False
    try:
        _acquire_play_lock(
            fd,
            operation="abandon",
            ticket=ticket,
            timeout_s=lock_timeout_s,
        )
        locked = True
        st = _queue_read(queue_path)
        _queue_write(queue_path, _abandon_ticket_locked(st, ticket))
        abandoned = True
    finally:
        if abandoned:
            _untrack_our_ticket(ticket)
        _close_play_lock_fd(fd, locked=locked)


def heal_tts_play_queue(*, missing_as_abandoned: bool = True) -> dict[str, Any]:
    """Detect abandoned tickets and advance ``serving`` (B099).

    *missing_as_abandoned*: treat tickets with no holder record (legacy queue or
    crash before holder write) as abandoned. Safe for doctor/ambient startup;
    waiters use a grace period before enabling this.

    Returns a status dict suitable for doctor / syslog.
    """
    lock_path = tts_play_lock_path()
    queue_path = tts_play_queue_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    before = _queue_read(queue_path) if queue_path.is_file() else _empty_queue_state()
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    locked = False
    try:
        _acquire_play_lock(fd, operation="heal")
        locked = True
        st = _queue_read(queue_path)
        st, recovered = _recover_deferred_abandons_locked(st)
        st, healed = _heal_abandoned_locked(
            st, missing_as_abandoned=missing_as_abandoned
        )
        healed = [*recovered, *healed]
        if healed or int(st["serving"]) != int(before.get("serving", 0)):
            _queue_write(queue_path, st)
            _finalize_deferred_abandons(recovered)
        for h in healed:
            _syslog_tts_queue(
                "tts.play_queue_healed",
                ticket=h.get("ticket"),
                reason=h.get("reason"),
                pid=h.get("pid"),
                where="heal",
                level="warn",
            )
        stuck = int(st["serving"]) < int(st["next"])
        return {
            "path": str(queue_path),
            "serving": int(st["serving"]),
            "next": int(st["next"]),
            "pending": max(0, int(st["next"]) - int(st["serving"])),
            "cancelled": list(st.get("cancelled") or []),
            "holders": _normalize_holders(st.get("holders")),
            "healed": healed,
            "healed_count": len(healed),
            "stuck": stuck and not healed,
            "ok": True,
        }
    except TtsPlayLockTimeout as exc:
        return {
            "path": str(queue_path),
            "ok": False,
            "error": str(exc)[:200],
            "error_type": "tts_play_lock_timeout",
            "tts_play_lock": exc.as_dict(),
            "healed": [],
            "healed_count": 0,
            "stuck": False,
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "path": str(queue_path),
            "ok": False,
            "error": str(exc)[:200],
            "healed": [],
            "healed_count": 0,
            "stuck": False,
        }
    finally:
        _close_play_lock_fd(fd, locked=locked)


def inspect_tts_play_queue() -> dict[str, Any]:
    """Read queue status without healing (tests / diagnostics)."""
    queue_path = tts_play_queue_path()
    if not queue_path.is_file():
        st = _empty_queue_state()
    else:
        st = _queue_read(queue_path)
    pending = max(0, int(st["next"]) - int(st["serving"]))
    return {
        "path": str(queue_path),
        "serving": int(st["serving"]),
        "next": int(st["next"]),
        "pending": pending,
        "cancelled": list(st.get("cancelled") or []),
        "holders": _normalize_holders(st.get("holders")),
        "exists": queue_path.is_file(),
    }


@contextmanager
def exclusive_playback(
    ticket: int | None = None,
    *,
    wait_timeout_s: float | None = None,
) -> Iterator[None]:
    """Hold the global TTS speaker for *ticket* (FIFO). Re-entrant same thread.

    Prefer claiming with :func:`claim_tts_play_ticket` **before** synthesize so
    five concurrent ``hark tts`` keep launch order even if synth finishes out
    of order. If *ticket* is None, claim now (play-time claim).

    While waiting for our turn, dead-PID heads are auto-healed (B099). After
    :data:`_MISSING_HOLDER_GRACE_S`, heads with no holder record are also
    skipped (legacy abandoned tickets).

    *wait_timeout_s*: if set, raise ``TtsPlayTimeout`` after this many seconds
    waiting for the speaker (ticket is abandoned so the queue does not stall).
    Use a short timeout for ambient boot TTS so wake arming is never blocked.
    When unset, finite flock probes still surface owner diagnostics via syslog
    but the waiter keeps its ticket until the live head releases the speaker.
    """
    depth = int(getattr(_play_tls, "depth", 0) or 0)
    if depth > 0:
        _play_tls.depth = depth + 1
        try:
            yield
        finally:
            _play_tls.depth = depth
        return

    import fcntl

    wait_start = time.monotonic()
    if ticket is None:
        claim_lock_timeout = None
        if wait_timeout_s is not None:
            claim_lock_timeout = min(_PLAY_LOCK_ACQUIRE_TIMEOUT_S, wait_timeout_s)
        ticket = claim_tts_play_ticket(lock_timeout_s=claim_lock_timeout)

    lock_path = tts_play_lock_path()
    queue_path = tts_play_queue_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    advanced = False
    abandon_deferred = False
    locked = False
    try:
        # Wait until we are head of line, then hold lock through playback
        while True:
            elapsed = time.monotonic() - wait_start
            lock_timeout_s = None
            if wait_timeout_s is not None:
                lock_timeout_s = min(
                    _PLAY_LOCK_ACQUIRE_TIMEOUT_S,
                    max(0.0, wait_timeout_s - elapsed),
                )
            try:
                _acquire_play_lock(
                    fd,
                    operation="wait",
                    ticket=ticket,
                    timeout_s=lock_timeout_s,
                )
                locked = True
            except TtsPlayLockTimeout as exc:
                total_elapsed = time.monotonic() - wait_start
                # Playback holds the flock for the full utterance. Unbounded
                # waiters must retry those finite acquire probes without
                # abandoning their FIFO ticket. Bounded waiters only fail once
                # the overall wait budget is exhausted.
                if wait_timeout_s is None or total_elapsed < wait_timeout_s:
                    _syslog_tts_queue(
                        "tts.play_lock_wait_retry",
                        ticket=ticket,
                        wait_timeout_s=wait_timeout_s,
                        elapsed_s=total_elapsed,
                        lock_timeout_s=exc.timeout_s,
                        lock_owner_pid=exc.lock_owner_pid,
                        queue_owner_pid=exc.queue_owner_pid,
                        serving=int(exc.queue_state.get("serving", 0)),
                        next=int(exc.queue_state.get("next", 0)),
                        level="info",
                    )
                    continue
                abandon_deferred = defer_tts_play_ticket_abandon(ticket)
                _syslog_tts_queue(
                    "tts.play_queue_wait_timeout",
                    ticket=ticket,
                    wait_timeout_s=wait_timeout_s,
                    serving=int(exc.queue_state.get("serving", 0)),
                    next=int(exc.queue_state.get("next", 0)),
                    lock_owner_pid=exc.lock_owner_pid,
                    level="warn",
                )
                raise
            elapsed = time.monotonic() - wait_start
            missing = elapsed >= _MISSING_HOLDER_GRACE_S
            st = _queue_read(queue_path)
            st, recovered = _recover_deferred_abandons_locked(st)
            st, healed = _heal_abandoned_locked(st, missing_as_abandoned=missing)
            st = _skip_cancelled_heads(st)
            healed = [*recovered, *healed]
            _queue_write(queue_path, st)
            _finalize_deferred_abandons(recovered)
            for h in healed:
                _syslog_tts_queue(
                    "tts.play_queue_healed",
                    ticket=h.get("ticket"),
                    reason=h.get("reason"),
                    pid=h.get("pid"),
                    where="wait",
                    waiter=ticket,
                )
            if int(st["serving"]) == ticket:
                break
            if wait_timeout_s is not None and elapsed >= wait_timeout_s:
                _queue_write(queue_path, _abandon_ticket_locked(st, ticket))
                _untrack_our_ticket(ticket)
                advanced = True
                error = TtsPlayQueueTimeout(
                    operation="queue_wait",
                    timeout_s=wait_timeout_s,
                    elapsed_s=elapsed,
                    ticket=ticket,
                    lock_owner_pid=None,
                    queue_state=st,
                )
                fcntl.flock(fd, fcntl.LOCK_UN)
                locked = False
                _syslog_tts_queue(
                    "tts.play_queue_wait_timeout",
                    ticket=ticket,
                    wait_timeout_s=wait_timeout_s,
                    serving=int(st["serving"]),
                    next=int(st["next"]),
                    queue_owner_pid=error.queue_owner_pid,
                    level="warn",
                )
                raise error
            fcntl.flock(fd, fcntl.LOCK_UN)
            locked = False
            time.sleep(0.03)

        _play_tls.depth = 1
        try:
            yield
        finally:
            primary_active = sys.exc_info()[0] is not None
            _play_tls.depth = 0
            try:
                st = _queue_read(queue_path)
                if int(st["serving"]) == ticket:
                    _queue_write(queue_path, _advance_serving(st))
                    advanced = True
                _untrack_our_ticket(ticket)
            except BaseException:
                if not primary_active:
                    raise
            finally:
                if locked:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except BaseException:
                        if not primary_active:
                            raise
                    finally:
                        locked = False
    except BaseException:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BaseException:
                pass
            locked = False
        if not advanced and not abandon_deferred:
            try:
                abandon_tts_play_ticket(ticket, lock_timeout_s=0.0)
            except TtsPlayLockTimeout:
                abandon_deferred = defer_tts_play_ticket_abandon(ticket)
            except Exception:
                pass
        raise
    finally:
        _close_play_lock_fd(fd, locked=locked)


def sniff_audio_format(data: bytes) -> str:
    """Return a short format tag: wav | mp3 | ogg | flac | pcm | unknown."""
    if len(data) < 4:
        return "unknown"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    return "pcm"


def estimate_duration_ms(data: bytes, sample_rate: int | None = None) -> int:
    """Best-effort duration from bytes (WAV exact; else ffprobe; else bitrate guess)."""
    fmt = sniff_audio_format(data)
    if fmt == "wav":
        try:
            with wave.open(io.BytesIO(data), "rb") as wf:
                return int(1000 * wf.getnframes() / max(1, wf.getframerate()))
        except Exception:
            pass
    if fmt == "pcm" and sample_rate:
        # 16-bit mono
        return int(1000 * (len(data) / 2) / sample_rate)
    # ffprobe
    if shutil.which("ffprobe"):
        with tempfile.NamedTemporaryFile(
            suffix=f".{fmt if fmt != 'unknown' else 'bin'}", delete=False
        ) as tmp:
            p = Path(tmp.name)
        try:
            p.write_bytes(data)
            r = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(p),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return max(0, int(float(r.stdout.strip()) * 1000))
        except Exception:
            pass
        finally:
            p.unlink(missing_ok=True)
    # MP3 crude: assume ~16 KB/s at 128kbps
    if fmt == "mp3" and len(data) > 0:
        return int(1000 * len(data) / 16000)
    return 0


def write_audio(path: Path | str, data: bytes, *, sample_rate: int = 16000) -> Path:
    path = Path(path)
    fmt = sniff_audio_format(data)
    suffix = path.suffix.lower()

    if fmt == "wav" or (fmt == "pcm" and suffix in (".wav", "")):
        if fmt == "wav":
            path.write_bytes(data)
        else:
            path.write_bytes(_pcm16_to_wav(data, sample_rate))
        return path

    if fmt == "mp3" and suffix in (".mp3", ".mpeg", ""):
        if not suffix:
            path = path.with_suffix(".mp3")
        path.write_bytes(data)
        return path

    if suffix == ".wav" or not suffix:
        if not suffix:
            path = path.with_suffix(".wav")
        return _ffmpeg_convert(data, path, fmt)

    path.write_bytes(data)
    return path


def write_wav(path: Path | str, pcm_or_wav: bytes, sample_rate: int = 16000) -> Path:
    return write_audio(path, pcm_or_wav, sample_rate=sample_rate)


def play_audio(
    data: bytes,
    *,
    sample_rate: int | None = None,
    playback_speed: float = 1.0,
    on_near_end: Callable[[], None] | None = None,
    near_end_ms: int = 0,
    exclusive: bool = True,
) -> PlayResult:
    """Play audio. Optional on_near_end fires ~near_end_ms before playback ends.

    Used so listen can arm ~0.3s before TTS finishes.

    ``exclusive`` (default True): take the cross-process TTS play lock (B092) so
    concurrent speakers queue. Pass False only when the caller already holds
    :func:`exclusive_playback`.
    """
    if exclusive:
        with exclusive_playback():
            return _play_audio_unlocked(
                data,
                sample_rate=sample_rate,
                playback_speed=playback_speed,
                on_near_end=on_near_end,
                near_end_ms=near_end_ms,
            )
    return _play_audio_unlocked(
        data,
        sample_rate=sample_rate,
        playback_speed=playback_speed,
        on_near_end=on_near_end,
        near_end_ms=near_end_ms,
    )


def _play_audio_unlocked(
    data: bytes,
    *,
    sample_rate: int | None = None,
    playback_speed: float = 1.0,
    on_near_end: Callable[[], None] | None = None,
    near_end_ms: int = 0,
) -> PlayResult:
    fmt = sniff_audio_format(data)
    if playback_speed != 1.0:
        data = _apply_playback_speed(
            data,
            fmt=fmt,
            sample_rate=sample_rate,
            playback_speed=playback_speed,
        )
        fmt = "wav"
        sample_rate = None
    duration_ms = estimate_duration_ms(data, sample_rate)

    def _maybe_schedule_near_end() -> threading.Timer | None:
        if not on_near_end or near_end_ms <= 0 or duration_ms <= 0:
            return None
        delay = max(0.0, (duration_ms - near_end_ms) / 1000.0)
        t = threading.Timer(delay, on_near_end)
        t.daemon = True
        t.start()
        return t

    timer = _maybe_schedule_near_end()
    t0 = time.monotonic()
    try:
        if fmt == "wav":
            pcm, sr = _wav_to_pcm16(data)
            _play_pcm16(pcm, sr)
        elif fmt == "pcm":
            _play_pcm16(data, sample_rate or 24000)
        else:
            ext = ".mp3" if fmt == "mp3" else f".{fmt}"
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                p = Path(tmp.name)
            try:
                p.write_bytes(data)
                _play_file(p)
            finally:
                p.unlink(missing_ok=True)
    finally:
        if timer is not None:
            timer.cancel()

    wall_ms = int(1000 * (time.monotonic() - t0))
    return PlayResult(duration_ms=duration_ms or wall_ms, format=fmt)


def play_wav_bytes(
    data: bytes,
    *,
    sample_rate: int | None = None,
    playback_speed: float = 1.0,
    on_near_end: Callable[[], None] | None = None,
    near_end_ms: int = 0,
    exclusive: bool = True,
) -> PlayResult:
    return play_audio(
        data,
        sample_rate=sample_rate,
        playback_speed=playback_speed,
        on_near_end=on_near_end,
        near_end_ms=near_end_ms,
        exclusive=exclusive,
    )


def _atempo_filter(playback_speed: float) -> str:
    """Build an ffmpeg atempo chain for any finite speed greater than zero."""
    if not np.isfinite(playback_speed) or playback_speed <= 0:
        raise ValueError("playback_speed must be a finite number greater than 0")

    remaining = float(playback_speed)
    factors: list[float] = []
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(remaining)
    return ",".join(f"atempo={factor:.12g}" for factor in factors)


def _apply_playback_speed(
    data: bytes,
    *,
    fmt: str,
    sample_rate: int | None,
    playback_speed: float,
) -> bytes:
    """Return pitch-preserving, tempo-adjusted WAV audio via ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("tts.playback_speed other than 1.0 requires ffmpeg")

    suffix = ".raw" if fmt == "pcm" else f".{fmt}"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as src_tmp:
        src = Path(src_tmp.name)
        src_tmp.write(data)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as dest_tmp:
        dest = Path(dest_tmp.name)

    try:
        cmd = [ffmpeg, "-y"]
        if fmt == "pcm":
            cmd.extend(["-f", "s16le", "-ar", str(sample_rate or 24000), "-ac", "1"])
        cmd.extend(
            [
                "-i",
                str(src),
                "-filter:a",
                _atempo_filter(playback_speed),
                "-ac",
                "1",
                str(dest),
            ]
        )
        try:
            subprocess.run(cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode(errors="replace").strip()
            raise RuntimeError(f"could not apply TTS playback speed: {detail}") from exc
        return dest.read_bytes()
    finally:
        src.unlink(missing_ok=True)
        dest.unlink(missing_ok=True)


def _play_pcm16(pcm: bytes, sample_rate: int) -> None:
    if sd is not None:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return

        def _stop() -> None:
            try:
                sd.stop()
            except Exception:
                pass

        with _skip_stopper(_stop):
            sd.play(samples, sample_rate)
            sd.wait()
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        p = Path(tmp.name)
    try:
        p.write_bytes(_pcm16_to_wav(pcm, sample_rate))
        _play_file(p)
    finally:
        p.unlink(missing_ok=True)


def _play_file(path: Path) -> None:
    for cmd in (
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(path)],
        ["paplay", str(path)],
        ["aplay", str(path)],
    ):
        if not shutil.which(cmd[0]):
            continue
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (FileNotFoundError, OSError):
            continue

        def _stop(p: subprocess.Popen[bytes] = proc) -> None:
            try:
                p.terminate()
            except OSError:
                pass

        with _skip_stopper(_stop) as skip_gen:
            try:
                rc = proc.wait()
            except BaseException:
                # Mirror subprocess.run: never leak a live player on interrupt.
                try:
                    proc.kill()
                    proc.wait()
                except OSError:
                    pass
                raise
        if rc == 0:
            return
        if playback_skip_generation() != skip_gen:
            # User skip terminated the player (B161) — do not restart the same
            # audio through the next fallback player.
            return
    if shutil.which("ffmpeg") and sd is not None:
        wav_path = path.with_suffix(".decoded.wav")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-ac",
                    "1",
                    "-f",
                    "wav",
                    str(wav_path),
                ],
                check=True,
                capture_output=True,
            )
            pcm, sr = _wav_to_pcm16(wav_path.read_bytes())
            _play_pcm16(pcm, sr)
            return
        except (subprocess.CalledProcessError, OSError):
            pass
        finally:
            wav_path.unlink(missing_ok=True)
    raise RuntimeError(
        "could not play audio — need ffplay/ffmpeg for MP3 TTS, or WAV input"
    )


def _ffmpeg_convert(data: bytes, dest: Path, fmt: str) -> Path:
    if not shutil.which("ffmpeg"):
        alt = dest.with_suffix(".mp3" if fmt == "mp3" else dest.suffix or ".bin")
        alt.write_bytes(data)
        return alt
    with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as tmp:
        src = Path(tmp.name)
    try:
        src.write_bytes(data)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ac", "1", str(dest)],
            check=True,
            capture_output=True,
        )
        return dest
    finally:
        src.unlink(missing_ok=True)


def _pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        width = wf.getsampwidth()
        ch = wf.getnchannels()
        if width != 2:
            raise RuntimeError(f"unsupported WAV sample width {width}")
        if ch > 1:
            mono = bytearray()
            step = width * ch
            for i in range(0, len(frames), step):
                mono.extend(frames[i : i + width])
            frames = bytes(mono)
        return frames, sr

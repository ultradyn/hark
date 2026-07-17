"""Handsfree ambient + watch worker lifecycle (`hark start` / `stop` / `restart`).

Product path for always-on workers (ambient wake + ``hark watch --for-monitor``).
Shares ``mode-a.pids`` / log paths with ``scripts/run-mode-a.sh`` and optional
``hark daemon start --workers``. See docs/HARKD.md.

Not the experimental harkd supervisor — that remains ``hark daemon …``.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from hark.daemon import (
    DaemonConflict,
    busy_lock_path,
    clear_pid_file,
    harkd_pid_path,
    mode_a_pids_path,
    probe_harkd,
    probe_mode_a,
    spawn_mode_a_workers,
)
from hark.exitcodes import ERROR, OK, USAGE
from hark.lifecycle import set_shutdown_reason
from hark.paths import state_dir
from hark.worker_process import (
    WorkerRecord,
    WorkerSignalError,
    WorkerStateUnavailableError,
    collect_worker_records,
    record_matches_lifetime,
    record_matches_process,
    signal_worker_records,
    worker_records_match_request,
)

# Default grace after SIGTERM before SIGKILL (seconds). Matches run-mode-a.sh;
# overridable via HARK_STOP_GRACE_S or --timeout.
DEFAULT_STOP_TIMEOUT_S = 120.0


def stop_timeout_default() -> float:
    raw = (os.environ.get("HARK_STOP_GRACE_S") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_STOP_TIMEOUT_S


def parse_pids_text(text: str) -> list[int]:
    """Parse pidfile body: bare ints, optional ``pid=N``, skip blanks/# comments."""
    pids: list[int] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("pid="):
            line = line.split("=", 1)[1].strip()
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def collect_worker_pids(root: Path | None = None) -> list[int]:
    """Validated worker PIDs (unsafe legacy entries are migrated or removed)."""
    root = root or state_dir()
    return [record.pid for record in collect_worker_records(mode_a_pids_path(root))]


def assert_no_live_harkd(root: Path | None = None) -> None:
    """Refuse to start handsfree workers while experimental harkd is live."""
    root = root or state_dir()
    harkd = probe_harkd(root)
    if harkd.running:
        pids = ", ".join(str(p) for p in harkd.pids)
        raise DaemonConflict(
            f"harkd is running (pid {pids} via {harkd_pid_path(root).name}); "
            "stop it first: hark daemon stop "
            "(handsfree workers and harkd must not both own ambient/watch — "
            "see docs/HARKD.md)"
        )
    # Stale harkd.pid
    path = harkd_pid_path(root)
    if path.is_file() and not harkd.running:
        clear_pid_file(path)


def _still_same_workers(records: list[WorkerRecord]) -> list[WorkerRecord]:
    return [
        record
        for record in records
        if (
            record_matches_lifetime(record)
            if record.provisional
            else record_matches_process(record)
        )
    ]


def stop_workers(
    root: Path | None = None,
    *,
    timeout_s: float | None = None,
    force: bool = False,
    reason: str = "stop",
) -> dict[str, Any]:
    """Stop ambient/watch workers via mode-a.pids.

    SIGTERM first; after *timeout_s* (or immediately when *force*), SIGKILL remaining.
    Clears the pidfile when nothing is left. Stages shutdown_reason for ambient TTS.
    """
    root = root or state_dir()
    if timeout_s is None:
        timeout_s = stop_timeout_default()
    if force:
        timeout_s = min(float(timeout_s), 0.5)

    path = mode_a_pids_path(root)
    try:
        records = collect_worker_records(path)
    except WorkerStateUnavailableError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "pids": list(exc.pids),
            "message": "worker identity unavailable; retaining ownership state",
        }
    live = [record.pid for record in records]
    if not records:
        return {
            "ok": True,
            "stopped": [],
            "killed": [],
            "message": "no Hark workers running",
            "pids": [],
        }

    set_shutdown_reason(reason)
    signal_results = [signal_worker_records(records, signal.SIGTERM)]

    deadline = time.monotonic() + max(0.0, float(timeout_s))
    while time.monotonic() < deadline:
        still_records = _still_same_workers(records)
        still = [record.pid for record in still_records]
        if not still_records:
            collect_worker_records(path)
            busy = busy_lock_path(root)
            try:
                busy.unlink(missing_ok=True)
            except OSError:
                pass
            if signal_results[0].errors:
                return {
                    "ok": False,
                    "stopped": live,
                    "killed": [],
                    "error": str(WorkerSignalError(signal_results)),
                    "pids": [],
                }
            return {
                "ok": True,
                "stopped": live,
                "killed": [],
                "message": "workers stopped",
                "pids": [],
            }
        # Canonicalize the current file transactionally without replacing it
        # from this older stop snapshot; a concurrent fresh owner must survive.
        collect_worker_records(path)
        # Prefer waiting out an active recording when busy.lock is present
        # (same intent as run-mode-a.sh --stop).
        time.sleep(0.1 if busy_lock_path(root).is_file() else 0.05)

    still_records = _still_same_workers(records)
    still = [record.pid for record in still_records]
    killed: list[int] = []
    if still_records:
        kill_result = signal_worker_records(still_records, signal.SIGKILL)
        signal_results.append(kill_result)
        killed = [record.pid for record in kill_result.sent_records]
        # brief wait for reaping
        kill_deadline = time.monotonic() + 2.0
        while time.monotonic() < kill_deadline:
            still_records = _still_same_workers(still_records)
            still = [record.pid for record in still_records]
            if not still_records:
                break
            time.sleep(0.05)
        still_records = _still_same_workers(still_records)
        still = [record.pid for record in still_records]

    signal_failures = [result for result in signal_results if result.errors]
    if still or signal_failures:
        collect_worker_records(path)
        errors: list[str] = []
        if signal_failures:
            errors.append(str(WorkerSignalError(signal_failures)))
        if still:
            errors.append(f"still running after SIGKILL: {still}")
        return {
            "ok": False,
            "stopped": [p for p in live if p not in still],
            "killed": killed,
            "still_running": still,
            "error": "; ".join(errors),
            "pids": still,
        }

    collect_worker_records(path)
    try:
        busy_lock_path(root).unlink(missing_ok=True)
    except OSError:
        pass
    return {
        "ok": True,
        "stopped": live,
        "killed": killed,
        "message": "workers stopped" if not killed else "workers force-killed",
        "pids": [],
    }


def start_workers(
    root: Path | None = None,
    *,
    session: str = "default",
    do_watch: bool = True,
    do_ambient: bool = True,
    settle_s: float = 0.3,
) -> dict[str, Any]:
    """Idempotent start of ambient + watch workers (detached).

    If workers are already live in mode-a.pids, returns success without spawning.
    Refuses when harkd is live.
    """
    root = root or state_dir()
    if not do_watch and not do_ambient:
        return {
            "ok": False,
            "error": "nothing to start (--no-watch and --no-ambient)",
            "pids": [],
        }

    try:
        assert_no_live_harkd(root)
    except DaemonConflict as exc:
        return {"ok": False, "error": str(exc), "pids": collect_worker_pids(root)}

    path = mode_a_pids_path(root)
    try:
        existing_records = collect_worker_records(path)
    except WorkerStateUnavailableError as exc:
        return {"ok": False, "error": str(exc), "pids": list(exc.pids)}
    existing = [record.pid for record in existing_records]
    if existing_records and worker_records_match_request(
        existing_records,
        watch=do_watch,
        ambient=do_ambient,
        session=session,
    ):
        return {
            "ok": True,
            "already_running": True,
            "pids": existing,
            "message": (
                f"workers already running (pids {', '.join(str(p) for p in existing)})"
            ),
        }
    if existing_records:
        return {
            "ok": False,
            "already_running": False,
            "pids": existing,
            "error": (
                "existing workers do not exactly match requested roles, session, "
                "and state scope; stop them before starting"
            ),
        }

    root.mkdir(parents=True, exist_ok=True)
    try:
        children = spawn_mode_a_workers(
            session=session,
            do_watch=do_watch,
            do_ambient=do_ambient,
            root=root,
            log_dir=root,
        )
    except OSError as exc:
        # Another compatible starter may have won while this caller waited for
        # the same pidfile transaction lock. Reclassify that lock winner as the
        # idempotent already-running outcome, never a spurious start failure.
        try:
            winner = collect_worker_records(path)
        except WorkerStateUnavailableError:
            winner = []
        if winner and worker_records_match_request(
            winner,
            watch=do_watch,
            ambient=do_ambient,
            session=session,
        ):
            pids = [record.pid for record in winner]
            return {
                "ok": True,
                "already_running": True,
                "pids": pids,
                "message": (
                    "workers already running "
                    f"(pids {', '.join(str(pid) for pid in pids)})"
                ),
            }
        return {"ok": False, "error": f"failed to spawn workers: {exc}", "pids": []}

    started = [c.pid for c in children if c.pid]
    if settle_s > 0:
        time.sleep(settle_s)

    # Revalidate the exact requested ready roles after the settle window.  A
    # nonempty partial set is failure, never a successful start.
    live_records = collect_worker_records(path)
    live = [record.pid for record in live_records]
    if not worker_records_match_request(
        live_records,
        watch=do_watch,
        ambient=do_ambient,
        session=session,
    ):
        attempt_pids = set(started)
        attempt_records = [
            record for record in live_records if record.pid in attempt_pids
        ]
        signal_results = [signal_worker_records(attempt_records, signal.SIGTERM)]
        cleanup_deadline = time.monotonic() + 2.0
        remaining = _still_same_workers(attempt_records)
        while remaining and time.monotonic() < cleanup_deadline:
            time.sleep(0.02)
            remaining = _still_same_workers(remaining)
        if remaining:
            signal_results.append(signal_worker_records(remaining, signal.SIGKILL))
        survivors = _still_same_workers(remaining)
        collect_worker_records(path)
        errors = [
            "workers did not settle into the exact requested role set "
            "(check ambient.jsonl / watch.jsonl)"
        ]
        signal_failures = [result for result in signal_results if result.errors]
        if signal_failures:
            errors.append(str(WorkerSignalError(signal_failures)))
        return {
            "ok": False,
            "error": "; ".join(errors),
            "pids": [record.pid for record in survivors],
            "started": started,
        }

    return {
        "ok": True,
        "already_running": False,
        "pids": live,
        "message": f"started workers (pids {', '.join(str(p) for p in live)})",
        "logs": {
            "watch": str(root / "watch.jsonl"),
            "ambient": str(root / "ambient.jsonl"),
        },
    }


def restart_workers(
    root: Path | None = None,
    *,
    session: str = "default",
    do_watch: bool = True,
    do_ambient: bool = True,
    timeout_s: float | None = None,
    force: bool = False,
    settle_s: float = 0.3,
) -> dict[str, Any]:
    """Stop then start workers (reason=restart for ambient TTS cue)."""
    root = root or state_dir()
    stop_result = stop_workers(root, timeout_s=timeout_s, force=force, reason="restart")
    if not stop_result.get("ok"):
        return {
            "ok": False,
            "error": stop_result.get("error") or "stop failed before restart",
            "stop": stop_result,
            "pids": stop_result.get("pids") or [],
        }
    # Brief pause so ambient can finish shutdown TTS / release mic.
    time.sleep(0.2)
    start_result = start_workers(
        root,
        session=session,
        do_watch=do_watch,
        do_ambient=do_ambient,
        settle_s=settle_s,
    )
    return {
        "ok": bool(start_result.get("ok")),
        "stop": stop_result,
        "start": start_result,
        "pids": start_result.get("pids") or [],
        "message": start_result.get("message") or start_result.get("error"),
        "error": start_result.get("error"),
    }


def workers_status(root: Path | None = None) -> dict[str, Any]:
    root = root or state_dir()
    mode_a = probe_mode_a(root)
    harkd = probe_harkd(root)
    from hark.monitor_feed import probe_monitor_consumer

    monitor = probe_monitor_consumer(root)
    return {
        "state_dir": str(root),
        "workers": {
            "running": mode_a.running,
            "pids": mode_a.pids,
            "pidfile": mode_a.pidfile,
        },
        "harkd": {
            "running": harkd.running,
            "pids": harkd.pids,
        },
        "monitor": monitor,
        "busy_lock": busy_lock_path(root).is_file(),
        "logs": {
            "watch": str(root / "watch.jsonl"),
            "ambient": str(root / "ambient.jsonl"),
            "system": str(root / "system.jsonl"),
        },
    }


def _print_result(result: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, indent=2))
    else:
        msg = result.get("message") or result.get("error")
        if msg:
            print(msg)
        elif result.get("ok"):
            print("ok")
        else:
            print(result.get("error") or "error", file=sys.stderr)
        logs = result.get("logs")
        if isinstance(logs, dict) and not result.get("already_running"):
            if logs.get("watch"):
                print(f"  watch log:   {logs['watch']}")
            if logs.get("ambient"):
                print(f"  ambient log: {logs['ambient']}")
        # Nested start from restart
        start = result.get("start")
        if isinstance(start, dict) and start.get("logs") and not as_json:
            slog = start["logs"]
            if isinstance(slog, dict):
                if slog.get("watch"):
                    print(f"  watch log:   {slog['watch']}")
                if slog.get("ambient"):
                    print(f"  ambient log: {slog['ambient']}")
    return OK if result.get("ok") else ERROR


def cmd_start(args: Any) -> int:
    if getattr(args, "status", False):
        st = workers_status()
        if getattr(args, "json", False):
            print(json.dumps(st, indent=2))
        else:
            w = st["workers"]
            if w["running"]:
                print(f"workers: running (pids {', '.join(str(p) for p in w['pids'])})")
            else:
                print("workers: not running")
            h = st["harkd"]
            if h["running"]:
                print(f"harkd: running (pids {', '.join(str(p) for p in h['pids'])})")
            else:
                print("harkd: not running")
            mon = st.get("monitor") or {}
            if mon.get("running"):
                print(f"monitor: running (pid {mon.get('pid')})")
            else:
                print("monitor: not running")
            print(f"state_dir: {st['state_dir']}")
        return OK

    # B125: session-local profile skips Herdr watch unless --force-watch.
    # Explicit --no-watch always wins; --force-watch overrides session_local.
    force_watch = bool(getattr(args, "force_watch", False))
    no_watch = bool(getattr(args, "no_watch", False))
    if no_watch:
        do_watch = False
    elif force_watch:
        do_watch = True
    else:
        try:
            from hark.session_profile import should_start_watch

            do_watch = should_start_watch()
        except Exception:
            do_watch = True
    do_ambient = not bool(getattr(args, "no_ambient", False))
    if not do_watch and not do_ambient:
        print(
            "hark start: nothing to start (--no-watch and --no-ambient)",
            file=sys.stderr,
        )
        return USAGE
    result = start_workers(
        session=str(getattr(args, "session", None) or "default"),
        do_watch=do_watch,
        do_ambient=do_ambient,
    )
    if not do_watch and result.get("ok"):
        result = {
            **result,
            "watch_skipped": True,
            "watch_skip_reason": (
                "session_profile.scope=session_local" if not no_watch else "--no-watch"
            ),
        }
    return _print_result(result, as_json=bool(getattr(args, "json", False)))


def cmd_stop(args: Any) -> int:
    timeout = getattr(args, "timeout", None)
    result = stop_workers(
        timeout_s=float(timeout) if timeout is not None else stop_timeout_default(),
        force=bool(getattr(args, "force", False)),
        reason="stop",
    )
    return _print_result(result, as_json=bool(getattr(args, "json", False)))


def cmd_restart(args: Any) -> int:
    force_watch = bool(getattr(args, "force_watch", False))
    no_watch = bool(getattr(args, "no_watch", False))
    if no_watch:
        do_watch = False
    elif force_watch:
        do_watch = True
    else:
        try:
            from hark.session_profile import should_start_watch

            do_watch = should_start_watch()
        except Exception:
            do_watch = True
    do_ambient = not bool(getattr(args, "no_ambient", False))
    if not do_watch and not do_ambient:
        print(
            "hark restart: nothing to start (--no-watch and --no-ambient)",
            file=sys.stderr,
        )
        return USAGE
    timeout = getattr(args, "timeout", None)
    result = restart_workers(
        session=str(getattr(args, "session", None) or "default"),
        do_watch=do_watch,
        do_ambient=do_ambient,
        timeout_s=float(timeout) if timeout is not None else stop_timeout_default(),
        force=bool(getattr(args, "force", False)),
    )
    return _print_result(result, as_json=bool(getattr(args, "json", False)))


def add_lifecycle_parsers(sub: Any) -> None:
    """Register ``start`` / ``stop`` / ``restart`` on the main hark subparsers."""
    st = sub.add_parser(
        "start",
        help=(
            "start handsfree workers (ambient + watch --for-monitor); "
            "idempotent; writes mode-a.pids"
        ),
    )
    st.add_argument(
        "--no-watch",
        action="store_true",
        help="do not start Herdr watch",
    )
    st.add_argument(
        "--force-watch",
        action="store_true",
        help=(
            "start Herdr watch even if session profile scope is session_local (B125)"
        ),
    )
    st.add_argument(
        "--no-ambient",
        action="store_true",
        help="do not start ambient wake loop",
    )
    st.add_argument(
        "--session",
        default="default",
        help="Herdr session id for watch (default: default)",
    )
    st.add_argument(
        "--status",
        action="store_true",
        help="print worker running state only (do not start)",
    )
    st.add_argument("--json", action="store_true")

    sp = sub.add_parser(
        "stop",
        help="stop handsfree workers (SIGTERM, then SIGKILL after grace)",
    )
    sp.add_argument(
        "--force",
        action="store_true",
        help="short grace then SIGKILL (default still SIGKILLs after --timeout)",
    )
    sp.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            f"seconds to wait after SIGTERM before SIGKILL "
            f"(default {DEFAULT_STOP_TIMEOUT_S:g} or HARK_STOP_GRACE_S)"
        ),
    )
    sp.add_argument("--json", action="store_true")

    rs = sub.add_parser(
        "restart",
        help="stop then start handsfree workers",
    )
    rs.add_argument(
        "--no-watch",
        action="store_true",
        help="after stop: do not start Herdr watch",
    )
    rs.add_argument(
        "--force-watch",
        action="store_true",
        help="after stop: start watch even if session profile is session_local (B125)",
    )
    rs.add_argument(
        "--no-ambient",
        action="store_true",
        help="after stop: do not start ambient wake loop",
    )
    rs.add_argument(
        "--session",
        default="default",
        help="Herdr session id for watch (default: default)",
    )
    rs.add_argument(
        "--force",
        action="store_true",
        help="short stop grace before restart",
    )
    rs.add_argument(
        "--timeout",
        type=float,
        default=None,
        help=(
            f"stop grace seconds (default {DEFAULT_STOP_TIMEOUT_S:g} or HARK_STOP_GRACE_S)"
        ),
    )
    rs.add_argument("--json", action="store_true")

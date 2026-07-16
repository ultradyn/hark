"""Experimental harkd process scaffold (optional always-on vs handsfree CLI).

Not required for handsfree v1. See docs/HARKD.md for the full boundary spec.

v0 provides:
  - single-instance pidfile under the shared XDG state dir
  - status / stop via that pidfile
  - refuse start when handsfree workers (mode-a.pids) or another harkd is live
  - optional --workers to supervise the same ambient/watch processes the launcher uses

It does **not** auto-deliver answers (no silent double-send with the skill path).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

from hark import __version__
from hark.exitcodes import ERROR, OK, USAGE
from hark.paths import state_dir
from hark.worker_process import (
    WORKER_PIDFILE_ENV,
    WORKER_ROLE_ENV,
    WorkerRecord,
    capture_worker_identity,
    collect_worker_records,
    open_process_handle,
    read_worker_records,
    record_matches_lifetime,
    record_matches_process,
    replace_owned_worker_records,
    signal_process_handle,
    signal_worker_records,
    signal_worker_lifetime,
    worker_pidfile_lock,
    wait_for_worker_role,
    write_worker_pidfile_bytes,
    write_worker_pidfile_bytes_direct,
    write_worker_records,
)

HARKD_PID_NAME = "harkd.pid"
MODE_A_PIDS_NAME = "mode-a.pids"
BUSY_NAME = "busy.lock"
MIC_LOCK_NAME = "mic.lock"


class DaemonConflict(RuntimeError):
    """Another owner already holds always-on workers or harkd."""


class WorkerSpawnError(OSError):
    """A worker-set startup failed, including any rollback diagnostics."""

    def __init__(
        self,
        failed_role: str,
        cause: BaseException,
        rollback_failures: Sequence[str] = (),
    ) -> None:
        self.failed_role = failed_role
        self.cause = cause
        self.rollback_failures = tuple(rollback_failures)
        message = f"{failed_role} startup failed: {cause}"
        if self.rollback_failures:
            message += "; rollback failures: " + "; ".join(self.rollback_failures)
        super().__init__(message)


# If both durable publication paths fail, closing the only pidfd would discard
# immutable authority over a surviving child. Retain those handles for process
# lifetime and report their descriptors in the raised rollback diagnostics.
_RETAINED_ROLLBACK_PIDFDS: dict[int, int] = {}


@dataclass
class ProcessProbe:
    running: bool
    pids: list[int] = field(default_factory=list)
    pidfile: str | None = None


@dataclass
class DaemonStatus:
    state_dir: str
    harkd: ProcessProbe
    mode_a: ProcessProbe
    busy_lock: bool
    mic_lock: bool
    version: str = __version__

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_dir": self.state_dir,
            "harkd": asdict(self.harkd),
            "mode_a": asdict(self.mode_a),
            "busy_lock": self.busy_lock,
            "mic_lock": self.mic_lock,
            "version": self.version,
        }


def harkd_pid_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / HARKD_PID_NAME


def mode_a_pids_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / MODE_A_PIDS_NAME


def busy_lock_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / BUSY_NAME


def mic_lock_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / MIC_LOCK_NAME


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not signalable by us — treat as live.
        return True
    except OSError:
        return False
    # Zombies remain in the process table until reaped; treat as not running
    # so stop/status do not hang waiting on a dead child we are not parenting.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # format: pid (comm) state ... — comm may contain spaces/parens
        rparen = stat.rfind(")")
        if rparen != -1:
            rest = stat[rparen + 2 :].split()
            if rest and rest[0] == "Z":
                return False
    except OSError:
        pass
    return True


def read_pids_file(path: Path) -> list[int]:
    if not path.is_file():
        return []
    pids: list[int] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # allow "pid=123" or bare "123"
        if line.startswith("pid="):
            line = line.split("=", 1)[1].strip()
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def live_pids_from_file(path: Path) -> list[int]:
    return sorted({p for p in read_pids_file(path) if pid_alive(p)})


def write_pid_file(path: Path, pids: Sequence[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    live = [p for p in pids if pid_alive(p)]
    if not live:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    path.write_text("".join(f"{p}\n" for p in live), encoding="utf-8")


def clear_pid_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def probe_harkd(root: Path | None = None) -> ProcessProbe:
    root = root or state_dir()
    path = harkd_pid_path(root)
    live = live_pids_from_file(path)
    return ProcessProbe(
        running=bool(live),
        pids=live,
        pidfile=str(path) if path.exists() else None,
    )


def probe_mode_a(root: Path | None = None) -> ProcessProbe:
    root = root or state_dir()
    path = mode_a_pids_path(root)
    live = [record.pid for record in collect_worker_records(path)]
    return ProcessProbe(
        running=bool(live),
        pids=live,
        pidfile=str(path) if path.exists() else None,
    )


def collect_status(root: Path | None = None) -> DaemonStatus:
    root = root or state_dir()
    return DaemonStatus(
        state_dir=str(root),
        harkd=probe_harkd(root),
        mode_a=probe_mode_a(root),
        busy_lock=busy_lock_path(root).is_file(),
        mic_lock=mic_lock_path(root).is_file(),
    )


def assert_can_start(root: Path | None = None, *, self_pid: int | None = None) -> None:
    """Refuse if another harkd or handsfree workers already own always-on role."""
    root = root or state_dir()
    harkd = probe_harkd(root)
    for pid in harkd.pids:
        if self_pid is not None and pid == self_pid:
            continue
        if pid_alive(pid):
            raise DaemonConflict(
                f"harkd already running (pid {pid}); stop it first: hark daemon stop"
            )
    mode_a = probe_mode_a(root)
    if mode_a.running:
        pids = ", ".join(str(p) for p in mode_a.pids)
        raise DaemonConflict(
            f"Hark workers are running (pids: {pids} via {MODE_A_PIDS_NAME}); "
            "stop them first (./scripts/run-mode-a.sh --stop) so harkd does not "
            "race ambient/watch or delivery ownership — see docs/HARKD.md"
        )


def acquire_harkd_pidfile(root: Path | None = None, *, pid: int | None = None) -> Path:
    """Write harkd.pid after exclusivity checks. Returns path."""
    root = root or state_dir()
    pid = os.getpid() if pid is None else pid
    assert_can_start(root, self_pid=pid)
    path = harkd_pid_path(root)
    # Stale pidfile (dead PID): replace
    existing = live_pids_from_file(path)
    if existing and existing != [pid]:
        raise DaemonConflict(
            f"harkd already running (pid {existing[0]}); stop it first: hark daemon stop"
        )
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def release_harkd_pidfile(root: Path | None = None, *, pid: int | None = None) -> None:
    root = root or state_dir()
    path = harkd_pid_path(root)
    if not path.is_file():
        return
    recorded = read_pids_file(path)
    if pid is not None and recorded and pid not in recorded:
        return
    clear_pid_file(path)


def stop_harkd(
    root: Path | None = None,
    *,
    timeout_s: float = 15.0,
    force: bool = False,
) -> dict[str, Any]:
    """SIGTERM the pid in harkd.pid; optionally SIGKILL after timeout."""
    root = root or state_dir()
    path = harkd_pid_path(root)
    live = live_pids_from_file(path)
    if not live:
        clear_pid_file(path)
        return {"ok": True, "stopped": [], "message": "harkd not running"}

    stopped: list[int] = []
    for pid in live:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            return {
                "ok": False,
                "stopped": stopped,
                "error": f"cannot signal pid {pid}: {exc}",
            }

    deadline = time.monotonic() + max(0.1, timeout_s)
    while time.monotonic() < deadline:
        still = [p for p in live if pid_alive(p)]
        if not still:
            clear_pid_file(path)
            return {"ok": True, "stopped": stopped, "message": "harkd stopped"}
        time.sleep(0.05)

    still = [p for p in live if pid_alive(p)]
    if still and force:
        for pid in still:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        clear_pid_file(path)
        return {
            "ok": True,
            "stopped": stopped,
            "killed": still,
            "message": "harkd force-killed",
        }

    if still:
        write_pid_file(path, still)
        return {
            "ok": False,
            "stopped": stopped,
            "still_running": still,
            "error": f"still running after {timeout_s:.0f}s; use --force",
        }

    clear_pid_file(path)
    return {"ok": True, "stopped": stopped, "message": "harkd stopped"}


def _hark_argv() -> list[str]:
    """Build argv prefix to re-enter the hark CLI (uv-friendly)."""
    test_executable = os.environ.get("HARK_TEST_WORKER_EXECUTABLE")
    if test_executable and os.environ.get("PYTEST_CURRENT_TEST"):
        # Integration tests need a harmless long-running process with the same
        # argv/provenance contract, without opening audio or network resources.
        return [test_executable]
    # Prefer the same interpreter running this package.
    return [sys.executable, "-m", "hark"]


def _rollback_worker_start(
    owned: Sequence[tuple[str, subprocess.Popen[Any]]],
    *,
    pid_path: Path,
    original_pidfile: bytes | None,
    restore_pidfile: bool,
    owned_records: dict[int, WorkerRecord] | None = None,
    owned_pidfds: dict[int, int] | None = None,
    timeout_s: float = 2.0,
) -> list[str]:
    """Stop/reap only workers owned by a failed startup attempt."""
    failures: list[str] = []
    records = owned_records or {}
    pidfds = owned_pidfds or {}

    def signal_owned(role: str, proc: subprocess.Popen[Any], sig: int) -> bool:
        record = records.get(proc.pid)
        pidfd = pidfds.get(proc.pid)
        if pidfd is not None:
            if record is not None and not record_matches_lifetime(record):
                return False
            try:
                signal_process_handle(pidfd, sig)
                return True
            except ProcessLookupError:
                return False
            except (OSError, ValueError) as exc:
                failures.append(f"{role} pidfd signal {sig} failed ({exc})")
                return False
        if record is not None:
            outcome = signal_worker_lifetime(record, sig)
            if outcome.error is not None:
                failures.append(f"{role} signal {sig} failed ({outcome.error})")
            return outcome.sent
        failures.append(
            f"{role} has no immutable lifetime handle; refusing raw PID signal"
        )
        return False

    for role, proc in reversed(owned):
        if proc.poll() is not None:
            continue
        signal_owned(role, proc, signal.SIGTERM)

    deadline = time.monotonic() + timeout_s
    for role, proc in reversed(owned):
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining if remaining > 0 else 0.1)
            continue
        except subprocess.TimeoutExpired:
            pass
        except OSError as exc:
            failures.append(f"{role} reap failed ({exc})")
            if proc.poll() is not None:
                continue

        signal_owned(role, proc, signal.SIGKILL)

        try:
            proc.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError) as exc:
            failures.append(f"{role} reap after SIGKILL failed ({exc})")

    survivors: list[tuple[str, subprocess.Popen[Any]]] = []
    for role, proc in owned:
        try:
            still_running = proc.poll() is None
        except OSError as exc:
            failures.append(f"{role} final status check failed ({exc})")
            # If status cannot be established, retain durable ownership rather
            # than risk making a live attempt-owned child undiscoverable.
            still_running = True
        if still_running:
            survivors.append((role, proc))

    if survivors:
        failures.append(
            "surviving workers still running: "
            + ", ".join(f"{role}={proc.pid}" for role, proc in survivors)
        )

    # A pidfd pins each direct child, so it is safe to recover structured
    # lifetime metadata here even when the earlier capture step failed.
    for role, proc in survivors:
        if proc.pid in records:
            continue
        recovered = capture_worker_identity(
            proc.pid,
            role=role,
            expected_parent_pid=os.getpid(),
            pidfile=pid_path,
        )
        if recovered is not None:
            records[proc.pid] = recovered

    retained_pidfds: set[int] = set()
    untracked_survivors = [
        (role, proc) for role, proc in survivors if proc.pid not in records
    ]
    for role, proc in untracked_survivors:
        pidfd = pidfds.get(proc.pid)
        if pidfd is None:
            continue
        _RETAINED_ROLLBACK_PIDFDS[proc.pid] = pidfd
        retained_pidfds.add(pidfd)
        failures.append(
            f"retained pidfd {pidfd} for untracked surviving {role} worker {proc.pid}"
        )

    if restore_pidfile or survivors:
        payload: bytes | None
        try:
            if survivors:
                payload = original_pidfile or b""
                if payload and not payload.endswith(b"\n"):
                    payload += b"\n"
                untracked = [f"{role}={proc.pid}" for role, proc in untracked_survivors]
                if untracked:
                    failures.append(
                        "surviving workers lack structured provisional identity: "
                        + ", ".join(untracked)
                    )
                payload += b"".join(
                    f"{replace(records[proc.pid], provisional=True).to_json()}\n".encode()
                    for _, proc in survivors
                    if proc.pid in records
                )
                write_worker_pidfile_bytes(pid_path, payload)
            elif original_pidfile is None:
                payload = None
                write_worker_pidfile_bytes(pid_path, payload)
            else:
                payload = original_pidfile
                write_worker_pidfile_bytes(pid_path, payload)
        except OSError as atomic_exc:
            try:
                write_worker_pidfile_bytes_direct(pid_path, payload)
                failures.append(
                    f"pidfile atomic restore failed ({atomic_exc}); used direct fallback"
                )
            except OSError as direct_exc:
                failures.append(
                    "pidfile restore failed "
                    f"(atomic: {atomic_exc}; direct: {direct_exc})"
                )
                for _, proc in survivors:
                    pidfd = pidfds.get(proc.pid)
                    if pidfd is None:
                        continue
                    _RETAINED_ROLLBACK_PIDFDS[proc.pid] = pidfd
                    retained_pidfds.add(pidfd)
                    failures.append(
                        f"retained pidfd {pidfd} for surviving worker {proc.pid}"
                    )

    for pidfd in pidfds.values():
        if pidfd in retained_pidfds:
            continue
        try:
            os.close(pidfd)
        except OSError as exc:
            failures.append(f"pidfd close failed ({exc})")

    return failures


def spawn_mode_a_workers(
    *,
    session: str = "default",
    do_watch: bool = True,
    do_ambient: bool = True,
    root: Path | None = None,
    log_dir: Path | None = None,
) -> list[subprocess.Popen[Any]]:
    """Transactionally start workers under the shared ownership lock."""
    root = root or state_dir()
    pid_path = mode_a_pids_path(root)
    with worker_pidfile_lock(pid_path):
        existing = collect_worker_records(pid_path, rewrite=False)
        if existing:
            pids = ", ".join(str(record.pid) for record in existing)
            raise WorkerSpawnError(
                "pidfile",
                DaemonConflict(f"Hark workers are already running (pids: {pids})"),
            )
        return _spawn_mode_a_workers_locked(
            session=session,
            do_watch=do_watch,
            do_ambient=do_ambient,
            root=root,
            log_dir=log_dir,
        )


def _spawn_mode_a_workers_locked(
    *,
    session: str = "default",
    do_watch: bool = True,
    do_ambient: bool = True,
    root: Path | None = None,
    log_dir: Path | None = None,
) -> list[subprocess.Popen[Any]]:
    """Start workers while the caller holds the pidfile transaction lock."""
    root = root or state_dir()
    log_dir = log_dir or root
    log_dir.mkdir(parents=True, exist_ok=True)
    base = _hark_argv()
    pid_path = mode_a_pids_path(root)
    try:
        original_pidfile = pid_path.read_bytes() if pid_path.exists() else None
    except OSError as exc:
        raise WorkerSpawnError("pidfile", exc) from exc

    owned: list[tuple[str, subprocess.Popen[Any]]] = []
    owned_records: dict[int, WorkerRecord] = {}
    owned_pidfds: dict[int, int] = {}
    failed_role = "watch"
    pidfile_touched = False

    def _spawn(role: str, argv: list[str], log_name: str) -> None:
        nonlocal failed_role, pidfile_touched
        failed_role = role
        # Popen duplicates the descriptor into the child, so the supervisor's
        # copy can and must close immediately after the spawn attempt.
        with open(log_dir / log_name, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={
                    **os.environ,
                    WORKER_PIDFILE_ENV: str(pid_path.resolve(strict=False)),
                    WORKER_ROLE_ENV: role,
                },
            )
            if not proc.pid:
                raise OSError("worker started without a process ID")
            # Retain ownership before even closing the parent log stream, since
            # context-manager cleanup is itself allowed to fail.
            owned.append((role, proc))
            try:
                owned_pidfds[proc.pid] = open_process_handle(proc.pid)
            except BaseException:
                provisional = capture_worker_identity(
                    proc.pid,
                    role=role,
                    expected_parent_pid=os.getpid(),
                    pidfile=pid_path,
                )
                if provisional is not None:
                    owned_records[proc.pid] = provisional
                raise
        if proc.poll() is not None:
            raise OSError(f"worker exited immediately with status {proc.returncode}")
        record = capture_worker_identity(
            proc.pid,
            role=role,
            expected_parent_pid=os.getpid(),
            pidfile=pid_path,
        )
        if record is None:
            raise OSError(f"could not capture {role} worker process identity")
        # The intended role is provisional until the exact captured lifetime
        # execs a provenance-scoped Hark worker command.
        owned_records[proc.pid] = record
        # Publish ownership before waiting for exec/role readiness. If the
        # supervisor is interrupted, this exact lifetime remains recoverable.
        failed_role = "pidfile"
        pidfile_touched = True
        write_worker_records(pid_path, owned_records.values())
        failed_role = role
        if not wait_for_worker_role(record, timeout_s=2.0):
            raise OSError(f"{role} worker did not reach its expected role")
        owned_records[proc.pid] = replace(record, provisional=False)

    try:
        if do_watch:
            _spawn(
                "watch",
                [
                    *base,
                    "watch",
                    "--session",
                    session,
                    "--for-monitor",
                    "--statuses",
                    "blocked,done",
                ],
                "watch.jsonl",
            )

        if do_ambient:
            _spawn("ambient", [*base, "ambient"], "ambient.jsonl")

        # A previously started role can exit while a later Popen is in flight.
        for role, proc in owned:
            if proc.poll() is not None:
                failed_role = role
                raise OSError(
                    f"worker exited immediately with status {proc.returncode}"
                )

        failed_role = "pidfile"
        pidfile_touched = True
        write_worker_records(pid_path, owned_records.values())

        recorded = {record.pid: record for record in read_worker_records(pid_path)}
        for role, proc in owned:
            if recorded.get(proc.pid) != owned_records[proc.pid]:
                failed_role = role
                raise OSError(f"worker identity for PID {proc.pid} was not recorded")
            if proc.poll() is not None:
                failed_role = role
                raise OSError(
                    f"worker exited immediately with status {proc.returncode}"
                )
    except BaseException as exc:
        rollback_failures = _rollback_worker_start(
            owned,
            pid_path=pid_path,
            original_pidfile=original_pidfile,
            restore_pidfile=pidfile_touched,
            owned_records=owned_records,
            owned_pidfds=owned_pidfds,
        )
        if not isinstance(exc, Exception):
            note = (
                f"worker startup interrupted during {failed_role}; rollback completed"
            )
            if rollback_failures:
                note += " with failures: " + "; ".join(rollback_failures)
            exc.add_note(note)
            raise
        raise WorkerSpawnError(failed_role, exc, rollback_failures) from exc

    for pidfd in owned_pidfds.values():
        os.close(pidfd)
    return [proc for _, proc in owned]


def _refresh_owned_worker_records(
    owned: Sequence[tuple[str, subprocess.Popen[Any]]], *, pid_path: Path
) -> list[WorkerRecord]:
    """Rebuild durable identity state from workers owned by the supervisor."""
    records: list[WorkerRecord] = []
    owned_pids = {proc.pid for _, proc in owned if proc.pid}
    for role, proc in owned:
        if not proc.pid or proc.poll() is not None:
            continue
        record = capture_worker_identity(proc.pid, role=role, pidfile=pid_path)
        if record is None:
            raise OSError(f"could not refresh {role} worker process identity")
        ready = replace(record, provisional=False)
        if not record_matches_process(ready):
            raise OSError(f"could not verify refreshed {role} worker process identity")
        records.append(ready)
    replace_owned_worker_records(pid_path, owned_pids=owned_pids, records=records)
    return records


def terminate_children(
    children: Sequence[subprocess.Popen[Any]],
    *,
    root: Path | None = None,
    timeout_s: float = 10.0,
) -> None:
    """Stop supervised children using their durable immutable identities."""
    root = root or state_dir()
    pid_path = mode_a_pids_path(root)
    child_pids = {proc.pid for proc in children if proc.pid}
    records = {
        record.pid: record
        for record in collect_worker_records(pid_path, discover=False)
        if record.pid in child_pids
    }
    signal_worker_records(
        [records[proc.pid] for proc in children if proc.pid in records],
        signal.SIGTERM,
    )
    deadline = time.monotonic() + timeout_s
    for proc in children:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining if remaining > 0 else 0.1)
        except subprocess.TimeoutExpired:
            record = records.get(proc.pid)
            if record is not None:
                signal_worker_records([record], signal.SIGKILL)
                try:
                    proc.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        except OSError:
            pass
    collect_worker_records(pid_path)


def run_foreground(
    *,
    root: Path | None = None,
    workers: bool = False,
    do_watch: bool = True,
    do_ambient: bool = True,
    session: str = "default",
    idle_sleep_s: float = 0.5,
) -> int:
    """Foreground supervisor: hold harkd.pid; optional ambient/watch workers; wait for SIGTERM."""
    root = root or state_dir()
    children: list[subprocess.Popen[Any]] = []
    owned_children: list[tuple[str, subprocess.Popen[Any]]] = []
    shutting_down = {"flag": False}

    def _handle_stop(signum: int, _frame: object) -> None:
        shutting_down["flag"] = True

    prev_term = signal.signal(signal.SIGTERM, _handle_stop)
    prev_int = signal.signal(signal.SIGINT, _handle_stop)

    try:
        acquire_harkd_pidfile(root, pid=os.getpid())
    except DaemonConflict as exc:
        print(f"harkd: {exc}", file=sys.stderr)
        return ERROR

    try:
        if workers:
            try:
                children = spawn_mode_a_workers(
                    session=session,
                    do_watch=do_watch,
                    do_ambient=do_ambient,
                    root=root,
                )
            except OSError as exc:
                print(f"harkd: failed to spawn workers: {exc}", file=sys.stderr)
                release_harkd_pidfile(root, pid=os.getpid())
                return ERROR
            roles = (["watch"] if do_watch else []) + (
                ["ambient"] if do_ambient else []
            )
            owned_children = list(zip(roles, children, strict=True))
            print(
                f"harkd: started with workers pids={[c.pid for c in children]} "
                f"(state={root})",
                flush=True,
            )
        else:
            print(
                f"harkd: running foreground supervisor pid={os.getpid()} "
                f"(no workers; state={root}) — see docs/HARKD.md",
                flush=True,
            )

        while not shutting_down["flag"]:
            # If we own workers and they all exited, leave.
            if children and all(c.poll() is not None for c in children):
                print("harkd: all workers exited", flush=True)
                break
            # Refresh mode-a.pids with still-live children
            if children:
                try:
                    _refresh_owned_worker_records(
                        owned_children, pid_path=mode_a_pids_path(root)
                    )
                except OSError as exc:
                    print(
                        f"harkd: failed to refresh worker identity: {exc}",
                        file=sys.stderr,
                    )
                    return ERROR
            # Keep our own pidfile honest
            if not harkd_pid_path(root).is_file():
                # External clear — re-assert ownership while we run
                try:
                    acquire_harkd_pidfile(root, pid=os.getpid())
                except DaemonConflict:
                    print("harkd: lost pidfile exclusivity", file=sys.stderr)
                    break
            time.sleep(idle_sleep_s)

        return OK
    finally:
        if children:
            terminate_children(children, root=root)
        release_harkd_pidfile(root, pid=os.getpid())
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
        print("harkd: stopped", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harkd",
        description=(
            "Experimental Hark always-on daemon scaffold (not required for handsfree v1). "
            "See docs/HARKD.md."
        ),
    )
    p.add_argument("--version", action="version", version=f"harkd {__version__}")
    sub = p.add_subparsers(dest="daemon_cmd", required=True)

    st = sub.add_parser("start", help="run foreground supervisor (single-instance)")
    st.add_argument(
        "--workers",
        action="store_true",
        help="also supervise ambient + watch (same pieces as run-mode-a.sh)",
    )
    st.add_argument(
        "--no-watch", action="store_true", help="with --workers: skip watch"
    )
    st.add_argument(
        "--no-ambient", action="store_true", help="with --workers: skip ambient"
    )
    st.add_argument("--session", default="default", help="Herdr session for watch")

    status = sub.add_parser("status", help="show harkd / workers / locks")
    status.add_argument("--json", action="store_true")

    stop = sub.add_parser("stop", help="SIGTERM harkd via pidfile")
    stop.add_argument(
        "--force",
        action="store_true",
        help="SIGKILL if still running after grace",
    )
    stop.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait after SIGTERM (default 15)",
    )
    stop.add_argument("--json", action="store_true")

    return p


def dispatch_daemon(args: argparse.Namespace) -> int:
    cmd = args.daemon_cmd
    if cmd == "start":
        if args.workers and args.no_watch and args.no_ambient:
            print(
                "harkd: --workers with both --no-watch and --no-ambient is empty",
                file=sys.stderr,
            )
            return USAGE
        return run_foreground(
            workers=bool(args.workers),
            do_watch=not bool(args.no_watch),
            do_ambient=not bool(args.no_ambient),
            session=str(args.session or "default"),
        )
    if cmd == "status":
        status = collect_status()
        if args.json:
            print(json.dumps(status.to_dict(), indent=2))
        else:
            h = status.harkd
            m = status.mode_a
            print(f"state_dir: {status.state_dir}")
            if h.running:
                print(f"harkd: running (pid {', '.join(str(p) for p in h.pids)})")
            else:
                print("harkd: not running")
            if m.running:
                print(f"workers: running (pids {', '.join(str(p) for p in m.pids)})")
            else:
                print("workers: not running")
            print(f"busy.lock: {'yes' if status.busy_lock else 'no'}")
            print(f"mic.lock: {'yes' if status.mic_lock else 'no'}")
        return OK
    if cmd == "stop":
        result = stop_harkd(timeout_s=float(args.timeout), force=bool(args.force))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result.get("message") or result.get("error") or json.dumps(result))
        return OK if result.get("ok") else ERROR
    return USAGE


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        from hark.stdio import configure_stdio

        configure_stdio()
    except Exception:
        pass
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else USAGE
    try:
        return dispatch_daemon(args)
    except DaemonConflict as exc:
        print(f"harkd: {exc}", file=sys.stderr)
        return ERROR
    except KeyboardInterrupt:
        return ABORT_SOFT


# local alias so KeyboardInterrupt maps cleanly without circular imports
ABORT_SOFT = 130


def run_cli_subcommand(argv: Sequence[str]) -> int:
    """Entry used by `hark daemon …` (argv without the 'daemon' word)."""
    return main(list(argv))

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
    WORKER_SPAWN_TOKEN_ENV,
    WorkerRecord,
    WorkerSignalError,
    WorkerSignalResult,
    WorkerSpawnClaim,
    WorkerStateUnavailableError,
    capture_worker_identity,
    collect_worker_records,
    create_worker_spawn_claim,
    inspect_worker,
    open_process_handle,
    provisional_record_from_claim,
    read_worker_records,
    record_matches_lifetime,
    record_matches_process,
    recover_worker_spawn_claim,
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


@dataclass(frozen=True)
class WorkerCleanupIssue:
    """One stage that prevented complete supervised-child cleanup."""

    stage: str
    role: str | None
    pid: int | None
    error: str


class WorkerCleanupError(WorkerSignalError):
    """Structured aggregate of signalling, authority, and survivor failures."""

    def __init__(
        self,
        signal_results: Sequence[WorkerSignalResult],
        issues: Sequence[WorkerCleanupIssue],
    ) -> None:
        self.results = tuple(result for result in signal_results if result.errors)
        self.issues = tuple(issues)
        details: list[str] = []
        if self.results:
            details.append(str(WorkerSignalError(self.results)))
        details.extend(
            f"{issue.stage} for {issue.role or 'unknown-role'}"
            f" pid {issue.pid if issue.pid is not None else 'unknown'}: {issue.error}"
            for issue in self.issues
        )
        RuntimeError.__init__(self, "worker cleanup failed: " + "; ".join(details))


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


@dataclass
class _OwnedWorker:
    """One startup attempt's process plus every form of lifetime authority."""

    role: str
    process: subprocess.Popen[Any]
    claim: WorkerSpawnClaim
    record: WorkerRecord | None = None
    pidfd: int | None = None


class _PreclaimedPopen(subprocess.Popen):
    """A Popen whose exact object is claimed before ``__init__`` can fork."""

    def __new__(cls, *_args, _claim, **_kwargs):
        instance = super().__new__(cls)
        _claim(instance)
        return instance

    def __init__(self, *args, _claim, **kwargs) -> None:
        # Python necessarily invokes this initializer on the exact object
        # returned and preclaimed by __new__; it cannot substitute another.
        super().__init__(*args, **kwargs)


def _spawn_owned_popen(*args, _claim, **kwargs) -> subprocess.Popen[Any]:
    return _PreclaimedPopen(*args, _claim=_claim, **kwargs)


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


def _close_owned_pidfds(owned: Sequence[_OwnedWorker]) -> list[str]:
    """Consume every descriptor exactly once and continue after failures.

    An asynchronous exception can arrive after the kernel closed the fd. Never
    retry a numeric descriptor: another thread may already have reused it.
    """
    failures: list[str] = []
    for owner in owned:
        pidfd = owner.pidfd
        if pidfd is None:
            continue
        try:
            os.close(pidfd)
        except BaseException as exc:
            try:
                detail = str(exc)
            except BaseException:
                detail = f"<{type(exc).__name__}>"
            failures.append(f"{owner.role} pidfd {pidfd} close failed ({detail})")
        finally:
            owner.pidfd = None
    return failures


def _rollback_worker_start(
    owned: Sequence[_OwnedWorker],
    *,
    pid_path: Path,
    original_pidfile: bytes | None,
    restore_pidfile: bool,
    timeout_s: float = 2.0,
) -> list[str]:
    """Stop/reap only workers owned by a failed startup attempt.

    Cleanup is deliberately a sequence of independently guarded stages. An
    asynchronous ``BaseException`` in one stage is diagnostic data; it must not
    prevent later signalling, durable survivor publication, or descriptor
    closure, and it never replaces the startup exception owned by the caller.
    """
    failures: list[str] = []

    def describe(exc: BaseException) -> str:
        try:
            return str(exc)
        except BaseException:
            return f"<{type(exc).__name__}>"

    def failed(stage: str, exc: BaseException) -> None:
        try:
            failures.append(f"{stage} ({describe(exc)})")
        except BaseException:
            # Even hostile instrumentation of the diagnostics container cannot
            # be allowed to abort ownership cleanup.
            pass

    def poll(proc: subprocess.Popen[Any], role: str) -> int | None:
        try:
            return proc.poll()
        except BaseException as exc:
            failed(f"{role} status check failed", exc)
            return None

    def signal_owned(owner: _OwnedWorker, sig: int) -> bool:
        role = owner.role
        record = owner.record
        pidfd = owner.pidfd
        if pidfd is not None:
            if record is not None and not record_matches_lifetime(record):
                return False
            try:
                signal_process_handle(pidfd, sig)
                return True
            except ProcessLookupError:
                return False
            except BaseException as exc:
                failed(f"{role} pidfd signal {sig} failed", exc)
                return False
        if record is not None:
            try:
                outcome = signal_worker_lifetime(record, sig)
            except BaseException as exc:
                failed(f"{role} signal {sig} failed", exc)
                return False
            if outcome.error is not None:
                failures.append(f"{role} signal {sig} failed ({outcome.error})")
            return outcome.sent
        failures.append(f"{role} has no immutable lifetime handle; refusing signal")
        return False

    for owner in reversed(owned):
        role, proc = owner.role, owner.process
        if poll(proc, role) is not None:
            continue
        try:
            signal_owned(owner, signal.SIGTERM)
        except BaseException as exc:
            failed(f"{role} SIGTERM stage interrupted", exc)

    try:
        deadline = time.monotonic() + timeout_s
    except BaseException as exc:
        failed("rollback deadline initialization failed", exc)
        deadline = 0.0
    for owner in reversed(owned):
        role, proc = owner.role, owner.process
        try:
            remaining = max(0.0, deadline - time.monotonic())
        except BaseException as exc:
            failed(f"{role} rollback deadline check failed", exc)
            remaining = 0.0
        try:
            proc.wait(timeout=remaining if remaining > 0 else 0.1)
            continue
        except subprocess.TimeoutExpired:
            pass
        except BaseException as exc:
            failed(f"{role} reap failed", exc)
            if poll(proc, role) is not None:
                continue

        try:
            signal_owned(owner, signal.SIGKILL)
        except BaseException as exc:
            failed(f"{role} SIGKILL stage interrupted", exc)

        try:
            proc.wait(timeout=1.0)
        except BaseException as exc:
            failed(f"{role} reap after SIGKILL failed", exc)

    survivors: list[_OwnedWorker] = []
    for owner in owned:
        role, proc = owner.role, owner.process
        still_running = poll(proc, role) is None
        if still_running:
            survivors.append(owner)

    if survivors:
        failures.append(
            "surviving workers still running: "
            + ", ".join(f"{owner.role}={owner.process.pid}" for owner in survivors)
        )

    # A pidfd pins each direct child, so it is safe to recover structured
    # lifetime metadata here even when the earlier capture step failed.
    for owner in survivors:
        role, proc = owner.role, owner.process
        if owner.record is not None:
            continue
        try:
            recovered = capture_worker_identity(
                proc.pid,
                role=role,
                expected_parent_pid=os.getpid(),
                pidfile=pid_path,
                spawn_token=owner.claim.token,
            )
        except BaseException as exc:
            failed(f"{role} survivor identity recovery failed", exc)
            recovered = None
        if recovered is not None:
            recovered = replace(
                recovered,
                boot_id=owner.claim.boot_id,
                spawn_token=owner.claim.token,
                pidfile=owner.claim.pidfile,
                config=owner.claim.config,
                provisional=True,
            )
            owner.record = recovered
        else:
            # A spawn token selected before fork remains safe authority even
            # when procfs cannot currently disclose start ticks. Collection
            # later requires the same boot/token/role/scope before signalling.
            owner.record = provisional_record_from_claim(owner.claim, pid=proc.pid)

    untracked_survivors = [owner for owner in survivors if owner.record is None]

    if restore_pidfile or survivors:
        payload: bytes | None = original_pidfile
        try:
            if survivors:
                payload = original_pidfile or b""
                if payload and not payload.endswith(b"\n"):
                    payload += b"\n"
                untracked = [
                    f"{owner.role}={owner.process.pid}" for owner in untracked_survivors
                ]
                if untracked:
                    failures.append(
                        "surviving workers lack structured provisional identity: "
                        + ", ".join(untracked)
                    )
                payload += b"".join(
                    f"{replace(owner.record, provisional=True).to_json()}\n".encode()
                    for owner in survivors
                    if owner.record is not None
                )
                write_worker_pidfile_bytes(pid_path, payload)
            elif original_pidfile is None:
                payload = None
                write_worker_pidfile_bytes(pid_path, payload)
            else:
                payload = original_pidfile
                write_worker_pidfile_bytes(pid_path, payload)
        except BaseException as atomic_exc:
            try:
                write_worker_pidfile_bytes_direct(pid_path, payload)
                failures.append(
                    "pidfile atomic restore failed "
                    f"({describe(atomic_exc)}); used direct fallback"
                )
            except BaseException as direct_exc:
                failures.append(
                    "pidfile restore failed "
                    f"(atomic: {describe(atomic_exc)}; direct: {describe(direct_exc)})"
                )
                # The raw restore adapters and the structured ownership writer
                # are independent publication paths. When legacy bytes cannot
                # be restored, preserving exact survivor authority is safer
                # than retaining a process-local pidfd that vanishes at exit.
                survivor_records = [
                    replace(owner.record, provisional=True)
                    for owner in survivors
                    if owner.record is not None
                ]
                if survivor_records:
                    try:
                        write_worker_records(pid_path, survivor_records)
                        failures.append(
                            "published structured survivor ownership fallback"
                        )
                    except BaseException as structured_exc:
                        failed(
                            "structured survivor ownership publication failed",
                            structured_exc,
                        )

    failures.extend(_close_owned_pidfds(owned))

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
        try:
            # Discover marker-scoped owners while the transaction lock is held:
            # direct callers do not necessarily perform the outer discovery.
            # Keep rewrite disabled so a failed/contended startup never alters
            # the raw ownership evidence it is evaluating.
            existing = collect_worker_records(
                pid_path, discover=True, rewrite=False
            )
        except WorkerStateUnavailableError as exc:
            raise WorkerSpawnError("pidfile", exc) from exc
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

    owned: list[_OwnedWorker] = []
    failed_role = "watch"
    pidfile_touched = False

    def _spawn(role: str, argv: list[str], log_name: str) -> None:
        nonlocal failed_role, pidfile_touched
        failed_role = role
        claim_authority = create_worker_spawn_claim(role=role, pidfile=pid_path)

        claimed: list[_OwnedWorker] = []

        def claim(proc: subprocess.Popen[Any]) -> None:
            if claimed:
                raise RuntimeError("worker initializer attempted multiple claims")
            owner = _OwnedWorker(role=role, process=proc, claim=claim_authority)
            claimed.append(owner)
            owned.append(owner)

        def establish_authority(owner: _OwnedWorker) -> WorkerRecord:
            proc = owner.process
            pid = getattr(proc, "pid", None)
            if not isinstance(pid, int) or pid <= 0:
                raise OSError("worker started without a process ID")
            try:
                owner.pidfd = open_process_handle(pid)
            except BaseException:
                provisional = capture_worker_identity(
                    pid,
                    role=role,
                    expected_parent_pid=os.getpid(),
                    pidfile=pid_path,
                    spawn_token=claim_authority.token,
                )
                if provisional is not None:
                    owner.record = replace(
                        provisional,
                        boot_id=claim_authority.boot_id,
                        spawn_token=claim_authority.token,
                        pidfile=claim_authority.pidfile,
                        config=claim_authority.config,
                        provisional=True,
                    )
                raise
            record = capture_worker_identity(
                pid,
                role=role,
                expected_parent_pid=os.getpid(),
                pidfile=pid_path,
                spawn_token=claim_authority.token,
            )
            if record is None:
                raise OSError(f"could not capture {role} worker process identity")
            # Test adapters and older capture implementations may omit the
            # fields selected before fork. The spawning transaction is the
            # authority that fills them, never pidfile input.
            record = replace(
                record,
                boot_id=claim_authority.boot_id,
                spawn_token=claim_authority.token,
                pidfile=claim_authority.pidfile,
                config=claim_authority.config,
                provisional=True,
            )
            owner.record = record
            return record

        # Popen duplicates the descriptor into the child, so the supervisor's
        # copy can and must close immediately after the spawn attempt.
        with open(log_dir / log_name, "a", encoding="utf-8") as log:
            try:
                proc = _spawn_owned_popen(
                    argv,
                    _claim=claim,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={
                        **os.environ,
                        WORKER_PIDFILE_ENV: str(pid_path.resolve(strict=False)),
                        WORKER_ROLE_ENV: role,
                        WORKER_SPAWN_TOKEN_ENV: claim_authority.token,
                    },
                )
                if len(claimed) != 1 or proc is not claimed[0].process:
                    raise RuntimeError(
                        "worker initializer did not return its preclaimed object"
                    )
                owner = claimed[0]
                record = establish_authority(owner)
            except BaseException as spawn_exc:
                if claimed:
                    owner = claimed[0]
                    proc = owner.process
                    pid = getattr(proc, "pid", None)
                    if isinstance(pid, int) and pid > 0:
                        # Popen stores its positive child PID before flipping
                        # the private flag used by poll/wait. An interruption
                        # there still names our exact preclaimed child.
                        if not getattr(proc, "_child_created", False):
                            proc._child_created = True
                        if owner.pidfd is None and owner.record is None:
                            try:
                                establish_authority(owner)
                            except BaseException as authority_exc:
                                spawn_exc.add_note(
                                    "failed to attach immutable authority after "
                                    f"initializer error: {authority_exc}"
                                )
                    else:
                        try:
                            recovered = recover_worker_spawn_claim(claim_authority)
                        except BaseException as recovery_exc:
                            spawn_exc.add_note(
                                "failed to recover preclaimed child after lost PID "
                                f"publication: {recovery_exc}"
                            )
                            recovered = None
                        if recovered is None:
                            owned.remove(owner)
                        else:
                            proc.pid = recovered.pid
                            proc._child_created = True
                            owner.record = recovered
                            try:
                                owner.pidfd = open_process_handle(recovered.pid)
                            except BaseException as authority_exc:
                                spawn_exc.add_note(
                                    "recovered child but failed to pin its lifetime: "
                                    f"{authority_exc}"
                                )
                raise
        if proc.poll() is not None:
            raise OSError(f"worker exited immediately with status {proc.returncode}")
        # The intended role is provisional until the exact captured lifetime
        # execs a provenance-scoped Hark worker command.
        owner.record = record
        # Publish ownership before waiting for exec/role readiness. If the
        # supervisor is interrupted, this exact lifetime remains recoverable.
        failed_role = "pidfile"
        pidfile_touched = True
        write_worker_records(
            pid_path,
            [candidate.record for candidate in owned if candidate.record is not None],
        )
        failed_role = role
        if not wait_for_worker_role(record, timeout_s=5.0):
            raise OSError(f"{role} worker did not reach its expected role")
        owner.record = replace(record, provisional=False)

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
        for owner in owned:
            role, proc = owner.role, owner.process
            if proc.poll() is not None:
                failed_role = role
                raise OSError(
                    f"worker exited immediately with status {proc.returncode}"
                )

        failed_role = "pidfile"
        pidfile_touched = True
        write_worker_records(
            pid_path,
            [candidate.record for candidate in owned if candidate.record is not None],
        )

        recorded = {record.pid: record for record in read_worker_records(pid_path)}
        for owner in owned:
            role, proc = owner.role, owner.process
            if owner.record is None or recorded.get(proc.pid) != owner.record:
                failed_role = role
                raise OSError(f"worker identity for PID {proc.pid} was not recorded")
            if proc.poll() is not None:
                failed_role = role
                raise OSError(
                    f"worker exited immediately with status {proc.returncode}"
                )
    except BaseException as exc:
        try:
            rollback_failures = _rollback_worker_start(
                owned,
                pid_path=pid_path,
                original_pidfile=original_pidfile,
                restore_pidfile=pidfile_touched,
            )
        except BaseException as rollback_exc:
            # A truly asynchronous interruption can land between guarded
            # rollback stages. Retry the idempotent transaction before
            # propagating the original startup failure.
            try:
                rollback_failure = str(rollback_exc)
            except BaseException:
                rollback_failure = f"<{type(rollback_exc).__name__}>"
            rollback_failures = [f"rollback interrupted ({rollback_failure})"]
            try:
                rollback_failures.extend(
                    _rollback_worker_start(
                        owned,
                        pid_path=pid_path,
                        original_pidfile=original_pidfile,
                        restore_pidfile=pidfile_touched,
                    )
                )
            except BaseException as retry_exc:
                rollback_failures.append(
                    f"rollback retry interrupted (<{type(retry_exc).__name__}>)"
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

    close_failures = _close_owned_pidfds(owned)
    if close_failures:
        rollback_failures = _rollback_worker_start(
            owned,
            pid_path=pid_path,
            original_pidfile=original_pidfile,
            restore_pidfile=True,
        )
        raise WorkerSpawnError(
            "pidfd",
            OSError("; ".join(close_failures)),
            rollback_failures,
        )
    return [owner.process for owner in owned]


def _refresh_owned_worker_records(
    owned: Sequence[tuple[str, subprocess.Popen[Any]]], *, pid_path: Path
) -> list[WorkerRecord]:
    """Rebuild durable identity state from workers owned by the supervisor."""
    records: list[WorkerRecord] = []
    owned_pids = {proc.pid for _, proc in owned if proc.pid}
    for role, proc in owned:
        if not proc.pid or proc.poll() is not None:
            continue
        record = inspect_worker(
            proc.pid,
            expected_role=role,
            expected_pidfile=pid_path,
        )
        if record is None or not record_matches_process(record):
            raise OSError(f"could not verify refreshed {role} worker process identity")
        records.append(record)
    replace_owned_worker_records(pid_path, owned_pids=owned_pids, records=records)
    return records


def terminate_children(
    children: Sequence[
        subprocess.Popen[Any] | tuple[str, subprocess.Popen[Any]]
    ],
    *,
    root: Path | None = None,
    timeout_s: float = 10.0,
) -> None:
    """Stop supervised children through marker identity plus a pinned pidfd."""
    root = root or state_dir()
    pid_path = mode_a_pids_path(root)
    issues: list[WorkerCleanupIssue] = []
    supervised: list[tuple[str | None, subprocess.Popen[Any]]] = []
    for child in children:
        if isinstance(child, tuple):
            role, proc = child
        else:
            role, proc = None, child
        supervised.append((role, proc))

    owned_pids: set[int] = set()
    records: dict[int, WorkerRecord] = {}
    roles: dict[int, str | None] = {}
    for supplied_role, proc in supervised:
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            issues.append(
                WorkerCleanupIssue(
                    "identity recovery", supplied_role, None, "missing process ID"
                )
            )
            continue
        owned_pids.add(pid)
        roles[pid] = supplied_role
        try:
            if proc.poll() is not None:
                continue
        except Exception as exc:
            issues.append(
                WorkerCleanupIssue("status check", supplied_role, pid, str(exc))
            )
        try:
            record = inspect_worker(
                pid,
                expected_role=supplied_role,
                expected_pidfile=pid_path,
            )
        except Exception as exc:
            issues.append(
                WorkerCleanupIssue("identity recovery", supplied_role, pid, str(exc))
            )
            continue
        if record is None:
            try:
                exited = proc.poll() is not None
            except Exception:
                exited = False
            if not exited:
                issues.append(
                    WorkerCleanupIssue(
                        "identity recovery",
                        supplied_role,
                        pid,
                        "exact marker-scoped identity unavailable",
                    )
                )
            continue
        roles[pid] = record.role
        records[pid] = record

    if records:
        try:
            replace_owned_worker_records(
                pid_path, owned_pids=owned_pids, records=records.values()
            )
        except Exception as exc:
            issues.append(
                WorkerCleanupIssue("ownership publication", None, None, str(exc))
            )

    signal_results: list[WorkerSignalResult] = []
    term_result = signal_worker_records(records.values(), signal.SIGTERM)
    signal_results.append(term_result)

    deadline = time.monotonic() + max(0.0, timeout_s)
    timed_out: list[subprocess.Popen[Any]] = []
    for supplied_role, proc in supervised:
        pid = getattr(proc, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining if remaining > 0 else 0.1)
        except subprocess.TimeoutExpired:
            timed_out.append(proc)
        except Exception as exc:
            issues.append(
                WorkerCleanupIssue("TERM wait", roles.get(pid, supplied_role), pid, str(exc))
            )
            try:
                if proc.poll() is None:
                    timed_out.append(proc)
            except Exception:
                timed_out.append(proc)

    kill_records = [
        records[proc.pid]
        for proc in timed_out
        if isinstance(getattr(proc, "pid", None), int) and proc.pid in records
    ]
    if kill_records:
        signal_results.append(signal_worker_records(kill_records, signal.SIGKILL))
    for proc in timed_out:
        pid = getattr(proc, "pid", None)
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        except Exception as exc:
            issues.append(
                WorkerCleanupIssue("KILL wait", roles.get(pid), pid, str(exc))
            )

    survivors: list[subprocess.Popen[Any]] = []
    for supplied_role, proc in supervised:
        pid = getattr(proc, "pid", None)
        try:
            running = proc.poll() is None
        except Exception as exc:
            running = True
            issues.append(
                WorkerCleanupIssue(
                    "survivor status", roles.get(pid, supplied_role), pid, str(exc)
                )
            )
        if running:
            survivors.append(proc)
            issues.append(
                WorkerCleanupIssue(
                    "surviving supervised worker",
                    roles.get(pid, supplied_role),
                    pid,
                    "still running after TERM/KILL waits",
                )
            )

    survivor_records = [
        records[proc.pid]
        for proc in survivors
        if isinstance(getattr(proc, "pid", None), int) and proc.pid in records
    ]
    if survivor_records or not survivors:
        try:
            replace_owned_worker_records(
                pid_path, owned_pids=owned_pids, records=survivor_records
            )
        except Exception as exc:
            issues.append(
                WorkerCleanupIssue(
                    "survivor ownership publication", None, None, str(exc)
                )
            )

    if issues or any(result.errors for result in signal_results):
        raise WorkerCleanupError(signal_results, issues)


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
    pidfile_acquired = False
    result = OK

    def _handle_stop(signum: int, _frame: object) -> None:
        shutting_down["flag"] = True

    prev_term = signal.signal(signal.SIGTERM, _handle_stop)
    prev_int = signal.signal(signal.SIGINT, _handle_stop)

    try:
        try:
            acquire_harkd_pidfile(root, pid=os.getpid())
        except DaemonConflict as exc:
            print(f"harkd: {exc}", file=sys.stderr)
            result = ERROR
        else:
            pidfile_acquired = True
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
                    result = ERROR
                else:
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

            while result == OK and not shutting_down["flag"]:
                if children and all(c.poll() is not None for c in children):
                    print("harkd: all workers exited", flush=True)
                    break
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
                        result = ERROR
                        break
                if not harkd_pid_path(root).is_file():
                    try:
                        acquire_harkd_pidfile(root, pid=os.getpid())
                    except DaemonConflict:
                        print("harkd: lost pidfile exclusivity", file=sys.stderr)
                        result = ERROR
                        break
                time.sleep(idle_sleep_s)
    finally:
        try:
            if children:
                try:
                    terminate_children(owned_children, root=root)
                except WorkerSignalError as exc:
                    print(f"harkd: failed to stop workers: {exc}", file=sys.stderr)
                    result = ERROR
                except Exception as exc:
                    print(f"harkd: failed to stop workers: {exc}", file=sys.stderr)
                    result = ERROR
        finally:
            try:
                if pidfile_acquired:
                    try:
                        release_harkd_pidfile(root, pid=os.getpid())
                    except Exception as exc:
                        print(f"harkd: failed to release pidfile: {exc}", file=sys.stderr)
                        result = ERROR
            finally:
                restore_interrupt: BaseException | None = None
                for signum, previous in (
                    (signal.SIGTERM, prev_term),
                    (signal.SIGINT, prev_int),
                ):
                    try:
                        signal.signal(signum, previous)
                    except (KeyboardInterrupt, SystemExit) as exc:
                        if restore_interrupt is None:
                            restore_interrupt = exc
                    except Exception as exc:
                        print(
                            f"harkd: failed to restore signal handler: {exc}",
                            file=sys.stderr,
                        )
                        result = ERROR
                if restore_interrupt is not None:
                    raise restore_interrupt
        if result == OK:
            print("harkd: stopped", flush=True)
    return result


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

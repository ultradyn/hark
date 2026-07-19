"""Durable, PID-reuse-safe identity for ambient and watch workers.

``mode-a.pids`` used to contain bare process IDs.  A PID only identifies a
slot in the process table and may be reused, so it is not safe authority for a
later signal.  This module owns the versioned JSON-lines replacement and keeps
legacy files compatible by removing dead or clearly unrelated bare entries while
failing closed for live Hark-shaped occupants whose historical lifetime is unknown.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import json
import math
import os
import re
import signal
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import IO, Collection, Iterable, Iterator, Sequence

WORKER_ROLES = frozenset({"ambient", "watch"})
RECORD_VERSION = 1
WORKER_PIDFILE_ENV = "HARK_WORKER_PIDFILE"
WORKER_ROLE_ENV = "HARK_WORKER_ROLE"
WORKER_SPAWN_TOKEN_ENV = "HARK_WORKER_SPAWN_TOKEN"


class WorkerStateUnavailableError(RuntimeError):
    """Recorded ownership cannot be resolved without risking another process."""

    def __init__(self, message: str, *, pids: Iterable[int] = ()) -> None:
        self.pids = tuple(sorted(set(pids)))
        super().__init__(message)


class _ProcfsUnavailableError(OSError):
    """A process may exist, but its identity could not be inspected safely."""


@dataclass(frozen=True, order=True)
class WorkerRecord:
    """Identity of one specific lifetime of a Hark worker process."""

    pid: int
    start_time: str
    role: str
    version: int = RECORD_VERSION
    pidfile: str | None = None
    config: str | None = None
    provisional: bool = False
    boot_id: str | None = None
    spawn_token: str | None = None
    legacy: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class WorkerSignalOutcome:
    """Result of trying to signal one recorded process lifetime."""

    record: WorkerRecord
    sent: bool = False
    error: str | None = None


@dataclass(frozen=True)
class WorkerSignalResult:
    """Complete result of signalling a batch without abandoning later records."""

    signal: int
    outcomes: tuple[WorkerSignalOutcome, ...]

    @property
    def errors(self) -> tuple[WorkerSignalOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.error is not None)

    @property
    def sent_records(self) -> tuple[WorkerRecord, ...]:
        return tuple(outcome.record for outcome in self.outcomes if outcome.sent)

    def raise_for_errors(self) -> None:
        if self.errors:
            raise WorkerSignalError([self])


class WorkerSignalError(RuntimeError):
    """One or more verified worker lifetimes could not be signalled."""

    def __init__(self, results: Iterable[WorkerSignalResult]) -> None:
        self.results = tuple(result for result in results if result.errors)
        details: list[str] = []
        for result in self.results:
            try:
                signal_name = signal.Signals(result.signal).name
            except ValueError:
                signal_name = str(result.signal)
            details.extend(
                f"{signal_name} pid {outcome.record.pid}: {outcome.error}"
                for outcome in result.errors
            )
        super().__init__(
            "failed to signal verified worker"
            + ("s" if len(details) != 1 else "")
            + ": "
            + "; ".join(details)
        )


@dataclass
class _HeldWorkerLock:
    handle: IO[bytes]
    exclusive: bool
    depth: int = 1


class PidfdUnavailableError(RuntimeError):
    """Raised when no race-free process signalling primitive is available."""


_WORKER_LOCKS = threading.local()


@dataclass(frozen=True)
class WorkerSpawnClaim:
    """Authority selected before a fork and independently recoverable afterward."""

    role: str
    pidfile: str
    config: str
    boot_id: str
    parent_pid: int
    parent_start_time: str
    token: str


def _current_boot_id() -> str:
    try:
        value = (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )
    except OSError as exc:
        raise _ProcfsUnavailableError(
            exc.errno or errno.EIO, "cannot read system boot identity"
        ) from exc
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise _ProcfsUnavailableError(
            errno.EIO, "system boot identity is empty or invalid"
        ) from exc
    return value


def create_worker_spawn_claim(*, role: str, pidfile: Path) -> WorkerSpawnClaim:
    """Preclaim one future child without depending on ``Popen.pid`` publication."""
    if role not in WORKER_ROLES:
        raise ValueError(f"invalid worker role: {role}")
    try:
        boot_id = _current_boot_id()
    except _ProcfsUnavailableError as exc:
        raise WorkerStateUnavailableError(
            "cannot establish worker spawn provenance: boot identity unavailable"
        ) from exc
    parent_stat = _proc_stat(os.getpid())
    config = _config_path_from_environ(dict(os.environ))
    if parent_stat is None or config is None:
        raise WorkerStateUnavailableError("cannot establish worker spawn provenance")
    return WorkerSpawnClaim(
        role=role,
        pidfile=str(pidfile.resolve(strict=False)),
        config=str(config),
        boot_id=boot_id,
        parent_pid=os.getpid(),
        parent_start_time=parent_stat[1],
        token=uuid.uuid4().hex,
    )


def provisional_record_from_claim(claim: WorkerSpawnClaim, *, pid: int) -> WorkerRecord:
    """Materialize durable token authority when procfs identity is unavailable."""
    if pid <= 0:
        raise ValueError("worker PID must be positive")
    return WorkerRecord(
        pid=pid,
        start_time=f"claim:{claim.token}",
        role=claim.role,
        pidfile=claim.pidfile,
        config=claim.config,
        provisional=True,
        boot_id=claim.boot_id,
        spawn_token=claim.token,
    )


def _load_libc_pidfd_functions():
    if not sys.platform.startswith("linux"):
        return None, None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        pidfd_open = libc.pidfd_open
        pidfd_send_signal = libc.pidfd_send_signal
    except (AttributeError, OSError):
        return None, None
    pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
    pidfd_open.restype = ctypes.c_int
    pidfd_send_signal.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    pidfd_send_signal.restype = ctypes.c_int
    return pidfd_open, pidfd_send_signal


_LIBC_PIDFD_OPEN, _LIBC_PIDFD_SEND_SIGNAL = _load_libc_pidfd_functions()


def _lock_key(path: Path) -> Path:
    return path.resolve(strict=False)


def _preserve_primary_cleanup_error(
    primary: BaseException, stage: str, cleanup: BaseException
) -> None:
    """Attach a cleanup failure without replacing the operation's primary."""
    try:
        detail = str(cleanup)
    except BaseException:
        detail = f"<{type(cleanup).__name__}>"
    try:
        primary.add_note(f"worker pidfile {stage} cleanup failed: {detail}")
    except BaseException:
        pass


@contextmanager
def worker_pidfile_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Hold the cross-process lock associated with one worker pidfile.

    Locks are reentrant within a thread so a larger ownership transaction can
    safely call the public read/write helpers.  Upgrading a nested shared lock
    is rejected rather than risking a self-deadlock or silently dropping it.
    """
    key = _lock_key(path)
    held: dict[Path, _HeldWorkerLock] = getattr(_WORKER_LOCKS, "held", {})
    existing = held.get(key)
    if existing is not None:
        if exclusive and not existing.exclusive:
            raise RuntimeError(f"cannot upgrade worker pidfile lock for {path}")
        existing.depth += 1
        try:
            yield
        finally:
            existing.depth -= 1
        return

    key.parent.mkdir(parents=True, exist_ok=True)
    lock_path = key.with_name(f"{key.name}.lock")
    handle = lock_path.open("a+b")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(handle.fileno(), operation)
    except BaseException as primary:
        try:
            handle.close()
        except BaseException as cleanup:
            _preserve_primary_cleanup_error(primary, "acquisition close", cleanup)
        raise

    entry = _HeldWorkerLock(handle=handle, exclusive=exclusive)
    if not hasattr(_WORKER_LOCKS, "held"):
        _WORKER_LOCKS.held = held
    held[key] = entry
    primary: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary = exc
        raise
    finally:
        entry.depth -= 1
        if entry.depth == 0:
            held.pop(key, None)
            cleanup_errors: list[tuple[str, BaseException]] = []
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except BaseException as exc:
                cleanup_errors.append(("unlock", exc))
            try:
                handle.close()
            except BaseException as exc:
                cleanup_errors.append(("close", exc))
            if primary is not None:
                for stage, cleanup in cleanup_errors:
                    _preserve_primary_cleanup_error(primary, stage, cleanup)
            elif cleanup_errors:
                first_stage, first = cleanup_errors[0]
                for stage, cleanup in cleanup_errors[1:]:
                    _preserve_primary_cleanup_error(first, stage, cleanup)
                try:
                    first.add_note(f"worker pidfile {first_stage} cleanup failed")
                except BaseException:
                    pass
                raise first


def _proc_stat(pid: int) -> tuple[str, str] | None:
    """Return ``(state, start_time_ticks)`` from Linux ``/proc/PID/stat``."""
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
        raise _ProcfsUnavailableError(
            exc.errno or errno.EIO, f"cannot inspect /proc/{pid}/stat"
        ) from exc
    # Field 2 (comm) is parenthesised and may itself contain spaces/parens.
    rparen = text.rfind(")")
    if rparen < 0:
        return None
    fields = text[rparen + 2 :].split()
    # fields[0] is field 3 (state); fields[19] is field 22 (starttime).
    if len(fields) <= 19 or fields[0] == "Z":
        return None
    return fields[0], fields[19]


def _proc_argv(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, ProcessLookupError)) or exc.errno in {
            errno.ENOENT,
            errno.ESRCH,
        }:
            return None
        raise _ProcfsUnavailableError(
            exc.errno or errno.EIO, f"cannot inspect /proc/{pid}/cmdline"
        ) from exc
    if not raw:
        return None
    return [
        part.decode("utf-8", errors="surrogateescape")
        for part in raw.split(b"\0")
        if part
    ]


def _proc_ppid(pid: int) -> int | None:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, ProcessLookupError)) or exc.errno in {
            errno.ENOENT,
            errno.ESRCH,
        }:
            return None
        raise _ProcfsUnavailableError(
            exc.errno or errno.EIO, f"cannot inspect /proc/{pid}/status"
        ) from exc
    parent_line = next(
        (line for line in status.splitlines() if line.startswith("PPid:")), None
    )
    if parent_line is None:
        return None
    try:
        parent = int(parent_line.split(":", 1)[1].strip())
    except ValueError:
        return None
    return parent if parent > 0 else None


def _proc_environ(pid: int) -> dict[str, str] | None:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        if isinstance(exc, (FileNotFoundError, ProcessLookupError)) or exc.errno in {
            errno.ENOENT,
            errno.ESRCH,
        }:
            return None
        raise _ProcfsUnavailableError(
            exc.errno or errno.EIO, f"cannot inspect /proc/{pid}/environ"
        ) from exc
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        result[key.decode(errors="surrogateescape")] = value.decode(
            errors="surrogateescape"
        )
    return result


def _config_path_from_environ(environ: dict[str, str]) -> Path | None:
    override = environ.get("HARK_CONFIG")
    if override:
        return Path(override).resolve(strict=False)
    config_home = environ.get("XDG_CONFIG_HOME")
    if config_home:
        return (Path(config_home) / "hark" / "config.toml").resolve(strict=False)
    home = environ.get("HOME")
    if not home:
        return None
    return (Path(home) / ".config" / "hark" / "config.toml").resolve(strict=False)


def worker_role_from_argv(argv: Sequence[str]) -> str | None:
    """Classify only the launch shapes Hark itself uses for workers."""
    if not argv:
        return None

    executable = Path(argv[0]).name.lower()

    def role_at(index: int) -> str | None:
        if index >= len(argv):
            return None
        role = argv[index]
        return role if role in WORKER_ROLES else None

    # Direct console script or native entry point: `hark ROLE ...`.
    if executable == "hark":
        return role_at(1)

    is_python = re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable)
    if is_python:
        # Console script through a Python shebang: `python /path/hark ROLE ...`.
        if len(argv) > 1 and Path(argv[1]).name == "hark":
            return role_at(2)
        # Package entry point used by the daemon: `python -m hark ROLE ...`.
        if len(argv) > 2 and argv[1:3] == ["-m", "hark"]:
            return role_at(3)
        return None

    # Shell launcher wrapper: `uv run hark ROLE ...`.
    if (
        executable == "uv"
        and len(argv) > 2
        and argv[1] == "run"
        and Path(argv[2]).name == "hark"
    ):
        return role_at(3)
    return None


def inspect_worker(
    pid: int,
    *,
    expected_role: str | None = None,
    expected_pidfile: Path | None = None,
    expected_config: Path | None = None,
    require_markers: bool = True,
) -> WorkerRecord | None:
    """Inspect the current process occupying *pid*, if it is a Hark worker."""
    stat = _proc_stat(pid)
    if stat is None:
        return None
    argv = _proc_argv(pid)
    if argv is None:
        return None
    environ = _proc_environ(pid)
    if environ is None:
        return None
    role = worker_role_from_argv(argv)
    if role is None or (expected_role is not None and role != expected_role):
        return None
    marker_role = environ.get(WORKER_ROLE_ENV)
    if require_markers and marker_role != role:
        return None
    if marker_role is not None and marker_role != role:
        return None
    if not require_markers and marker_role is None:
        executable = Path(argv[0]).name.lower()
        module_launch = (
            re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable)
            and len(argv) > 2
            and argv[1:3] == ["-m", "hark"]
        )
        direct_native = executable == "hark"
        uv_launch = (
            executable == "uv"
            and len(argv) > 2
            and argv[1] == "run"
            and Path(argv[2]).name == "hark"
        )
        if not (module_launch or direct_native or uv_launch):
            return None
    raw_pidfile = environ.get(WORKER_PIDFILE_ENV)
    if require_markers and not raw_pidfile:
        return None
    pidfile = (
        str(Path(raw_pidfile).resolve(strict=False))
        if raw_pidfile
        else str(expected_pidfile.resolve(strict=False))
        if expected_pidfile is not None
        else None
    )
    if expected_pidfile is not None and pidfile != str(
        expected_pidfile.resolve(strict=False)
    ):
        return None
    live_config = _config_path_from_environ(environ)
    if live_config is None:
        return None
    config = str(live_config)
    if expected_config is not None and config != str(
        expected_config.resolve(strict=False)
    ):
        return None
    return WorkerRecord(
        pid=pid,
        start_time=stat[1],
        role=role,
        pidfile=pidfile,
        config=config,
        boot_id=_current_boot_id(),
        spawn_token=environ.get(WORKER_SPAWN_TOKEN_ENV),
        legacy=not require_markers and marker_role is None,
    )


def capture_worker_identity(
    pid: int,
    *,
    role: str,
    expected_parent_pid: int | None = None,
    pidfile: Path | None = None,
    spawn_token: str | None = None,
) -> WorkerRecord | None:
    """Capture a newly spawned, caller-owned worker before later validation."""
    if role not in WORKER_ROLES:
        raise ValueError(f"invalid worker role: {role}")
    stat = _proc_stat(pid)
    if stat is None:
        return None
    scope = str(pidfile.resolve(strict=False)) if pidfile is not None else None
    config_path = _config_path_from_environ(dict(os.environ))
    try:
        boot_id = _current_boot_id()
    except _ProcfsUnavailableError as exc:
        raise WorkerStateUnavailableError(
            "cannot capture worker identity: boot identity unavailable"
        ) from exc
    record = WorkerRecord(
        pid=pid,
        start_time=stat[1],
        role=role,
        pidfile=scope,
        config=str(config_path) if config_path is not None else None,
        provisional=True,
        boot_id=boot_id,
        spawn_token=spawn_token,
    )
    if expected_parent_pid is not None:
        if expected_parent_pid <= 0 or _proc_ppid(pid) != expected_parent_pid:
            return None
        # Close the lifetime-swap window between start-time and parent reads.
        if not _record_matches_kernel_lifetime(record):
            return None
    return record


def _record_matches_kernel_lifetime(record: WorkerRecord) -> bool:
    """Match boot/start identity without assuming the child has execed yet."""
    stat = _proc_stat(record.pid)
    if stat is None:
        return False
    if not record.start_time.startswith("claim:") and stat[1] != record.start_time:
        return False
    if record.boot_id is not None and _current_boot_id() != record.boot_id:
        return False
    return True


def record_matches_lifetime(record: WorkerRecord) -> bool:
    """Return whether *record* still names its provenance-proven lifetime."""
    if not _record_matches_kernel_lifetime(record):
        return False
    if record.provisional:
        if not record.boot_id or not record.spawn_token:
            return False
        environ = _proc_environ(record.pid)
        if environ is None:
            return False
        if environ.get(WORKER_SPAWN_TOKEN_ENV) != record.spawn_token:
            return False
        if environ.get(WORKER_ROLE_ENV) != record.role:
            return False
        if record.pidfile is None or environ.get(WORKER_PIDFILE_ENV) is None:
            return False
        if str(Path(environ[WORKER_PIDFILE_ENV]).resolve(strict=False)) != str(
            Path(record.pidfile).resolve(strict=False)
        ):
            return False
    return True


def record_matches_process(record: WorkerRecord) -> bool:
    """Return whether *record* still names the same worker process lifetime."""
    if record.provisional:
        return False
    expected_pidfile = Path(record.pidfile) if record.pidfile is not None else None
    expected_config = Path(record.config) if record.config is not None else None
    return (
        inspect_worker(
            record.pid,
            expected_role=record.role,
            expected_pidfile=expected_pidfile,
            expected_config=expected_config,
            require_markers=not record.legacy,
        )
        == record
    )


def recover_worker_spawn_claim(
    claim: WorkerSpawnClaim,
    *,
    timeout_s: float = 2.0,
    poll_interval_s: float = 0.01,
) -> WorkerRecord | None:
    """Recover the exact child for a pre-fork claim after lost ``Popen.pid``.

    The token is selected before entering CPython's fork/exec machinery and is
    visible only after that child has execed with the claimed environment. This
    closes the Python bytecode gap between ``_fork_exec`` returning a PID and
    ``Popen`` publishing it on the object.
    """
    try:
        current_boot_id = _current_boot_id()
    except _ProcfsUnavailableError as exc:
        raise WorkerStateUnavailableError(
            "cannot recover worker spawn claim: boot identity unavailable"
        ) from exc
    if current_boot_id != claim.boot_id:
        return None
    parent_stat = _proc_stat(claim.parent_pid)
    if parent_stat is None or parent_stat[1] != claim.parent_start_time:
        return None
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            entries = list(Path("/proc").iterdir())
        except OSError as exc:
            raise WorkerStateUnavailableError("cannot enumerate /proc") from exc
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                if _proc_ppid(pid) != claim.parent_pid:
                    continue
                environ = _proc_environ(pid)
            except _ProcfsUnavailableError:
                continue
            if environ is None or environ.get(WORKER_SPAWN_TOKEN_ENV) != claim.token:
                continue
            if environ.get(WORKER_ROLE_ENV) != claim.role:
                continue
            record = capture_worker_identity(
                pid,
                role=claim.role,
                expected_parent_pid=claim.parent_pid,
                pidfile=Path(claim.pidfile),
                spawn_token=claim.token,
            )
            if record is not None and record.config == claim.config:
                return record
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(poll_interval_s, remaining))


def wait_for_worker_role(
    record: WorkerRecord,
    *,
    timeout_s: float,
    poll_interval_s: float = 0.02,
) -> bool:
    """Wait boundedly for one captured lifetime to exec its expected role."""
    if not math.isfinite(timeout_s) or timeout_s < 0 or timeout_s > 10:
        return False
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        if not _record_matches_kernel_lifetime(record):
            return False
        ready = replace(record, provisional=False)
        if record_matches_process(ready):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_interval_s, remaining))


def worker_records_match_request(
    records: Collection[WorkerRecord],
    *,
    watch: bool,
    ambient: bool,
    session: str,
) -> bool:
    """Whether live records exactly implement one shell start request."""
    requested_roles = {
        role for role, enabled in (("watch", watch), ("ambient", ambient)) if enabled
    }
    if any(record.provisional for record in records):
        return False
    if len(records) != len(requested_roles):
        return False
    if {record.role for record in records} != requested_roles:
        return False
    requested_config = _config_path_from_environ(dict(os.environ))
    if requested_config is None:
        return False
    for record in records:
        if not record_matches_process(record):
            return False
        live_environ = _proc_environ(record.pid)
        if live_environ is None:
            return False
        if _config_path_from_environ(live_environ) != requested_config:
            return False
        if record.role != "watch":
            continue
        argv = _proc_argv(record.pid)
        if argv is None:
            return False
        try:
            session_index = argv.index("--session")
            live_session = argv[session_index + 1]
        except (ValueError, IndexError):
            return False
        if live_session != session:
            return False
    return True


def _parse_stored_record(line: str) -> WorkerRecord | None:
    try:
        value = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    try:
        pid = value["pid"]
        start_time = value["start_time"]
        role = value["role"]
        version = value["version"]
    except KeyError:
        return None
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(start_time, str)
        or not start_time
        or not isinstance(role, str)
        or role not in WORKER_ROLES
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version != RECORD_VERSION
    ):
        return None
    pidfile = value.get("pidfile")
    config = value.get("config")
    provisional = value.get("provisional", False)
    boot_id = value.get("boot_id")
    spawn_token = value.get("spawn_token")
    legacy = value.get("legacy", False)
    if pidfile is not None and (not isinstance(pidfile, str) or not pidfile):
        return None
    if config is not None and (not isinstance(config, str) or not config):
        return None
    if not isinstance(provisional, bool):
        return None
    if boot_id is not None and (not isinstance(boot_id, str) or not boot_id):
        return None
    if spawn_token is not None and (
        not isinstance(spawn_token, str) or not spawn_token
    ):
        return None
    if provisional and (boot_id is None or spawn_token is None):
        return None
    if not isinstance(legacy, bool) or (provisional and legacy):
        return None
    return WorkerRecord(
        pid=pid,
        start_time=start_time,
        role=role,
        version=version,
        pidfile=pidfile,
        config=config,
        provisional=provisional,
        boot_id=boot_id,
        spawn_token=spawn_token,
        legacy=legacy,
    )


def _parse_legacy_pid(line: str) -> int | None:
    if line.startswith("pid="):
        line = line.split("=", 1)[1].strip()
    try:
        pid = int(line)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _read_worker_records_unlocked(path: Path) -> list[WorkerRecord]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records = {
        record.pid: record
        for line in lines
        if (record := _parse_stored_record(line.strip())) is not None
    }
    return sorted(records.values(), key=lambda record: record.pid)


def read_worker_records(path: Path) -> list[WorkerRecord]:
    """Read structured identities under the shared pidfile lock."""
    with worker_pidfile_lock(path, exclusive=False):
        return _read_worker_records_unlocked(path)


def _write_worker_records_unlocked(path: Path, records: Iterable[WorkerRecord]) -> None:
    unique = {record.pid: record for record in records}
    ordered = sorted(unique.values(), key=lambda record: record.pid)
    if not ordered:
        existed = path.exists()
        path.unlink(missing_ok=True)
        if existed:
            _fsync_directory(path.parent)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{record.to_json()}\n" for record in ordered)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def write_worker_records(path: Path, records: Iterable[WorkerRecord]) -> None:
    """Atomically replace *path* under its cross-process ownership lock."""
    with worker_pidfile_lock(path):
        _write_worker_records_unlocked(path, records)


def _write_worker_records_direct_unlocked(
    path: Path, records: Iterable[WorkerRecord]
) -> None:
    """Synchronously rewrite structured identities without an atomic rename."""
    unique = {record.pid: record for record in records}
    ordered = sorted(unique.values(), key=lambda record: record.pid)
    if not ordered:
        existed = path.exists()
        path.unlink(missing_ok=True)
        if existed:
            _fsync_directory(path.parent)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{record.to_json()}\n" for record in ordered)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def write_worker_records_direct(path: Path, records: Iterable[WorkerRecord]) -> None:
    """Durably publish structured identities when atomic rename is unavailable."""
    with worker_pidfile_lock(path):
        _write_worker_records_direct_unlocked(path, records)


def write_worker_pidfile_bytes(path: Path, payload: bytes | None) -> None:
    """Atomically restore raw legacy ownership within a larger transaction."""
    with worker_pidfile_lock(path):
        if payload is None:
            existed = path.exists()
            path.unlink(missing_ok=True)
            if existed:
                _fsync_directory(path.parent)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def write_worker_pidfile_bytes_direct(path: Path, payload: bytes | None) -> None:
    """Synchronously restore raw ownership when atomic replacement fails."""
    with worker_pidfile_lock(path):
        if payload is None:
            existed = path.exists()
            path.unlink(missing_ok=True)
            if existed:
                _fsync_directory(path.parent)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)


def replace_owned_worker_records(
    path: Path,
    *,
    owned_pids: Collection[int],
    records: Iterable[WorkerRecord],
) -> None:
    """Replace one producer's records while preserving other live owners."""
    replacements = list(records)
    with worker_pidfile_lock(path):
        current = _collect_worker_records_unlocked(path, discover=False, rewrite=False)
        merged = [record for record in current if record.pid not in owned_pids]
        merged.extend(replacements)
        _write_worker_records_unlocked(path, merged)


def _discover_workers(path: Path, config_path: Path) -> list[WorkerRecord]:
    records: list[WorkerRecord] = []
    try:
        _current_boot_id()
    except _ProcfsUnavailableError as exc:
        raise WorkerStateUnavailableError(
            "cannot discover workers: boot provenance unavailable"
        ) from exc
    try:
        proc_entries = Path("/proc").iterdir()
    except OSError:
        return records
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        try:
            record = inspect_worker(
                int(entry.name),
                expected_pidfile=path,
                expected_config=config_path,
            )
        except _ProcfsUnavailableError:
            # Unreadable unrelated /proc entries do not constrain this
            # pidfile. Recorded entries are handled fail-closed above.
            continue
        if record is not None:
            records.append(record)

    # `uv run hark ...` may briefly expose both a waiting wrapper and its Hark
    # child. They are one logical worker only when ancestry proves that
    # relationship; keep the leaf that actually owns the long-running role.
    def is_ancestor(ancestor_pid: int, descendant_pid: int) -> bool:
        seen: set[int] = set()
        current = descendant_pid
        for _ in range(64):
            parent = _proc_ppid(current)
            if parent is None or parent in seen:
                return False
            if parent == ancestor_pid:
                return True
            seen.add(parent)
            current = parent
        return False

    return [
        record
        for record in records
        if not any(
            other.pid != record.pid
            and other.role == record.role
            and other.pidfile == record.pidfile
            and is_ancestor(record.pid, other.pid)
            for other in records
        )
    ]


def collect_worker_records(
    path: Path,
    *,
    discover: bool = False,
    rewrite: bool = True,
) -> list[WorkerRecord]:
    """Load, validate, migrate, and optionally discover current workers.

    Bare legacy PIDs never authorise signalling because they contain no
    historical start time. Dead or clearly unrelated entries are omitted, but
    a live Hark-shaped occupant makes ownership unavailable and leaves the raw
    pidfile untouched. Structured pre-scope records may still be migrated after
    their historical PID/start-time/role tuple is validated.
    """
    with worker_pidfile_lock(path, exclusive=rewrite):
        return _collect_worker_records_unlocked(
            path, discover=discover, rewrite=rewrite
        )


def _collect_worker_records_unlocked(
    path: Path,
    *,
    discover: bool,
    rewrite: bool,
) -> list[WorkerRecord]:
    records: dict[int, WorkerRecord] = {}
    # A PID rejected from this ownership snapshot must not be rediscovered as a
    # fresh orphan in the same transaction. In particular, a reused PID may
    # now contain another marker-scoped Hark worker, but it is not the recorded
    # lifetime and therefore is not authority for this stop operation.
    recorded_pids: set[int] = set()
    expected_config_path = _config_path_from_environ(dict(os.environ))
    if expected_config_path is None:
        return []
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    lines = original.splitlines()

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        stored = _parse_stored_record(line)
        if stored is not None:
            recorded_pids.add(stored.pid)
            stored_scope = (
                str(Path(stored.pidfile).resolve(strict=False))
                if stored.pidfile is not None
                else None
            )
            expected_scope = str(path.resolve(strict=False))
            if stored_scope is not None and stored_scope != expected_scope:
                continue
            stored_config = (
                str(Path(stored.config).resolve(strict=False))
                if stored.config is not None
                else None
            )
            try:
                if stored.provisional:
                    # Provisional authority is valid only with boot-scoped,
                    # child-provided spawn provenance. Never infer it.
                    if (
                        stored_scope is not None
                        and stored_config is not None
                        and stored.boot_id is not None
                        and stored.spawn_token is not None
                        and record_matches_lifetime(stored)
                    ):
                        records[stored.pid] = stored
                elif (
                    stored_scope is None
                    or stored_config is None
                    or stored.boot_id is None
                ):
                    # Migrate pre-B127 records from the actual historical
                    # launch shape. Marker env vars did not exist yet.
                    migrated = inspect_worker(
                        stored.pid,
                        expected_role=stored.role,
                        expected_pidfile=path,
                        expected_config=(
                            Path(stored_config)
                            if stored_config is not None
                            else expected_config_path
                        ),
                        require_markers=False,
                    )
                    if (
                        migrated is not None
                        and migrated.start_time == stored.start_time
                    ):
                        records[migrated.pid] = migrated
                elif record_matches_process(stored):
                    records[stored.pid] = stored
            except _ProcfsUnavailableError as exc:
                raise WorkerStateUnavailableError(
                    f"cannot verify recorded worker pid {stored.pid}; "
                    "retaining ownership state",
                    pids=[stored.pid],
                ) from exc
            continue
        legacy_pid = _parse_legacy_pid(line)
        if legacy_pid is not None:
            recorded_pids.add(legacy_pid)
            try:
                migrated = inspect_worker(
                    legacy_pid,
                    expected_pidfile=path,
                    expected_config=expected_config_path,
                    require_markers=False,
                )
            except _ProcfsUnavailableError as exc:
                raise WorkerStateUnavailableError(
                    f"cannot verify legacy worker pid {legacy_pid}; "
                    "retaining ownership state",
                    pids=[legacy_pid],
                ) from exc
            if migrated is not None:
                raise WorkerStateUnavailableError(
                    f"legacy bare pid {legacy_pid} has no historical start time; "
                    "refusing to signal its live Hark-shaped occupant and "
                    "retaining ownership state",
                    pids=[legacy_pid],
                )

    if discover:
        for record in _discover_workers(path, expected_config_path):
            if record.pid not in recorded_pids:
                records[record.pid] = record

    result = sorted(records.values(), key=lambda record: record.pid)
    canonical = "".join(f"{record.to_json()}\n" for record in result)
    if rewrite and (original != canonical or (not result and path.exists())):
        _write_worker_records_unlocked(path, result)
    return result


def _open_pidfd(pid: int) -> int:
    stdlib_open = getattr(os, "pidfd_open", None)
    if stdlib_open is not None:
        return stdlib_open(pid)
    if _LIBC_PIDFD_OPEN is None:
        raise PidfdUnavailableError("pidfd_open is unavailable")
    ctypes.set_errno(0)
    fd = _LIBC_PIDFD_OPEN(pid, 0)
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(fd)


def _send_pidfd_signal(pidfd: int, sig: int) -> None:
    stdlib_send = getattr(signal, "pidfd_send_signal", None)
    if stdlib_send is not None:
        stdlib_send(pidfd, sig)
        return
    if _LIBC_PIDFD_SEND_SIGNAL is None:
        raise PidfdUnavailableError("pidfd_send_signal is unavailable")
    ctypes.set_errno(0)
    result = _LIBC_PIDFD_SEND_SIGNAL(pidfd, sig, None, 0)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def open_process_handle(pid: int) -> int:
    """Open a lifetime-pinning pidfd for a just-spawned owned process."""
    return _open_pidfd(pid)


def signal_process_handle(pidfd: int, sig: int) -> None:
    """Signal the exact lifetime pinned by *pidfd*."""
    _send_pidfd_signal(pidfd, sig)


def _signal_worker(
    record: WorkerRecord, sig: int, *, lifetime_only: bool = False
) -> WorkerSignalOutcome:
    """Verify identity immediately before safely signalling one worker.

    A pidfd pins the process lifetime across verification and signal, closing
    the final PID-reuse race.  Python's wrappers are preferred and libc fills
    the gap on runtimes that omit them; without either, signalling fails closed.
    A process that has exited or changed identity is a benign miss; an error
    signalling a still verified process is reported separately.
    """
    matches = (
        record_matches_lifetime
        if lifetime_only or record.provisional
        else record_matches_process
    )

    def verify(stage: str) -> tuple[bool | None, str | None]:
        try:
            return matches(record), None
        except Exception as exc:
            return None, f"{stage} identity verification failed: {exc}"

    try:
        pidfd = _open_pidfd(record.pid)
    except ProcessLookupError:
        return WorkerSignalOutcome(record)
    except PidfdUnavailableError as exc:
        matched, verify_error = verify("after pidfd unavailable")
        if verify_error is not None:
            return WorkerSignalOutcome(
                record, error=f"pidfd unavailable: {exc}; {verify_error}"
            )
        if not matched:
            return WorkerSignalOutcome(record)
        return WorkerSignalOutcome(record, error=f"pidfd unavailable: {exc}")
    except Exception as exc:
        matched, verify_error = verify("after pidfd_open failure")
        if verify_error is not None:
            return WorkerSignalOutcome(
                record, error=f"pidfd_open failed: {exc}; {verify_error}"
            )
        if not matched:
            return WorkerSignalOutcome(record)
        return WorkerSignalOutcome(record, error=f"pidfd_open failed: {exc}")

    matched, verify_error = verify("post-pidfd-open")
    if verify_error is not None:
        outcome = WorkerSignalOutcome(record, error=verify_error)
    elif not matched:
        outcome = WorkerSignalOutcome(record)
    else:
        try:
            _send_pidfd_signal(pidfd, sig)
        except ProcessLookupError:
            outcome = WorkerSignalOutcome(record)
        except PidfdUnavailableError as exc:
            outcome = WorkerSignalOutcome(record, error=f"pidfd unavailable: {exc}")
        except Exception as exc:
            outcome = WorkerSignalOutcome(
                record, error=f"pidfd_send_signal failed: {exc}"
            )
        else:
            outcome = WorkerSignalOutcome(record, sent=True)

    try:
        os.close(pidfd)
    except Exception as exc:
        close_error = f"pidfd close failed: {exc}"
        outcome = replace(
            outcome,
            error=(
                f"{outcome.error}; {close_error}"
                if outcome.error is not None
                else close_error
            ),
        )
    return outcome


def signal_worker(record: WorkerRecord, sig: int) -> bool:
    """Return whether *sig* was sent to the exact recorded worker lifetime."""
    return _signal_worker(record, sig).sent


def signal_worker_lifetime(record: WorkerRecord, sig: int) -> WorkerSignalOutcome:
    """Signal a trusted spawn record before it has reached its final role."""
    return _signal_worker(record, sig, lifetime_only=True)


def signal_worker_records(
    records: Iterable[WorkerRecord], sig: int
) -> WorkerSignalResult:
    """Attempt every record and preserve benign misses separately from errors."""
    outcomes: list[WorkerSignalOutcome] = []
    for record in records:
        try:
            outcome = _signal_worker(record, sig)
        except Exception as exc:
            outcome = WorkerSignalOutcome(
                record,
                error=f"unexpected signal attempt failure: {type(exc).__name__}: {exc}",
            )
        outcomes.append(outcome)
    return WorkerSignalResult(sig, tuple(outcomes))


def _parse_signal(value: str) -> int:
    name = value.upper()
    if name.startswith("SIG"):
        name = name[3:]
    if name.isdigit():
        return int(name)
    try:
        return int(getattr(signal, f"SIG{name}"))
    except AttributeError as exc:
        raise argparse.ArgumentTypeError(f"unknown signal: {value}") from exc


def _parse_capture_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("capture timeout must be numeric") from exc
    if not math.isfinite(timeout) or timeout < 0 or timeout > 10:
        raise argparse.ArgumentTypeError(
            "capture timeout must be finite and between 0 and 10 seconds"
        )
    return timeout


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect")
    collect.add_argument("pidfile", type=Path)
    collect.add_argument("--discover", action="store_true")
    send = sub.add_parser("signal")
    send.add_argument("pidfile", type=Path)
    send.add_argument("signal", type=_parse_signal)
    send.add_argument("--discover", action="store_true")
    compatible = sub.add_parser("compatible")
    compatible.add_argument("pidfile", type=Path)
    compatible.add_argument("--discover", action="store_true")
    compatible.add_argument("--watch", action="store_true")
    compatible.add_argument("--ambient", action="store_true")
    compatible.add_argument("--session", required=True)
    capture = sub.add_parser("capture")
    capture.add_argument("pid", type=int)
    capture.add_argument("role", choices=sorted(WORKER_ROLES))
    capture.add_argument("--timeout", type=_parse_capture_timeout, default=2.0)
    capture.add_argument("--parent-pid", type=int, required=True)
    matches = sub.add_parser("match-records")
    matches.add_argument("records", nargs="+")
    matches.add_argument("--lifetime-only", action="store_true")
    send_records = sub.add_parser("signal-records")
    send_records.add_argument("signal", type=_parse_signal)
    send_records.add_argument("records", nargs="+")
    send_records.add_argument("--lifetime-only", action="store_true")
    publish = sub.add_parser("publish")
    publish.add_argument("pidfile", type=Path)
    publish.add_argument("records", nargs="+")
    publish.add_argument("--direct", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "capture":
        record = capture_worker_identity(
            args.pid,
            role=args.role,
            expected_parent_pid=args.parent_pid,
            pidfile=Path(os.environ[WORKER_PIDFILE_ENV])
            if os.environ.get(WORKER_PIDFILE_ENV)
            else None,
        )
        if record is None:
            return 1
        if wait_for_worker_role(record, timeout_s=args.timeout):
            print(replace(record, provisional=False).to_json())
            return 0
        # A failed pre-role launch still needs durable lifetime authority for
        # rollback.  Callers capture stdout even when this command exits 2.
        print(record.to_json())
        return 2

    if args.command in {"match-records", "signal-records", "publish"}:
        supplied: list[WorkerRecord] = []
        for raw_record in args.records:
            record = _parse_stored_record(raw_record)
            if record is None:
                parser.error("invalid structured worker record")
            supplied.append(record)
        if args.command == "match-records":
            matcher = (
                record_matches_lifetime
                if args.lifetime_only
                else record_matches_process
            )
            for record in supplied:
                if matcher(record):
                    print(record.to_json())
            return 0
        if args.command == "signal-records":
            outcomes = [
                _signal_worker(record, args.signal, lifetime_only=args.lifetime_only)
                for record in supplied
            ]
            errors = [outcome for outcome in outcomes if outcome.error is not None]
            for outcome in errors:
                print(
                    f"failed to signal worker pid {outcome.record.pid}: {outcome.error}",
                    file=sys.stderr,
                )
            return 1 if errors else 0
        writer = write_worker_records_direct if args.direct else write_worker_records
        writer(args.pidfile, supplied)
        return 0

    try:
        records = collect_worker_records(args.pidfile, discover=args.discover)
    except WorkerStateUnavailableError as exc:
        print(f"worker identity unavailable: {exc}", file=sys.stderr)
        return 1
    if args.command == "compatible":
        return (
            0
            if worker_records_match_request(
                records,
                watch=args.watch,
                ambient=args.ambient,
                session=args.session,
            )
            else 1
        )
    if args.command == "signal":
        outcomes = [_signal_worker(record, args.signal) for record in records]
        records = [outcome.record for outcome in outcomes if outcome.sent]
        errors = [outcome for outcome in outcomes if outcome.error is not None]
        for outcome in errors:
            print(
                f"failed to signal worker pid {outcome.record.pid}: {outcome.error}",
                file=sys.stderr,
            )
        if errors:
            return 1
    for record in records:
        print(record.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

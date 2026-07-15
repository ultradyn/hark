"""Durable, PID-reuse-safe identity for ambient and watch workers.

``mode-a.pids`` used to contain bare process IDs.  A PID only identifies a
slot in the process table and may be reused, so it is not safe authority for a
later signal.  This module owns the versioned JSON-lines replacement and keeps
legacy files compatible by migrating only processes whose live command line is
recognisably a Hark ambient or watch worker.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import os
import re
import signal
import sys
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import IO, Collection, Iterable, Iterator, Sequence

WORKER_ROLES = frozenset({"ambient", "watch"})
RECORD_VERSION = 1


@dataclass(frozen=True, order=True)
class WorkerRecord:
    """Identity of one specific lifetime of a Hark worker process."""

    pid: int
    start_time: str
    role: str
    version: int = RECORD_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class WorkerSignalOutcome:
    """Result of trying to signal one recorded process lifetime."""

    record: WorkerRecord
    sent: bool = False
    error: str | None = None


@dataclass
class _HeldWorkerLock:
    handle: IO[bytes]
    exclusive: bool
    depth: int = 1


class PidfdUnavailableError(RuntimeError):
    """Raised when no race-free process signalling primitive is available."""


_WORKER_LOCKS = threading.local()
_INHERITED_LOCK_PATH_ENV = "HARK_WORKER_PIDFILE_LOCK_PATH"
_INHERITED_LOCK_FD_ENV = "HARK_WORKER_PIDFILE_LOCK_FD"
_INHERITED_LOCK_OWNER_ENV = "HARK_WORKER_PIDFILE_LOCK_OWNER_PID"


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


def _is_ancestor_process(candidate: int) -> bool:
    pid = os.getpid()
    for _ in range(64):
        if pid == candidate:
            return True
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except OSError:
            return False
        parent_line = next(
            (line for line in status.splitlines() if line.startswith("PPid:")), None
        )
        if parent_line is None:
            return False
        try:
            pid = int(parent_line.split(":", 1)[1].strip())
        except ValueError:
            return False
        if pid <= 0:
            return False
    return False


def _has_inherited_exclusive_lock(key: Path) -> bool:
    """Validate and reuse an outer shell lock across helper processes."""
    raw_path = os.environ.get(_INHERITED_LOCK_PATH_ENV)
    raw_fd = os.environ.get(_INHERITED_LOCK_FD_ENV)
    if not raw_path or not raw_fd:
        return False
    try:
        if _lock_key(Path(raw_path)) != key:
            return False
        fd = int(raw_fd)
        if fd < 0:
            return False
        lock_path = key.with_name(f"{key.name}.lock")
        lock_stat = lock_path.stat()
        try:
            descriptor = os.fstat(fd)
            if (descriptor.st_dev, descriptor.st_ino) == (
                lock_stat.st_dev,
                lock_stat.st_ino,
            ):
                # The inherited descriptor shares the shell's open file
                # description, so this is a no-op and retains exclusivity.
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
        except (BlockingIOError, OSError):
            pass

        # uv may close non-standard descriptors before it launches Python.
        # In that case, prove that the descriptor remains open and locked in
        # an ancestor shell which is synchronously waiting for this helper.
        owner = int(os.environ[_INHERITED_LOCK_OWNER_ENV])
        if owner <= 0 or not _is_ancestor_process(owner):
            return False
        owner_descriptor = Path(f"/proc/{owner}/fd/{fd}").stat()
        if (owner_descriptor.st_dev, owner_descriptor.st_ino) != (
            lock_stat.st_dev,
            lock_stat.st_ino,
        ):
            return False
        probe = lock_path.open("a+b")
        try:
            try:
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
            return False
        finally:
            probe.close()
    except (KeyError, OSError, TypeError, ValueError):
        return False


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

    if _has_inherited_exclusive_lock(key):
        yield
        return

    key.parent.mkdir(parents=True, exist_ok=True)
    lock_path = key.with_name(f"{key.name}.lock")
    handle = lock_path.open("a+b")
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(handle.fileno(), operation)
    except BaseException:
        handle.close()
        raise

    entry = _HeldWorkerLock(handle=handle, exclusive=exclusive)
    if not hasattr(_WORKER_LOCKS, "held"):
        _WORKER_LOCKS.held = held
    held[key] = entry
    try:
        yield
    finally:
        entry.depth -= 1
        if entry.depth == 0:
            held.pop(key, None)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _proc_stat(pid: int) -> tuple[str, str] | None:
    """Return ``(state, start_time_ticks)`` from Linux ``/proc/PID/stat``."""
    if pid <= 0:
        return None
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return None
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
    except OSError:
        return None
    if not raw:
        return None
    return [
        part.decode("utf-8", errors="surrogateescape")
        for part in raw.split(b"\0")
        if part
    ]


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
    pid: int, *, expected_role: str | None = None
) -> WorkerRecord | None:
    """Inspect the current process occupying *pid*, if it is a Hark worker."""
    stat = _proc_stat(pid)
    argv = _proc_argv(pid)
    if stat is None or argv is None:
        return None
    role = worker_role_from_argv(argv)
    if role is None or (expected_role is not None and role != expected_role):
        return None
    return WorkerRecord(pid=pid, start_time=stat[1], role=role)


def capture_worker_identity(pid: int, *, role: str) -> WorkerRecord | None:
    """Capture a newly spawned, caller-owned worker before later validation."""
    if role not in WORKER_ROLES:
        raise ValueError(f"invalid worker role: {role}")
    stat = _proc_stat(pid)
    if stat is None:
        return None
    return WorkerRecord(pid=pid, start_time=stat[1], role=role)


def record_matches_process(record: WorkerRecord) -> bool:
    """Return whether *record* still names the same worker process lifetime."""
    return inspect_worker(record.pid, expected_role=record.role) == record


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
    return WorkerRecord(pid=pid, start_time=start_time, role=role, version=version)


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
        path.unlink(missing_ok=True)
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


def write_worker_pidfile_bytes(path: Path, payload: bytes | None) -> None:
    """Atomically restore raw legacy ownership within a larger transaction."""
    with worker_pidfile_lock(path):
        if payload is None:
            path.unlink(missing_ok=True)
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
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


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


def _discover_workers() -> list[WorkerRecord]:
    records: list[WorkerRecord] = []
    try:
        proc_entries = Path("/proc").iterdir()
    except OSError:
        return records
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        record = inspect_worker(int(entry.name))
        if record is not None:
            records.append(record)
    return records


def collect_worker_records(
    path: Path,
    *,
    discover: bool = False,
    rewrite: bool = True,
) -> list[WorkerRecord]:
    """Load, validate, migrate, and optionally discover current workers.

    Bare legacy PIDs are accepted only when their current process shape is a
    Hark worker.  Malformed, dead, role-mismatched, or PID-reused entries are
    omitted.  Rewriting removes those unsafe entries and upgrades valid legacy
    entries to structured records.
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
            if record_matches_process(stored):
                records[stored.pid] = stored
            continue
        legacy_pid = _parse_legacy_pid(line)
        if legacy_pid is not None:
            migrated = inspect_worker(legacy_pid)
            if migrated is not None:
                records[migrated.pid] = migrated

    if discover:
        for record in _discover_workers():
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


def _signal_worker(record: WorkerRecord, sig: int) -> WorkerSignalOutcome:
    """Verify identity immediately before safely signalling one worker.

    A pidfd pins the process lifetime across verification and signal, closing
    the final PID-reuse race.  Python's wrappers are preferred and libc fills
    the gap on runtimes that omit them; without either, signalling fails closed.
    A process that has exited or changed identity is a benign miss; an error
    signalling a still verified process is reported separately.
    """
    try:
        pidfd = _open_pidfd(record.pid)
    except ProcessLookupError:
        return WorkerSignalOutcome(record)
    except PidfdUnavailableError as exc:
        if not record_matches_process(record):
            return WorkerSignalOutcome(record)
        return WorkerSignalOutcome(record, error=f"pidfd unavailable: {exc}")
    except (OSError, ValueError) as exc:
        if not record_matches_process(record):
            return WorkerSignalOutcome(record)
        return WorkerSignalOutcome(record, error=f"pidfd_open failed: {exc}")
    try:
        if not record_matches_process(record):
            return WorkerSignalOutcome(record)
        try:
            _send_pidfd_signal(pidfd, sig)
        except ProcessLookupError:
            return WorkerSignalOutcome(record)
        except PidfdUnavailableError as exc:
            return WorkerSignalOutcome(record, error=f"pidfd unavailable: {exc}")
        except (OSError, ValueError) as exc:
            return WorkerSignalOutcome(record, error=f"pidfd_send_signal failed: {exc}")
        return WorkerSignalOutcome(record, sent=True)
    finally:
        os.close(pidfd)


def signal_worker(record: WorkerRecord, sig: int) -> bool:
    """Return whether *sig* was sent to the exact recorded worker lifetime."""
    return _signal_worker(record, sig).sent


def signal_worker_records(
    records: Iterable[WorkerRecord], sig: int
) -> list[WorkerRecord]:
    return [record for record in records if signal_worker(record, sig)]


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
    args = parser.parse_args(argv)

    records = collect_worker_records(args.pidfile, discover=args.discover)
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

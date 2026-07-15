"""PID-reuse-safe worker identity and signalling regression tests (B127)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import hark.worker_process as worker_process


@pytest.mark.parametrize(
    ("argv", "role"),
    [
        (["hark", "ambient"], "ambient"),
        (["/venv/bin/hark", "watch", "--for-monitor"], "watch"),
        (["python3", "/venv/bin/hark", "ambient"], "ambient"),
        (["python", "-m", "hark", "watch", "--session", "lab"], "watch"),
        (
            ["python", "-m", "hark", "watch", "--session", "run-mode-a-lab"],
            "watch",
        ),
        (["/usr/bin/uv", "run", "hark", "ambient"], "ambient"),
    ],
)
def test_worker_role_accepts_real_launch_shapes(argv: list[str], role: str):
    assert worker_process.worker_role_from_argv(argv) == role


def test_forged_inherited_lock_rejects_unrelated_contention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    pidfile = tmp_path / "mode-a.pids"
    lock_path = tmp_path / "mode-a.pids.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, pathlib, sys, time; "
                "f = pathlib.Path(sys.argv[1]).open('a+b'); "
                "fcntl.flock(f.fileno(), fcntl.LOCK_EX); "
                "print('ready', flush=True); time.sleep(30)"
            ),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        monkeypatch.setenv("HARK_WORKER_PIDFILE_LOCK_PATH", str(pidfile))
        monkeypatch.setenv("HARK_WORKER_PIDFILE_LOCK_FD", str(descriptor))

        assert worker_process._has_inherited_exclusive_lock(pidfile) is False
    finally:
        os.close(descriptor)
        holder.kill()
        holder.wait(timeout=2)


def test_worker_request_compatibility_requires_exact_roles_and_watch_session(
    monkeypatch: pytest.MonkeyPatch,
):
    watch = worker_process.WorkerRecord(pid=101, start_time="w", role="watch")
    ambient = worker_process.WorkerRecord(pid=202, start_time="a", role="ambient")
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(
        worker_process,
        "_proc_argv",
        lambda pid: (
            ["hark", "watch", "--session", "requested"]
            if pid == watch.pid
            else ["hark", "ambient"]
        ),
    )
    monkeypatch.setattr(
        worker_process, "_proc_environ", lambda _pid: dict(os.environ)
    )

    assert worker_process.worker_records_match_request(
        [watch, ambient], watch=True, ambient=True, session="requested"
    )
    assert not worker_process.worker_records_match_request(
        [watch, ambient], watch=True, ambient=True, session="different"
    )
    assert not worker_process.worker_records_match_request(
        [watch], watch=False, ambient=True, session="requested"
    )
    monkeypatch.setattr(
        worker_process,
        "_proc_environ",
        lambda _pid: {**os.environ, "HARK_CONFIG": "/different/config.toml"},
    )
    assert not worker_process.worker_records_match_request(
        [watch, ambient], watch=True, ambient=True, session="requested"
    )


def test_direct_publication_keeps_structured_worker_identity(tmp_path: Path):
    path = tmp_path / "mode-a.pids"
    record = worker_process.WorkerRecord(
        pid=4321, start_time="captured-start", role="ambient"
    )

    assert worker_process.main(
        ["publish", str(path), "--direct", record.to_json()]
    ) == 0

    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(record.to_json())


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", "sleep 60", "hark", "ambient"],
        ["python", "-c", "import time", "hark", "watch"],
        ["python", "script.py", "hark", "ambient"],
        ["python-malware", "-m", "hark", "watch"],
        ["pypymalware", "/venv/bin/hark", "ambient"],
        ["python3", "/repo/scripts/run-mode-a.sh", "ambient"],
        ["uv", "tool", "hark", "watch"],
    ],
)
def test_worker_role_rejects_unrelated_trailing_hark_args(argv: list[str]):
    assert worker_process.worker_role_from_argv(argv) is None


def spawn_process(
    directory: Path, *, role: str | None = None
) -> subprocess.Popen[bytes]:
    if role:
        launcher = directory / "hark"
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import signal\n"
            "import time\n"
            "def stop(*_args):\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "while True:\n"
            "    time.sleep(1)\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        child = subprocess.Popen([str(launcher), role], start_new_session=True)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if worker_process.inspect_worker(child.pid, expected_role=role) is not None:
                return child
            time.sleep(0.01)
        kill_child(child)
        raise AssertionError(f"worker argv did not become ready for role {role}")
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )


def kill_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            child.kill()
    child.wait(timeout=2)


def test_legacy_worker_is_migrated_with_role_and_start_time(tmp_path: Path):
    child = spawn_process(tmp_path, role="ambient")
    path = tmp_path / "mode-a.pids"
    try:
        path.write_text(f"pid={child.pid}\n", encoding="utf-8")
        records = worker_process.collect_worker_records(path)
        assert [(record.pid, record.role) for record in records] == [
            (child.pid, "ambient")
        ]
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert stored == {
            "pid": child.pid,
            "role": "ambient",
            "start_time": records[0].start_time,
            "version": 1,
        }
    finally:
        kill_child(child)


def test_live_unrelated_legacy_pid_is_removed_without_signal(tmp_path: Path):
    child = spawn_process(tmp_path)
    path = tmp_path / "mode-a.pids"
    try:
        path.write_text(f"{child.pid}\n", encoding="utf-8")
        assert worker_process.collect_worker_records(path) == []
        assert child.poll() is None
        assert not path.exists()
    finally:
        kill_child(child)


def test_unrelated_suffix_is_ignored_by_migration_and_discovery(tmp_path: Path):
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "hark",
            "ambient",
        ],
        start_new_session=True,
    )
    path = tmp_path / "mode-a.pids"
    try:
        path.write_text(f"{child.pid}\n", encoding="utf-8")
        records = worker_process.collect_worker_records(path, discover=True)
        assert child.pid not in {record.pid for record in records}
        assert child.poll() is None
    finally:
        kill_child(child)


def test_orphan_discovery_records_untracked_worker(tmp_path: Path):
    child = spawn_process(tmp_path, role="watch")
    path = tmp_path / "mode-a.pids"
    try:
        records = worker_process.collect_worker_records(path, discover=True)
        matching = [record for record in records if record.pid == child.pid]
        assert len(matching) == 1
        assert matching[0].role == "watch"
        assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])[
            "start_time"
        ]
    finally:
        kill_child(child)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: worker_process.WorkerRecord(
            pid=record.pid, start_time="reused-pid-start-time", role=record.role
        ),
        lambda record: worker_process.WorkerRecord(
            pid=record.pid, start_time=record.start_time, role="watch"
        ),
    ],
    ids=["reused-pid", "role-mismatch"],
)
def test_mismatched_structured_record_is_removed_without_signal(
    tmp_path: Path,
    mutation,
):
    child = spawn_process(tmp_path, role="ambient")
    path = tmp_path / "mode-a.pids"
    try:
        actual = worker_process.inspect_worker(child.pid)
        assert actual is not None
        worker_process.write_worker_records(path, [mutation(actual)])
        records = worker_process.collect_worker_records(path)
        assert records == []
        assert child.poll() is None
        assert not path.exists()
    finally:
        kill_child(child)


def test_malformed_and_stale_entries_are_removed(tmp_path: Path):
    path = tmp_path / "mode-a.pids"
    path.write_text(
        "\n".join(
            [
                "not-json-or-pid",
                '{"pid":true,"role":"watch","start_time":"1","version":1}',
                '{"pid":123,"role":[],"start_time":"1","version":1}',
                '{"pid":999999999,"role":"watch","start_time":"1","version":1}',
            ]
        ),
        encoding="utf-8",
    )
    assert worker_process.collect_worker_records(path) == []
    assert not path.exists()


def test_unreadable_pidfile_fails_closed_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "mode-a.pids"
    original = b'{"pid":123,"role":"watch","start_time":"1","version":1}\n'
    path.write_bytes(original)
    real_read_text = Path.read_text

    def deny_target_read(candidate: Path, *args, **kwargs):
        if candidate == path:
            raise PermissionError("ownership state unreadable")
        return real_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_target_read)

    with pytest.raises(PermissionError, match="ownership state unreadable"):
        worker_process.collect_worker_records(path)

    assert path.read_bytes() == original


def test_paused_empty_collector_cannot_erase_concurrent_fresh_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "mode-a.pids"
    path.write_text("malformed-old-state\n", encoding="utf-8")
    fresh = worker_process.WorkerRecord(
        pid=4321, start_time="fresh-owner", role="watch"
    )
    collector_at_rewrite = threading.Event()
    release_collector = threading.Event()
    writer_entered_unlocked = threading.Event()
    failures: list[BaseException] = []
    real_write = worker_process._write_worker_records_unlocked

    def controlled_write(target: Path, records):
        materialized = list(records)
        if threading.current_thread().name == "paused-collector":
            collector_at_rewrite.set()
            if not release_collector.wait(timeout=2):
                raise TimeoutError("collector was not released")
        else:
            writer_entered_unlocked.set()
        real_write(target, materialized)

    monkeypatch.setattr(
        worker_process, "_write_worker_records_unlocked", controlled_write
    )

    def collect_stale() -> None:
        try:
            assert worker_process.collect_worker_records(path) == []
        except BaseException as exc:
            failures.append(exc)

    def write_fresh() -> None:
        try:
            worker_process.write_worker_records(path, [fresh])
        except BaseException as exc:
            failures.append(exc)

    collector = threading.Thread(target=collect_stale, name="paused-collector")
    collector.start()
    assert collector_at_rewrite.wait(timeout=2)
    writer = threading.Thread(target=write_fresh, name="fresh-writer")
    writer.start()

    # The writer cannot reach its unlocked replace while the collector owns
    # the transaction lock.  Without serialization it writes now and the
    # resumed empty collector unlinks that fresh ownership.
    assert not writer_entered_unlocked.wait(timeout=0.1)
    release_collector.set()
    collector.join(timeout=2)
    writer.join(timeout=2)

    assert not collector.is_alive()
    assert not writer.is_alive()
    assert failures == []
    assert worker_process.read_worker_records(path) == [fresh]


def test_owned_writer_preserves_other_live_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "mode-a.pids"
    other = worker_process.WorkerRecord(
        pid=1111, start_time="other-owner", role="ambient"
    )
    old_owned = worker_process.WorkerRecord(
        pid=2222, start_time="old-owned", role="watch"
    )
    refreshed_owned = worker_process.WorkerRecord(
        pid=2222, start_time="refreshed-owned", role="watch"
    )
    worker_process.write_worker_records(path, [other, old_owned])
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: True)

    worker_process.replace_owned_worker_records(
        path,
        owned_pids={old_owned.pid},
        records=[refreshed_owned],
    )

    assert worker_process.read_worker_records(path) == [other, refreshed_owned]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_signal_reverifies_after_opening_pidfd(
    monkeypatch: pytest.MonkeyPatch, sig: int
):
    record = worker_process.WorkerRecord(
        pid=os.getpid(), start_time="old", role="watch"
    )
    read_fd, write_fd = os.pipe()
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_process.os, "pidfd_open", lambda _pid: read_fd)
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda fd, sig: sent.append((fd, sig)),
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: False)
    try:
        assert worker_process.signal_worker(record, sig) is False
        assert sent == []
    finally:
        os.close(write_fd)


def test_signal_records_cli_does_not_signal_reused_spawn_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    """A captured attempt record is not authority after its PID lifetime changes."""
    record = worker_process.WorkerRecord(
        pid=4321, start_time="spawn-time", role="watch"
    )
    read_fd, write_fd = os.pipe()
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(worker_process.os, "pidfd_open", lambda _pid: read_fd)
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: False)
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda fd, sig: sent.append((fd, sig)),
    )
    try:
        assert worker_process.main(
            ["signal-records", "TERM", record.to_json()]
        ) == 0
    finally:
        os.close(write_fd)

    assert sent == []


@pytest.mark.parametrize(
    "failure_stage", ["pidfd_open", "pidfd_send_signal", "pidfd_unavailable"]
)
def test_signal_cli_fails_when_verified_worker_cannot_be_signalled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_stage: str,
):
    record = worker_process.WorkerRecord(pid=1234, start_time="verified", role="watch")
    monkeypatch.setattr(
        worker_process, "collect_worker_records", lambda *_args, **_kwargs: [record]
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: True)

    write_fd: int | None = None
    if failure_stage == "pidfd_open":
        monkeypatch.setattr(
            worker_process.os,
            "pidfd_open",
            lambda _pid: (_ for _ in ()).throw(PermissionError("denied")),
        )
        monkeypatch.setattr(
            worker_process.signal,
            "pidfd_send_signal",
            lambda _fd, _sig: None,
            raising=False,
        )
    elif failure_stage == "pidfd_send_signal":
        read_fd, write_fd = os.pipe()
        monkeypatch.setattr(worker_process.os, "pidfd_open", lambda _pid: read_fd)
        monkeypatch.setattr(
            worker_process.signal,
            "pidfd_send_signal",
            lambda _fd, _sig: (_ for _ in ()).throw(PermissionError("denied")),
            raising=False,
        )
    else:
        monkeypatch.delattr(worker_process.os, "pidfd_open", raising=False)
        monkeypatch.delattr(worker_process.signal, "pidfd_send_signal", raising=False)
        monkeypatch.setattr(worker_process, "_LIBC_PIDFD_OPEN", None)
        monkeypatch.setattr(worker_process, "_LIBC_PIDFD_SEND_SIGNAL", None)
        monkeypatch.setattr(
            worker_process.os,
            "kill",
            lambda _pid, _sig: (_ for _ in ()).throw(
                AssertionError("unsafe PID fallback used")
            ),
        )

    try:
        assert worker_process.main(["signal", "/tmp/mode-a.pids", "KILL"]) == 1
    finally:
        if write_fd is not None:
            os.close(write_fd)

    captured = capsys.readouterr()
    assert captured.out == ""
    expected = (
        "pidfd unavailable"
        if failure_stage == "pidfd_unavailable"
        else f"{failure_stage} failed"
    )
    assert expected in captured.err
    assert "worker pid 1234" in captured.err


def test_missing_stdlib_pidfd_uses_libc_adapter_without_pid_kill(
    monkeypatch: pytest.MonkeyPatch,
):
    record = worker_process.WorkerRecord(pid=1234, start_time="verified", role="watch")
    read_fd, write_fd = os.pipe()
    opened: list[tuple[int, int]] = []
    sent: list[tuple[int, int, object, int]] = []
    monkeypatch.delattr(worker_process.os, "pidfd_open", raising=False)
    monkeypatch.delattr(worker_process.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(
        worker_process,
        "_LIBC_PIDFD_OPEN",
        lambda pid, flags: opened.append((pid, flags)) or read_fd,
    )
    monkeypatch.setattr(
        worker_process,
        "_LIBC_PIDFD_SEND_SIGNAL",
        lambda fd, sig, info, flags: sent.append((fd, sig, info, flags)) or 0,
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(
        worker_process.os,
        "kill",
        lambda _pid, _sig: (_ for _ in ()).throw(
            AssertionError("unsafe PID fallback used")
        ),
    )
    try:
        assert worker_process.signal_worker(record, signal.SIGTERM) is True
    finally:
        os.close(write_fd)

    assert opened == [(record.pid, 0)]
    assert sent == [(read_fd, signal.SIGTERM, None, 0)]


def test_missing_stdlib_pidfd_rejects_swapped_occupant_after_open(
    monkeypatch: pytest.MonkeyPatch,
):
    record = worker_process.WorkerRecord(pid=1234, start_time="old", role="watch")
    read_fd, write_fd = os.pipe()
    sent: list[int] = []
    monkeypatch.delattr(worker_process.os, "pidfd_open", raising=False)
    monkeypatch.delattr(worker_process.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(
        worker_process, "_LIBC_PIDFD_OPEN", lambda _pid, _flags: read_fd
    )
    monkeypatch.setattr(
        worker_process,
        "_LIBC_PIDFD_SEND_SIGNAL",
        lambda _fd, _sig, _info, _flags: sent.append(_sig) or 0,
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: False)
    monkeypatch.setattr(
        worker_process.os,
        "kill",
        lambda _pid, _sig: (_ for _ in ()).throw(
            AssertionError("unsafe PID fallback used")
        ),
    )
    try:
        assert worker_process.signal_worker(record, signal.SIGKILL) is False
    finally:
        os.close(write_fd)

    assert sent == []


@pytest.mark.parametrize("benign_stage", ["gone", "identity-mismatch"])
def test_signal_cli_ignores_workers_that_are_no_longer_the_recorded_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    benign_stage: str,
):
    record = worker_process.WorkerRecord(pid=1234, start_time="old", role="watch")
    monkeypatch.setattr(
        worker_process, "collect_worker_records", lambda *_args, **_kwargs: [record]
    )
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda _fd, _sig: None,
        raising=False,
    )
    if benign_stage == "gone":
        monkeypatch.setattr(
            worker_process.os,
            "pidfd_open",
            lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
        )
    else:
        monkeypatch.setattr(
            worker_process.os,
            "pidfd_open",
            lambda _pid: (_ for _ in ()).throw(PermissionError("denied")),
        )
        monkeypatch.setattr(
            worker_process, "record_matches_process", lambda _record: False
        )

    assert worker_process.main(["signal", "/tmp/mode-a.pids", "TERM"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_shell_signal_adapter_delegates_to_identity_module(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    trace = tmp_path / "trace"
    command = f"""
set -euo pipefail
export HARK_RUN_MODE_A_SOURCE_ONLY=1
export XDG_STATE_HOME={tmp_path!s}
source {script!s}
worker_identity() {{ printf '%s\\n' "$*" >> {trace!s}; }}
signal_pids TERM 123 456
"""
    subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    expected_pidfile = tmp_path / "hark" / "mode-a.pids"
    assert trace.read_text(encoding="utf-8").strip() == (
        f"signal {expected_pidfile} TERM --discover"
    )


def test_shell_structured_attempt_writer_uses_same_pidfile_lock(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    pidfile = state / "hark" / "mode-a.pids"
    entered = tmp_path / "entered"
    record = worker_process.WorkerRecord(
        pid=1234, start_time="captured", role="watch"
    )
    command = f"""
set -euo pipefail
export HARK_RUN_MODE_A_SOURCE_ONLY=1
export XDG_STATE_HOME={state!s}
source {script!s}
printf 'entered\n' > {entered!s}
write_attempt_worker_records '{record.to_json()}'
printf 'done\n'
"""
    process: subprocess.Popen[str] | None = None
    try:
        with worker_process.worker_pidfile_lock(pidfile):
            process = subprocess.Popen(
                ["bash", "-c", command],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 2
            while not entered.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert entered.exists()
            time.sleep(0.05)
            assert process.poll() is None
            assert not pidfile.exists()

        stdout, stderr = process.communicate(timeout=2)
        assert process.returncode == 0, stderr
        assert stdout.strip() == "done"
        assert json.loads(pidfile.read_text(encoding="utf-8")) == json.loads(
            record.to_json()
        )
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def test_shell_rollback_durably_records_structured_survivor_after_atomic_failure(
    tmp_path: Path,
):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    record = worker_process.WorkerRecord(
        pid=4321, start_time="captured-start", role="watch"
    ).to_json()
    command = f"""
set -euo pipefail
export HARK_RUN_MODE_A_SOURCE_ONLY=1
export XDG_STATE_HOME={state!s}
source {script!s}
started=(4321)
sleep() {{ return 0; }}
worker_identity() {{
  case "$1" in
    match-records) printf '%s\n' '{record}' ;;
    signal-records) return 0 ;;
    publish)
      if [[ " $* " == *" --direct "* ]]; then
        printf '%s\n' '{record}' > "$PIDFILE"
      else
        return 42
      fi
      ;;
  esac
}}
rollback_started_workers '{record}'
"""

    result = subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    pidfile = state / "hark" / "mode-a.pids"
    assert json.loads(pidfile.read_text(encoding="utf-8"))["start_time"] == (
        "captured-start"
    )
    assert "retained surviving rollback workers" in result.stderr


def test_shell_rollback_never_signals_a_reused_pid(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    trace = tmp_path / "trace"
    record = worker_process.WorkerRecord(
        pid=4321, start_time="original-start", role="watch"
    ).to_json()
    command = f"""
set -euo pipefail
export HARK_RUN_MODE_A_SOURCE_ONLY=1
export XDG_STATE_HOME={state!s}
source {script!s}
started=(4321)
worker_identity() {{
  case "$1" in
    match-records) return 0 ;;
    signal-records) printf '%s\n' "$*" >> {trace!s} ;;
    *) return 42 ;;
  esac
}}
rollback_started_workers '{record}'
"""

    subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert not trace.exists()
    assert not (state / "hark" / "mode-a.pids").exists()


def test_shell_stop_retains_pidfile_when_identity_collection_fails(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    pidfile = state / "hark" / "mode-a.pids"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("sentinel\n", encoding="utf-8")
    command = f"""
set -euo pipefail
export HARK_RUN_MODE_A_SOURCE_ONLY=1
export XDG_STATE_HOME={state!s}
source {script!s}
worker_identity() {{ return 42; }}
set +e
graceful_stop 0 stop
status=$?
set -e
printf '%s\n' "$status"
"""
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "1"
    assert "refusing to stop" in result.stderr
    assert pidfile.read_text(encoding="utf-8") == "sentinel\n"


def test_shell_force_stop_retains_pidfile_when_kill_signal_fails(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    pidfile = state / "hark" / "mode-a.pids"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("sentinel\n", encoding="utf-8")
    command = f"""
set -euo pipefail
export HARK_RUN_MODE_A_SOURCE_ONLY=1
export XDG_STATE_HOME={state!s}
export HARK_STOP_GRACE_S=0
source {script!s}
worker_identity() {{
  case "$1" in
    collect) printf '1234\\n' ;;
    signal) [[ "$3" == TERM ]] ;;
  esac
}}
set +e
graceful_stop 1 stop
status=$?
set -e
printf '%s\n' "$status"
"""
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().endswith("1")
    assert "force-killing remaining processes: 1234" in result.stdout
    assert "retaining pidfile" in result.stderr
    assert pidfile.read_text(encoding="utf-8") == "sentinel\n"


def test_shell_start_refuses_when_initial_identity_collection_fails(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    pidfile = state / "hark" / "mode-a.pids"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("sentinel\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    uv.chmod(0o755)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(state)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        [str(script), "--no-ambient"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "refusing to start" in result.stderr
    assert pidfile.read_text(encoding="utf-8") == "sentinel\n"


def test_shell_post_spawn_collection_failure_retains_structured_identity(
    tmp_path: Path,
):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    count = tmp_path / "collect-count"
    real_uv = shutil.which("uv")
    assert real_uv is not None
    fake_hark = tmp_path / "hark"
    fake_hark.write_text(
        "#!/usr/bin/env python3\n"
        "import signal\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: exit(0))\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    fake_hark.chmod(0o755)
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        "if printf '%s\\n' \"$*\" | grep -q 'hark.worker_process collect'; then\n"
        f"  n=$(cat {count!s} 2>/dev/null || echo 0)\n"
        "  n=$((n + 1))\n"
        f"  printf '%s\\n' \"$n\" > {count!s}\n"
        '  [ "$n" -le 2 ] && exit 0\n'
        "  exit 42\n"
        "fi\n"
        'if [ "${1:-}" = run ] && [ "${2:-}" = hark ]; then\n'
        "  shift 2\n"
        '  exec "$FAKE_HARK" "$@"\n'
        "fi\n"
        'exec "$REAL_UV" "$@"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(state)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["REAL_UV"] = real_uv
    env["FAKE_HARK"] = str(fake_hark)

    result = subprocess.run(
        [str(script), "--no-ambient"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    pidfile = state / "hark" / "mode-a.pids"
    stored = json.loads(pidfile.read_text(encoding="utf-8"))
    recorded = stored["pid"]
    try:
        assert result.returncode != 0
        assert "retaining structured ownership" in result.stderr
        assert stored["start_time"]
        assert stored["role"] == "watch"
        os.kill(recorded, 0)
    finally:
        try:
            os.kill(recorded, signal.SIGKILL)
        except ProcessLookupError:
            pass


@pytest.mark.parametrize("collector_fails", [True, False])
def test_shell_publication_failure_rolls_back_started_workers(
    tmp_path: Path, collector_fails: bool
):
    """Both collector-error and successful-empty fallbacks are transactional."""
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    real_uv = shutil.which("uv")
    assert real_uv is not None
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    count = tmp_path / "collect-count"
    spawn_log = tmp_path / "spawn"
    fake_hark = tmp_path / "hark"
    fake_hark.write_text(
        "#!/usr/bin/env python3\n"
        "import signal\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: exit(0))\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    fake_hark.chmod(0o755)
    uv = fake_bin / "uv"
    fail_condition = '[ "$n" -gt 2 ] && exit 42\n' if collector_fails else ""
    uv.write_text(
        "#!/bin/sh\n"
        "if printf '%s\\n' \"$*\" | grep -q 'hark.worker_process collect'; then\n"
        f"  n=$(cat {count!s} 2>/dev/null || echo 0)\n"
        "  n=$((n + 1))\n"
        f"  printf '%s\\n' \"$n\" > {count!s}\n"
        f"  {fail_condition}"
        "  exit 0\n"
        "fi\n"
        "if printf '%s\\n' \"$*\" | grep -q 'hark.worker_process publish'; then\n"
        "  exit 42\n"
        "fi\n"
        'if [ "${1:-}" = run ] && [ "${2:-}" = hark ]; then\n'
        "    shift 2\n"
        f"    printf '%s\\n' \"$$\" > {spawn_log!s}\n"
        '    exec "$FAKE_HARK" "$@"\n'
        "fi\n"
        'exec "$REAL_UV" "$@"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "XDG_STATE_HOME": str(state),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "REAL_UV": real_uv,
            "FAKE_HARK": str(fake_hark),
        }
    )

    result = subprocess.run(
        [str(script), "--no-ambient"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert "rolling back unpublished Hark worker identities" in result.stderr
    spawned = int(spawn_log.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(spawned, 0)
    assert not (state / "hark" / "mode-a.pids").exists()


@pytest.mark.parametrize(
    ("first_args", "second_args", "spawn_roles", "second_message"),
    [
        (
            ["--no-ambient", "--session", "shared"],
            ["--no-ambient", "--session", "shared"],
            ["watch"],
            "workers already started by concurrent invocation",
        ),
        (
            ["--no-ambient", "--session", "first"],
            ["--no-ambient", "--session", "second"],
            ["watch", "watch"],
            "restarting previous workers",
        ),
        (
            ["--no-ambient", "--session", "first"],
            ["--no-watch"],
            ["watch", "ambient"],
            "restarting previous workers",
        ),
    ],
)
def test_concurrent_shell_start_only_accepts_compatible_predecessor(
    tmp_path: Path,
    first_args: list[str],
    second_args: list[str],
    spawn_roles: list[str],
    second_message: str,
):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    real_uv = shutil.which("uv")
    assert real_uv is not None
    state = tmp_path / "state"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    spawn_log = tmp_path / "spawns"
    fake_hark = tmp_path / "hark"
    fake_hark.write_text(
        "#!/usr/bin/env python3\n"
        "import signal\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: exit(0))\n"
        "while True:\n"
        "    time.sleep(1)\n",
        encoding="utf-8",
    )
    fake_hark.chmod(0o755)
    uv = fake_bin / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'if [ "${1:-}" = run ] && [ "${2:-}" = hark ]; then\n'
        "  shift 2\n"
        '  printf \'%s %s\\n\' "$$" "${1:-}" >> "$SPAWN_LOG"\n'
        "  sleep 0.2\n"
        '  exec "$FAKE_HARK" "$@"\n'
        "fi\n"
        'exec "$REAL_UV" "$@"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "XDG_STATE_HOME": str(state),
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "REAL_UV": real_uv,
            "FAKE_HARK": str(fake_hark),
            "SPAWN_LOG": str(spawn_log),
        }
    )
    processes: list[subprocess.Popen[str]] = []
    worker_pid: int | None = None
    try:
        first = subprocess.Popen(
            [str(script), *first_args],
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        processes.append(first)
        deadline = time.monotonic() + 5
        while not spawn_log.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert spawn_log.exists()

        second = subprocess.Popen(
            [str(script), *second_args],
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        processes.append(second)
        first_stdout, first_stderr = first.communicate(timeout=15)
        second_stdout, second_stderr = second.communicate(timeout=15)
        assert first.returncode == 0, first_stderr
        assert second.returncode == 0, second_stderr

        spawn_lines = spawn_log.read_text(encoding="utf-8").splitlines()
        assert [line.split()[1] for line in spawn_lines] == spawn_roles
        spawned_pid, role = spawn_lines[-1].split()
        worker_pid = int(spawned_pid)
        records = worker_process.collect_worker_records(state / "hark" / "mode-a.pids")
        assert [(record.pid, record.role) for record in records] == [
            (worker_pid, role)
        ]
        assert f"starting {spawn_roles[0]}" in first_stdout
        assert second_message in second_stdout
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
        if worker_pid is not None:
            try:
                os.kill(worker_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

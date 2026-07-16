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
    monkeypatch.setattr(worker_process, "_proc_environ", lambda _pid: dict(os.environ))

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

    assert (
        worker_process.main(["publish", str(path), "--direct", record.to_json()]) == 0
    )

    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(record.to_json())


def test_capture_role_poll_rejects_pid_lifetime_change(
    monkeypatch: pytest.MonkeyPatch,
):
    record = worker_process.WorkerRecord(pid=4321, start_time="original", role="watch")
    lifetimes = iter([True, False])
    monkeypatch.setattr(
        worker_process,
        "record_matches_lifetime",
        lambda _record: next(lifetimes),
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: False)
    monkeypatch.setattr(worker_process.time, "sleep", lambda _seconds: None)

    assert not worker_process.wait_for_worker_role(record, timeout_s=1.0)


def test_capture_rejects_pid_that_is_not_the_launchers_child(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(worker_process, "_proc_stat", lambda _pid: ("S", "start"))
    monkeypatch.setattr(worker_process, "_proc_ppid", lambda _pid: 999)

    assert (
        worker_process.capture_worker_identity(
            4321, role="watch", expected_parent_pid=111
        )
        is None
    )


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


def test_inspect_rejects_unrelated_python_script_named_hark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    pidfile = tmp_path / "mode-a.pids"
    monkeypatch.setattr(worker_process, "_proc_stat", lambda _pid: ("S", "123"))
    monkeypatch.setattr(
        worker_process,
        "_proc_argv",
        lambda _pid: ["python3", str(tmp_path / "hark"), "ambient"],
    )
    monkeypatch.setattr(worker_process, "_proc_environ", lambda _pid: {})

    assert (
        worker_process.worker_role_from_argv(
            ["python3", str(tmp_path / "hark"), "ambient"]
        )
        == "ambient"
    )
    assert (
        worker_process.inspect_worker(
            4321, expected_role="ambient", expected_pidfile=pidfile
        )
        is None
    )


def test_discovery_is_scoped_and_deduplicates_only_wrapper_ancestry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    pidfile = tmp_path / "one" / "mode-a.pids"
    other_pidfile = tmp_path / "two" / "mode-a.pids"
    config = tmp_path / "config" / "hark" / "config.toml"
    candidates = {
        101: worker_process.WorkerRecord(
            101, "a", "watch", pidfile=str(pidfile), config=str(config)
        ),
        102: worker_process.WorkerRecord(
            102, "b", "watch", pidfile=str(pidfile), config=str(config)
        ),
        201: worker_process.WorkerRecord(
            201,
            "c",
            "ambient",
            pidfile=str(other_pidfile),
            config=str(config),
        ),
    }

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    monkeypatch.setattr(
        worker_process.Path,
        "iterdir",
        lambda _path: [Entry(str(pid)) for pid in candidates],
    )
    monkeypatch.setattr(
        worker_process,
        "inspect_worker",
        lambda pid, *, expected_pidfile=None, **_kwargs: (
            candidates[pid]
            if candidates[pid].pidfile == str(expected_pidfile)
            else None
        ),
    )
    monkeypatch.setattr(
        worker_process,
        "_proc_ppid",
        lambda pid: {102: 101, 101: 1}.get(pid),
    )

    assert worker_process._discover_workers(pidfile, config) == [candidates[102]]


def test_live_provisional_record_is_retained_but_never_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "mode-a.pids"
    provisional = worker_process.WorkerRecord(
        4321,
        "captured",
        "watch",
        pidfile=str(path.resolve()),
        config=str(worker_process._config_path_from_environ(dict(os.environ))),
        provisional=True,
    )
    path.write_text(provisional.to_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(worker_process, "record_matches_lifetime", lambda _r: True)
    monkeypatch.setattr(
        worker_process,
        "record_matches_process",
        lambda _r: (_ for _ in ()).throw(
            AssertionError("provisional record was treated as healthy")
        ),
    )

    assert worker_process.collect_worker_records(path) == [provisional]
    assert not worker_process.worker_records_match_request(
        [provisional], watch=True, ambient=False, session="default"
    )


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
        pidfile = directory / "mode-a.pids"
        child = subprocess.Popen(
            [str(launcher), role],
            start_new_session=True,
            env={
                **os.environ,
                worker_process.WORKER_PIDFILE_ENV: str(pidfile.resolve()),
                worker_process.WORKER_ROLE_ENV: role,
            },
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if (
                worker_process.inspect_worker(
                    child.pid,
                    expected_role=role,
                    expected_pidfile=pidfile,
                )
                is not None
            ):
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
            "config": records[0].config,
            "pid": child.pid,
            "pidfile": str(path.resolve()),
            "provisional": False,
            "role": "ambient",
            "start_time": records[0].start_time,
            "version": 1,
        }
    finally:
        kill_child(child)


def test_pre_scope_structured_worker_is_migrated_only_from_live_provenance(
    tmp_path: Path,
):
    child = spawn_process(tmp_path, role="watch")
    path = tmp_path / "mode-a.pids"
    try:
        live = worker_process.inspect_worker(
            child.pid, expected_role="watch", expected_pidfile=path
        )
        assert live is not None
        old = worker_process.WorkerRecord(
            pid=live.pid, start_time=live.start_time, role=live.role
        )
        path.write_text(old.to_json() + "\n", encoding="utf-8")

        assert worker_process.collect_worker_records(path) == [live]
        assert json.loads(path.read_text(encoding="utf-8"))["pidfile"] == str(
            path.resolve()
        )
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
    scope = str(path.resolve())
    config = str(worker_process._config_path_from_environ(dict(os.environ)))
    other = worker_process.WorkerRecord(
        pid=1111,
        start_time="other-owner",
        role="ambient",
        pidfile=scope,
        config=config,
    )
    old_owned = worker_process.WorkerRecord(
        pid=2222,
        start_time="old-owned",
        role="watch",
        pidfile=scope,
        config=config,
    )
    refreshed_owned = worker_process.WorkerRecord(
        pid=2222,
        start_time="refreshed-owned",
        role="watch",
        pidfile=scope,
        config=config,
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
    monkeypatch.setattr(
        worker_process.os, "pidfd_open", lambda _pid: read_fd, raising=False
    )
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda fd, sig: sent.append((fd, sig)),
        raising=False,
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
    monkeypatch.setattr(
        worker_process.os, "pidfd_open", lambda _pid: read_fd, raising=False
    )
    monkeypatch.setattr(
        worker_process, "record_matches_lifetime", lambda _record: False
    )
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda fd, sig: sent.append((fd, sig)),
        raising=False,
    )
    try:
        assert (
            worker_process.main(
                ["signal-records", "TERM", "--lifetime-only", record.to_json()]
            )
            == 0
        )
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
            raising=False,
        )
        monkeypatch.setattr(
            worker_process.signal,
            "pidfd_send_signal",
            lambda _fd, _sig: None,
            raising=False,
        )
    elif failure_stage == "pidfd_send_signal":
        read_fd, write_fd = os.pipe()
        monkeypatch.setattr(
            worker_process.os, "pidfd_open", lambda _pid: read_fd, raising=False
        )
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
            raising=False,
        )
    else:
        monkeypatch.setattr(
            worker_process.os,
            "pidfd_open",
            lambda _pid: (_ for _ in ()).throw(PermissionError("denied")),
            raising=False,
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


def test_real_uv_shell_start_records_one_scoped_logical_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The real uv controller must not become a second/global worker identity."""
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    real_uv = shutil.which("uv")
    assert real_uv is not None
    state = tmp_path / "state"
    other_state = tmp_path / "other-state"
    config = tmp_path / "config"
    fake_bin = tmp_path / "workers"
    fake_bin.mkdir()
    fake_hark = fake_bin / "hark"
    fake_hark.write_text(
        "#!/usr/bin/env python3\n"
        "import signal\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: exit(0))\n"
        "while True:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    fake_hark.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "XDG_STATE_HOME": str(state),
            "XDG_CONFIG_HOME": str(config),
            "HARK_TEST_WORKER_EXECUTABLE": str(fake_hark),
            "PATH": f"{Path(real_uv).parent}{os.pathsep}{env['PATH']}",
        }
    )
    pidfile = state / "hark" / "mode-a.pids"
    records: list[worker_process.WorkerRecord] = []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    try:
        result = subprocess.run(
            [str(script), "--no-ambient", "--session", "uv-regression"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        records = worker_process.collect_worker_records(pidfile, discover=True)
        assert [(record.role, record.provisional) for record in records] == [
            ("watch", False)
        ]
        assert records[0].pidfile == str(pidfile.resolve())
        assert (
            worker_process.collect_worker_records(
                other_state / "hark" / "mode-a.pids", discover=True
            )
            == []
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other-config"))
        assert (
            worker_process.collect_worker_records(pidfile, discover=True, rewrite=False)
            == []
        )
    finally:
        if not records and pidfile.exists():
            records = worker_process.read_worker_records(pidfile)
        for record in records:
            worker_process.signal_worker(record, signal.SIGTERM)

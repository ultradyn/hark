"""harkd scaffold: pidfile single-instance, status, refuse if handsfree workers running."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

import hark.daemon as daemon
import hark.worker_process as worker_process
from hark.exitcodes import ERROR, OK
from hark.worker_process import (
    WORKER_PIDFILE_ENV,
    WORKER_ROLE_ENV,
    WorkerRecord,
    inspect_worker,
)


def spawn_hark_worker(role: str, directory: Path) -> subprocess.Popen[bytes]:
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
    child = subprocess.Popen(
        [str(launcher), role],
        start_new_session=True,
        env={
            **os.environ,
            WORKER_PIDFILE_ENV: str((directory / "mode-a.pids").resolve()),
            WORKER_ROLE_ENV: role,
        },
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if (
            inspect_worker(
                child.pid,
                expected_role=role,
                expected_pidfile=directory / "mode-a.pids",
            )
            is not None
        ):
            return child
        time.sleep(0.01)
    kill_child(child)
    raise AssertionError(f"worker argv did not become ready for role {role}")


def kill_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            child.kill()
    child.wait(timeout=2)


class FakeWorker:
    registry: dict[int, "FakeWorker"] = {}

    def __init__(self, pid: int, *, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.wait_calls = 0
        self.terminate_error: OSError | None = None
        self.kill_error: OSError | None = None
        FakeWorker.registry[pid] = self

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        return self.returncode

    def terminate(self) -> None:
        if self.terminate_error is not None:
            raise self.terminate_error
        self.returncode = -signal.SIGTERM

    def kill(self) -> None:
        if self.kill_error is not None:
            raise self.kill_error
        self.returncode = -signal.SIGKILL


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(daemon, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        daemon,
        "capture_worker_identity",
        lambda pid, *, role, **_kwargs: WorkerRecord(
            pid=pid,
            start_time=f"start-{pid}",
            role=role,
            pidfile=str((tmp_path / "mode-a.pids").resolve()),
            provisional=True,
        ),
    )
    monkeypatch.setattr(daemon, "wait_for_worker_role", lambda *_a, **_k: True)
    handles: dict[int, int] = {}

    def open_handle(pid: int) -> int:
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        handles[read_fd] = pid
        return read_fd

    monkeypatch.setattr(daemon, "open_process_handle", open_handle)

    def signal_handle(pidfd: int, sig: int) -> None:
        worker = FakeWorker.registry[handles[pidfd]]
        if sig == signal.SIGTERM and worker.terminate_error is not None:
            raise worker.terminate_error
        if sig == signal.SIGKILL and worker.kill_error is not None:
            raise worker.kill_error
        worker.returncode = -sig

    monkeypatch.setattr(daemon, "signal_process_handle", signal_handle)
    monkeypatch.setattr(daemon, "record_matches_lifetime", lambda _record: True)
    monkeypatch.setattr(daemon, "record_matches_process", lambda _record: True)
    return tmp_path


def test_spawn_workers_reports_first_role_failure_and_closes_log(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    streams = []

    def fail_first(*_args, **kwargs):
        streams.append(kwargs["stdout"])
        raise OSError("fork refused")

    monkeypatch.setattr(daemon.subprocess, "Popen", fail_first)

    with pytest.raises(
        daemon.WorkerSpawnError, match="watch startup failed.*fork refused"
    ):
        daemon.spawn_mode_a_workers(root=state)

    assert len(streams) == 1
    assert streams[0].closed
    assert not (state / "mode-a.pids").exists()


def test_spawn_workers_rolls_back_when_identity_capture_fails(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    watch = FakeWorker(101)

    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_args, **_kwargs: watch)
    monkeypatch.setattr(daemon, "capture_worker_identity", lambda _pid, **_kwargs: None)

    with pytest.raises(
        daemon.WorkerSpawnError,
        match="watch startup failed.*could not capture watch worker process identity",
    ):
        daemon.spawn_mode_a_workers(root=state, do_ambient=False)

    assert watch.returncode == -signal.SIGTERM
    assert watch.wait_calls == 1
    assert not (state / "mode-a.pids").exists()


def test_spawn_publishes_provisional_identity_before_role_wait(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    watch = FakeWorker(101)
    observed: list[WorkerRecord] = []
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_args, **_kwargs: watch)

    def observe_wait(_record: WorkerRecord, **_kwargs) -> bool:
        observed.extend(daemon.read_worker_records(state / "mode-a.pids"))
        return True

    monkeypatch.setattr(daemon, "wait_for_worker_role", observe_wait)

    daemon.spawn_mode_a_workers(root=state, do_ambient=False)

    assert [(record.pid, record.role, record.provisional) for record in observed] == [
        (101, "watch", True)
    ]


def test_pidfd_open_failure_durably_retains_unreaped_provisional_owner(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    watch = FakeWorker(101)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_args, **_kwargs: watch)
    monkeypatch.setattr(
        daemon,
        "open_process_handle",
        lambda _pid: (_ for _ in ()).throw(PermissionError("pidfd denied")),
    )

    with pytest.raises(daemon.WorkerSpawnError) as caught:
        daemon.spawn_mode_a_workers(root=state, do_ambient=False)

    assert "pidfd denied" in str(caught.value)
    assert "surviving workers still running: watch=101" in str(caught.value)
    retained = daemon.read_worker_records(state / "mode-a.pids")
    assert [(record.pid, record.role, record.provisional) for record in retained] == [
        (101, "watch", True)
    ]


def test_spawn_workers_rolls_back_first_child_when_second_role_fails(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    watch = FakeWorker(101)
    streams = []
    calls = 0

    def fail_second(*_args, **kwargs):
        nonlocal calls
        streams.append(kwargs["stdout"])
        calls += 1
        if calls == 1:
            return watch
        raise OSError("ambient fork refused")

    def killpg(pid: int, sig: int) -> None:
        assert (pid, sig) == (watch.pid, signal.SIGTERM)
        watch.returncode = -sig

    monkeypatch.setattr(daemon.subprocess, "Popen", fail_second)
    monkeypatch.setattr(daemon.os, "killpg", killpg)

    with pytest.raises(
        daemon.WorkerSpawnError, match="ambient startup failed.*ambient fork refused"
    ):
        daemon.spawn_mode_a_workers(root=state)

    assert watch.returncode == -signal.SIGTERM
    assert watch.wait_calls == 1
    assert len(streams) == 2
    assert all(stream.closed for stream in streams)
    assert not (state / "mode-a.pids").exists()


@pytest.mark.parametrize(
    "interrupt",
    [KeyboardInterrupt(), SystemExit(17)],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_spawn_workers_rolls_back_then_preserves_base_exception(
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: BaseException,
):
    watch = FakeWorker(101)
    calls = 0

    def interrupt_second(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return watch
        raise interrupt

    def killpg(pid: int, sig: int) -> None:
        assert (pid, sig) == (watch.pid, signal.SIGTERM)
        watch.returncode = -sig

    monkeypatch.setattr(daemon.subprocess, "Popen", interrupt_second)
    monkeypatch.setattr(daemon.os, "killpg", killpg)

    with pytest.raises(type(interrupt)) as caught:
        daemon.spawn_mode_a_workers(root=state)

    assert caught.value is interrupt
    assert watch.returncode == -signal.SIGTERM
    assert watch.wait_calls == 1
    assert not (state / "mode-a.pids").exists()


def test_spawn_workers_reports_rollback_signal_failure(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    watch = FakeWorker(101)
    watch.terminate_error = OSError("terminate refused")
    calls = 0

    def fail_second(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return watch
        raise OSError("ambient fork refused")

    monkeypatch.setattr(daemon.subprocess, "Popen", fail_second)

    with pytest.raises(daemon.WorkerSpawnError) as caught:
        daemon.spawn_mode_a_workers(root=state)

    message = str(caught.value)
    assert "ambient startup failed" in message
    assert "rollback failures" in message
    assert "watch pidfd signal 15 failed" in message
    assert "terminate refused" in message
    assert watch.returncode == -signal.SIGKILL
    assert watch.wait_calls == 2


def test_spawn_workers_records_and_reports_child_that_survives_rollback(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    pidfile = state / "mode-a.pids"
    pidfile.write_bytes(b"777\n")
    watch = FakeWorker(101)
    watch.terminate_error = OSError("terminate refused")
    watch.kill_error = OSError("kill refused")
    calls = 0

    def fail_second(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return watch
        raise OSError("ambient fork refused")

    def deny_group_signal(_pid: int, sig: int) -> None:
        raise PermissionError(f"signal {sig} refused")

    monkeypatch.setattr(daemon.subprocess, "Popen", fail_second)
    monkeypatch.setattr(daemon.os, "killpg", deny_group_signal)

    with pytest.raises(daemon.WorkerSpawnError) as caught:
        daemon.spawn_mode_a_workers(root=state)

    message = str(caught.value)
    assert "rollback failures" in message
    assert "surviving workers still running: watch=101" in message
    assert watch.poll() is None
    assert watch.wait_calls == 2
    assert daemon.read_pids_file(pidfile) == [777]
    retained = daemon.read_worker_records(pidfile)
    assert [(record.pid, record.role) for record in retained] == [(101, "watch")]
    assert retained[0].provisional is True
    assert retained[0].pidfile == str(pidfile.resolve())


def test_rollback_recaptures_survivor_when_initial_identity_capture_failed(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    pidfile = state / "mode-a.pids"
    watch = FakeWorker(101)
    watch.terminate_error = OSError("terminate refused")
    watch.kill_error = OSError("kill refused")
    calls = 0

    def capture(pid: int, *, role: str, **_kwargs) -> WorkerRecord | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return WorkerRecord(
            pid=pid,
            start_time=f"start-{pid}",
            role=role,
            pidfile=str(pidfile.resolve()),
            config=str(worker_process._config_path_from_environ(dict(os.environ))),
            provisional=True,
        )

    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_args, **_kwargs: watch)
    monkeypatch.setattr(daemon, "capture_worker_identity", capture)

    with pytest.raises(daemon.WorkerSpawnError) as caught:
        daemon.spawn_mode_a_workers(root=state, do_ambient=False)

    assert "surviving workers still running: watch=101" in str(caught.value)
    retained = daemon.read_worker_records(pidfile)
    assert [(record.pid, record.role, record.provisional) for record in retained] == [
        (101, "watch", True)
    ]


def test_rollback_retains_pidfd_when_survivor_identity_cannot_be_recaptured(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    watch = FakeWorker(101)
    watch.terminate_error = OSError("terminate refused")
    watch.kill_error = OSError("kill refused")
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *_args, **_kwargs: watch)
    monkeypatch.setattr(
        daemon, "capture_worker_identity", lambda *_args, **_kwargs: None
    )

    retained_fd: int | None = None
    try:
        with pytest.raises(daemon.WorkerSpawnError) as caught:
            daemon.spawn_mode_a_workers(root=state, do_ambient=False)

        retained_fd = daemon._RETAINED_ROLLBACK_PIDFDS.pop(watch.pid)
        os.fstat(retained_fd)
        assert f"retained pidfd {retained_fd} for untracked surviving watch" in str(
            caught.value
        )
        assert daemon.read_worker_records(state / "mode-a.pids") == []
    finally:
        if retained_fd is not None:
            os.close(retained_fd)


@pytest.mark.parametrize("direct_fails", [False, True])
def test_rollback_falls_back_or_retains_pidfd_when_survivor_publish_fails(
    state: Path, monkeypatch: pytest.MonkeyPatch, direct_fails: bool
):
    pidfile = state / "mode-a.pids"
    watch = FakeWorker(101)
    watch.terminate_error = OSError("terminate refused")
    watch.kill_error = OSError("kill refused")
    calls = 0

    def fail_second(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return watch
        raise OSError("ambient fork refused")

    real_direct = daemon.write_worker_pidfile_bytes_direct
    monkeypatch.setattr(daemon.subprocess, "Popen", fail_second)
    monkeypatch.setattr(
        daemon,
        "write_worker_pidfile_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rename denied")),
    )
    if direct_fails:
        monkeypatch.setattr(
            daemon,
            "write_worker_pidfile_bytes_direct",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("direct denied")),
        )
    else:
        monkeypatch.setattr(daemon, "write_worker_pidfile_bytes_direct", real_direct)

    retained_fd: int | None = None
    try:
        with pytest.raises(daemon.WorkerSpawnError) as caught:
            daemon.spawn_mode_a_workers(root=state)

        if direct_fails:
            retained_fd = daemon._RETAINED_ROLLBACK_PIDFDS.pop(watch.pid)
            os.fstat(retained_fd)
            assert f"retained pidfd {retained_fd}" in str(caught.value)
        else:
            assert "used direct fallback" in str(caught.value)
            retained = daemon.read_worker_records(pidfile)
            assert [(record.pid, record.provisional) for record in retained] == [
                (101, True)
            ]
    finally:
        if retained_fd is not None:
            os.close(retained_fd)


def test_spawn_workers_rolls_back_and_reports_early_child_exit(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    watch = FakeWorker(101, returncode=7)
    calls = 0

    def exited_first(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return watch

    monkeypatch.setattr(daemon.subprocess, "Popen", exited_first)

    with pytest.raises(
        daemon.WorkerSpawnError, match="watch startup failed.*exited immediately.*7"
    ):
        daemon.spawn_mode_a_workers(root=state)

    assert calls == 1
    assert watch.wait_calls == 1
    assert not (state / "mode-a.pids").exists()


def test_spawn_workers_restores_pidfile_when_write_fails(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    pidfile = state / "mode-a.pids"
    pidfile.write_bytes(b"777\n")
    all_workers = [FakeWorker(101), FakeWorker(102)]
    workers = list(all_workers)

    def spawn(*_args, **_kwargs):
        return workers.pop(0)

    def fail_write(path: Path, _records) -> None:
        path.write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    monkeypatch.setattr(daemon, "write_worker_records", fail_write)

    with pytest.raises(
        daemon.WorkerSpawnError, match="pidfile startup failed.*disk full"
    ):
        daemon.spawn_mode_a_workers(root=state)

    assert pidfile.read_bytes() == b"777\n"
    assert all_workers[0].returncode == -signal.SIGTERM
    assert all_workers[0].wait_calls == 1
    assert all_workers[1].returncode is None
    assert all_workers[1].wait_calls == 0


def test_spawn_workers_successfully_starts_both_roles_and_closes_logs(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_workers = [FakeWorker(101), FakeWorker(102)]
    spawned: list[list[str]] = []
    streams = []

    def spawn(argv, **kwargs):
        spawned.append(argv)
        streams.append(kwargs["stdout"])
        return fake_workers[len(spawned) - 1]

    monkeypatch.setattr(daemon.subprocess, "Popen", spawn)
    children = daemon.spawn_mode_a_workers(root=state, session="lab")

    assert children == fake_workers
    assert spawned[0][-6:] == [
        "watch",
        "--session",
        "lab",
        "--for-monitor",
        "--statuses",
        "blocked,done",
    ]
    assert spawned[1][-1] == "ambient"
    records = daemon.read_worker_records(state / "mode-a.pids")
    assert [(record.pid, record.role) for record in records] == [
        (101, "watch"),
        (102, "ambient"),
    ]
    assert all(stream.closed for stream in streams)


def test_spawn_workers_rechecks_existing_ownership_inside_transaction(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    child = spawn_hark_worker("watch", state)
    pidfile = state / "mode-a.pids"
    pidfile.write_text(f"{child.pid}\n", encoding="utf-8")
    monkeypatch.setattr(
        daemon.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("spawned despite existing ownership")
        ),
    )
    try:
        with pytest.raises(
            daemon.WorkerSpawnError,
            match=rf"pidfile startup failed.*already running.*{child.pid}",
        ):
            daemon.spawn_mode_a_workers(root=state, do_ambient=False)
        assert child.poll() is None
        assert pidfile.read_text(encoding="utf-8") == f"{child.pid}\n"
    finally:
        kill_child(child)


def test_run_foreground_reports_transactional_worker_failure(
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    error = daemon.WorkerSpawnError(
        "ambient",
        OSError("fork refused"),
        ["watch SIGTERM failed (denied); terminate failed (denied)"],
    )

    def fail_spawn(**_kwargs):
        raise error

    monkeypatch.setattr(daemon, "spawn_mode_a_workers", fail_spawn)

    assert daemon.run_foreground(root=state, workers=True) == ERROR
    stderr = capsys.readouterr().err
    assert "ambient startup failed" in stderr
    assert "rollback failures" in stderr
    assert not (state / "harkd.pid").exists()


@pytest.mark.parametrize("initial_state", ["missing", "corrupt"])
def test_run_foreground_rebuilds_worker_identity_state(
    state: Path, monkeypatch: pytest.MonkeyPatch, initial_state: str
):
    watch = FakeWorker(101)
    ambient = FakeWorker(102)
    pidfile = state / "mode-a.pids"
    observed: list[list[tuple[int, str]]] = []

    def fake_spawn(**_kwargs):
        if initial_state == "corrupt":
            pidfile.write_text("corrupt\n", encoding="utf-8")
        else:
            pidfile.unlink(missing_ok=True)
        return [watch, ambient]

    def finish_after_refresh(_seconds: float) -> None:
        observed.append(
            [
                (record.pid, record.role)
                for record in daemon.read_worker_records(pidfile)
            ]
        )
        watch.returncode = 0
        ambient.returncode = 0

    monkeypatch.setattr(daemon, "spawn_mode_a_workers", fake_spawn)
    monkeypatch.setattr(daemon.time, "sleep", finish_after_refresh)

    assert daemon.run_foreground(root=state, workers=True, idle_sleep_s=0) == OK
    assert observed == [[(101, "watch"), (102, "ambient")]]


def test_run_foreground_refresh_failure_terminates_workers_and_returns_error(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    watch = FakeWorker(101)
    ambient = FakeWorker(102)
    terminated: list[FakeWorker] = []

    monkeypatch.setattr(
        daemon,
        "spawn_mode_a_workers",
        lambda **_kwargs: [watch, ambient],
    )
    monkeypatch.setattr(
        daemon,
        "_refresh_owned_worker_records",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    monkeypatch.setattr(
        daemon,
        "terminate_children",
        lambda children, **_kwargs: terminated.extend(children),
    )

    assert daemon.run_foreground(root=state, workers=True, idle_sleep_s=0) == ERROR
    assert terminated == [watch, ambient]


def test_pid_alive_self():
    assert daemon.pid_alive(os.getpid()) is True
    assert daemon.pid_alive(0) is False
    # Linux: PID 1 usually exists; if not, still must not raise
    daemon.pid_alive(1)


def test_read_and_write_pids_file(state: Path):
    path = state / "mode-a.pids"
    daemon.write_pid_file(path, [os.getpid(), 999999999])
    live = daemon.live_pids_from_file(path)
    assert live == [os.getpid()]
    daemon.clear_pid_file(path)
    assert not path.exists()


def test_spawn_mode_a_workers_persists_role_and_identity(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    next_pid = iter([41001, 41002])

    class FakeProcess:
        def __init__(self, *_args, **_kwargs):
            self.pid = next(next_pid)
            self.returncode = None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(daemon.subprocess, "Popen", FakeProcess)
    children = daemon.spawn_mode_a_workers(root=state, log_dir=state)
    assert [child.pid for child in children] == [41001, 41002]
    stored = [
        json.loads(line)
        for line in (state / "mode-a.pids").read_text(encoding="utf-8").splitlines()
    ]
    assert [(record["pid"], record["role"]) for record in stored] == [
        (41001, "watch"),
        (41002, "ambient"),
    ]
    assert all(record["start_time"].startswith("start-") for record in stored)


def test_assert_can_start_clean(state: Path):
    daemon.assert_can_start(state)


def test_assert_can_start_refuses_live_harkd(state: Path):
    (state / "harkd.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    with pytest.raises(daemon.DaemonConflict, match="harkd already running"):
        daemon.assert_can_start(state, self_pid=os.getpid() + 1)


def test_assert_can_start_allows_own_pid(state: Path):
    (state / "harkd.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    daemon.assert_can_start(state, self_pid=os.getpid())


def test_assert_can_start_refuses_mode_a(state: Path):
    child = spawn_hark_worker("watch", state)
    try:
        (state / "mode-a.pids").write_text(f"{child.pid}\n", encoding="utf-8")
        with pytest.raises(daemon.DaemonConflict, match="Hark workers"):
            daemon.assert_can_start(state)
    finally:
        kill_child(child)


def test_assert_can_start_ignores_stale_mode_a_pid(state: Path):
    (state / "mode-a.pids").write_text("999999999\n", encoding="utf-8")
    daemon.assert_can_start(state)


def test_acquire_and_release_pidfile(state: Path):
    path = daemon.acquire_harkd_pidfile(state, pid=os.getpid())
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
    daemon.release_harkd_pidfile(state, pid=os.getpid())
    assert not path.exists()


def test_acquire_refuses_second_instance(state: Path):
    daemon.acquire_harkd_pidfile(state, pid=os.getpid())
    with pytest.raises(daemon.DaemonConflict):
        daemon.acquire_harkd_pidfile(state, pid=os.getpid() + 999)


def test_collect_status(state: Path):
    (state / "busy.lock").write_text("pid=1\n", encoding="utf-8")
    (state / "mic.lock").write_text("x", encoding="utf-8")
    (state / "harkd.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (state / "mode-a.pids").write_text("999999999\n", encoding="utf-8")
    status = daemon.collect_status(state)
    d = status.to_dict()
    assert d["harkd"]["running"] is True
    assert os.getpid() in d["harkd"]["pids"]
    assert d["mode_a"]["running"] is False  # stale pid ignored
    assert d["busy_lock"] is True
    assert d["mic_lock"] is True
    assert d["state_dir"] == str(state)


def test_status_cli_json(state: Path, capsys: pytest.CaptureFixture[str]):
    ns = daemon.build_parser().parse_args(["status", "--json"])
    code = daemon.dispatch_daemon(ns)
    assert code == OK
    out = capsys.readouterr().out
    assert '"harkd"' in out
    assert str(state) in out or "state_dir" in out


def test_stop_when_not_running(state: Path):
    result = daemon.stop_harkd(state)
    assert result["ok"] is True
    assert result["stopped"] == []


def test_stop_signals_live_process(state: Path):
    """Spawn a child that holds a pidfile entry and wait for SIGTERM."""
    import subprocess
    import sys

    # Default SIGTERM disposition terminates the process (no custom handler).
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    try:
        # Ensure child is running before we signal
        deadline = time.time() + 2.0
        while child.poll() is not None and time.time() < deadline:
            time.sleep(0.01)
        assert child.poll() is None
        (state / "harkd.pid").write_text(f"{child.pid}\n", encoding="utf-8")
        result = daemon.stop_harkd(state, timeout_s=5.0)
        assert result["ok"] is True, result
        assert child.pid in result["stopped"]
        child.wait(timeout=5)
        assert not (state / "harkd.pid").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_run_foreground_idle_and_sigterm(state: Path):
    """Start supervisor in a child; stop via pidfile SIGTERM."""
    import subprocess
    import sys

    repo_src = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    # Point state via env; inject package path for system python.
    xdg = state.parent / "xdg"
    xdg.mkdir(exist_ok=True)
    (xdg / "hark").mkdir(exist_ok=True)
    env["XDG_STATE_HOME"] = str(xdg)
    env["PYTHONPATH"] = str(repo_src) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    hark_cmd = [sys.executable, "-m", "hark", "daemon"]
    child = subprocess.Popen(
        [*hark_cmd, "start"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pidfile = xdg / "hark" / "harkd.pid"
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline and not pidfile.is_file():
            if child.poll() is not None:
                out, err = child.communicate(timeout=1)
                pytest.fail(f"daemon exited early: {child.returncode}\n{out}\n{err}")
            time.sleep(0.05)
        assert pidfile.is_file(), "harkd.pid was not written"
        recorded = int(pidfile.read_text(encoding="utf-8").strip())
        assert recorded == child.pid

        # Second start must refuse
        refuse = subprocess.run(
            [*hark_cmd, "start"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert refuse.returncode == ERROR
        assert "already running" in (refuse.stderr + refuse.stdout).lower()

        stop = subprocess.run(
            [*hark_cmd, "stop", "--timeout", "5"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert stop.returncode == OK, (stop.stdout, stop.stderr)
        child.wait(timeout=5)
        assert not pidfile.exists() or not daemon.pid_alive(recorded)
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()


def test_refuse_start_when_mode_a_pids_live(state: Path):
    """Integration-style: mode-a.pids with our pid blocks acquire."""
    child = spawn_hark_worker("ambient", state)
    try:
        (state / "mode-a.pids").write_text(f"{child.pid}\n", encoding="utf-8")
        with pytest.raises(daemon.DaemonConflict, match="Hark workers"):
            daemon.acquire_harkd_pidfile(state, pid=os.getpid())
    finally:
        kill_child(child)


def test_cli_daemon_status_via_main(state: Path, monkeypatch: pytest.MonkeyPatch):
    """hark.cli dispatch reaches daemon status with isolated state_dir."""
    import hark.cli as cli

    # daemon.collect_status uses monkeypatched state_dir from fixture
    code = cli.main(["daemon", "status", "--json"])
    assert code == OK

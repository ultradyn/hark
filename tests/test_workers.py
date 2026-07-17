"""B089 — hark start / stop / restart (handsfree ambient + watch workers)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import hark.workers as workers
import hark.worker_process as worker_process
from hark.exitcodes import OK, USAGE


def spawn_hark_worker(role: str, directory: Path) -> subprocess.Popen[bytes]:
    """Long-lived process with the same decisive argv shape as a Hark worker."""
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
            worker_process.WORKER_PIDFILE_ENV: str(
                (directory / "mode-a.pids").resolve()
            ),
            worker_process.WORKER_ROLE_ENV: role,
        },
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if (
            worker_process.inspect_worker(
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


def spawn_markerless_hark_worker(role: str, directory: Path) -> subprocess.Popen[bytes]:
    """Live Hark-shaped process launched independently without ownership markers."""
    package_root = directory / "markerless"
    package = package_root / "hark"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: exit(0))\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [sys.executable, "-m", "hark", role],
        start_new_session=True,
        env={**os.environ, "PYTHONPATH": str(package_root)},
    )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if (
            worker_process.inspect_worker(
                child.pid,
                expected_role=role,
                expected_pidfile=directory / "mode-a.pids",
                require_markers=False,
            )
            is not None
        ):
            return child
        time.sleep(0.01)
    kill_child(child)
    raise AssertionError(f"markerless worker did not become ready for role {role}")


def write_live_worker_record(child: subprocess.Popen[bytes], state: Path) -> None:
    record = worker_process.inspect_worker(
        child.pid, expected_pidfile=state / "mode-a.pids"
    )
    assert record is not None
    worker_process.write_worker_records(state / "mode-a.pids", [record])


def kill_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            child.kill()
    child.wait(timeout=2)


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(workers, "state_dir", lambda: tmp_path)
    # daemon helpers used by workers also resolve state_dir
    import hark.daemon as daemon

    monkeypatch.setattr(daemon, "state_dir", lambda: tmp_path)
    import hark.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "state_dir", lambda: tmp_path)
    return tmp_path


def test_parse_pids_text():
    text = """
# comment
123
  456  
pid=789
not-a-pid
pid=abc
"""
    assert workers.parse_pids_text(text) == [123, 456, 789]


def test_parse_pids_text_empty():
    assert workers.parse_pids_text("") == []
    assert workers.parse_pids_text("# only\n\n") == []


def test_stop_when_not_running(state: Path):
    result = workers.stop_workers(state)
    assert result["ok"] is True
    assert result["stopped"] == []
    assert "no Hark workers" in result["message"]
    assert not (state / "mode-a.pids").exists()


def test_stop_clears_stale_pidfile(state: Path):
    (state / "mode-a.pids").write_text("999999999\n", encoding="utf-8")
    result = workers.stop_workers(state)
    assert result["ok"] is True
    assert result["stopped"] == []
    assert not (state / "mode-a.pids").exists()


def test_start_when_already_running(state: Path):
    child = spawn_hark_worker("ambient", state)
    try:
        write_live_worker_record(child, state)
        result = workers.start_workers(state, do_watch=False, settle_s=0)
        assert result["ok"] is True
        assert result["already_running"] is True
        assert child.pid in result["pids"]
        assert "already running" in result["message"]
    finally:
        kill_child(child)


def test_start_recovers_compatible_marker_scoped_orphan_without_duplicate(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    child = spawn_hark_worker("ambient", state)
    spawned: list[dict[str, object]] = []
    monkeypatch.setattr(
        workers,
        "spawn_mode_a_workers",
        lambda **kwargs: spawned.append(kwargs) or [],
    )
    try:
        assert not (state / "mode-a.pids").exists()

        result = workers.start_workers(state, do_watch=False, settle_s=0)

        assert result["ok"] is True
        assert result["already_running"] is True
        assert result["pids"] == [child.pid]
        assert spawned == []
        assert child.poll() is None
    finally:
        kill_child(child)


def test_start_refuses_incompatible_marker_scoped_orphan_without_duplicate(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    child = spawn_hark_worker("ambient", state)
    spawned: list[dict[str, object]] = []
    monkeypatch.setattr(
        workers,
        "spawn_mode_a_workers",
        lambda **kwargs: spawned.append(kwargs) or [],
    )
    try:
        assert not (state / "mode-a.pids").exists()

        result = workers.start_workers(
            state, do_watch=True, do_ambient=False, settle_s=0
        )

        assert result["ok"] is False
        assert "existing workers do not exactly match" in result["error"]
        assert result["pids"] == [child.pid]
        assert spawned == []
        assert child.poll() is None
    finally:
        kill_child(child)


def test_concurrent_compatible_start_reclassifies_lock_winner_as_already_running(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    import hark.daemon as daemon

    record = worker_process.WorkerRecord(
        pid=4321,
        start_time="winner",
        role="ambient",
        pidfile=str((state / "mode-a.pids").resolve()),
        config=str(worker_process._config_path_from_environ(dict(os.environ))),
        boot_id=worker_process._current_boot_id(),
    )
    collections = iter([[], [record]])
    monkeypatch.setattr(
        workers, "collect_worker_records", lambda _path, **_kwargs: next(collections)
    )
    monkeypatch.setattr(
        workers,
        "worker_records_match_request",
        lambda records, **_kwargs: records == [record],
    )
    monkeypatch.setattr(
        workers,
        "spawn_mode_a_workers",
        lambda **_kwargs: (_ for _ in ()).throw(
            daemon.WorkerSpawnError(
                "pidfile", daemon.DaemonConflict("winner owns pidfile")
            )
        ),
    )

    result = workers.start_workers(state, do_watch=False, settle_s=0)

    assert result == {
        "ok": True,
        "already_running": True,
        "pids": [4321],
        "message": "workers already running (pids 4321)",
    }


def test_start_refuses_live_harkd(state: Path):
    (state / "harkd.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = workers.start_workers(state, settle_s=0)
    assert result["ok"] is False
    assert "harkd" in (result.get("error") or "").lower()


def test_start_clears_stale_harkd_pid(state: Path, monkeypatch: pytest.MonkeyPatch):
    (state / "harkd.pid").write_text("999999999\n", encoding="utf-8")
    spawned: list[dict] = []

    def fake_spawn(**kwargs):
        spawned.append(kwargs)
        child_pid = os.getpid()

        class P:
            pid = child_pid

            def poll(self):
                return None

        return [P()]

    monkeypatch.setattr(workers, "spawn_mode_a_workers", fake_spawn)
    record = worker_process.WorkerRecord(
        pid=os.getpid(),
        start_time="fake",
        role="ambient",
        pidfile=str((state / "mode-a.pids").resolve()),
    )
    monkeypatch.setattr(
        workers,
        "collect_worker_records",
        lambda _path, **_kwargs: [record] if spawned else [],
    )
    monkeypatch.setattr(workers, "worker_records_match_request", lambda *_a, **_k: True)
    result = workers.start_workers(state, do_watch=False, settle_s=0)
    assert result["ok"] is True
    assert not result.get("already_running")
    assert spawned
    assert not (state / "harkd.pid").exists()


def test_start_reports_transactional_spawn_failure(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    import hark.daemon as daemon

    error = daemon.WorkerSpawnError(
        "ambient",
        OSError("fork refused"),
        ["watch SIGTERM failed (denied); terminate failed (denied)"],
    )

    def fail_spawn(**_kwargs):
        raise error

    monkeypatch.setattr(workers, "spawn_mode_a_workers", fail_spawn)

    result = workers.start_workers(state, settle_s=0)
    assert result["ok"] is False
    assert result["pids"] == []
    assert "ambient startup failed" in result["error"]
    assert "rollback failures" in result["error"]


def test_start_fails_and_cleans_up_partial_post_settle_role_set(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    spawned = [SimpleNamespace(pid=101), SimpleNamespace(pid=102)]
    watch = worker_process.WorkerRecord(
        101,
        "watch-start",
        "watch",
        pidfile=str((state / "mode-a.pids").resolve()),
    )
    calls = 0
    terminated: list[tuple[list[object], dict[str, object]]] = []

    def collect(_path, **_kwargs):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else [watch]

    monkeypatch.setattr(workers, "spawn_mode_a_workers", lambda **_kwargs: spawned)
    monkeypatch.setattr(workers, "collect_worker_records", collect)
    monkeypatch.setattr(
        workers,
        "worker_records_match_request",
        lambda records, **_kwargs: (
            {record.role for record in records} == {"watch", "ambient"}
        ),
    )

    def terminate(children, **kwargs):
        terminated.append((list(children), kwargs))

    monkeypatch.setattr(workers, "terminate_children", terminate)

    result = workers.start_workers(state, settle_s=0)

    assert result["ok"] is False
    assert "exact requested role set" in result["error"]
    assert terminated == [
        (
            [("watch", spawned[0]), ("ambient", spawned[1])],
            {"root": state, "timeout_s": 2.0},
        )
    ]


def test_start_post_settle_rollback_terminates_returned_child_without_pidfile(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    path = state / "mode-a.pids"
    spawned: list[subprocess.Popen[bytes]] = []

    def fake_spawn(**_kwargs):
        child = spawn_hark_worker("ambient", state)
        spawned.append(child)
        write_live_worker_record(child, state)
        path.unlink()
        return [child]

    monkeypatch.setattr(workers, "spawn_mode_a_workers", fake_spawn)
    monkeypatch.setattr(
        workers, "worker_records_match_request", lambda *_args, **_kwargs: False
    )
    try:
        result = workers.start_workers(
            state, do_watch=False, do_ambient=True, settle_s=0
        )

        assert len(spawned) == 1
        child = spawned[0]
        deadline = time.monotonic() + 2.0
        while child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child.poll() is not None, (
            f"child {child.pid} remains alive; result pids={result.get('pids', [])}, "
            f"pidfile exists={path.exists()}"
        )
        assert result["ok"] is False
        assert "exact requested role set" in result["error"]
        assert result["pids"] == []
        assert not path.exists()
    finally:
        for child in spawned:
            kill_child(child)


def test_start_post_settle_cleanup_reports_term_and_kill_signal_failures(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    path = state / "mode-a.pids"
    spawned: list[tuple[subprocess.Popen[bytes], worker_process.WorkerRecord]] = []

    def fake_spawn(**_kwargs):
        child = spawn_hark_worker("watch", state)
        record = worker_process.inspect_worker(
            child.pid, expected_role="watch", expected_pidfile=path
        )
        assert record is not None
        spawned.append((child, record))
        worker_process.write_worker_records(path, [record])
        return [child]

    monkeypatch.setattr(workers, "spawn_mode_a_workers", fake_spawn)
    monkeypatch.setattr(
        workers, "worker_records_match_request", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        worker_process.os,
        "pidfd_open",
        lambda _pid: os.open("/dev/null", os.O_RDONLY),
        raising=False,
    )
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda _fd, _sig: (_ for _ in ()).throw(PermissionError("denied")),
        raising=False,
    )
    try:
        result = workers.start_workers(state, settle_s=0)

        assert len(spawned) == 1
        child, record = spawned[0]
        assert result["ok"] is False
        assert "SIGTERM" in result["error"]
        assert "SIGKILL" in result["error"]
        assert result["pids"] == [child.pid]
        assert child.poll() is None
        assert worker_process.read_worker_records(path) == [record]
    finally:
        for child, _record in spawned:
            kill_child(child)


def test_stop_discovers_and_gracefully_terminates_marker_scoped_orphan(
    state: Path,
):
    child = spawn_hark_worker("ambient", state)
    path = state / "mode-a.pids"
    try:
        assert not path.exists()

        result = workers.stop_workers(state, timeout_s=2.0)

        assert result["ok"] is True, result
        assert result["stopped"] == [child.pid]
        child.wait(timeout=2.0)
        assert not path.exists()
    finally:
        kill_child(child)


def test_stop_reports_verified_term_pidfd_open_failure_and_retains_owner(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    child = spawn_hark_worker("ambient", state)
    path = state / "mode-a.pids"
    try:
        write_live_worker_record(child, state)
        original = path.read_text(encoding="utf-8")
        monkeypatch.setattr(
            worker_process.os,
            "pidfd_open",
            lambda _pid: (_ for _ in ()).throw(PermissionError("denied")),
            raising=False,
        )

        result = workers.stop_workers(state, timeout_s=0.0)

        assert result["ok"] is False
        assert "SIGTERM" in result["error"]
        assert "pidfd_open failed" in result["error"]
        assert result["pids"] == [child.pid]
        assert child.poll() is None
        assert path.read_text(encoding="utf-8") == original
    finally:
        kill_child(child)


def test_stop_reports_verified_kill_pidfd_send_failure_and_retains_owner(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    child = spawn_hark_worker("ambient", state)
    path = state / "mode-a.pids"
    try:
        write_live_worker_record(child, state)
        original = path.read_text(encoding="utf-8")
        monkeypatch.setattr(
            worker_process.os,
            "pidfd_open",
            lambda _pid: os.open("/dev/null", os.O_RDONLY),
            raising=False,
        )

        def fail_kill(_fd: int, sig: int) -> None:
            if sig == signal.SIGKILL:
                raise PermissionError("denied")

        monkeypatch.setattr(
            worker_process.signal, "pidfd_send_signal", fail_kill, raising=False
        )

        result = workers.stop_workers(state, timeout_s=0.0)

        assert result["ok"] is False
        assert "SIGKILL" in result["error"]
        assert "pidfd_send_signal failed" in result["error"]
        assert result["pids"] == [child.pid]
        assert result["killed"] == []
        assert child.poll() is None
        assert path.read_text(encoding="utf-8") == original
    finally:
        kill_child(child)


def test_stop_does_not_signal_unrelated_legacy_pid(state: Path):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        deadline = time.time() + 2.0
        while child.poll() is not None and time.time() < deadline:
            time.sleep(0.01)
        assert child.poll() is None
        (state / "mode-a.pids").write_text(f"{child.pid}\n", encoding="utf-8")
        result = workers.stop_workers(state, timeout_s=0.0)
        assert result["ok"] is True, result
        assert result["stopped"] == []
        assert child.poll() is None
        assert not (state / "mode-a.pids").exists()
    finally:
        kill_child(child)


def test_stop_does_not_signal_unproven_python_script_named_hark(
    state: Path, tmp_path: Path
):
    unrelated = tmp_path / "hark"
    unrelated.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    child = subprocess.Popen(
        [sys.executable, str(unrelated), "ambient"],
        start_new_session=True,
    )
    try:
        (state / "mode-a.pids").write_text(f"{child.pid}\n", encoding="utf-8")

        result = workers.stop_workers(state, timeout_s=0.0)

        assert result["ok"] is True
        assert result["stopped"] == []
        assert child.poll() is None
        assert not (state / "mode-a.pids").exists()
    finally:
        kill_child(child)


def test_stop_does_not_signal_simulated_reused_pid(state: Path):
    child = spawn_hark_worker("ambient", state)
    try:
        actual = worker_process.inspect_worker(child.pid)
        assert actual is not None
        reused = worker_process.WorkerRecord(
            pid=actual.pid,
            start_time="previous-process-lifetime",
            role=actual.role,
        )
        worker_process.write_worker_records(state / "mode-a.pids", [reused])

        result = workers.stop_workers(state, timeout_s=0.0)

        assert result["ok"] is True
        assert result["stopped"] == []
        assert child.poll() is None
        assert not (state / "mode-a.pids").exists()
    finally:
        kill_child(child)


def test_stop_fails_closed_for_bare_pid_of_live_markerless_hark_worker(
    state: Path, monkeypatch: pytest.MonkeyPatch
):
    child = spawn_markerless_hark_worker("ambient", state)
    path = state / "mode-a.pids"
    try:
        path.write_text(f"{child.pid}\n", encoding="utf-8")
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(
            worker_process.signal,
            "pidfd_send_signal",
            lambda fd, sig: sent.append((fd, sig)),
            raising=False,
        )

        result = workers.stop_workers(state, timeout_s=0.0)

        assert result["ok"] is False
        assert "legacy" in result["error"].lower()
        assert result["pids"] == [child.pid]
        assert sent == []
        assert child.poll() is None
        assert path.read_text(encoding="utf-8") == f"{child.pid}\n"

        start_result = workers.start_workers(state, do_watch=False, settle_s=0)
        assert start_result["ok"] is False
        assert start_result["pids"] == [child.pid]
        assert sent == []
        assert child.poll() is None
        assert path.read_text(encoding="utf-8") == f"{child.pid}\n"
    finally:
        kill_child(child)


def test_graceful_stop_preserves_busy_cleanup_and_restart_reason(state: Path):
    child = spawn_hark_worker("ambient", state)
    try:
        (state / "busy.lock").write_text("recording\n", encoding="utf-8")
        write_live_worker_record(child, state)
        result = workers.stop_workers(state, timeout_s=5.0, reason="restart")
        assert result["ok"] is True, result
        child.wait(timeout=5)
        assert not (state / "busy.lock").exists()
        assert (state / "shutdown_reason").read_text(encoding="utf-8").strip() == (
            "restart"
        )
    finally:
        kill_child(child)


def test_restart_stop_then_start(state: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def fake_stop(*_a, **_k):
        calls.append("stop")
        return {
            "ok": True,
            "stopped": [],
            "message": "no Hark workers running",
            "pids": [],
        }

    def fake_start(*_a, **_k):
        calls.append("start")
        return {
            "ok": True,
            "already_running": False,
            "pids": [42],
            "message": "started workers (pids 42)",
        }

    monkeypatch.setattr(workers, "stop_workers", fake_stop)
    monkeypatch.setattr(workers, "start_workers", fake_start)
    result = workers.restart_workers(state)
    assert result["ok"] is True
    assert calls == ["stop", "start"]
    assert result["pids"] == [42]


def test_cmd_start_status(state: Path, capsys: pytest.CaptureFixture[str]):
    ns = type("A", (), {"status": True, "json": False})()
    code = workers.cmd_start(ns)
    assert code == OK
    out = capsys.readouterr().out
    assert "workers: not running" in out


def test_cmd_start_empty_flags(capsys: pytest.CaptureFixture[str]):
    ns = type(
        "A",
        (),
        {
            "status": False,
            "json": False,
            "no_watch": True,
            "no_ambient": True,
            "session": "default",
        },
    )()
    code = workers.cmd_start(ns)
    assert code == USAGE


def test_cli_parser_start_stop_restart():
    import hark.cli as cli

    p = cli.build_parser()
    a = p.parse_args(["start", "--no-ambient", "--session", "lab", "--json"])
    assert a.cmd == "start"
    assert a.no_ambient is True
    assert a.session == "lab"

    b = p.parse_args(["stop", "--force", "--timeout", "1.5"])
    assert b.cmd == "stop"
    assert b.force is True
    assert b.timeout == 1.5

    c = p.parse_args(["restart", "--no-watch"])
    assert c.cmd == "restart"
    assert c.no_watch is True

    d = p.parse_args(["start", "--status"])
    assert d.status is True


def test_cli_stop_not_running(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import hark.cli as cli
    import hark.daemon as daemon

    monkeypatch.setattr(daemon, "state_dir", lambda: state)
    monkeypatch.setattr(workers, "state_dir", lambda: state)
    code = cli.main(["stop", "--json"])
    assert code == OK
    out = capsys.readouterr().out
    assert (
        "no Hark workers" in out or '"ok": true' in out.lower() or '"ok": true' in out
    )


def test_cli_start_already_running(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import hark.cli as cli
    import hark.daemon as daemon

    monkeypatch.setattr(daemon, "state_dir", lambda: state)
    monkeypatch.setattr(workers, "state_dir", lambda: state)
    child = spawn_hark_worker("ambient", state)
    try:
        write_live_worker_record(child, state)
        code = cli.main(["start", "--no-watch", "--json"])
        assert code == OK
        out = capsys.readouterr().out
        assert "already_running" in out or str(child.pid) in out
    finally:
        kill_child(child)

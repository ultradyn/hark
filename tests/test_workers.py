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
        (state / "mode-a.pids").write_text(f"{child.pid}\n", encoding="utf-8")
        result = workers.start_workers(state, do_watch=False, settle_s=0)
        assert result["ok"] is True
        assert result["already_running"] is True
        assert child.pid in result["pids"]
        assert "already running" in result["message"]
    finally:
        kill_child(child)


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
        lambda _path: [record] if spawned else [],
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
    signalled: list[tuple[list[worker_process.WorkerRecord], int]] = []

    def collect(_path):
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
    monkeypatch.setattr(
        workers,
        "signal_worker_records",
        lambda records, sig: signalled.append((list(records), sig)) or list(records),
    )
    monkeypatch.setattr(workers, "_still_same_workers", lambda _records: [])

    result = workers.start_workers(state, settle_s=0)

    assert result["ok"] is False
    assert "exact requested role set" in result["error"]
    assert signalled == [([watch], signal.SIGTERM)]


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


def test_stop_migrates_and_signals_legitimate_legacy_worker(state: Path):
    child = spawn_hark_worker("watch", state)
    try:
        (state / "mode-a.pids").write_text(f"{child.pid}\n", encoding="utf-8")
        result = workers.stop_workers(state, timeout_s=5.0)
        assert result["ok"] is True, result
        assert child.pid in result["stopped"]
        child.wait(timeout=5)
        assert not (state / "mode-a.pids").exists()
    finally:
        kill_child(child)


def test_graceful_stop_preserves_busy_cleanup_and_restart_reason(state: Path):
    child = spawn_hark_worker("ambient", state)
    try:
        (state / "busy.lock").write_text("recording\n", encoding="utf-8")
        (state / "mode-a.pids").write_text(f"{child.pid}\n", encoding="utf-8")
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
        (state / "mode-a.pids").write_text(f"{child.pid}\n", encoding="utf-8")
        code = cli.main(["start", "--no-watch", "--json"])
        assert code == OK
        out = capsys.readouterr().out
        assert "already_running" in out or str(child.pid) in out
    finally:
        kill_child(child)

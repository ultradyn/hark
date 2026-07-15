"""B089 — hark start / stop / restart (handsfree ambient + watch workers)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import hark.workers as workers
from hark.exitcodes import OK, USAGE


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
    (state / "mode-a.pids").write_text(f"{os.getpid()}\n", encoding="utf-8")
    result = workers.start_workers(state, settle_s=0)
    assert result["ok"] is True
    assert result["already_running"] is True
    assert os.getpid() in result["pids"]
    assert "already running" in result["message"]


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
        (state / "mode-a.pids").write_text(f"{child_pid}\n", encoding="utf-8")

        class P:
            pid = child_pid

            def poll(self):
                return None

        return [P()]

    monkeypatch.setattr(workers, "spawn_mode_a_workers", fake_spawn)
    result = workers.start_workers(state, settle_s=0)
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


def test_stop_signals_live_child(state: Path):
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
        result = workers.stop_workers(state, timeout_s=5.0)
        assert result["ok"] is True, result
        assert child.pid in result["stopped"]
        child.wait(timeout=5)
        assert not (state / "mode-a.pids").exists()
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                child.kill()
            child.wait(timeout=2)


def test_restart_stop_then_start(state: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def fake_stop(*_a, **_k):
        calls.append("stop")
        return {"ok": True, "stopped": [], "message": "no Hark workers running", "pids": []}

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
    assert "no Hark workers" in out or '"ok": true' in out.lower() or '"ok": true' in out


def test_cli_start_already_running(
    state: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    import hark.cli as cli
    import hark.daemon as daemon

    monkeypatch.setattr(daemon, "state_dir", lambda: state)
    monkeypatch.setattr(workers, "state_dir", lambda: state)
    (state / "mode-a.pids").write_text(f"{os.getpid()}\n", encoding="utf-8")
    code = cli.main(["start", "--json"])
    assert code == OK
    out = capsys.readouterr().out
    assert "already_running" in out or str(os.getpid()) in out

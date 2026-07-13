"""harkd scaffold: pidfile single-instance, status, refuse if Mode A running."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

import hark.daemon as daemon
from hark.exitcodes import ERROR, OK


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(daemon, "state_dir", lambda: tmp_path)
    return tmp_path


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
    (state / "mode-a.pids").write_text(f"{os.getpid()}\n", encoding="utf-8")
    with pytest.raises(daemon.DaemonConflict, match="Mode A workers"):
        daemon.assert_can_start(state)


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
    (state / "mode-a.pids").write_text(f"{os.getpid()}\n", encoding="utf-8")
    with pytest.raises(daemon.DaemonConflict, match="Mode A"):
        daemon.acquire_harkd_pidfile(state, pid=os.getpid())


def test_cli_daemon_status_via_main(state: Path, monkeypatch: pytest.MonkeyPatch):
    """hark.cli dispatch reaches daemon status with isolated state_dir."""
    import hark.cli as cli

    # daemon.collect_status uses monkeypatched state_dir from fixture
    code = cli.main(["daemon", "status", "--json"])
    assert code == OK

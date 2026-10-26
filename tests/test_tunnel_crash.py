"""B152: exercise the real spawn/exec path with a harmless SSH stand-in."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hark.herdr import tunnel, tunnel_child


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death signal")
_WAIT_S = 15.0


def _wait_for(path: Path, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + _WAIT_S
    while not path.exists():
        if process.poll() is not None:
            raise AssertionError(f"owner exited before {path.name}: {process.returncode}")
        if time.monotonic() >= deadline:
            raise AssertionError(f"owner did not reach {path.name}")
        time.sleep(0.01)


@pytest.fixture
def subreaper():
    """Adopt only the known test child so it can be reaped after owner death."""
    libc = ctypes.CDLL(None, use_errno=True)
    original = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(original), 0, 0, 0) != 0:  # PR_GET_CHILD_SUBREAPER
        pytest.skip("Linux subreaper unavailable")
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        pytest.skip("Linux subreaper unavailable")
    try:
        yield
    finally:
        assert libc.prctl(36, original.value, 0, 0, 0) == 0


def _fake_ssh(tmp_path: Path) -> Path:
    executable = tmp_path / "ssh"
    executable.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import os, socket, sys, time
            from pathlib import Path
            root = Path(os.environ['HARK_TEST_TUNNEL_ROOT'])
            (root / 'child-pid').write_text(str(os.getpid()))
            if os.environ['HARK_TEST_TUNNEL_PHASE'] == 'before_socket':
                (root / 'before-socket').touch()
                while True:
                    time.sleep(0.05)
            local = sys.argv[sys.argv.index('-L') + 1].split(':', 1)[0]
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(local)
            listener.listen(1)
            (root / 'socket-ready').touch()
            while True:
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


@pytest.mark.parametrize("phase", ["before_socket", "before_marker", "published"])
@pytest.mark.parametrize("from_thread", [False, True], ids=["main-thread", "worker-thread"])
def test_owner_sigkill_cannot_strand_ssh_child(
    monkeypatch, tmp_path, subreaper, phase, from_thread
):
    _fake_ssh(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HARK_TEST_TUNNEL_ROOT", str(tmp_path))
    monkeypatch.setenv("HARK_TEST_TUNNEL_PHASE", phase)
    monkeypatch.setenv("HARK_TEST_TUNNEL_THREAD", str(int(from_thread)))
    script = textwrap.dedent(
        """
        import os, threading, time
        from pathlib import Path
        from hark.herdr import tunnel
        root = Path(os.environ['HARK_TEST_TUNNEL_ROOT'])
        original_spawn = tunnel.subprocess.Popen
        def spawn(*args, **kwargs):
            process = original_spawn(*args, **kwargs)
            (root / 'supervisor-pid').write_text(str(process.pid))
            return process
        tunnel.subprocess.Popen = spawn
        original_write = tunnel._write_owner_marker
        def publish(path, marker):
            if os.environ['HARK_TEST_TUNNEL_PHASE'] == 'before_marker':
                (root / 'before-marker').touch()
                while True:
                    time.sleep(0.05)
            original_write(path, marker)
        tunnel._write_owner_marker = publish
        value = tunnel.Tunnel('test', 'unused-host', root / 'forward.sock', '/remote.sock')
        if os.environ['HARK_TEST_TUNNEL_THREAD'] == '1':
            worker = threading.Thread(target=value.start)
            worker.start()
            worker.join()
            if value.proc is None or value.proc.poll() is not None:
                raise RuntimeError('tunnel died with creating thread')
        else:
            value.start()
        (root / 'published').touch()
        while True:
            time.sleep(0.05)
        """
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    child_pid = None
    supervisor_pid = None
    reaped = False
    try:
        checkpoint = {
            "before_socket": "before-socket",
            "before_marker": "before-marker",
            "published": "published",
        }[phase]
        _wait_for(tmp_path / checkpoint, owner)
        child_pid = int((tmp_path / "child-pid").read_text())
        supervisor_pid = int((tmp_path / "supervisor-pid").read_text())
        identity = tunnel.capture_process_identity(child_pid)
        assert identity is not None
        marker_path = tunnel._owner_marker_path(tmp_path / "forward.sock")
        assert marker_path.exists() == (phase == "published")
        if phase == "published":
            assert tunnel._read_owner_marker(marker_path).child.pid == supervisor_pid
            # Joining the creator thread must not kill a process-owned forward.
            # Wait beyond multiple supervisor polls before killing the process.
            if from_thread:
                time.sleep(0.2)
                assert tunnel.capture_process_identity(child_pid) == identity

        owner.kill()
        owner.wait(timeout=_WAIT_S)
        deadline = time.monotonic() + _WAIT_S
        while True:
            reaped_pid, status = os.waitpid(supervisor_pid, os.WNOHANG)
            if reaped_pid == supervisor_pid:
                reaped = True
                assert os.waitstatus_to_exitcode(status) == 0
                break
            if time.monotonic() >= deadline:
                pytest.fail("SSH supervisor survived its creating process")
            time.sleep(0.01)
        assert tunnel.identity_state(identity) is tunnel._IdentityState.STALE
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=_WAIT_S)
        if child_pid is None and (tmp_path / "child-pid").exists():
            child_pid = int((tmp_path / "child-pid").read_text())
        if supervisor_pid is None and (tmp_path / "supervisor-pid").exists():
            supervisor_pid = int((tmp_path / "supervisor-pid").read_text())
        for pid in [supervisor_pid, child_pid]:
            if pid is None or (pid == supervisor_pid and reaped):
                continue
            # Prove this PID is still our unreaped child before signalling it.
            # A child slot cannot be reused until its parent reaps it.
            try:
                waited, _status = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited = pid
            if waited == 0:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)


def test_owned_exec_refuses_parent_that_died_before_helper_setup(monkeypatch):
    calls = []

    class Prctl:
        def __call__(self, *args):
            calls.append(args)
            return 0

    monkeypatch.setattr(tunnel_child.ctypes, "CDLL", lambda *_a, **_kw: SimpleNamespace(prctl=Prctl()))
    monkeypatch.setattr(tunnel_child.os, "getppid", lambda: 777)
    monkeypatch.setattr(tunnel_child.os, "execvp", lambda *_a: pytest.fail("must not exec SSH"))

    with pytest.raises(RuntimeError, match="owner exited"):
        tunnel_child.exec_owned(778, ["ssh", "host"])

    assert calls == [(1, signal.SIGKILL, 0, 0, 0)]


def test_owned_exec_fails_closed_when_parent_death_setup_fails(monkeypatch):
    class Prctl:
        def __call__(self, *_args):
            ctypes.set_errno(1)
            return -1

    monkeypatch.setattr(tunnel_child.ctypes, "CDLL", lambda *_a, **_kw: SimpleNamespace(prctl=Prctl()))
    monkeypatch.setattr(tunnel_child.os, "execvp", lambda *_a: pytest.fail("must not exec SSH"))

    with pytest.raises(OSError):
        tunnel_child.exec_owned(778, ["ssh", "host"])


@pytest.mark.parametrize("stale_after_open", [False, True])
def test_verified_signal_uses_portable_pidfd_adapter_and_rechecks_identity(
    monkeypatch, stale_after_open
):
    identity = tunnel.ProcessIdentity(pid=123, start_time="456", boot_id="boot")
    states = iter([
        tunnel._IdentityState.LIVE,
        tunnel._IdentityState.STALE if stale_after_open else tunnel._IdentityState.LIVE,
    ])
    sent = []
    closed = []
    monkeypatch.setattr(tunnel, "identity_state", lambda _identity: next(states))
    monkeypatch.setattr(tunnel.pidfd_api, "available", lambda: True)
    monkeypatch.setattr(tunnel.pidfd_api, "open_pidfd", lambda pid, flags: 999)
    monkeypatch.setattr(tunnel.pidfd_api, "send_signal", lambda fd, sig: sent.append((fd, sig)))
    monkeypatch.setattr(tunnel.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(tunnel.os, "kill", lambda *_a: pytest.fail("must not use PID signalling"))

    assert tunnel._signal_verified(identity, signal.SIGTERM) is not stale_after_open
    assert sent == ([] if stale_after_open else [(999, signal.SIGTERM)])
    assert closed == [999]

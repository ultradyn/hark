"""Real pidfd coverage with the Python wrappers absent (B195)."""

import ctypes
import errno
import os
import signal
import subprocess
import sys

import pytest

from hark import pidfd


_HAS_LIBC_PIDFD = sys.platform == "linux" and pidfd._LIBC_OPEN is not None and pidfd._LIBC_SEND is not None


def test_missing_stdlib_uses_libc_and_retains_errno(monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    opened = []
    sent = []
    monkeypatch.setattr(pidfd, "_LIBC_OPEN", lambda pid, flags: opened.append((pid, flags)) or 42)
    monkeypatch.setattr(pidfd, "_LIBC_SEND", lambda fd, sig, info, flags: sent.append((fd, sig, info, flags)) or 0)
    assert pidfd.open_pidfd(123, 0) == 42
    pidfd.send_signal(42, signal.SIGTERM)
    assert opened == [(123, 0)]
    assert sent == [(42, signal.SIGTERM, None, 0)]

    def missing(_pid, _flags):
        ctypes.set_errno(errno.ESRCH)
        return -1

    monkeypatch.setattr(pidfd, "_LIBC_OPEN", missing)
    with pytest.raises(ProcessLookupError):
        pidfd.open_pidfd(123)


def test_unavailable_interfaces_fail_explicitly(monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(pidfd, "_LIBC_OPEN", None)
    monkeypatch.setattr(pidfd, "_LIBC_SEND", None)
    assert pidfd.available() is False
    with pytest.raises(OSError) as caught:
        pidfd.open_pidfd(123)
    assert caught.value.errno == errno.ENOSYS
    with pytest.raises(OSError) as caught:
        pidfd.send_signal(42, signal.SIGTERM)
    assert caught.value.errno == errno.ENOSYS


@pytest.mark.skipif(not _HAS_LIBC_PIDFD, reason="Linux libc pidfd interfaces unavailable")
def test_libc_adapter_opens_signals_and_closes_real_child_without_stdlib(monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    descriptor = None
    try:
        try:
            descriptor = pidfd.open_pidfd(process.pid)
        except OSError as exc:
            if exc.errno == errno.ENOSYS:
                pytest.skip("kernel does not support pidfd_open")
            raise
        pidfd.send_signal(descriptor, 0)
        pidfd.send_signal(descriptor, signal.SIGTERM)
        assert process.wait(timeout=30) == -signal.SIGTERM
        with pytest.raises(ProcessLookupError):
            pidfd.send_signal(descriptor, 0)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)

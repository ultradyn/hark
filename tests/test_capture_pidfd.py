"""Parent lifetime watches use the portable pidfd adapter (B195)."""

import os
import select
import subprocess
import sys

import pytest

from hark import pidfd
from hark.audio import capture


@pytest.mark.skipif(not pidfd.available(), reason="requires Linux pidfds")
def test_parent_pidfd_adapter_works_without_stdlib_wrapper(monkeypatch):
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    if pidfd._LIBC_OPEN is None:
        pytest.skip("libc pidfd_open unavailable")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    fd = None
    try:
        fd = capture.process_pidfd.open_pidfd(child.pid)
        assert select.select([fd], [], [], 0)[0] == []
        child.terminate()
        child.wait(timeout=10)
        assert select.select([fd], [], [], 10)[0] == [fd]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        if fd is not None:
            os.close(fd)

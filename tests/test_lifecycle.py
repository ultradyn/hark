import os
import signal
from pathlib import Path

from hark.lifecycle import BusySection, busy_path, request_shutdown, shutdown_requested


def test_busy_section_writes_lock(tmp_path, monkeypatch):
    import hark.lifecycle as lc

    monkeypatch.setattr(lc, "state_dir", lambda: tmp_path)
    # reset module globals carefully
    lc._busy_depth = 0
    path = busy_path()
    assert not path.exists()
    with BusySection("listen"):
        assert path.is_file()
        assert "listen" in path.read_text()
    assert not path.exists()


def test_shutdown_flag():
    import hark.lifecycle as lc

    lc._shutdown = False
    assert shutdown_requested() is False
    request_shutdown(15)
    assert shutdown_requested() is True
    lc._shutdown = False

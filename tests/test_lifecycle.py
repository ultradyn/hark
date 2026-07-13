from hark.lifecycle import (
    BusySection,
    PHRASE_RESTART,
    PHRASE_SHUTDOWN,
    busy_path,
    clear_reload_request,
    get_shutdown_reason,
    reload_requested,
    request_reload,
    request_shutdown,
    set_shutdown_reason,
    shutdown_phrase,
    shutdown_requested,
)


def test_busy_section_writes_lock(tmp_path, monkeypatch):
    import hark.lifecycle as lc

    monkeypatch.setattr(lc, "state_dir", lambda: tmp_path)
    lc._busy_depth = 0
    path = busy_path()
    assert not path.exists()
    with BusySection("listen"):
        assert path.is_file()
        assert "listen" in path.read_text()
    assert not path.exists()


def test_shutdown_flag(tmp_path, monkeypatch):
    import hark.lifecycle as lc

    monkeypatch.setattr(lc, "state_dir", lambda: tmp_path)
    lc._shutdown = False
    assert shutdown_requested() is False
    request_shutdown(15, reason="stop")
    assert shutdown_requested() is True
    assert get_shutdown_reason() == "stop"
    lc._shutdown = False


def test_shutdown_phrases():
    assert "shutting down" in PHRASE_SHUTDOWN.lower()
    assert "restart" in PHRASE_RESTART.lower()
    assert shutdown_phrase("stop") == PHRASE_SHUTDOWN
    assert shutdown_phrase("restart") == PHRASE_RESTART


def test_set_shutdown_reason_file(tmp_path, monkeypatch):
    import hark.lifecycle as lc

    monkeypatch.setattr(lc, "state_dir", lambda: tmp_path)
    monkeypatch.delenv("HARK_SHUTDOWN_REASON", raising=False)
    set_shutdown_reason("restart")
    assert (tmp_path / "shutdown_reason").read_text().strip() == "restart"
    assert get_shutdown_reason() == "restart"
    set_shutdown_reason("stop")
    assert get_shutdown_reason() == "stop"


def test_reload_request_flag():
    clear_reload_request()
    assert reload_requested() is False
    request_reload(signum=1)
    assert reload_requested() is True
    clear_reload_request()
    assert reload_requested() is False

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


def test_signal_scope_restores_prior_handlers_and_supports_nesting():
    import signal
    import hark.lifecycle as lc

    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in signals}
    with lc.signal_handler_scope():
        installed = {signum: signal.getsignal(signum) for signum in signals}
        assert lc._handlers_installed is True
        assert installed != previous
        with lc.signal_handler_scope():
            assert {signum: signal.getsignal(signum) for signum in signals} == installed
        assert {signum: signal.getsignal(signum) for signum in signals} == installed
    assert {signum: signal.getsignal(signum) for signum in signals} == previous
    assert lc._handlers_installed is False


def test_signal_uninstall_preserves_a_new_external_owner():
    import signal
    import hark.lifecycle as lc

    previous = signal.getsignal(signal.SIGINT)
    external = lambda _sig, _frame: None
    try:
        with lc.signal_handler_scope():
            signal.signal(signal.SIGINT, external)
        assert signal.getsignal(signal.SIGINT) is external
        assert lc._handlers_installed is False
    finally:
        signal.signal(signal.SIGINT, previous)


def test_partial_signal_install_failure_rolls_back(monkeypatch):
    import signal
    import pytest
    import hark.lifecycle as lc

    native = signal.signal
    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    def fail_install(signum, handler):
        if signum == signal.SIGINT:
            raise ValueError("install failed")
        return native(signum, handler)

    monkeypatch.setattr(signal, "signal", fail_install)
    with pytest.raises(ValueError, match="install failed"):
        lc.install_signal_handlers()
    assert {signum: signal.getsignal(signum) for signum in previous} == previous
    assert lc._handlers_installed is False
    assert lc._installed_handlers == {}


def test_ambient_missing_model_return_restores_signal_handlers(monkeypatch):
    import io
    import signal
    import hark.ambient as ambient
    from hark.config import HarkConfig

    cfg = HarkConfig()
    cfg.ambient.engine = "vosk"
    cfg.ambient.model_path = ""
    cfg.audio.sync_hw_unmute = False
    previous = signal.getsignal(signal.SIGINT)
    assert ambient.run_ambient_loop(cfg, out=io.StringIO(), announce=False) == 1
    assert signal.getsignal(signal.SIGINT) is previous


def test_pending_sigint_during_uninstall_finishes_all_restoration(monkeypatch):
    import os
    import signal
    import pytest
    import hark.lifecycle as lc

    if not hasattr(signal, "pthread_sigmask"):
        pytest.skip("requires POSIX signal masks")
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals.append(signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in signals}
    assert lc.install_signal_handlers() is True
    native = signal.signal
    delivered = False

    def restore_then_interrupt(signum, handler):
        nonlocal delivered
        result = native(signum, handler)
        if signum == signal.SIGINT and not delivered:
            delivered = True
            os.kill(os.getpid(), signal.SIGINT)
        return result

    monkeypatch.setattr(signal, "signal", restore_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        lc.uninstall_signal_handlers()
    assert delivered is True
    assert {signum: signal.getsignal(signum) for signum in signals} == previous
    assert lc._handlers_installed is False


def test_ambient_setup_exception_restores_signal_handlers(monkeypatch):
    import signal
    import pytest
    import hark.ambient as ambient
    from hark.config import HarkConfig

    previous = signal.getsignal(signal.SIGINT)

    def fail(*_args, **_kwargs):
        raise RuntimeError("setup failed")

    monkeypatch.setattr(ambient, "_run_ambient_loop", fail)
    with pytest.raises(RuntimeError, match="setup failed"):
        ambient.run_ambient_loop(HarkConfig(), announce=False)
    assert signal.getsignal(signal.SIGINT) is previous

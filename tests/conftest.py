"""Shared pytest fixtures.

Disable GitHub release self-update checks by default so CI and unit tests never
hit the network (B088). Opt in via ``HARK_UPDATE_CHECK=1`` in specific tests.
"""

from __future__ import annotations

import signal
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def isolated_state_home(tmp_path: Path) -> Path:
    """Per-test XDG state root; never allow defaults to reach operator state."""
    return tmp_path / "xdg-state"


@pytest.fixture(autouse=True)
def _isolate_xdg_state(
    monkeypatch: pytest.MonkeyPatch,
    isolated_state_home: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_state_home))
    monkeypatch.setenv("HARK_EVENT_PROVENANCE", "test")


@pytest.fixture(autouse=True)
def _disable_update_check_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARK_UPDATE_CHECK", "0")


# B184: the interrupt regressions deliver a real signal to this process and
# expect Python's default KeyboardInterrupt. That is an assumption about
# ambient process state which the tests never established, and it fails two
# ways. A suite launched as a shell background job (or under ``nohup``)
# inherits SIGINT=SIG_IGN, and the kernel then discards
# ``os.kill(os.getpid(), signal.SIGINT)`` outright. And
# ``hark.lifecycle.install_signal_handlers`` has no uninstall, so the first
# test that runs an ambient loop leaves its ``_stop_handler`` -- which only
# records a shutdown request and returns -- owning SIGINT and SIGTERM for
# every later test in the session. Either way the signal never becomes an
# exception, so the test fails in a full run and passes when run alone.
_OWNED_SIGNALS = tuple(
    signum
    for signum in (
        getattr(signal, "SIGINT", None),
        getattr(signal, "SIGTERM", None),
        getattr(signal, "SIGHUP", None),
    )
    if signum is not None
)


@pytest.fixture(autouse=True)
def _own_interrupt_dispositions() -> Iterator[None]:
    """Give every test the signal environment it gets when run alone."""
    from hark import lifecycle

    previous = {signum: signal.getsignal(signum) for signum in _OWNED_SIGNALS}
    handlers_installed = lifecycle._handlers_installed
    # Pin SIGINT only. A background-job launcher ignores SIGINT and SIGQUIT
    # but never SIGTERM, so restoring SIGTERM is enough for it, and pinning
    # SIGTERM to SIG_DFL would turn a test's mistimed self-signal into a kill
    # of the whole session rather than a failure of that one test.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    lifecycle._handlers_installed = False
    try:
        yield
    finally:
        lifecycle._handlers_installed = handlers_installed
        for signum, handler in previous.items():
            # ``getsignal`` reports None for a disposition installed outside
            # Python; there is nothing to hand back in that case.
            if handler is not None:
                signal.signal(signum, handler)

"""Graceful process lifecycle: finish active recording before exit."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from hark.paths import state_dir
from hark.signal_safety import SigintMaskGuard
from hark.syslog import log as syslog

_lock = threading.Lock()
_shutdown = False
_shutdown_reason = "stop"  # stop | restart
_reload = False
_reload_source: str | None = None
_busy_depth = 0
_handlers_installed = False
_previous_handlers: dict[int, object] = {}
_installed_handlers: dict[int, object] = {}

# Spoken cues (short, cacheable)
PHRASE_SHUTDOWN = "Hark shutting down."
PHRASE_RESTART = "Hark restarting."


def busy_path() -> Path:
    return state_dir() / "busy.lock"


def shutdown_reason_path() -> Path:
    return state_dir() / "shutdown_reason"


def shutdown_requested() -> bool:
    return _shutdown


def get_shutdown_reason() -> str:
    """Return stop|restart from memory, env, or state file."""
    global _shutdown_reason
    env = (os.environ.get("HARK_SHUTDOWN_REASON") or "").strip().lower()
    if env in ("stop", "restart", "shutdown"):
        return "restart" if env == "restart" else "stop"
    try:
        p = shutdown_reason_path()
        if p.is_file():
            raw = p.read_text(encoding="utf-8").strip().lower()
            if raw in ("stop", "restart", "shutdown"):
                return "restart" if raw == "restart" else "stop"
    except OSError:
        pass
    return _shutdown_reason if _shutdown_reason in ("stop", "restart") else "stop"


def set_shutdown_reason(reason: str) -> None:
    global _shutdown_reason
    r = (reason or "stop").strip().lower()
    if r == "shutdown":
        r = "stop"
    if r not in ("stop", "restart"):
        r = "stop"
    _shutdown_reason = r
    try:
        path = shutdown_reason_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(r + "\n", encoding="utf-8")
    except OSError:
        pass


def clear_shutdown_reason() -> None:
    try:
        shutdown_reason_path().unlink(missing_ok=True)
    except OSError:
        pass


def request_shutdown(signum: int | None = None, reason: str | None = None) -> None:
    global _shutdown
    if reason:
        set_shutdown_reason(reason)
    else:
        # Prefer reason already staged by the stop/restart script
        set_shutdown_reason(get_shutdown_reason())
    _shutdown = True
    syslog(
        "lifecycle.shutdown_requested",
        component="lifecycle",
        signal=signum,
        reason=get_shutdown_reason(),
        busy=_busy_depth > 0,
    )


def request_reload(
    signum: int | None = None,
    *,
    source: str | None = None,
) -> None:
    """Ask ambient (or other long-running loops) to re-read config.

    Used by SIGHUP and by the config.toml file-watch poller. Does not stop
    the process; the next safe point should call ``clear_reload_request``
    after applying the new config.

    *source* labels the requester (``"sighup"``, ``"config_watch"``, …) for
    syslog and ``ambient.reloaded``.
    """
    global _reload, _reload_source
    _reload = True
    if source:
        src = source
    elif signum is not None and hasattr(signal, "SIGHUP") and signum == signal.SIGHUP:
        src = "sighup"
    elif signum is not None:
        src = f"signal:{signum}"
    else:
        src = "manual"
    _reload_source = src
    syslog(
        "lifecycle.reload_requested",
        component="lifecycle",
        signal=signum,
        source=src,
        busy=_busy_depth > 0,
    )


def reload_requested() -> bool:
    return _reload


def reload_source() -> str | None:
    """Who asked for the pending reload (``config_watch``, ``sighup``, …)."""
    return _reload_source


def clear_reload_request() -> None:
    global _reload, _reload_source
    _reload = False
    _reload_source = None


def shutdown_phrase(reason: str | None = None) -> str:
    r = reason or get_shutdown_reason()
    return PHRASE_RESTART if r == "restart" else PHRASE_SHUTDOWN


def install_signal_handlers() -> bool:
    """Install lifecycle handlers; return whether this call acquired ownership.

    Pair a successful installation with ``uninstall_signal_handlers``. Nested
    users should prefer ``signal_handler_scope`` to preserve an outer owner.
    """
    global _handlers_installed
    if _handlers_installed:
        return False

    def _stop_handler(signum: int, _frame: object) -> None:
        request_shutdown(signum)

    def _hup_handler(signum: int, _frame: object) -> None:
        request_reload(signum, source="sighup")

    handlers = {signal.SIGTERM: _stop_handler, signal.SIGINT: _stop_handler}
    if hasattr(signal, "SIGHUP"):
        handlers[signal.SIGHUP] = _hup_handler
    try:
        for signum, handler in handlers.items():
            previous = signal.getsignal(signum)
            if previous is None:
                raise ValueError(f"cannot preserve non-Python handler for signal {signum}")
            # Publish ownership even when an injected signal.signal wrapper
            # raises after installing the handler.
            try:
                signal.signal(signum, handler)
            finally:
                if signal.getsignal(signum) is handler:
                    _previous_handlers[signum] = previous
                    _installed_handlers[signum] = handler
    except BaseException:
        try:
            uninstall_signal_handlers()
        except Exception:
            # Keep the original installation failure and retained ownership
            # state so a later explicit uninstall can retry.
            pass
        raise
    _handlers_installed = True
    return True


def uninstall_signal_handlers() -> None:
    """Restore handlers still owned by Hark, preserving external replacements."""
    guard = SigintMaskGuard.acquire()
    try:
        _restore_owned_signal_handlers()
    finally:
        # In particular, pending SIGINT may reach the previous owner only
        # after SIGHUP/SIGTERM ownership and our bookkeeping are settled.
        guard.restore_preserving_primary()


def _restore_owned_signal_handlers() -> None:
    global _handlers_installed
    primary = sys.exception()
    first_error: BaseException | None = None
    for signum, installed in list(_installed_handlers.items()):
        previous = _previous_handlers[signum]
        try:
            if signal.getsignal(signum) is installed and previous is not None:
                signal.signal(signum, previous)
        except BaseException as exc:
            first_error = first_error or exc
            continue
        _installed_handlers.pop(signum, None)
        _previous_handlers.pop(signum, None)
    _handlers_installed = bool(_installed_handlers)
    if first_error is not None and primary is None:
        raise first_error


@contextmanager
def signal_handler_scope() -> Iterator[None]:
    """Own process signal handling only for this scope's lifetime."""
    installed_here = install_signal_handlers()
    try:
        yield
    finally:
        if installed_here:
            uninstall_signal_handlers()


class BusySection:
    """Mark process busy (e.g. recording) so stop scripts can wait."""

    def __init__(self, reason: str = "recording") -> None:
        self.reason = reason

    def __enter__(self) -> BusySection:
        global _busy_depth
        with _lock:
            _busy_depth += 1
            if _busy_depth == 1:
                path = busy_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    f"pid={os.getpid()}\nreason={self.reason}\nts={time.time()}\n",
                    encoding="utf-8",
                )
                syslog(
                    "lifecycle.busy",
                    component="lifecycle",
                    reason=self.reason,
                    pid=os.getpid(),
                )
        return self

    def __exit__(self, *args: object) -> None:
        global _busy_depth
        with _lock:
            _busy_depth = max(0, _busy_depth - 1)
            if _busy_depth == 0:
                try:
                    busy_path().unlink(missing_ok=True)
                except OSError:
                    pass
                syslog("lifecycle.idle", component="lifecycle", pid=os.getpid())

"""Graceful process lifecycle: finish active recording before exit."""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

from hark.paths import state_dir
from hark.syslog import log as syslog

_lock = threading.Lock()
_shutdown = False
_shutdown_reason = "stop"  # stop | restart
_reload = False
_reload_source: str | None = None
_busy_depth = 0
_handlers_installed = False

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


def install_signal_handlers() -> None:
    """Install SIGTERM/SIGINT (stop) and SIGHUP (config reload) handlers."""
    global _handlers_installed
    if _handlers_installed:
        return

    def _stop_handler(signum: int, _frame: object) -> None:
        request_shutdown(signum)

    def _hup_handler(signum: int, _frame: object) -> None:
        request_reload(signum, source="sighup")

    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)
    # SIGHUP: reload config (custom wake phrases, etc.) without full restart
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _hup_handler)
    _handlers_installed = True


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

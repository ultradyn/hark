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
_busy_depth = 0
_handlers_installed = False


def busy_path() -> Path:
    return state_dir() / "busy.lock"


def shutdown_requested() -> bool:
    return _shutdown


def request_shutdown(signum: int | None = None) -> None:
    global _shutdown
    _shutdown = True
    syslog(
        "lifecycle.shutdown_requested",
        component="lifecycle",
        signal=signum,
        busy=_busy_depth > 0,
    )


def install_signal_handlers() -> None:
    """Install SIGTERM/SIGINT handlers (idempotent)."""
    global _handlers_installed
    if _handlers_installed:
        return

    def _handler(signum: int, _frame: object) -> None:
        request_shutdown(signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
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

"""Coordinate mic ownership between ambient wake and bound listen/ask.

Ambient holds a continuous capture stream (MicLease + ring buffer) while
scanning for wake phrases. Mode A answer flows (listen / ask / tts --listen)
need exclusive access. Cooperative protocol:

  1. Listener writes ``state/ambient.pause``
  2. Ambient closes the continuous stream (releases lease) when pause is set
  3. Listener acquires ``MicLease``, records, then clears the pause file

Max wait for ambient to yield is one hop (~0.5–1 s of 20 ms reads) plus poll
time — not a full snippet open/close cycle.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hark.audio.capture import MicBusyError, MicLease
from hark.paths import state_dir
from hark.syslog import log as syslog

DEFAULT_YIELD_TIMEOUT_S = 15.0
_POLL_S = 0.05


def pause_path() -> Path:
    return state_dir() / "ambient.pause"


def ambient_pause_requested() -> bool:
    return pause_path().is_file()


def request_ambient_pause(
    *,
    reason: str = "listen",
    pid: int | None = None,
) -> Path:
    path = pause_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason,
        "pid": pid if pid is not None else os.getpid(),
        "requested_at": time.time(),
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    syslog(
        "ambient.pause_request",
        component="mic",
        level="info",
        reason=reason,
        pid=payload["pid"],
    )
    return path


def clear_ambient_pause() -> None:
    path = pause_path()
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
            syslog("ambient.pause_clear", component="mic", level="info")
    except OSError:
        pass


def wait_for_mic_free(*, timeout_s: float = DEFAULT_YIELD_TIMEOUT_S) -> None:
    """Block until no other process holds mic.lock (or timeout)."""
    deadline = time.monotonic() + max(0.1, timeout_s)
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            # Probe: acquire and immediately release
            with MicLease("mic-probe"):
                return
        except MicBusyError as exc:
            last_err = exc
            time.sleep(_POLL_S)
    raise MicBusyError(
        f"mic still busy after {timeout_s:.1f}s waiting for ambient to yield"
        + (f" ({last_err})" if last_err else "")
    )


@contextmanager
def pause_ambient_for_mic(
    *,
    reason: str = "listen",
    timeout_s: float = DEFAULT_YIELD_TIMEOUT_S,
) -> Iterator[None]:
    """Ask ambient to yield the mic, wait until free, clear pause on exit.

    Use around bound listen/ask so ambient wake scanning does not raise mic busy.
    """
    request_ambient_pause(reason=reason)
    try:
        wait_for_mic_free(timeout_s=timeout_s)
        yield
    finally:
        clear_ambient_pause()

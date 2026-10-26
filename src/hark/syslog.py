"""Unified high-resolution Hark system log (JSONL).

All subsystems append here so operators get one timeline:

  ~/.local/state/hark/system.jsonl

Also mirrored lightly into usage.jsonl for TTS/STT aggregates.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hark.paths import state_dir

_lock = threading.Lock()
_seq = 0


def system_log_path() -> Path:
    override = os.environ.get("HARK_SYSTEM_LOG")
    if override:
        return Path(override)
    return state_dir() / "system.jsonl"


@dataclass
class LogEvent:
    ts: float = field(default_factory=time.time)
    seq: int = 0
    level: str = "info"  # debug | info | warn | error
    component: str = "hark"
    event: str = ""
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    pid: int = field(default_factory=os.getpid)


def _next_seq() -> int:
    global _seq
    with _lock:
        _seq += 1
        return _seq


def log(
    event: str,
    *,
    component: str = "hark",
    level: str = "info",
    message: str = "",
    **data: Any,
) -> None:
    """Append one structured line to the system log (best-effort, never raises)."""
    try:
        path = system_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Drop Nones for compactness
        clean = {k: v for k, v in data.items() if v is not None}
        rec = LogEvent(
            seq=_next_seq(),
            level=level,
            component=component,
            event=event,
            message=message or event,
            data=clean,
        )
        line = json.dumps(asdict(rec), separators=(",", ":"), default=str)
        with _lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        # logging must never break the product path
        pass


def log_exception(
    event: str,
    exc: BaseException,
    *,
    component: str = "hark",
    **data: Any,
) -> None:
    log(
        event,
        component=component,
        level="error",
        message=str(exc),
        error_type=type(exc).__name__,
        traceback="".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[-2000:],
        **data,
    )


def tail_lines(n: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    if n <= 0:
        return []
    p = path or system_log_path()
    if not p.is_file():
        return []
    # efficient-ish tail for moderate files
    with p.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        block = 8192
        data = b""
        while size > 0 and data.count(b"\n") <= n:
            step = min(block, size)
            size -= step
            fh.seek(size)
            data = fh.read(step) + data
        lines = data.splitlines()[-n:]
    out: list[dict[str, Any]] = []
    for raw in lines:
        try:
            out.append(json.loads(raw.decode("utf-8", errors="replace")))
        except json.JSONDecodeError:
            continue
    return out

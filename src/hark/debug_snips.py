"""Dev-mode wake snippet capture (audio + transcript), 7-day retention."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from hark.audio.capture import write_wav_bytes
from hark.paths import state_dir
from hark.syslog import log as syslog

DEFAULT_RETENTION_DAYS = 7


def debug_wake_dir() -> Path:
    return state_dir() / "debug" / "wake"


def purge_old_debug_snips(
    *,
    retention_days: float = DEFAULT_RETENTION_DAYS,
    root: Path | None = None,
) -> int:
    """Delete files under debug/wake older than retention_days. Returns count removed."""
    root = root or debug_wake_dir()
    if not root.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    # prune empty dirs
    for d in sorted(root.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                next(d.iterdir())
            except StopIteration:
                try:
                    d.rmdir()
                except OSError:
                    pass
    if removed:
        syslog(
            "debug.purge",
            component="ambient",
            level="info",
            removed=removed,
            retention_days=retention_days,
        )
    return removed


def save_wake_snippet(
    *,
    pcm16: bytes,
    sample_rate: int = 16000,
    text: str | None,
    matched: bool,
    phrase: str | None = None,
    rms: float | None = None,
    backend: str | None = None,
    enabled: bool = True,
) -> Path | None:
    """Save wav + sidecar JSON. No-op if disabled. Best-effort."""
    if not enabled:
        return None
    try:
        purge_old_debug_snips()  # cheap enough occasionally; mtime check is light
        day = time.strftime("%Y-%m-%d")
        ts = time.strftime("%H%M%S")
        ms = int((time.time() % 1) * 1000)
        tag = "hit" if matched else "miss"
        base = debug_wake_dir() / day
        base.mkdir(parents=True, exist_ok=True)
        stem = f"{ts}-{ms:03d}-{tag}"
        wav_path = base / f"{stem}.wav"
        meta_path = base / f"{stem}.json"
        wav_path.write_bytes(write_wav_bytes(pcm16, sample_rate))
        meta: dict[str, Any] = {
            "ts": time.time(),
            "matched": matched,
            "phrase": phrase,
            "text": text,
            "rms": rms,
            "backend": backend,
            "sample_rate": sample_rate,
            "wav": str(wav_path),
            "pcm_bytes": len(pcm16),
        }
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        syslog(
            "debug.wake_snip",
            component="ambient",
            level="debug",
            matched=matched,
            text=text,
            phrase=phrase,
            path=str(wav_path),
            rms=rms,
        )
        return wav_path
    except Exception as exc:
        syslog(
            "debug.wake_snip_error",
            component="ambient",
            level="warn",
            error=str(exc)[:160],
        )
        return None

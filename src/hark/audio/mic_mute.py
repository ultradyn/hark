"""System capture mute during TTS (Pulse/PipeWire → Elgato Wave mute light).

When the default source is the Wave (or any PA source with mute-sync hardware),
`pactl set-source-mute` toggles the same path the Wave ring uses (white→red).
"""

from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass
class MuteState:
    source: str | None
    was_muted: bool | None
    applied: bool


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, timeout=5)


def default_source() -> str | None:
    if _which("pactl"):
        p = _run(["pactl", "get-default-source"])
        if p.returncode == 0:
            name = (p.stdout or "").strip()
            return name or None
    return None


def source_is_muted(source: str) -> bool | None:
    p = _run(["pactl", "get-source-mute", source])
    if p.returncode != 0:
        return None
    # "Mute: yes" / "Mute: no"
    out = (p.stdout or "").lower()
    if "yes" in out:
        return True
    if "no" in out:
        return False
    return None


def set_source_mute(source: str, mute: bool) -> bool:
    p = _run(["pactl", "set-source-mute", source, "1" if mute else "0"])
    return p.returncode == 0


def _which(bin_name: str) -> bool:
    from shutil import which

    return which(bin_name) is not None


_lock = threading.Lock()
_depth = 0
_saved: MuteState | None = None


@contextmanager
def mic_muted_during_tts(*, enabled: bool = True) -> Iterator[MuteState]:
    """Nestable: mute default capture source while TTS plays; restore after.

    Only mutes if we are the first nested holder and the source was unmuted
    (or unknown). Always restores to pre-TTS state when the outermost exits.
    """
    state = MuteState(source=None, was_muted=None, applied=False)
    if not enabled or not _which("pactl"):
        yield state
        return

    global _depth, _saved
    with _lock:
        _depth += 1
        if _depth == 1:
            src = default_source()
            state.source = src
            if src:
                was = source_is_muted(src)
                state.was_muted = was
                if was is not True:
                    if set_source_mute(src, True):
                        state.applied = True
                _saved = MuteState(
                    source=src, was_muted=was, applied=state.applied
                )
            else:
                _saved = state
        else:
            # nested: report outer state
            if _saved:
                state = MuteState(
                    source=_saved.source,
                    was_muted=_saved.was_muted,
                    applied=_saved.applied,
                )

    try:
        yield state
    finally:
        with _lock:
            _depth = max(0, _depth - 1)
            if _depth == 0 and _saved is not None:
                if _saved.applied and _saved.source and _saved.was_muted is not True:
                    set_source_mute(_saved.source, False)
                _saved = None

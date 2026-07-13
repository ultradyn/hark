"""System capture mute during TTS (Pulse/PipeWire → Elgato Wave mute light).

When the default source is the Wave (or any PA source with mute-sync hardware),
`pactl set-source-mute` toggles the same path the Wave ring uses (white→red).

**Hardware unmute sync:** the Wave mute button and ALSA capture switch can
diverge from the PipeWire source mute (or from app mute indicators). A small
watcher (``start_mute_sync_watcher``) detects mute→unmute edges and runs
``ensure_unmuted`` so OS/Pulse (and ALSA Mic) follow a manual unmute.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator


@dataclass
class MuteState:
    source: str | None
    was_muted: bool | None
    applied: bool


def _run(args: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, capture_output=True, text=True, check=False, timeout=timeout
    )


def _which(bin_name: str) -> bool:
    from shutil import which

    return which(bin_name) is not None


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


# ---------------------------------------------------------------------------
# ALSA Wave capture switch (hardware mute path)
# ---------------------------------------------------------------------------

_WAVE_CARD_RE = re.compile(r"card\s+(\d+):\s+(\S+)\s+\[([^\]]+)\]", re.I)


def find_wave_alsa_card() -> tuple[str, int] | None:
    """Return (card_id_or_index, index) for Elgato Wave if present."""
    if not _which("arecord"):
        return None
    p = _run(["arecord", "-l"])
    if p.returncode != 0:
        return None
    for line in (p.stdout or "").splitlines():
        m = _WAVE_CARD_RE.search(line)
        if not m:
            continue
        idx = int(m.group(1))
        short = m.group(2)
        long = m.group(3)
        if "wave" in short.lower() or "wave" in long.lower() or "elgato" in long.lower():
            # prefer ALSA id string when available
            return (short, idx)
    return None


def alsa_mic_capture_on(card: str | int | None = None) -> bool | None:
    """True if ALSA Mic capture switch is on (unmuted), False if off, None if n/a."""
    if not _which("amixer"):
        return None
    if card is None:
        found = find_wave_alsa_card()
        if not found:
            return None
        card = found[0]
    p = _run(["amixer", "-c", str(card), "sget", "Mic"])
    if p.returncode != 0:
        return None
    out = p.stdout or ""
    # Mono: Capture N [pct%] [dB] [on|off]
    if re.search(r"\[off\]", out, re.I):
        return False
    if re.search(r"\[on\]", out, re.I):
        return True
    return None


def set_alsa_mic_capture(on: bool, card: str | int | None = None) -> bool:
    if not _which("amixer"):
        return False
    if card is None:
        found = find_wave_alsa_card()
        if not found:
            return False
        card = found[0]
    # 'cap' / 'nocap' or unmute/mute for cswitch
    arg = "cap" if on else "nocap"
    p = _run(["amixer", "-c", str(card), "-q", "sset", "Mic", arg])
    if p.returncode == 0:
        return True
    # fallback wording
    p2 = _run(
        ["amixer", "-c", str(card), "-q", "sset", "Mic", "unmute" if on else "mute"]
    )
    return p2.returncode == 0


def ensure_unmuted(*, source: str | None = None) -> dict[str, bool | None]:
    """Force OS + ALSA capture unmuted (manual/hardware unmute cascade).

    Also **fully clears** any in-process TTS mute hold (depth + saved state)
    so B084 listen clocks and future captures are not stuck thinking mute is
    still held (B086).

    Returns which steps succeeded.
    """
    released = force_clear_tts_mute_hold(reason="ensure_unmuted")
    src = source or default_source()
    result: dict[str, bool | None] = {
        "pulse": None,
        "alsa": None,
        "released_hark_hold": bool(released.get("cleared")),
    }
    if src and _which("pactl"):
        result["pulse"] = set_source_mute(src, False)
    alsa = set_alsa_mic_capture(True)
    result["alsa"] = alsa if find_wave_alsa_card() else None
    try:
        from hark.syslog import log

        log(
            "mic.ensure_unmuted",
            component="audio",
            source=src,
            pulse=result["pulse"],
            alsa=result["alsa"],
            released_hark_hold=result["released_hark_hold"],
        )
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Nestable TTS mute
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_depth = 0
_saved: MuteState | None = None
# User (or HW button) overrode mute while we held it — do not re-mute on exit
_user_unmuted_override = False


def force_clear_tts_mute_hold(*, reason: str = "force") -> dict[str, object]:
    """Fully drop Hark TTS mute hold (depth + saved) and unmute OS+ALSA.

    Used for desync recovery and ``ensure_unmuted``. Nested ``mic_muted_during_tts``
    finally blocks become no-ops once depth/saved are cleared.
    """
    global _depth, _saved, _user_unmuted_override
    with _lock:
        had_hold = _depth > 0 or _saved is not None
        depth_before = _depth
        src = _saved.source if _saved else None
        applied = bool(_saved.applied) if _saved else False
        _depth = 0
        _saved = None
        _user_unmuted_override = False
    if not src:
        src = default_source()
    pulse_ok: bool | None = None
    if src and _which("pactl"):
        pulse_ok = set_source_mute(src, False)
    alsa_ok = set_alsa_mic_capture(True)
    out: dict[str, object] = {
        "cleared": had_hold,
        "depth_before": depth_before,
        "source": src,
        "applied": applied,
        "pulse": pulse_ok,
        "alsa": alsa_ok if find_wave_alsa_card() else None,
        "reason": reason,
    }
    if had_hold:
        try:
            from hark.syslog import log

            log(
                "mic.mute_hold_cleared",
                component="audio",
                **{k: v for k, v in out.items() if k != "cleared"},
            )
        except Exception:
            pass
    return out


def release_tts_mute_hold() -> bool:
    """Cancel intentional TTS mute hold (user unmuted for a demo).

    Clears depth fully (B086) so listen no longer freezes on ``tts_mute_depth``.
    """
    result = force_clear_tts_mute_hold(reason="release_hold")
    return bool(result.get("cleared"))


def tts_mute_depth() -> int:
    with _lock:
        return _depth


def tts_mute_hold_active() -> bool:
    """True if depth or saved mute state is still held (B086 diagnostics)."""
    with _lock:
        return _depth > 0 or _saved is not None


def _unmute_after_tts(saved: MuteState, *, reason: str) -> None:
    """Restore capture after outermost TTS mute context exits."""
    if saved.source and _which("pactl"):
        set_source_mute(saved.source, False)
    # Always clear ALSA Wave path too — Pulse-only unmute left HW desynced (B086)
    set_alsa_mic_capture(True)
    try:
        from hark.syslog import log

        log(
            "mic.unmuted",
            component="audio",
            source=saved.source,
            reason=reason,
        )
    except Exception:
        pass


def repair_tts_mute_after_play(
    *,
    mute_was_enabled: bool,
    mute_applied: bool = False,
) -> dict[str, object]:
    """Post-``run_tts`` safety: depth must be 0; source unmuted if we muted.

    Logs ``mic.mute_desync`` when a repair was required. Safe to call when mute
    was disabled (no-op check for stuck depth from a prior turn).
    """
    report: dict[str, object] = {
        "depth": tts_mute_depth(),
        "repaired": False,
        "reasons": [],
    }
    reasons: list[str] = []

    if tts_mute_hold_active():
        force_clear_tts_mute_hold(reason="post_tts_depth")
        reasons.append("depth_nonzero")
        report["repaired"] = True

    # If we applied mute (or mute was requested), verify Pulse is not stuck muted.
    # Do not fight a user who was already muted before TTS (was_muted=True → not applied).
    if mute_was_enabled and (mute_applied or report["repaired"]):
        src = default_source()
        if src and _which("pactl"):
            muted = source_is_muted(src)
            if muted is True:
                set_source_mute(src, False)
                set_alsa_mic_capture(True)
                reasons.append("source_still_muted")
                report["repaired"] = True
                report["source"] = src

    report["reasons"] = reasons
    report["depth"] = tts_mute_depth()
    if report["repaired"]:
        try:
            from hark.syslog import log

            log(
                "mic.mute_desync",
                component="audio",
                reasons=reasons,
                mute_applied=mute_applied,
                depth=report["depth"],
                source=report.get("source"),
            )
        except Exception:
            pass
    return report


@contextmanager
def mic_muted_during_tts(*, enabled: bool = True) -> Iterator[MuteState]:
    """Nestable: mute default capture source while TTS plays; restore after.

    Only mutes if we are the first nested holder and the source was unmuted
    (or unknown). Always restores to pre-TTS state when the outermost exits,
    unless the user/hardware unmutes mid-hold (override / force-clear).
    """
    state = MuteState(source=None, was_muted=None, applied=False)
    if not enabled or not _which("pactl"):
        yield state
        return

    global _depth, _saved, _user_unmuted_override
    with _lock:
        _depth += 1
        if _depth == 1:
            _user_unmuted_override = False
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
        if state.applied:
            try:
                from hark.syslog import log

                log(
                    "mic.muted",
                    component="audio",
                    source=state.source,
                    was_muted=state.was_muted,
                )
            except Exception:
                pass
        yield state
    finally:
        with _lock:
            # force_clear may have already zeroed depth/saved — still safe
            if _depth > 0:
                _depth -= 1
            else:
                _depth = 0
            if _depth == 0 and _saved is not None:
                saved = _saved
                override = _user_unmuted_override
                _saved = None
                _user_unmuted_override = False
            else:
                saved = None
                override = False
        if saved is not None:
            if override:
                _unmute_after_tts(saved, reason="user_override")
            elif saved.applied and saved.source and saved.was_muted is not True:
                _unmute_after_tts(saved, reason="tts_end")
            elif saved.applied and saved.source:
                # was already muted before we entered: leave Pulse as-is, but
                # still clear ALSA if we somehow toggled it
                pass


# ---------------------------------------------------------------------------
# Mute sync watcher (hardware / ALSA unmute → OS unmute)
# ---------------------------------------------------------------------------

_watcher_lock = threading.Lock()
_watcher_thread: threading.Thread | None = None
_watcher_stop = threading.Event()


def _read_mute_snapshot() -> tuple[bool | None, bool | None]:
    """(pulse_muted, alsa_capture_on)."""
    src = default_source()
    pulse = source_is_muted(src) if src else None
    alsa_on = alsa_mic_capture_on()
    return pulse, alsa_on


def mute_sync_tick(
    prev_pulse: bool | None,
    prev_alsa_on: bool | None,
) -> tuple[bool | None, bool | None, bool]:
    """One poll step. Returns (pulse, alsa_on, did_sync)."""
    pulse, alsa_on = _read_mute_snapshot()
    did = False

    # Unmute edges only (not levels) so TTS hold is not cancelled spuriously
    alsa_edge = prev_alsa_on is False and alsa_on is True
    pulse_edge = prev_pulse is True and pulse is False
    if alsa_edge or pulse_edge:
        ensure_unmuted()
        did = True
        pulse, alsa_on = _read_mute_snapshot()

    return pulse, alsa_on, did


def start_mute_sync_watcher(
    *,
    poll_s: float = 0.15,
    enabled: bool = True,
) -> bool:
    """Start background thread (idempotent). Returns True if running/started."""
    global _watcher_thread
    if not enabled:
        return False
    with _watcher_lock:
        if _watcher_thread is not None and _watcher_thread.is_alive():
            return True
        _watcher_stop.clear()

        def _loop() -> None:
            pulse, alsa_on = _read_mute_snapshot()
            while not _watcher_stop.is_set():
                try:
                    pulse, alsa_on, did = mute_sync_tick(pulse, alsa_on)
                    if did:
                        try:
                            from hark.syslog import log

                            log(
                                "mic.sync",
                                component="audio",
                                pulse_muted=pulse,
                                alsa_capture_on=alsa_on,
                            )
                        except Exception:
                            pass
                except Exception:
                    pass
                _watcher_stop.wait(poll_s)

        _watcher_thread = threading.Thread(
            target=_loop, name="hark-mute-sync", daemon=True
        )
        _watcher_thread.start()
        try:
            from hark.syslog import log

            log("mic.sync_start", component="audio", poll_s=poll_s)
        except Exception:
            pass
        return True


def stop_mute_sync_watcher() -> None:
    global _watcher_thread
    with _watcher_lock:
        _watcher_stop.set()
        t = _watcher_thread
        _watcher_thread = None
    if t is not None:
        t.join(timeout=2.0)

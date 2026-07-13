"""Detect active video/voice conferences and hold TTS announcements.

When the operator is in Zoom/Teams/Meet/etc., speaking full blocked-agent
questions over speakers is disruptive. With ``hold_during_conference`` (default
on), TTS play paths:

  1. Detect conference (process list + optional PipeWire/Pulse stream names)
  2. Emit ``announce.held`` (syslog + HEP shape)
  3. Optionally play a soft chime (not the full question text)
  4. Queue the text and poll until the call ends
  5. Resume full TTS (``announce.resumed``)

Detection is fail-open by default: missing tools / unreadable ``/proc`` means
"not in conference" so Mode A still works on stripped environments.

Deep module: one public decision surface for callers (``is_conference_active``,
``apply_conference_hold``); internals stay private.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from hark.paths import state_dir
from hark.syslog import log as syslog

if TYPE_CHECKING:
    from hark.config import AudioConfig, HarkConfig

# Process basenames / comm fragments (case-insensitive substring match).
DEFAULT_PROCESS_NAMES: tuple[str, ...] = (
    "zoom",
    "zoom.us",
    "teams",
    "ms-teams",
    "teams-for-linux",
    "webex",
    "ciscowebexstart",
    "skypeforlinux",
    "skype",
    "discord",
    "slack",
)

# Substrings in /proc/*/cmdline (covers Chrome Meet tabs, browser Teams, etc.).
DEFAULT_CMDLINE_MARKERS: tuple[str, ...] = (
    "meet.google.com",
    "teams.microsoft.com",
    "teams.live.com",
    "zoom.us/j/",
    "zoommtg:",
    "webex.com",
)

# Substrings in pactl/pw stream application names.
DEFAULT_STREAM_MARKERS: tuple[str, ...] = (
    "zoom",
    "teams",
    "microsoft teams",
    "webex",
    "skype",
    "discord",
    "slack",
    "google chrome",  # weak alone; only with meet markers elsewhere
    "chromium",
    "firefox",
)

# Soft dual-tone conference-hold chime (Hz).
_CHIME_FREQS: tuple[float, float] = (523.25, 392.0)  # C5 → G4 soft drop
_CHIME_MS = 70
_DEFAULT_POLL_S = 2.0
_DEFAULT_MAX_HOLD_S = 0.0  # 0 = wait until free (no cap)


@dataclass(frozen=True)
class ConferenceMatch:
    """What triggered conference detection (for logs / tests)."""

    active: bool
    sources: tuple[str, ...] = ()
    matched: tuple[str, ...] = ()
    detail: str = ""
    error: str | None = None


@dataclass
class HoldResult:
    """Outcome of a conference hold attempt before TTS play."""

    held: bool
    skipped: bool = False  # policy=skip while conference active
    chime_played: bool = False
    wait_ms: int = 0
    matched: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    queue_id: str | None = None
    timed_out: bool = False
    event: dict[str, Any] | None = None

    def as_meta(self) -> dict[str, Any]:
        return {
            "held": self.held,
            "skipped": self.skipped,
            "chime_played": self.chime_played,
            "wait_ms": self.wait_ms,
            "matched": list(self.matched),
            "sources": list(self.sources),
            "queue_id": self.queue_id,
            "timed_out": self.timed_out,
        }


def _audio_cfg(cfg: "HarkConfig | AudioConfig | None") -> Any:
    if cfg is None:
        return None
    return getattr(cfg, "audio", cfg)


def process_names_from_config(cfg: "HarkConfig | AudioConfig | None") -> list[str]:
    audio = _audio_cfg(cfg)
    if audio is None:
        return list(DEFAULT_PROCESS_NAMES)
    names = getattr(audio, "conference_process_names", None)
    if names:
        return [str(n).strip().lower() for n in names if str(n).strip()]
    return list(DEFAULT_PROCESS_NAMES)


def fail_open_from_config(cfg: "HarkConfig | AudioConfig | None") -> bool:
    audio = _audio_cfg(cfg)
    if audio is None:
        return True
    return bool(getattr(audio, "conference_fail_open", True))


def hold_enabled(cfg: "HarkConfig | AudioConfig | None") -> bool:
    audio = _audio_cfg(cfg)
    if audio is None:
        return True
    return bool(getattr(audio, "hold_during_conference", True))


def chime_only_from_config(cfg: "HarkConfig | AudioConfig | None") -> bool:
    audio = _audio_cfg(cfg)
    if audio is None:
        return True
    return bool(getattr(audio, "conference_chime_only", True))


def poll_s_from_config(cfg: "HarkConfig | AudioConfig | None") -> float:
    audio = _audio_cfg(cfg)
    if audio is None:
        return _DEFAULT_POLL_S
    ms = getattr(audio, "conference_poll_ms", None)
    if ms is None:
        return _DEFAULT_POLL_S
    return max(0.2, float(ms) / 1000.0)


def max_hold_s_from_config(cfg: "HarkConfig | AudioConfig | None") -> float:
    audio = _audio_cfg(cfg)
    if audio is None:
        return _DEFAULT_MAX_HOLD_S
    return max(0.0, float(getattr(audio, "conference_max_hold_s", _DEFAULT_MAX_HOLD_S)))


# ---------------------------------------------------------------------------
# Process / stream scanners (injectable for tests)
# ---------------------------------------------------------------------------


def iter_proc_entries(proc_root: Path | str = "/proc") -> list[tuple[str, str, str]]:
    """Return list of (pid, comm, cmdline) for readable PIDs.

    cmdline uses spaces instead of NULs. Best-effort; never raises.
    """
    root = Path(proc_root)
    out: list[tuple[str, str, str]] = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return out
    for entry in entries:
        name = entry.name
        if not name.isdigit():
            continue
        comm = ""
        cmdline = ""
        try:
            comm = (entry / "comm").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        try:
            raw = (entry / "cmdline").read_bytes()
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        except OSError:
            pass
        if comm or cmdline:
            out.append((name, comm, cmdline))
    return out


def _match_process_list(
    entries: Iterable[tuple[str, str, str]],
    process_names: Iterable[str],
    cmdline_markers: Iterable[str] = DEFAULT_CMDLINE_MARKERS,
) -> list[str]:
    names = [n.lower() for n in process_names if n]
    markers = [m.lower() for m in cmdline_markers if m]
    hits: list[str] = []
    seen: set[str] = set()
    for _pid, comm, cmdline in entries:
        comm_l = (comm or "").lower()
        cmd_l = (cmdline or "").lower()
        exe0 = ""
        if cmd_l.split():
            exe0 = Path(cmd_l.split()[0]).name.lower()
        for needle in names:
            if not needle:
                continue
            if (
                needle in comm_l
                or needle == exe0
                or needle in exe0
                or (needle in cmd_l)
            ):
                key = f"proc:{needle}"
                if key not in seen:
                    seen.add(key)
                    hits.append(key)
        for marker in markers:
            if marker and marker in cmd_l:
                key = f"cmdline:{marker}"
                if key not in seen:
                    seen.add(key)
                    hits.append(key)
    return hits


def _run_capture(args: list[str], *, timeout: float = 3.0) -> str:
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if p.returncode != 0:
        return (p.stdout or "") + (p.stderr or "")
    return p.stdout or ""


def list_audio_stream_blobs() -> list[str]:
    """Collect stream/application name text from pactl or pw-cli (best-effort)."""
    blobs: list[str] = []
    from shutil import which

    if which("pactl"):
        for cmd in (
            ["pactl", "list", "sink-inputs"],
            ["pactl", "list", "source-outputs"],
            ["pactl", "list", "short", "sink-inputs"],
            ["pactl", "list", "short", "source-outputs"],
        ):
            text = _run_capture(cmd)
            if text.strip():
                blobs.append(text)
    if which("pw-cli"):
        text = _run_capture(["pw-cli", "ls", "Node"])
        if text.strip():
            blobs.append(text)
        # info dump can be large; keep short list only if ls empty
    return blobs


def _match_stream_blobs(
    blobs: Iterable[str],
    markers: Iterable[str] = DEFAULT_STREAM_MARKERS,
    process_names: Iterable[str] = DEFAULT_PROCESS_NAMES,
) -> list[str]:
    """Match conference markers in audio stream dumps.

    Browser-only markers (chrome/chromium/firefox) are weak alone — require a
    stronger conference marker elsewhere in the same blob, or skip them for
    stream-only hits unless paired with meet/teams/zoom/etc.
    """
    weak = {"google chrome", "chromium", "firefox", "chrome"}
    strong = [m.lower() for m in list(markers) + list(process_names) if m.lower() not in weak]
    weak_list = [m.lower() for m in markers if m.lower() in weak]
    hits: list[str] = []
    seen: set[str] = set()
    for blob in blobs:
        low = (blob or "").lower()
        if not low.strip():
            continue
        strong_hit = False
        for marker in strong:
            if marker and marker in low:
                strong_hit = True
                key = f"stream:{marker}"
                if key not in seen:
                    seen.add(key)
                    hits.append(key)
        if strong_hit:
            for marker in weak_list:
                if marker and marker in low:
                    key = f"stream:{marker}"
                    if key not in seen:
                        seen.add(key)
                        hits.append(key)
    return hits


def detect_conference(
    *,
    process_names: Iterable[str] | None = None,
    proc_entries: list[tuple[str, str, str]] | None = None,
    stream_blobs: list[str] | None = None,
    check_audio: bool = True,
    fail_open: bool = True,
    proc_root: Path | str = "/proc",
) -> ConferenceMatch:
    """Inspect processes (and optionally audio streams) for conference apps.

    Parameters allow full mocking in tests (no real Zoom).
    """
    names = list(process_names) if process_names is not None else list(DEFAULT_PROCESS_NAMES)
    sources: list[str] = []
    matched: list[str] = []
    errors: list[str] = []

    # Processes
    try:
        entries = (
            proc_entries
            if proc_entries is not None
            else iter_proc_entries(proc_root)
        )
        if proc_entries is None and not Path(proc_root).is_dir():
            errors.append(f"proc_root missing: {proc_root}")
        else:
            sources.append("process")
            matched.extend(_match_process_list(entries, names))
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"process scan: {exc}")

    # Audio streams
    if check_audio:
        try:
            blobs = stream_blobs if stream_blobs is not None else list_audio_stream_blobs()
            if blobs:
                sources.append("audio")
                matched.extend(
                    _match_stream_blobs(blobs, process_names=names)
                )
        except Exception as exc:  # pragma: no cover
            errors.append(f"audio scan: {exc}")

    # Deduplicate matched preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for m in matched:
        if m not in seen:
            seen.add(m)
            uniq.append(m)

    if uniq:
        return ConferenceMatch(
            active=True,
            sources=tuple(sources),
            matched=tuple(uniq),
            detail="conference signals present",
        )

    if errors and not sources:
        # No successful detection path
        if fail_open:
            return ConferenceMatch(
                active=False,
                sources=(),
                matched=(),
                detail="detection failed (fail-open → free)",
                error="; ".join(errors),
            )
        # fail-closed: treat as active so we do not barge into unknown state
        return ConferenceMatch(
            active=True,
            sources=(),
            matched=(),
            detail="detection failed (fail-closed → hold)",
            error="; ".join(errors),
        )

    return ConferenceMatch(
        active=False,
        sources=tuple(sources),
        matched=(),
        detail="no conference signals",
        error="; ".join(errors) if errors else None,
    )


def is_conference_active(
    cfg: "HarkConfig | AudioConfig | None" = None,
    *,
    process_names: Iterable[str] | None = None,
    proc_entries: list[tuple[str, str, str]] | None = None,
    stream_blobs: list[str] | None = None,
    check_audio: bool | None = None,
    fail_open: bool | None = None,
    detect: Callable[..., ConferenceMatch] | None = None,
) -> bool:
    """Return True when a known conference app appears active."""
    names = (
        list(process_names)
        if process_names is not None
        else process_names_from_config(cfg)
    )
    fo = fail_open_from_config(cfg) if fail_open is None else fail_open
    audio = _audio_cfg(cfg)
    if check_audio is None:
        check_audio = bool(getattr(audio, "conference_check_audio", True)) if audio else True
    detector = detect or detect_conference
    match = detector(
        process_names=names,
        proc_entries=proc_entries,
        stream_blobs=stream_blobs,
        check_audio=check_audio,
        fail_open=fo,
    )
    return match.active


# ---------------------------------------------------------------------------
# Queue + hold
# ---------------------------------------------------------------------------


def announce_queue_path() -> Path:
    return state_dir() / "announce_hold_queue.jsonl"


def enqueue_held_announcement(
    text: str,
    *,
    matched: list[str] | None = None,
    sources: list[str] | None = None,
    queue_id: str | None = None,
) -> str:
    """Append a held announcement record; return queue id."""
    qid = queue_id or uuid.uuid4().hex[:12]
    path = announce_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": qid,
        "ts": time.time(),
        "status": "held",
        "text": text,
        "matched": matched or [],
        "sources": sources or [],
        "pid": os.getpid(),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return qid


def mark_queue_status(queue_id: str, status: str, **extra: Any) -> None:
    path = announce_queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "id": queue_id,
        "ts": time.time(),
        "status": status,
        "pid": os.getpid(),
        **extra,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")


def make_announce_held_event(
    text: str,
    *,
    matched: list[str] | None = None,
    sources: list[str] | None = None,
    chime: bool = False,
    queue_id: str | None = None,
) -> dict[str, Any]:
    from hark.events import new_event_id, utc_now_iso
    from hark import __schema__

    preview = (text or "").strip()
    if len(preview) > 160:
        preview = preview[:157] + "…"
    return {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": "announce.held",
        "priority": 55,
        "disposition": "held",
        "reason": "conference",
        "matched": list(matched or []),
        "sources": list(sources or []),
        "chime": chime,
        "queue_id": queue_id,
        "text_preview": preview or None,
        "instructions": (
            "Announcement held while a conference app is active. "
            "Full TTS resumes when the call ends (or conference_hold is disabled)."
        ),
    }


def make_announce_resumed_event(
    text: str,
    *,
    queue_id: str | None = None,
    wait_ms: int = 0,
) -> dict[str, Any]:
    from hark.events import new_event_id, utc_now_iso
    from hark import __schema__

    preview = (text or "").strip()
    if len(preview) > 160:
        preview = preview[:157] + "…"
    return {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": "announce.resumed",
        "priority": 50,
        "disposition": "info",
        "reason": "conference_ended",
        "queue_id": queue_id,
        "wait_ms": wait_ms,
        "text_preview": preview or None,
    }


def play_conference_chime(*, volume: float | None = None) -> bool:
    """Play a soft dual-tone chime. Returns True if playback attempted ok."""
    try:
        from hark.audio.cues import build_beep
        from hark.audio.playback import play_audio

        # Quiet soft cue — do not barge into a call with full volume beeps
        vol = 0.14 if volume is None else max(0.0, min(1.0, float(volume)))
        play_audio(build_beep(_CHIME_FREQS[0], ms=_CHIME_MS, vol=vol))
        time.sleep(0.04)
        play_audio(build_beep(_CHIME_FREQS[1], ms=_CHIME_MS + 15, vol=vol * 0.9))
        syslog(
            "announce.chime",
            component="conference",
            level="info",
            volume=vol,
        )
        return True
    except Exception as exc:
        syslog(
            "announce.chime_error",
            component="conference",
            level="warn",
            error=str(exc)[:120],
        )
        return False


def wait_until_conference_free(
    cfg: "HarkConfig | AudioConfig | None" = None,
    *,
    is_active: Callable[[], bool] | None = None,
    poll_s: float | None = None,
    max_hold_s: float | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[bool, int]:
    """Poll until conference inactive.

    Returns (became_free, wait_ms). If max_hold_s elapses first, became_free
    is False (timed out while still active).
    """
    poll = poll_s if poll_s is not None else poll_s_from_config(cfg)
    cap = max_hold_s if max_hold_s is not None else max_hold_s_from_config(cfg)
    check = is_active or (lambda: is_conference_active(cfg))
    t0 = monotonic()
    while True:
        if not check():
            return True, int(1000 * (monotonic() - t0))
        elapsed = monotonic() - t0
        if cap > 0 and elapsed >= cap:
            return False, int(1000 * elapsed)
        sleep_fn(poll)


def _default_detect(cfg: "HarkConfig | AudioConfig | None") -> ConferenceMatch:
    audio = _audio_cfg(cfg)
    check_audio = bool(getattr(audio, "conference_check_audio", True)) if audio else True
    return detect_conference(
        process_names=process_names_from_config(cfg),
        check_audio=check_audio,
        fail_open=fail_open_from_config(cfg),
    )


def apply_conference_hold(
    cfg: "HarkConfig | AudioConfig | None",
    text: str,
    *,
    policy: str = "hold",
    detect_match: Callable[[], ConferenceMatch] | ConferenceMatch | None = None,
    is_active: Callable[[], bool] | None = None,
    play_chime: Callable[[], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> HoldResult:
    """Decide hold/skip/force for an announcement about to be spoken.

    Policies:
      - ``hold``: if conference active, chime (optional), queue, wait until free
      - ``skip``: if conference active, do not speak (lifecycle cues)
      - ``force``: never hold

    When hold is disabled in config, behaves like force.
    """
    policy = (policy or "hold").lower().strip()
    if policy == "force" or not hold_enabled(cfg):
        return HoldResult(held=False)

    if isinstance(detect_match, ConferenceMatch):
        match = detect_match
    elif callable(detect_match):
        match = detect_match()
    else:
        match = _default_detect(cfg)

    if not match.active:
        return HoldResult(
            held=False,
            matched=list(match.matched),
            sources=list(match.sources),
        )

    if policy == "skip":
        syslog(
            "announce.skipped",
            component="conference",
            level="info",
            message="lifecycle TTS skipped during conference",
            matched=list(match.matched),
            sources=list(match.sources),
        )
        return HoldResult(
            held=True,
            skipped=True,
            matched=list(match.matched),
            sources=list(match.sources),
        )

    # policy == hold
    do_chime = chime_only_from_config(cfg)
    qid = enqueue_held_announcement(
        text,
        matched=list(match.matched),
        sources=list(match.sources),
    )
    chime_ok = False
    if do_chime:
        chime_fn = play_chime or play_conference_chime
        chime_ok = bool(chime_fn())

    held_event = make_announce_held_event(
        text,
        matched=list(match.matched),
        sources=list(match.sources),
        chime=chime_ok,
        queue_id=qid,
    )
    syslog(
        "announce.held",
        component="conference",
        level="info",
        message="TTS held during conference",
        matched=list(match.matched),
        sources=list(match.sources),
        queue_id=qid,
        chime=chime_ok,
        text_preview=held_event.get("text_preview"),
        event_id=held_event.get("event_id"),
    )

    def _active() -> bool:
        if is_active is not None:
            return is_active()
        return _default_detect(cfg).active

    free, wait_ms = wait_until_conference_free(
        cfg,
        is_active=_active,
        sleep_fn=sleep_fn,
        monotonic=monotonic,
    )

    if free:
        mark_queue_status(qid, "resumed", wait_ms=wait_ms)
        resumed = make_announce_resumed_event(text, queue_id=qid, wait_ms=wait_ms)
        syslog(
            "announce.resumed",
            component="conference",
            level="info",
            message="conference ended; resuming TTS",
            queue_id=qid,
            wait_ms=wait_ms,
            event_id=resumed.get("event_id"),
        )
        return HoldResult(
            held=True,
            skipped=False,
            chime_played=chime_ok,
            wait_ms=wait_ms,
            matched=list(match.matched),
            sources=list(match.sources),
            queue_id=qid,
            timed_out=False,
            event=held_event,
        )

    # Timed out still in conference — speak anyway so Mode A is not stuck forever
    mark_queue_status(qid, "timeout_resume", wait_ms=wait_ms)
    syslog(
        "announce.hold_timeout",
        component="conference",
        level="warn",
        message="max hold elapsed; speaking despite conference",
        queue_id=qid,
        wait_ms=wait_ms,
    )
    return HoldResult(
        held=True,
        skipped=False,
        chime_played=chime_ok,
        wait_ms=wait_ms,
        matched=list(match.matched),
        sources=list(match.sources),
        queue_id=qid,
        timed_out=True,
        event=held_event,
    )


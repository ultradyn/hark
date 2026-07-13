"""Detect active non-Hark media playback (Pulse/PipeWire + optional MPRIS).

Foundation for I002 ducking (B044): **detection only** — no volume changes.

Precedence for callers (B045/B046):

  conference active + hold_during_conference?
    yes → B017 hold / chime / queue (no media duck fight)
    no  → media ducking if enabled and duckable sink-inputs present

Fail-open: missing ``pactl`` / parse errors → media inactive (TTS/STT as today).
Conference streams may appear in the match; conference hold remains first-class
for callers — do not duck *instead of* holding.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from shutil import which
from typing import TYPE_CHECKING, Any, Callable, Iterable

if TYPE_CHECKING:
    from hark.config import AudioConfig, HarkConfig

# Sink-input block header: "Sink Input #12" or "Sink Input #2772116"
_SINK_INPUT_HEADER = re.compile(r"^Sink Input #(\d+)\s*$", re.MULTILINE)

# Volume line samples:
#   Volume: mono: 65536 / 100% / 0.00 dB
#   Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50% / ...
_VOLUME_RAW = re.compile(r"Volume:\s*\S+:\s*(\d+)", re.IGNORECASE)
_VOLUME_PCT = re.compile(r"/\s*(\d+(?:\.\d+)?)\s*%")

# Property lines inside a block (indented key = "value")
_PROP_LINE = re.compile(
    r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"([^"]*)"\s*$',
    re.MULTILINE,
)
_FIELD_LINE = re.compile(
    r"^\s*(Corked|Mute|State|Volume)\s*:\s*(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Hark-owned playback markers (application.name / binary / media.name).
DEFAULT_HARK_OWNED_MARKERS: tuple[str, ...] = (
    "ffplay",
    "paplay",
    "pacat",
    "ffmpeg",
    "hark",
    "sounddevice",
)

# Conference app markers — still *detectable*, but helpers can exclude from
# duckable lists so B017 hold stays authoritative (see I002 plan).
DEFAULT_CONFERENCE_MARKERS: tuple[str, ...] = (
    "zoom",
    "teams",
    "microsoft teams",
    "webex",
    "skype",
    "discord",
    "slack",
)


@dataclass(frozen=True)
class SinkInputInfo:
    """One Pulse/PipeWire sink-input (playback stream)."""

    index: int
    volume_pct: float = 100.0
    volume_raw: str = "65536"  # first channel integer for restore
    mute: bool = False
    corked: bool = False
    state: str | None = None  # RUNNING / CORKED / … when present
    application_name: str = ""
    media_name: str = ""
    binary: str = ""
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def is_playing(self) -> bool:
        """True when the stream looks like active (uncorked, unmuted) playback.

        PipeWire often omits ``State:``; Corked=no is the reliable signal.
        When State is present, prefer RUNNING (or equivalent live states).
        """
        if self.mute or self.corked:
            return False
        if self.state:
            st = self.state.strip().upper()
            if st in {"CORKED", "IDLE", "SUSPENDED", "DRAINED", "TERMINATED"}:
                return False
            if st in {"RUNNING", "STARTED", "PLAYING"}:
                return True
            # Unknown state with corked=no → treat as playing (fail toward detect)
        return True

    def identity_blob(self) -> str:
        parts = [
            self.application_name,
            self.media_name,
            self.binary,
            self.properties.get("node.name", ""),
            self.properties.get("application.process.binary", ""),
        ]
        return " ".join(p for p in parts if p).lower()


@dataclass(frozen=True)
class MediaMatch:
    """Structured result of media-active detection (logs / duck helpers / tests)."""

    active: bool
    sources: tuple[str, ...] = ()
    indices: tuple[int, ...] = ()
    app_names: tuple[str, ...] = ()
    volumes: tuple[float, ...] = ()  # percent (0–100+)
    volume_raw: tuple[str, ...] = ()  # first-channel Pulse integers
    mpris_players: tuple[str, ...] = ()
    sink_inputs: tuple[SinkInputInfo, ...] = ()
    detail: str = ""
    error: str | None = None

    def as_meta(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "sources": list(self.sources),
            "indices": list(self.indices),
            "app_names": list(self.app_names),
            "volumes": list(self.volumes),
            "volume_raw": list(self.volume_raw),
            "mpris_players": list(self.mpris_players),
            "detail": self.detail,
            "error": self.error,
        }


def _audio_cfg(cfg: "HarkConfig | AudioConfig | None") -> Any:
    if cfg is None:
        return None
    return getattr(cfg, "audio", cfg)


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
        # Some tools write useful text only on stderr; still prefer stdout.
        return (p.stdout or "") or (p.stderr or "")
    return p.stdout or ""


def list_sink_input_blob(*, run_capture: Callable[[list[str]], str] | None = None) -> str:
    """Return ``pactl list sink-inputs`` text, or empty when unavailable."""
    capture = run_capture or (lambda args: _run_capture(args))
    if run_capture is None and not which("pactl"):
        return ""
    return capture(["pactl", "list", "sink-inputs"])


def parse_sink_inputs(blob: str) -> list[SinkInputInfo]:
    """Parse ``pactl list sink-inputs`` into structured rows. Never raises."""
    if not (blob or "").strip():
        return []
    # Split on headers while keeping indices
    headers = list(_SINK_INPUT_HEADER.finditer(blob))
    if not headers:
        return []
    out: list[SinkInputInfo] = []
    for i, m in enumerate(headers):
        try:
            index = int(m.group(1))
        except ValueError:
            continue
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(blob)
        block = blob[start:end]
        out.append(_parse_sink_input_block(index, block))
    return out


def _parse_sink_input_block(index: int, block: str) -> SinkInputInfo:
    corked = False
    mute = False
    state: str | None = None
    volume_pct = 100.0
    volume_raw = "65536"

    for fm in _FIELD_LINE.finditer(block):
        key = fm.group(1).strip().lower()
        val = fm.group(2).strip()
        if key == "corked":
            corked = val.lower() in {"yes", "true", "1"}
        elif key == "mute":
            mute = val.lower() in {"yes", "true", "1"}
        elif key == "state":
            state = val.strip()
        elif key == "volume":
            raw_m = _VOLUME_RAW.search(fm.group(0))
            if raw_m:
                volume_raw = raw_m.group(1)
            # Prefer first percent on the volume line
            pct_m = _VOLUME_PCT.search(fm.group(0))
            if pct_m:
                try:
                    volume_pct = float(pct_m.group(1))
                except ValueError:
                    pass

    # Some dumps put multi-line volume; scan whole block for volume if needed
    if volume_raw == "65536":
        raw_m = _VOLUME_RAW.search(block)
        if raw_m:
            volume_raw = raw_m.group(1)
    if volume_pct == 100.0:
        pct_m = _VOLUME_PCT.search(block)
        if pct_m:
            try:
                volume_pct = float(pct_m.group(1))
            except ValueError:
                pass

    props: dict[str, str] = {}
    for pm in _PROP_LINE.finditer(block):
        props[pm.group(1)] = pm.group(2)

    app = props.get("application.name", "") or props.get("application.process.binary", "")
    media = props.get("media.name", "")
    binary = props.get("application.process.binary", "") or props.get("node.name", "")

    return SinkInputInfo(
        index=index,
        volume_pct=volume_pct,
        volume_raw=volume_raw,
        mute=mute,
        corked=corked,
        state=state,
        application_name=app,
        media_name=media,
        binary=binary,
        properties=props,
    )


def _matches_any_marker(text: str, markers: Iterable[str]) -> bool:
    low = (text or "").lower()
    if not low.strip():
        return False
    for m in markers:
        needle = (m or "").strip().lower()
        if needle and needle in low:
            return True
    return False


def is_hark_owned(
    info: SinkInputInfo,
    *,
    markers: Iterable[str] = DEFAULT_HARK_OWNED_MARKERS,
) -> bool:
    """True when the stream is attributable to Hark TTS/cues (exclude from duck)."""
    return _matches_any_marker(info.identity_blob(), markers)


def is_conference_stream(
    info: SinkInputInfo,
    *,
    markers: Iterable[str] = DEFAULT_CONFERENCE_MARKERS,
) -> bool:
    """True when the stream looks like a B017 conference app."""
    return _matches_any_marker(info.identity_blob(), markers)


def filter_duckable(
    inputs: Iterable[SinkInputInfo],
    *,
    exclude_hark: bool = True,
    exclude_conference: bool = False,
    hark_markers: Iterable[str] = DEFAULT_HARK_OWNED_MARKERS,
    conference_markers: Iterable[str] = DEFAULT_CONFERENCE_MARKERS,
    exclude_apps: Iterable[str] | None = None,
    require_playing: bool = True,
) -> list[SinkInputInfo]:
    """Filter sink-inputs suitable for volume ducking (B045/B046).

    By default keeps playing, unmuted, non-Hark streams. Conference streams
    remain included unless ``exclude_conference=True`` — callers that already
    applied B017 hold should exclude them when building a duck list.
    """
    extra = [str(a).strip().lower() for a in (exclude_apps or []) if str(a).strip()]
    out: list[SinkInputInfo] = []
    for info in inputs:
        if require_playing and not info.is_playing:
            continue
        if exclude_hark and is_hark_owned(info, markers=hark_markers):
            continue
        if exclude_conference and is_conference_stream(
            info, markers=conference_markers
        ):
            continue
        if extra and _matches_any_marker(info.identity_blob(), extra):
            continue
        out.append(info)
    return out


def duckable_indices_and_volumes(
    inputs: Iterable[SinkInputInfo] | None = None,
    *,
    blob: str | None = None,
    exclude_conference: bool = False,
    exclude_apps: Iterable[str] | None = None,
) -> list[tuple[int, float, str]]:
    """Return ``[(index, volume_pct, volume_raw), ...]`` for duckable streams.

    Helper for B045/B046: snapshot prior volumes before
    ``pactl set-sink-input-volume``.
    """
    if inputs is None:
        parsed = parse_sink_inputs(blob if blob is not None else list_sink_input_blob())
    else:
        parsed = list(inputs)
    duckable = filter_duckable(
        parsed,
        exclude_conference=exclude_conference,
        exclude_apps=exclude_apps,
    )
    return [(s.index, s.volume_pct, s.volume_raw) for s in duckable]


def probe_mpris_playing(
    *,
    run_capture: Callable[[list[str]], str] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
) -> list[str]:
    """Best-effort MPRIS players with PlaybackStatus=Playing via ``playerctl``.

    Returns player names (may be empty). Never raises; missing tool → [].
    """
    capture = run_capture or (lambda args: _run_capture(args))
    # Explicit which_fn=None-returning means "tool missing" (tests); otherwise
    # only skip when we would shell out without an injected capturer.
    if which_fn is not None:
        if not which_fn("playerctl"):
            return []
    elif run_capture is None and not which("playerctl"):
        return []

    # Prefer a single formatted pass over all players.
    text = capture(
        [
            "playerctl",
            "-a",
            "metadata",
            "--format",
            "{{playerName}}|{{status}}",
        ]
    )
    players: list[str] = []
    seen: set[str] = set()
    if text.strip():
        for line in text.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            name, status = line.rsplit("|", 1)
            name = name.strip()
            if status.strip().lower() == "playing" and name and name not in seen:
                seen.add(name)
                players.append(name)
        if players:
            return players

    # Fallback: list players + per-player status
    listing = capture(["playerctl", "-l"])
    if not listing.strip():
        # Single default player
        st = capture(["playerctl", "status"]).strip().lower()
        if st == "playing":
            return ["default"]
        return []
    for name in listing.splitlines():
        name = name.strip()
        if not name:
            continue
        st = capture(["playerctl", "-p", name, "status"]).strip().lower()
        if st == "playing" and name not in seen:
            seen.add(name)
            players.append(name)
    return players


def detect_media(
    *,
    sink_input_blob: str | None = None,
    sink_inputs: list[SinkInputInfo] | None = None,
    mpris_players: list[str] | None = None,
    check_mpris: bool = True,
    exclude_hark: bool = True,
    hark_markers: Iterable[str] = DEFAULT_HARK_OWNED_MARKERS,
    fail_open: bool = True,
    run_capture: Callable[[list[str]], str] | None = None,
) -> MediaMatch:
    """Inspect sink-inputs (and optionally MPRIS) for active media.

    Parameters allow full mocking in tests (no real Spotify).
    Fail-open: tool/parse failure with no data → ``active=False``.
    """
    sources: list[str] = []
    errors: list[str] = []
    parsed: list[SinkInputInfo] = []

    try:
        if sink_inputs is not None:
            parsed = list(sink_inputs)
            sources.append("sink-input")
        else:
            blob = (
                sink_input_blob
                if sink_input_blob is not None
                else list_sink_input_blob(run_capture=run_capture)
            )
            if sink_input_blob is None and not blob.strip():
                # No pactl or empty — not an error by itself
                if run_capture is None and not which("pactl"):
                    errors.append("pactl missing")
            else:
                sources.append("sink-input")
                parsed = parse_sink_inputs(blob)
    except Exception as exc:  # pragma: no cover - defensive
        errors.append(f"sink-input scan: {exc}")

    playing = filter_duckable(
        parsed,
        exclude_hark=exclude_hark,
        exclude_conference=False,
        hark_markers=hark_markers,
        require_playing=True,
    )

    mpris: list[str] = []
    if check_mpris:
        try:
            if mpris_players is not None:
                mpris = [p for p in mpris_players if p]
                if mpris:
                    sources.append("mpris")
            else:
                mpris = probe_mpris_playing(run_capture=run_capture)
                if mpris:
                    sources.append("mpris")
        except Exception as exc:  # pragma: no cover
            errors.append(f"mpris scan: {exc}")

    indices = tuple(s.index for s in playing)
    app_names = tuple(
        (s.application_name or s.binary or f"sink-input-{s.index}") for s in playing
    )
    volumes = tuple(s.volume_pct for s in playing)
    volume_raw = tuple(s.volume_raw for s in playing)

    active = bool(playing) or bool(mpris)
    if active:
        detail_bits = []
        if playing:
            detail_bits.append(f"{len(playing)} sink-input(s) playing")
        if mpris:
            detail_bits.append(f"mpris: {', '.join(mpris)}")
        return MediaMatch(
            active=True,
            sources=tuple(dict.fromkeys(sources)),
            indices=indices,
            app_names=app_names,
            volumes=volumes,
            volume_raw=volume_raw,
            mpris_players=tuple(mpris),
            sink_inputs=tuple(playing),
            detail="; ".join(detail_bits),
        )

    if errors and not sources and fail_open:
        return MediaMatch(
            active=False,
            sources=(),
            detail="detection failed (fail-open → inactive)",
            error="; ".join(errors),
        )

    return MediaMatch(
        active=False,
        sources=tuple(dict.fromkeys(sources)),
        detail="no active media",
        error="; ".join(errors) if errors else None,
    )


def is_media_active(
    cfg: "HarkConfig | AudioConfig | None" = None,
    *,
    sink_input_blob: str | None = None,
    sink_inputs: list[SinkInputInfo] | None = None,
    mpris_players: list[str] | None = None,
    check_mpris: bool | None = None,
    exclude_hark: bool = True,
    fail_open: bool = True,
    detect: Callable[..., MediaMatch] | None = None,
    run_capture: Callable[[list[str]], str] | None = None,
) -> MediaMatch:
    """Return a :class:`MediaMatch` describing active non-Hark media.

    Unlike ``is_conference_active`` (bool), this returns the full structured
    match so duck helpers can reuse indices/volumes without a second scan.

    Config is optional today (B047 will add duck toggles). ``cfg`` is accepted
    for forward compatibility; ``getattr`` defaults apply when fields missing.
    """
    audio = _audio_cfg(cfg)
    if check_mpris is None:
        check_mpris = bool(getattr(audio, "media_check_mpris", True)) if audio else True
    # Optional future exclude list
    exclude_apps = None
    if audio is not None:
        raw = getattr(audio, "duck_exclude_apps", None)
        if raw:
            exclude_apps = list(raw)

    detector = detect or detect_media
    match = detector(
        sink_input_blob=sink_input_blob,
        sink_inputs=sink_inputs,
        mpris_players=mpris_players,
        check_mpris=check_mpris,
        exclude_hark=exclude_hark,
        fail_open=fail_open,
        run_capture=run_capture,
    )
    if exclude_apps and match.sink_inputs:
        # Re-filter if config excludes extra apps
        filtered = filter_duckable(
            match.sink_inputs,
            exclude_hark=False,  # already excluded
            exclude_apps=exclude_apps,
            require_playing=True,
        )
        if len(filtered) != len(match.sink_inputs) or not filtered:
            mpris = match.mpris_players
            active = bool(filtered) or bool(mpris)
            return MediaMatch(
                active=active,
                sources=match.sources if active else match.sources,
                indices=tuple(s.index for s in filtered),
                app_names=tuple(
                    (s.application_name or s.binary or f"sink-input-{s.index}")
                    for s in filtered
                ),
                volumes=tuple(s.volume_pct for s in filtered),
                volume_raw=tuple(s.volume_raw for s in filtered),
                mpris_players=mpris,
                sink_inputs=tuple(filtered),
                detail=match.detail,
                error=match.error,
            )
    return match

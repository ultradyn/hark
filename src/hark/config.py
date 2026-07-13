"""Load and merge Hark config (TOML + env)."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hark.listen_end import (
    DEFAULT_CANCEL_PHRASES,
    DEFAULT_END_PHRASES,
    DEFAULT_SOFT_END_PHRASES,
    DEFAULT_SOFT_END_PHRASES_ENABLED,
    EndMode,
    parse_end_mode,
)
from hark.paths import default_config_path, default_herdr_socket
from hark.wake import (
    DEFAULT_ACTIVATION_PHRASES,
    DEFAULT_WAKE_MODE,
    DEFAULT_WAKE_NAMES,
    WakePolicy,
)
from hark.wake_learn import load_learned


KNOWN_TOP_KEYS = frozenset(
    {
        "version",
        "herdr",
        "watch",
        "audio",
        "listen",
        "ambient",
        "stt",
        "tts",
        "confirm",
        "safety",
    }
)

KNOWN_SECTION_KEYS: dict[str, frozenset[str]] = {
    "herdr": frozenset({"sessions"}),
    "watch": frozenset({
        "statuses",
        "debounce_ms",
        "transport",
        "poll_ms",
        "heartbeat_s",
        "detect_false_done",
    }),
    "audio": frozenset({
        "half_duplex",
        "post_tts_guard_ms",
        "listen_pre_arm_ms",
        "overlap_prearm",
        "overlap_discard_ms",
        "mute_mic_during_tts",
        "sync_hw_unmute",
        "cue_volume",
        "cue_start_path",
        "cue_stop_path",
        # Conference hold (B017): pause full TTS while Zoom/Teams/Meet is active
        "hold_during_conference",
        "conference_chime_only",
        "conference_process_names",
        "conference_fail_open",
        "conference_check_audio",
        "conference_poll_ms",
        "conference_max_hold_s",
        # Media ducking during TTS (B045 / I002); STT duck is B046
        "duck_media_during_tts",
        "pause_media_during_tts",
        "duck_level",
        "duck_exclude_apps",
        "media_check_mpris",
    }),
    "listen": frozenset({
        "end_mode",
        "end_phrases",
        "cancel_phrases",
        "strip_phrase",
        "max_listen_s",
        "nudge_silence_s",
        "end_silence_s",
        "radio_end_silence_s",
        "radio_partial_silence_s",
        "stream_partials",
        "empty_stt_retry",
        "empty_stt_nudge",
        # Energy-gate open (B031) — softer abs floor so quiet post-wake speech opens
        "abs_open_db",
        "open_margin_db",
        "initial_timeout_s",
        "no_open_retry",
        "no_open_nudge",
        # Optional informal closers (default on) — see listen_end.DEFAULT_SOFT_END_PHRASES
        "soft_end_phrases_enabled",
        "soft_end_phrases",
        # Pluggable endpointing (B007) — energy gate default + optional smart turn
        "endpoint_strategy",
        "endpoint_probe_silence_s",
        "endpoint_max_silence_s",
        "smart_turn_model_path",
        "smart_turn_threshold",
    }),
    "ambient": frozenset({
        "enabled",
        # Wake customization: names (default) or full phrases
        "wake_mode",  # "names" | "phrases"
        "activation_mode",  # alias of wake_mode
        "names",
        "activation_names",  # alias of names
        "wake_names",  # alias of names
        "extra_names",
        "learn_from_near_misses",  # default true
        "activation_phrases",
        "trigger_phrases",  # alias of activation_phrases
        "extra_activation_phrases",  # append to defaults (or to base list)
        "extra_trigger_phrases",  # alias of extra_activation_phrases
        "engine",
        "model_path",
        "snippet_s",
        "timeout_s",
        # Continuous idle ambient.timeout heartbeat (default on)
        "surface_timeouts",
        "emit_timeout_events",  # alias of surface_timeouts
        "debug",
        "debug_retention_days",
        # Post-wake listen (B031)
        "post_wake_lead_in_ms",
        "post_wake_arm_cue",
        "post_wake_abs_open_db",
        "post_wake_timeout_s",
        "post_wake_no_open_nudge",
        "post_wake_no_open_tts",
        # Live-reload config.toml (B036) — mtime poll; same path as SIGHUP
        "config_watch",
        "config_watch_poll_ms",
        "config_watch_debounce_ms",
    }),
    "stt": frozenset({"provider"}),
    "tts": frozenset({"provider", "voice", "language", "max_chars", "allow_espeak_fallback"}),
    "confirm": frozenset({"mode"}),
    "safety": frozenset({"deny_patterns"}),
}
KNOWN_SESSION_KEYS = frozenset({"id", "socket", "ssh", "herdr_bin", "label", "remote_socket"})


@dataclass
class SessionConfig:
    id: str
    socket: str | None = None
    ssh: str | None = None
    herdr_bin: str | None = None
    label: str | None = None
    remote_socket: str | None = None


@dataclass
class WatchConfig:
    statuses: list[str] = field(default_factory=lambda: ["blocked", "done"])
    debounce_ms: int = 250
    transport: str = "auto"  # auto | socket | poll
    poll_ms: int = 1000
    heartbeat_s: float = 30.0
    # When status is done/idle but trailing pane text looks like a menu/ask,
    # emit agent.needs_input (false done). Default on for dogfood.
    detect_false_done: bool = True


@dataclass
class AudioConfig:
    half_duplex: bool = True
    # After TTS ends, wait this long before arming capture (tight handoff)
    post_tts_guard_ms: int = 100
    # Fire near-end callback this many ms before TTS finishes (pre-arm signal)
    listen_pre_arm_ms: int = 300
    # Optional true overlap: start capture near TTS end (default off = half-duplex)
    overlap_prearm: bool = False
    # When overlap_prearm: drop audio until TTS ends + this many ms (echo guard)
    overlap_discard_ms: int = 150
    # Mute system default capture during TTS (Wave ring white→red via pactl)
    mute_mic_during_tts: bool = True
    # Watch Wave/ALSA/Pulse mute edges; hardware unmute → force OS unmute
    sync_hw_unmute: bool = True
    # Record start/stop cue volume for generated blips (0.0–1.0)
    cue_volume: float = 0.22
    # Optional custom WAV/MP3 paths (empty = assets/cues defaults)
    cue_start_path: str | None = None
    cue_stop_path: str | None = None
    # Hold full TTS while a conference app is active (Zoom/Teams/Meet…); default ON
    hold_during_conference: bool = True
    # Soft chime while held instead of speaking the full question immediately
    conference_chime_only: bool = True
    # Process name fragments matched against /proc (case-insensitive)
    conference_process_names: list[str] = field(
        default_factory=lambda: [
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
        ]
    )
    # If detection tools/proc are missing: treat as free (True) or hold (False)
    conference_fail_open: bool = True
    # Also scan pactl/pw-cli stream names when available
    conference_check_audio: bool = True
    # Poll interval while waiting for the call to end
    conference_poll_ms: int = 2000
    # Max seconds to hold (0 = wait until free / no cap). On timeout, speak anyway.
    conference_max_hold_s: float = 0.0
    # Lower other apps' sink-input volumes while TTS plays (I002 / B045)
    duck_media_during_tts: bool = True
    # Prefer MPRIS/playerctl Pause for Playing players, then duck remaining
    pause_media_during_tts: bool = False
    # Fraction of each stream's prior volume (0.0–1.0); default ~15%
    duck_level: float = 0.15
    # Extra application.name / binary substrings to never duck
    duck_exclude_apps: list[str] = field(default_factory=list)
    # Secondary media signal via playerctl / MPRIS
    media_check_mpris: bool = True


@dataclass
class ListenConfig:
    end_mode: str = EndMode.SILENCE.value
    end_phrases: list[str] = field(default_factory=lambda: list(DEFAULT_END_PHRASES))
    cancel_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_CANCEL_PHRASES)
    )
    strip_phrase: bool = True
    max_listen_s: float = 300.0
    nudge_silence_s: float = 0.0
    # Seconds of quiet before ending a silence-mode capture
    end_silence_s: float = 2.1
    # Legacy radio segment hang (kept for config BC; not used for partial cadence)
    radio_end_silence_s: float = 2.5
    # Radio-only: quiet seconds between speech segments before interim STT/partial
    # (shorter than end_silence_s so Mode A gets frequent HOLD partials). Does NOT
    # finalize the turn — end phrases / agent listen-end still required.
    radio_partial_silence_s: float = 0.6
    # Radio mode: emit interim STT to agent with HOLD warnings (before end phrase)
    stream_partials: bool = True
    # After empty STT (gate opened but no text): one automatic re-listen
    empty_stt_retry: bool = True
    # If still empty after retry: TTS "Sorry, I didn't catch that." then re-listen once
    empty_stt_nudge: bool = True
    # Energy gate absolute open floor (dBFS). Quieter speech needs a lower value.
    # Dogfood: peak≈-45 with default -38 never opened → default softened to -48 (B031).
    abs_open_db: float = -48.0
    # Relative margin above adaptive noise floor (dB)
    open_margin_db: float = 8.0
    # Seconds to wait for speech to open the gate before timeout
    initial_timeout_s: float = 45.0
    # Gate never opened (TimeoutError "no speech detected"): silent re-listen once
    no_open_retry: bool = True
    # Still no open: TTS nudge then one more listen (text overridable by ambient)
    no_open_nudge: bool = True
    # Soft informal end phrases (default ON for radio dogfood; utterance-final only)
    soft_end_phrases_enabled: bool = DEFAULT_SOFT_END_PHRASES_ENABLED
    soft_end_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_SOFT_END_PHRASES)
    )
    # Endpointing strategy (B007): "energy" (default/fallback) | "smart_turn".
    # Smart turn needs the optional [smart-turn] extra + a model file; if it
    # cannot load, capture transparently falls back to the energy gate.
    endpoint_strategy: str = "energy"
    # Trailing silence before a smart strategy is consulted (lets it finish
    # early). Ignored by the energy gate. 0 = auto (min(end_silence_s, 0.6)).
    endpoint_probe_silence_s: float = 0.0
    # Max trailing silence to wait when a smart strategy says "incomplete".
    # 0 = same as end_silence_s (energy gate stays the ceiling). Raise it to let
    # smart turn hold longer through mid-thought pauses.
    endpoint_max_silence_s: float = 0.0
    # Path to the Smart Turn v3 ONNX model (required for endpoint_strategy="smart_turn").
    smart_turn_model_path: str | None = None
    # Completion probability at/above which smart turn ends the turn.
    smart_turn_threshold: float = 0.5


@dataclass
class AmbientConfig:
    """When not answering a bound question: listen for activation → new prompt."""

    enabled: bool = False
    # names (default) | phrases — see WakePolicy / docs/CUSTOM_WAKE.md
    wake_mode: str = "names"
    names: list[str] = field(default_factory=lambda: ["hark", "herald"])
    # Display / exact extras / phrase-mode list (resolved)
    activation_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_ACTIVATION_PHRASES)
    )
    learn_from_near_misses: bool = True
    # local | vosk | text_probe — never cloud during wake scan
    engine: str = "vosk"
    model_path: str | None = None
    snippet_s: float = 2.5
    # One-shot wake wait / continuous loop tick (seconds). 0 = wait indefinitely
    # (no ambient.timeout cycle). Continuous Mode A still uses this as the idle
    # cycle length when > 0; see surface_timeouts to hide the heartbeat event.
    timeout_s: float = 300.0
    # When true (default), continuous ambient emits ambient.timeout NDJSON/syslog
    # each idle cycle as a heartbeat. Set false for quieter long-running Mode A.
    surface_timeouts: bool = True
    # Dev: save wake audio+text under state/debug/wake (7-day cleanup)
    debug: bool = False
    debug_retention_days: float = 7.0
    # Post-wake cloud listen (B031): settle + arm cue + optional softer gate
    post_wake_lead_in_ms: int = 150
    # Play record-start when post-wake listen arms (not only when speech opens)
    post_wake_arm_cue: bool = True
    # Override listen.abs_open_db for post-wake only (None = use listen default)
    post_wake_abs_open_db: float | None = None
    # Gate open wait after wake (None = listen.initial_timeout_s). Shorter = faster nudge.
    post_wake_timeout_s: float | None = 15.0
    # After no-open exhausted: optional TTS before ambient.error (and as last nudge)
    post_wake_no_open_nudge: bool = True
    post_wake_no_open_tts: str = "I heard the wake but not your prompt."
    # Live-reload config.toml while ambient runs (B036). Default on; same apply
    # path as SIGHUP. Env HARK_CONFIG_WATCH=0|1 overrides.
    config_watch: bool = True
    config_watch_poll_ms: int = 1000
    config_watch_debounce_ms: int = 400
    # Full wake policy (names/phrases + learned aliases); set by load_config
    wake_policy: Any = None


@dataclass
class SttConfig:
    provider: str = "auto"


@dataclass
class TtsConfig:
    provider: str = "auto"
    # Provider voice id (xAI: eve/ara/leo/rex/sal/… — `hark providers voices`)
    voice: str | None = None
    language: str = "en"
    max_chars: int = 500
    allow_espeak_fallback: bool = False


@dataclass
class ConfirmConfig:
    mode: str = "auto"


@dataclass
class SafetyConfig:
    deny_patterns: list[str] = field(default_factory=list)


@dataclass
class HarkConfig:
    version: int = 1
    sessions: list[SessionConfig] = field(default_factory=list)
    watch: WatchConfig = field(default_factory=WatchConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    listen: ListenConfig = field(default_factory=ListenConfig)
    ambient: AmbientConfig = field(default_factory=AmbientConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    confirm: ConfirmConfig = field(default_factory=ConfirmConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    path: Path | None = None
    warnings: list[str] = field(default_factory=list)

    def session_by_id(self, session_id: str) -> SessionConfig | None:
        for s in self.sessions:
            if s.id == session_id:
                return s
        return None


DEFAULT_CONFIG_TOML = """\
# Hark config — ~/.config/hark/config.toml  (see docs/SPEC.md)
version = 1

[[herdr.sessions]]
id = "local"
# socket = "~/.config/herdr/herdr.sock"
# label = "local herdr"
# ssh = "workbox"            # optional remote tunnel
# remote_socket = "~/.config/herdr/herdr.sock"

[watch]
statuses = ["blocked", "done"]
debounce_ms = 250
transport = "auto"           # auto | socket | poll
poll_ms = 1000
detect_false_done = true     # done/idle + menu-like pane → agent.needs_input

[audio]
half_duplex = true
post_tts_guard_ms = 100      # after TTS ends → start listen (tight handoff)
listen_pre_arm_ms = 300      # signal ~0.3s before TTS ends
overlap_prearm = false       # true: start capture near TTS end (still drops echo)
overlap_discard_ms = 150     # with overlap_prearm: drop audio until TTS ends + this
mute_mic_during_tts = true   # pactl mute → Elgato Wave ring red while speaking
sync_hw_unmute = true        # Wave/ALSA unmute button → force OS/Pulse unmute
cue_volume = 0.22            # generated start/stop beep volume (0–1)
# cue_start_path = "/path/to/record-start.wav"
# cue_stop_path  = "/path/to/record-stop.wav"
# Hold full TTS during Zoom/Teams/Meet (process list + optional audio streams)
hold_during_conference = true
conference_chime_only = true # soft cue while held; full question after call ends
# conference_process_names = ["zoom", "teams", "webex", "discord", "slack"]
conference_fail_open = true  # missing /proc or tools → allow TTS
conference_check_audio = true
conference_poll_ms = 2000
# conference_max_hold_s = 0  # 0 = wait until free; >0 speak after timeout
# Duck other media while TTS plays (I002 / B045) — never changes master/sink volume
duck_media_during_tts = true
pause_media_during_tts = false  # true: playerctl Pause Playing players + duck rest
duck_level = 0.15               # fraction of prior per-stream volume (0.0–1.0)
# duck_exclude_apps = ["easyeffects"]  # optional app name / binary substrings
media_check_mpris = true        # secondary media signal via playerctl

# Bound answer windows — how spoken replies end
# Defaults are product-scoped so normal speech does not trigger control.
[listen]
end_mode = "silence"         # silence | radio
# end_mode = "radio"         # keep listening until end phrase (long pauses OK)
end_silence_s = 2.1          # quiet seconds before ending silence-mode capture
# radio_partial_silence_s = 0.6  # radio only: quiet before interim STT/partial (B037)
# radio_end_silence_s = 2.5      # legacy; segment cadence is radio_partial_silence_s
# Endpointing strategy (B007): "energy" (default) reduces to the fixed
# end_silence_s gate. "smart_turn" consults a Smart Turn v3 model to finish
# earlier when you clearly stopped, or wait longer through mid-thought pauses.
# It needs the optional extra + a model; if it can't load, capture falls back
# to the energy gate. See docs/ENDPOINTING.md.
endpoint_strategy = "energy" # energy | smart_turn
# endpoint_probe_silence_s = 0.4   # smart turn: trailing quiet before first probe (0 = auto)
# endpoint_max_silence_s = 3.0     # smart turn: max quiet to wait when "incomplete" (0 = end_silence_s)
# smart_turn_model_path = "~/.local/share/hark/models/smart-turn-v3.onnx"
# smart_turn_threshold = 0.5
stream_partials = true       # radio mode: stream interim text to agent (HOLD until final)
end_phrases = [
  "okay hark send",
  "ok hark send",
  "hark send it",
  "hark send",
  "end prompt",
  "end of prompt",
  "hark over",
]
cancel_phrases = [
  "hark cancel",
  "cancel hark",
  "abort hark send",
  "hark abort",
]
# Soft (informal) end phrases — DEFAULT ON (B039 radio dogfood).
# Finalize on utterance-final soft closers after radio silence (e.g. "send it",
# "that's all", sentence-final "over"). Does NOT match mid-clause
# ("that's all I know about X", "over the weekend", "turn it over").
# Set false to require product phrases only. Safe list: docs/AUDIO_DESIGN.md
soft_end_phrases_enabled = true
# soft_end_phrases = ["send it", "send that", "that's all", "over and out", "over"]
strip_phrase = true
max_listen_s = 300
empty_stt_retry = true       # re-listen once if STT returns empty transcript
empty_stt_nudge = true       # TTS "Sorry, I didn't catch that." then re-listen once more
# Energy gate open (B031). abs_open_db is the absolute floor (dBFS); quieter mics need lower.
# Default -48 (was hardcoded -38) so normal close-talk speech opens; raise if noisy room.
abs_open_db = -48.0
open_margin_db = 8.0         # dB above adaptive noise floor
initial_timeout_s = 45       # wait for speech open before timeout (answer windows)
no_open_retry = true         # re-listen once if gate never opens
no_open_nudge = true         # TTS then re-listen once more on no-open

# Ambient: when NOT replying to a blocked agent question
# Local 2–3s snippets scan for activation; cloud STT only after wake.
# Setup: ./scripts/setup-ambient.sh
#
# Wake customization — pick ONE style (see docs/CUSTOM_WAKE.md):
#
# 1) Name-based (default): set product names; greating+name / bare name wake.
#    Near-misses auto-learn alternate name tokens (no restart).
#      wake_mode = "names"
#      names = ["hark", "herald"]
#      # extra_names = ["alice"]
#
# 2) Full-phrase: entire trigger strings only (no name fuzzy).
#    Near-misses auto-learn alternate full phrases (no restart).
#      wake_mode = "phrases"
#      trigger_phrases = ["start prompt", "begin dictation"]
#
# Legacy: activation_phrases / extra_trigger_phrases still work.
# Config edits: auto-reloaded by mtime file-watch (default) or kill -HUP.
# Learning needs neither HUP nor restart.
[ambient]
enabled = false
wake_mode = "names"
names = ["hark", "herald"]
# extra_names = ["alice"]
# wake_mode = "phrases"
# trigger_phrases = ["start prompt"]
# extra_trigger_phrases = ["begin dictation"]
learn_from_near_misses = true
engine = "vosk"              # vosk | text_probe (tests)
# model_path = "~/.local/share/hark/models/vosk-model-small-en-us-0.15"
snippet_s = 2.5
# One-shot wake wait / continuous idle cycle length (seconds). 0 = wait forever
# (no ambient.timeout). Continuous Mode A re-enters the wake wait each timeout_s.
timeout_s = 300
# Surface ambient.timeout on continuous idle cycles (NDJSON + syslog). Default on
# as a heartbeat (useful for provider cache / dogfood). Set false to quiet Mode A.
surface_timeouts = true
# emit_timeout_events = true  # alias of surface_timeouts
debug = true                 # save wake wav+text under ~/.local/state/hark/debug/wake
debug_retention_days = 7
# Post-wake listen after successful activation (B031)
post_wake_lead_in_ms = 150   # settle after wake before arming cloud listen
post_wake_arm_cue = true     # record-start beep when listen arms (operator feedback)
# post_wake_abs_open_db = -50  # optional softer gate for post-wake only
post_wake_timeout_s = 15     # gate-open wait after wake (faster nudge than answer windows)
post_wake_no_open_nudge = true
# post_wake_no_open_tts = "I heard the wake but not your prompt."
# Live-reload this file while ambient runs (B036). Same apply path as SIGHUP.
config_watch = true
config_watch_poll_ms = 1000
config_watch_debounce_ms = 400
# Env override: HARK_CONFIG_WATCH=0 to disable, =1 to force on.

[stt]
provider = "auto"

[tts]
provider = "auto"
voice = "eve"                # xAI: eve ara leo rex sal … — hark providers voices
language = "en"
# voice = "ara"
max_chars = 500

[confirm]
mode = "auto"

[safety]
deny_patterns = []
"""


def _as_list_str(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return list(default)


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _dedupe_phrases(phrases: list[str]) -> list[str]:
    """Preserve order, drop empty/duplicate (case-insensitive)."""
    seen: set[str] = set()
    out: list[str] = []
    for p in phrases:
        s = str(p).strip()
        if not s:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def _resolve_surface_timeouts(ambient_raw: dict[str, Any]) -> bool:
    """Whether continuous ambient should emit ambient.timeout (default true).

    Canonical key: surface_timeouts. Alias: emit_timeout_events.
    If both are set, surface_timeouts wins.
    """
    if "surface_timeouts" in ambient_raw:
        return _as_bool(ambient_raw.get("surface_timeouts"), default=True)
    if "emit_timeout_events" in ambient_raw:
        return _as_bool(ambient_raw.get("emit_timeout_events"), default=True)
    return True


def _build_ambient_config(
    ambient_raw: dict[str, Any],
    *,
    ambient_enabled: bool,
) -> AmbientConfig:
    policy = resolve_wake_policy(ambient_raw)
    return AmbientConfig(
        enabled=ambient_enabled,
        wake_mode=policy.normalized_mode(),
        names=list(policy.canonical_names()),
        activation_phrases=policy.display_phrases(),
        learn_from_near_misses=policy.learn,
        engine=str(ambient_raw.get("engine", "vosk")),
        model_path=_resolve_vosk_model_path(
            str(ambient_raw["model_path"])
            if ambient_raw.get("model_path")
            else os.environ.get("HARK_VOSK_MODEL")
        ),
        snippet_s=float(ambient_raw.get("snippet_s", 2.5)),
        timeout_s=float(ambient_raw.get("timeout_s", 300)),
        surface_timeouts=_resolve_surface_timeouts(ambient_raw),
        debug=bool(
            ambient_raw.get(
                "debug",
                os.environ.get("HARK_DEBUG", "").lower()
                in ("1", "true", "yes", "on"),
            )
        ),
        debug_retention_days=float(ambient_raw.get("debug_retention_days", 7)),
        post_wake_lead_in_ms=int(ambient_raw.get("post_wake_lead_in_ms", 150)),
        post_wake_arm_cue=bool(ambient_raw.get("post_wake_arm_cue", True)),
        post_wake_abs_open_db=(
            float(ambient_raw["post_wake_abs_open_db"])
            if ambient_raw.get("post_wake_abs_open_db") is not None
            else None
        ),
        post_wake_timeout_s=(
            float(ambient_raw["post_wake_timeout_s"])
            if ambient_raw.get("post_wake_timeout_s") is not None
            else 15.0
        ),
        post_wake_no_open_nudge=bool(ambient_raw.get("post_wake_no_open_nudge", True)),
        post_wake_no_open_tts=str(
            ambient_raw.get(
                "post_wake_no_open_tts",
                "I heard the wake but not your prompt.",
            )
        ),
        config_watch=_as_bool(ambient_raw.get("config_watch"), default=True),
        config_watch_poll_ms=int(ambient_raw.get("config_watch_poll_ms", 1000)),
        config_watch_debounce_ms=int(
            ambient_raw.get("config_watch_debounce_ms", 400)
        ),
        wake_policy=policy,
    )


def resolve_activation_phrases(ambient_raw: dict[str, Any]) -> list[str]:
    """Build display/exact phrase list (legacy helper; prefer resolve_wake_policy)."""
    policy = resolve_wake_policy(ambient_raw)
    return policy.display_phrases()


def resolve_wake_policy(
    ambient_raw: dict[str, Any],
    *,
    load_learned_state: bool = True,
) -> WakePolicy:
    """Resolve name-based vs full-phrase wake policy from ambient TOML.

    **names** (default): ``names`` / ``extra_names``; greating+name + bare +
    seed/learned aliases. Optional full-phrase extras via
    ``extra_trigger_phrases``.

    **phrases**: ``trigger_phrases`` / ``activation_phrases`` (+ extras);
    no name fuzzy; learned phrase alternates expand the list.

    Inference when ``wake_mode`` omitted:
      - explicit ``names`` / ``extra_names`` → names
      - explicit primary ``trigger_phrases``/``activation_phrases`` without
        names keys → phrases (legacy exclusive custom)
      - else → names with defaults hark/herald
    """
    mode_raw = ambient_raw.get("wake_mode")
    if mode_raw is None:
        mode_raw = ambient_raw.get("activation_mode")

    names_raw = None
    for key in ("names", "activation_names", "wake_names"):
        if key in ambient_raw and ambient_raw[key] is not None:
            names_raw = ambient_raw[key]
            break
    extra_names = _as_list_str(ambient_raw.get("extra_names"), [])

    primary = ambient_raw.get("activation_phrases")
    if primary is None:
        primary = ambient_raw.get("trigger_phrases")
    extras: list[str] = []
    for key in ("extra_activation_phrases", "extra_trigger_phrases"):
        if key in ambient_raw and ambient_raw[key] is not None:
            extras.extend(_as_list_str(ambient_raw[key], []))

    if mode_raw is not None:
        mode = str(mode_raw).strip().lower()
        if mode in ("phrase", "phrases", "full", "full_phrase", "full-phrase"):
            mode = "phrases"
        else:
            mode = "names"
    elif names_raw is not None or extra_names:
        mode = "names"
    elif primary is not None:
        # Legacy: exclusive custom list without product names → phrases mode.
        # Default hey-hark list (or any list mentioning hark/herald) → names.
        primary_list = _as_list_str(primary, [])
        joined = " ".join(primary_list).lower()
        if "hark" in joined or "herald" in joined:
            mode = "names"
        else:
            mode = "phrases"
    else:
        mode = DEFAULT_WAKE_MODE

    learn = _as_bool(ambient_raw.get("learn_from_near_misses"), default=True)

    if mode == "phrases":
        if primary is None:
            base = list(DEFAULT_ACTIVATION_PHRASES)
        else:
            base = _as_list_str(primary, [])
        phrases = _dedupe_phrases(base + extras)
        policy = WakePolicy(
            mode="phrases",
            names=[],
            phrases=phrases,
            learn=learn,
        )
    else:
        if names_raw is None:
            names = list(DEFAULT_WAKE_NAMES)
        else:
            names = _as_list_str(names_raw, list(DEFAULT_WAKE_NAMES))
        names = _dedupe_phrases(names + extra_names)
        # Full-phrase extras still allowed alongside names
        phrase_extras: list[str] = []
        if primary is not None:
            # User set both names mode and a phrase list — treat primary as extras
            # only when they look like non-default custom adds; if it's the old
            # default hey-hark list, ignore (names cover it).
            primary_list = _as_list_str(primary, [])
            default_set = {p.lower() for p in DEFAULT_ACTIVATION_PHRASES}
            if any(p.lower() not in default_set for p in primary_list):
                phrase_extras.extend(
                    p for p in primary_list if p.lower() not in default_set
                )
        phrase_extras.extend(extras)
        policy = WakePolicy(
            mode="names",
            names=names or list(DEFAULT_WAKE_NAMES),
            phrases=_dedupe_phrases(phrase_extras),
            learn=learn,
        )

    if load_learned_state and learn:
        learned = load_learned()
        policy = policy.merge_learned(
            name_aliases=learned.name_aliases,
            phrase_aliases=learned.phrase_aliases,
        )
    return policy


def _warn_unknown_keys(
    raw: dict[str, Any],
    known: frozenset[str],
    section: str,
    warnings: list[str],
) -> None:
    for key in raw:
        if key not in known:
            warnings.append(f"unknown config key: {section}.{key}")


def default_vosk_model_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "hark" / "models" / "vosk-model-small-en-us-0.15"


def _resolve_vosk_model_path(raw: str | None) -> str | None:
    if raw:
        p = Path(os.path.expanduser(raw))
        return str(p)
    # Auto-detect common install location from setup-ambient.sh
    auto = default_vosk_model_path()
    if auto.is_dir():
        return str(auto)
    return None


def load_config(path: Path | None = None) -> HarkConfig:
    cfg_path = Path(path) if path is not None else default_config_path()
    raw: dict[str, Any] = {}
    warnings: list[str] = []

    if cfg_path.is_file():
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh) or {}
        for key in raw:
            if key not in KNOWN_TOP_KEYS:
                warnings.append(f"unknown config key: {key!r}")

    for section, known in KNOWN_SECTION_KEYS.items():
        value = raw.get(section)
        if isinstance(value, dict):
            _warn_unknown_keys(value, known, section, warnings)

    herdr = raw.get("herdr") or {}
    sessions_raw = herdr.get("sessions") if isinstance(herdr, dict) else None
    sessions: list[SessionConfig] = []
    if isinstance(sessions_raw, list):
        for index, item in enumerate(sessions_raw):
            if not isinstance(item, dict) or "id" not in item:
                warnings.append(f"skipping invalid session entry: {item!r}")
                continue
            _warn_unknown_keys(
                item,
                KNOWN_SESSION_KEYS,
                f"herdr.sessions[{index}]",
                warnings,
            )
            sessions.append(
                SessionConfig(
                    id=str(item["id"]),
                    socket=item.get("socket"),
                    ssh=item.get("ssh"),
                    herdr_bin=item.get("herdr_bin"),
                    label=item.get("label"),
                    remote_socket=item.get("remote_socket"),
                )
            )
    if not sessions:
        sessions = [SessionConfig(id="local")]

    env_sock = os.environ.get("HERDR_SOCKET_PATH")
    if env_sock:
        for s in sessions:
            if s.id == "local" or s.socket is None:
                s.socket = env_sock
                break

    watch_raw = raw.get("watch") if isinstance(raw.get("watch"), dict) else {}
    audio_raw = raw.get("audio") if isinstance(raw.get("audio"), dict) else {}
    listen_raw = raw.get("listen") if isinstance(raw.get("listen"), dict) else {}
    ambient_raw = raw.get("ambient") if isinstance(raw.get("ambient"), dict) else {}
    stt_raw = raw.get("stt") if isinstance(raw.get("stt"), dict) else {}
    tts_raw = raw.get("tts") if isinstance(raw.get("tts"), dict) else {}
    confirm_raw = raw.get("confirm") if isinstance(raw.get("confirm"), dict) else {}
    safety_raw = raw.get("safety") if isinstance(raw.get("safety"), dict) else {}

    stt_provider = os.environ.get("HARK_STT_PROVIDER") or str(
        stt_raw.get("provider", "auto")
    )
    tts_provider = str(tts_raw.get("provider", "auto"))

    end_mode_raw = str(listen_raw.get("end_mode", "silence"))
    if os.environ.get("HARK_LISTEN_END_MODE"):
        end_mode_raw = os.environ["HARK_LISTEN_END_MODE"]
    try:
        end_mode = parse_end_mode(end_mode_raw).value
    except ValueError as exc:
        warnings.append(str(exc))
        end_mode = EndMode.SILENCE.value

    soft_end_enabled = bool(
        listen_raw.get("soft_end_phrases_enabled", DEFAULT_SOFT_END_PHRASES_ENABLED)
    )
    env_soft = os.environ.get("HARK_SOFT_END_PHRASES_ENABLED")
    if env_soft is not None:
        soft_end_enabled = env_soft.strip().lower() in ("1", "true", "yes", "on")

    endpoint_strategy = str(listen_raw.get("endpoint_strategy", "energy"))
    env_endpoint = os.environ.get("HARK_LISTEN_ENDPOINT_STRATEGY")
    if env_endpoint:
        endpoint_strategy = env_endpoint
    smart_turn_model_path = os.environ.get("HARK_SMART_TURN_MODEL") or (
        str(listen_raw["smart_turn_model_path"])
        if listen_raw.get("smart_turn_model_path")
        else None
    )

    ambient_enabled = bool(ambient_raw.get("enabled", False))
    if os.environ.get("HARK_AMBIENT"):
        ambient_enabled = os.environ["HARK_AMBIENT"].lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

    return HarkConfig(
        version=int(raw.get("version", 1)) if isinstance(raw.get("version", 1), int) else 1,
        sessions=sessions,
        watch=WatchConfig(
            statuses=_as_list_str(watch_raw.get("statuses"), ["blocked", "done"]),
            debounce_ms=int(watch_raw.get("debounce_ms", 250)),
            transport=str(watch_raw.get("transport", "auto")),
            poll_ms=int(watch_raw.get("poll_ms", 1000)),
            heartbeat_s=float(watch_raw.get("heartbeat_s", 30.0)),
            detect_false_done=_as_bool(
                watch_raw.get("detect_false_done"),
                default=True,
            ),
        ),
        audio=AudioConfig(
            half_duplex=bool(audio_raw.get("half_duplex", True)),
            post_tts_guard_ms=int(audio_raw.get("post_tts_guard_ms", 100)),
            listen_pre_arm_ms=int(audio_raw.get("listen_pre_arm_ms", 300)),
            overlap_prearm=bool(audio_raw.get("overlap_prearm", False)),
            overlap_discard_ms=int(audio_raw.get("overlap_discard_ms", 150)),
            mute_mic_during_tts=bool(audio_raw.get("mute_mic_during_tts", True)),
            sync_hw_unmute=bool(audio_raw.get("sync_hw_unmute", True)),
            cue_volume=float(
                audio_raw.get(
                    "cue_volume",
                    os.environ.get("HARK_CUE_VOLUME", 0.22),
                )
            ),
            cue_start_path=(
                str(audio_raw["cue_start_path"])
                if audio_raw.get("cue_start_path")
                else os.environ.get("HARK_CUE_START")
            ),
            cue_stop_path=(
                str(audio_raw["cue_stop_path"])
                if audio_raw.get("cue_stop_path")
                else os.environ.get("HARK_CUE_STOP")
            ),
            hold_during_conference=bool(
                audio_raw.get(
                    "hold_during_conference",
                    os.environ.get("HARK_HOLD_DURING_CONFERENCE", "true").lower()
                    not in ("0", "false", "no", "off"),
                )
            ),
            conference_chime_only=bool(audio_raw.get("conference_chime_only", True)),
            conference_process_names=_as_list_str(
                audio_raw.get("conference_process_names"),
                [
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
                ],
            ),
            conference_fail_open=bool(audio_raw.get("conference_fail_open", True)),
            conference_check_audio=bool(audio_raw.get("conference_check_audio", True)),
            conference_poll_ms=int(audio_raw.get("conference_poll_ms", 2000)),
            conference_max_hold_s=float(audio_raw.get("conference_max_hold_s", 0)),
            duck_media_during_tts=_as_bool(
                audio_raw.get("duck_media_during_tts"),
                default=True,
            ),
            pause_media_during_tts=_as_bool(
                audio_raw.get("pause_media_during_tts"),
                default=False,
            ),
            duck_level=float(audio_raw.get("duck_level", 0.15)),
            duck_exclude_apps=_as_list_str(
                audio_raw.get("duck_exclude_apps"),
                [],
            ),
            media_check_mpris=_as_bool(
                audio_raw.get("media_check_mpris"),
                default=True,
            ),
        ),
        listen=ListenConfig(
            end_mode=end_mode,
            end_phrases=_as_list_str(
                listen_raw.get("end_phrases"), list(DEFAULT_END_PHRASES)
            ),
            cancel_phrases=_as_list_str(
                listen_raw.get("cancel_phrases"), list(DEFAULT_CANCEL_PHRASES)
            ),
            strip_phrase=bool(listen_raw.get("strip_phrase", True)),
            max_listen_s=float(listen_raw.get("max_listen_s", 300)),
            nudge_silence_s=float(listen_raw.get("nudge_silence_s", 0)),
            end_silence_s=float(listen_raw.get("end_silence_s", 2.1)),
            radio_end_silence_s=float(listen_raw.get("radio_end_silence_s", 2.5)),
            radio_partial_silence_s=float(
                listen_raw.get("radio_partial_silence_s", 0.6)
            ),
            stream_partials=bool(listen_raw.get("stream_partials", True)),
            empty_stt_retry=bool(listen_raw.get("empty_stt_retry", True)),
            empty_stt_nudge=bool(listen_raw.get("empty_stt_nudge", True)),
            abs_open_db=float(listen_raw.get("abs_open_db", -48.0)),
            open_margin_db=float(listen_raw.get("open_margin_db", 8.0)),
            initial_timeout_s=float(listen_raw.get("initial_timeout_s", 45.0)),
            no_open_retry=bool(listen_raw.get("no_open_retry", True)),
            no_open_nudge=bool(listen_raw.get("no_open_nudge", True)),
            soft_end_phrases_enabled=soft_end_enabled,
            soft_end_phrases=_as_list_str(
                listen_raw.get("soft_end_phrases"), list(DEFAULT_SOFT_END_PHRASES)
            ),
            endpoint_strategy=endpoint_strategy,
            endpoint_probe_silence_s=float(
                listen_raw.get("endpoint_probe_silence_s", 0.0)
            ),
            endpoint_max_silence_s=float(
                listen_raw.get("endpoint_max_silence_s", 0.0)
            ),
            smart_turn_model_path=smart_turn_model_path,
            smart_turn_threshold=float(listen_raw.get("smart_turn_threshold", 0.5)),
        ),
        ambient=_build_ambient_config(
            ambient_raw if isinstance(ambient_raw, dict) else {},
            ambient_enabled=ambient_enabled,
        ),
        stt=SttConfig(provider=stt_provider),
        tts=TtsConfig(
            provider=tts_provider,
            voice=(
                str(tts_raw["voice"])
                if tts_raw.get("voice")
                else os.environ.get("HARK_TTS_VOICE")
            ),
            language=str(
                tts_raw.get("language")
                or os.environ.get("HARK_TTS_LANGUAGE")
                or "en"
            ),
            max_chars=int(tts_raw.get("max_chars", 500)),
            allow_espeak_fallback=bool(tts_raw.get("allow_espeak_fallback", False)),
        ),
        confirm=ConfirmConfig(mode=str(confirm_raw.get("mode", "auto"))),
        safety=SafetyConfig(
            deny_patterns=_as_list_str(safety_raw.get("deny_patterns"), [])
        ),
        path=cfg_path if cfg_path.is_file() else None,
        warnings=warnings,
    )


def resolve_session_socket(session: SessionConfig) -> Path:
    if session.socket:
        return Path(os.path.expanduser(session.socket))
    if session.ssh:
        # tunnel local path (created by ensure_tunnel)
        from hark.paths import cache_dir

        return cache_dir() / "tunnels" / f"{session.id}.sock"
    # Herdr "default" session uses the main config dir sock (not sessions/default/)
    if session.id in ("local", "default"):
        return default_herdr_socket()
    named = Path.home() / ".config" / "herdr" / "sessions" / session.id / "herdr.sock"
    if named.exists():
        return named
    return default_herdr_socket()


def write_default_config(path: Path | None = None, force: bool = False) -> Path:
    dest = path or default_config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        raise FileExistsError(f"config already exists: {dest}")
    dest.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return dest


def config_to_dict(cfg: HarkConfig) -> dict[str, Any]:
    return {
        "version": cfg.version,
        "path": str(cfg.path) if cfg.path else None,
        "warnings": list(cfg.warnings),
        "herdr": {
            "sessions": [
                {
                    "id": s.id,
                    "socket": s.socket,
                    "ssh": s.ssh,
                    "herdr_bin": s.herdr_bin,
                    "label": s.label,
                    "remote_socket": s.remote_socket,
                }
                for s in cfg.sessions
            ]
        },
        "watch": {
            "statuses": cfg.watch.statuses,
            "debounce_ms": cfg.watch.debounce_ms,
            "transport": cfg.watch.transport,
            "poll_ms": cfg.watch.poll_ms,
            "heartbeat_s": cfg.watch.heartbeat_s,
            "detect_false_done": cfg.watch.detect_false_done,
        },
        "audio": {
            "half_duplex": cfg.audio.half_duplex,
            "post_tts_guard_ms": cfg.audio.post_tts_guard_ms,
            "listen_pre_arm_ms": cfg.audio.listen_pre_arm_ms,
            "overlap_prearm": cfg.audio.overlap_prearm,
            "overlap_discard_ms": cfg.audio.overlap_discard_ms,
            "mute_mic_during_tts": cfg.audio.mute_mic_during_tts,
            "sync_hw_unmute": cfg.audio.sync_hw_unmute,
            "cue_volume": cfg.audio.cue_volume,
            "cue_start_path": cfg.audio.cue_start_path,
            "cue_stop_path": cfg.audio.cue_stop_path,
            "hold_during_conference": cfg.audio.hold_during_conference,
            "conference_chime_only": cfg.audio.conference_chime_only,
            "conference_process_names": list(cfg.audio.conference_process_names),
            "conference_fail_open": cfg.audio.conference_fail_open,
            "conference_check_audio": cfg.audio.conference_check_audio,
            "conference_poll_ms": cfg.audio.conference_poll_ms,
            "conference_max_hold_s": cfg.audio.conference_max_hold_s,
            "duck_media_during_tts": cfg.audio.duck_media_during_tts,
            "pause_media_during_tts": cfg.audio.pause_media_during_tts,
            "duck_level": cfg.audio.duck_level,
            "duck_exclude_apps": list(cfg.audio.duck_exclude_apps),
            "media_check_mpris": cfg.audio.media_check_mpris,
        },
        "listen": {
            "end_mode": cfg.listen.end_mode,
            "end_phrases": list(cfg.listen.end_phrases),
            "cancel_phrases": list(cfg.listen.cancel_phrases),
            "strip_phrase": cfg.listen.strip_phrase,
            "max_listen_s": cfg.listen.max_listen_s,
            "nudge_silence_s": cfg.listen.nudge_silence_s,
            "end_silence_s": cfg.listen.end_silence_s,
            "radio_end_silence_s": cfg.listen.radio_end_silence_s,
            "radio_partial_silence_s": cfg.listen.radio_partial_silence_s,
            "stream_partials": cfg.listen.stream_partials,
            "empty_stt_retry": cfg.listen.empty_stt_retry,
            "empty_stt_nudge": cfg.listen.empty_stt_nudge,
            "abs_open_db": cfg.listen.abs_open_db,
            "open_margin_db": cfg.listen.open_margin_db,
            "initial_timeout_s": cfg.listen.initial_timeout_s,
            "no_open_retry": cfg.listen.no_open_retry,
            "no_open_nudge": cfg.listen.no_open_nudge,
            "soft_end_phrases_enabled": cfg.listen.soft_end_phrases_enabled,
            "soft_end_phrases": list(cfg.listen.soft_end_phrases),
            "endpoint_strategy": cfg.listen.endpoint_strategy,
            "endpoint_probe_silence_s": cfg.listen.endpoint_probe_silence_s,
            "endpoint_max_silence_s": cfg.listen.endpoint_max_silence_s,
            "smart_turn_model_path": cfg.listen.smart_turn_model_path,
            "smart_turn_threshold": cfg.listen.smart_turn_threshold,
        },
        "ambient": {
            "enabled": cfg.ambient.enabled,
            "wake_mode": cfg.ambient.wake_mode,
            "names": list(cfg.ambient.names),
            "activation_phrases": list(cfg.ambient.activation_phrases),
            "learn_from_near_misses": cfg.ambient.learn_from_near_misses,
            "engine": cfg.ambient.engine,
            "model_path": cfg.ambient.model_path,
            "snippet_s": cfg.ambient.snippet_s,
            "timeout_s": cfg.ambient.timeout_s,
            "surface_timeouts": cfg.ambient.surface_timeouts,
            "debug": cfg.ambient.debug,
            "debug_retention_days": cfg.ambient.debug_retention_days,
            "post_wake_lead_in_ms": cfg.ambient.post_wake_lead_in_ms,
            "post_wake_arm_cue": cfg.ambient.post_wake_arm_cue,
            "post_wake_abs_open_db": cfg.ambient.post_wake_abs_open_db,
            "post_wake_timeout_s": cfg.ambient.post_wake_timeout_s,
            "post_wake_no_open_nudge": cfg.ambient.post_wake_no_open_nudge,
            "post_wake_no_open_tts": cfg.ambient.post_wake_no_open_tts,
            "config_watch": cfg.ambient.config_watch,
            "config_watch_poll_ms": cfg.ambient.config_watch_poll_ms,
            "config_watch_debounce_ms": cfg.ambient.config_watch_debounce_ms,
        },
        "stt": {"provider": cfg.stt.provider},
        "tts": {
            "provider": cfg.tts.provider,
            "voice": cfg.tts.voice,
            "language": cfg.tts.language,
            "max_chars": cfg.tts.max_chars,
        },
        "confirm": {"mode": cfg.confirm.mode},
    }


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)

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
        "dashboard",
        "agents",
        "update",
    }
)

KNOWN_SECTION_KEYS: dict[str, frozenset[str]] = {
    "herdr": frozenset({"sessions"}),
    "agents": frozenset(
        {
            "prefer_aliases",
            "claude",
            "codex",
            "grok",
            "cursor_agent",
            "cursor-agent",
            "opencode",
            "pi",
            "agy",
            "cli",
        }
    ),
    "watch": frozenset({
        "statuses",
        "debounce_ms",
        "transport",
        "poll_ms",
        "heartbeat_s",
        "detect_false_done",
        "pane_capture",
        "pane_capture_lines",
        "pane_capture_max_chars",
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
        "answer_arm_cue",
        "mute_edge_pad_ms",
        # Conference hold (B017): pause full TTS while Zoom/Teams/Meet is active
        "hold_during_conference",
        "conference_chime_only",
        "conference_process_names",
        "conference_fail_open",
        "conference_check_audio",
        "conference_browser_av_heuristic",
        "conference_poll_ms",
        "conference_max_hold_s",
        # Media ducking during TTS/STT (B045/B046 / I002)
        "duck_media_during_tts",
        "pause_media_during_tts",
        "duck_media_during_stt",
        "pause_media_during_stt",
        "duck_level",
        "duck_exclude_apps",
        "media_check_mpris",
        # B097: defer TTS play/mute while operator listen/radio is capturing
        "defer_tts_while_listening",
        "defer_tts_max_wait_s",
        "defer_tts_poll_ms",
        "defer_tts_quiet_ms",
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
        "radio_idle_end_silence_s",
        "radio_segment_pad_ms",
        "radio_segment_overlap_ms",
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
        # Pre-speech lead-in from capture ring (B079); 250–500 ms
        "pre_roll_ms",
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
        # Overlapping wake window hop (B079); hop < snippet_s
        "snippet_hop_s",
        # Continuous capture ring capacity seconds (B079)
        "ring_s",
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
        # Ambient streaming mode (B098): short live TTS on partials allowed
        "streaming",
        # B105: min operator quiet before streaming TTS play (seconds)
        "streaming_ack_min_quiet_s",
    }),
    "stt": frozenset({
        "provider",
        # Optional local full-STT (B072) — cloud remains default under auto
        "local_model",
        "local_device",
        "local_compute_type",
        "local_model_path",
        "local_fail_open",
        "local_download",
    }),
    "tts": frozenset(
        {
            "provider",
            "voice",
            "language",
            "max_chars",
            "chunk_chars",
            "allow_espeak_fallback",
            # B095: print question text to terminal on ask / tts --listen
            "print_prompt",
        }
    ),
    "confirm": frozenset({"mode"}),
    "safety": frozenset({"deny_patterns"}),
    "dashboard": frozenset({
        "host",
        "port",
        "token",
        "require_token",
        "tls_terminated",
        "history_limit",
    }),
    "update": frozenset({
        "enabled",
        "repo",
    }),
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
    # Attach full recent pane text on agent wake HEP (blocked / needs_input / …).
    # Mode A can decide without a mandatory second `hark context` fetch.
    pane_capture: bool = True
    pane_capture_lines: int = 100
    pane_capture_max_chars: int = 12000


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
    # After TTS (ask / tts --listen / confirm): beep when listen arms, not when
    # speech opens. Avoids a multi-second silent wait that felt like lag.
    answer_arm_cue: bool = True
    # B084: after TTS mute releases, discard this many ms (not user silence)
    mute_edge_pad_ms: int = 300
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
    # B118/B117: browser Playback sink-input + mic RecordStream source-output → conference
    conference_browser_av_heuristic: bool = True
    # Poll interval while waiting for the call to end
    conference_poll_ms: int = 2000
    # Max seconds to hold (0 = wait until free / no cap). On timeout, speak anyway.
    conference_max_hold_s: float = 30.0  # 0 = wait forever; default 30s then speak (avoid stuck TTS)
    # Lower other apps' sink-input volumes while TTS plays (I002 / B045)
    duck_media_during_tts: bool = True
    # Prefer MPRIS/playerctl Pause for Playing players, then duck remaining
    pause_media_during_tts: bool = False
    # Lower other apps' volumes while STT capture windows run (I002 / B046)
    duck_media_during_stt: bool = True
    # Prefer MPRIS Pause during STT (dogfood default on — reduces bleed into mic)
    pause_media_during_stt: bool = True
    # Fraction of each stream's prior volume (0.0–1.0); default ~15%
    duck_level: float = 0.15
    # Extra application.name / binary substrings to never duck
    duck_exclude_apps: list[str] = field(default_factory=list)
    # Secondary media signal via playerctl / MPRIS
    media_check_mpris: bool = True
    # B097: if listen/radio is capturing user speech, defer TTS play + mic mute
    # until the stream finalizes (or max wait). Prevents cutting off mid-utterance.
    defer_tts_while_listening: bool = True
    # Cap so TTS cannot hang forever (0 = wait until capture ends, no cap)
    defer_tts_max_wait_s: float = 45.0
    defer_tts_poll_ms: int = 100
    # After capture clears, settle this long before speaking (trailing quiet pad)
    defer_tts_quiet_ms: int = 200


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
    # (shorter than end_silence_s so the orchestrator gets frequent HOLD partials). Does NOT
    # finalize the turn — end phrases / agent listen-end still required.
    radio_partial_silence_s: float = 0.6
    # Radio answer windows only: after speech has opened at least once, continuous
    # quiet longer than this auto-finishes the capture (soft-end path, not cancel).
    # Default is 3× end_silence_s (~6.3s). Does not apply before first open.
    # Partial cadence stays radio_partial_silence_s; short thinking pauses still OK.
    radio_idle_end_silence_s: float = 6.3
    # Radio-only: silence pad (ms) each side of a segment WAV before STT so edge
    # phonemes aren't hard-cut at the energy gate (B075). Clamped to stay well
    # under radio_partial_silence_s (see effective_radio_segment_pad_ms).
    radio_segment_pad_ms: int = 250
    # B085: real PCM lookback from ring into each radio STT window (ms)
    radio_segment_overlap_ms: int = 300
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
    # Pre-speech PCM kept when the energy gate opens (B079). Clamped 250–500 ms.
    pre_roll_ms: int = 300


@dataclass
class AmbientConfig:
    """When not answering a bound question: listen for activation → new prompt."""

    enabled: bool = False
    # names (default) | phrases — see WakePolicy / docs/CUSTOM_WAKE.md
    wake_mode: str = "names"
    names: list[str] = field(
        default_factory=lambda: ["iris", "mercury", "hark", "herald"]
    )
    # Display / exact extras / phrase-mode list (resolved)
    activation_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_ACTIVATION_PHRASES)
    )
    learn_from_near_misses: bool = True
    # vosk (default) | sherpa_kws | text_probe — never cloud during wake scan
    engine: str = "vosk"
    model_path: str | None = None
    snippet_s: float = 2.5
    # Hop between overlapping score windows (B079). None → ~0.3 * snippet_s.
    # Must stay < snippet_s so greeting+name rarely straddles non-overlapping cuts.
    snippet_hop_s: float | None = None
    # Continuous capture ring capacity (seconds). Enough for snippet + pre-roll.
    ring_s: float = 5.0
    # One-shot wake wait / continuous loop tick (seconds). 0 = wait indefinitely
    # (no ambient.timeout cycle). Continuous handsfree still uses this as the idle
    # cycle length when > 0; see surface_timeouts to hide the heartbeat event.
    timeout_s: float = 300.0
    # When true (default), continuous ambient emits ambient.timeout NDJSON/syslog
    # each idle cycle as a heartbeat. Set false for quieter long-running handsfree.
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
    # Streaming mode (B098): when true, ambient.partial HEP instructions allow
    # short live TTS acks/interim replies (not hard HOLD-only). Default false
    # keeps classic radio HOLD until ambient.prompt / final.
    # Full duplex barge-in is separate; B105 gates play on operator quiet.
    streaming: bool = False
    # B105: when streaming, live TTS play waits until operator has been quiet
    # this many seconds (or listen ends). Continuous speech without the pause
    # keeps acks deferred so half-duplex mute does not barge mid-thought.
    streaming_ack_min_quiet_s: float = 2.0
    # Full wake policy (names/phrases + learned aliases); set by load_config
    wake_policy: Any = None


@dataclass
class SttConfig:
    # auto | xai | openai | google | faster_whisper | moonshine
    # (aliases: local / whisper / faster-whisper → faster_whisper)
    provider: str = "auto"
    # Local full-STT (B072 / optional). Not used for ambient wake.
    local_model: str = "tiny.en"  # faster-whisper: tiny.en | base.en | …
    local_device: str = "cpu"  # cpu | cuda (GPU optional, never required)
    local_compute_type: str = "int8"
    local_model_path: str | None = None  # CT2 dir override; else HF cache by name
    local_fail_open: bool = True  # if local missing → cloud auto
    local_download: bool = True  # allow HF download on first use


@dataclass
class TtsConfig:
    provider: str = "auto"
    # Provider voice id (xAI: eve/ara/leo/rex/sal/… — `hark providers voices`)
    voice: str | None = None
    language: str = "en"
    # Total char cap for one run_tts (0 = unlimited). Soft word-boundary cut +
    # tts.truncated HEP for monitor if exceeded (B091).
    max_chars: int = 0
    # Per provider synth request size; long text is multi-chunk played in full.
    chunk_chars: int = 1500
    allow_espeak_fallback: bool = False
    # Print full question text to the controlling terminal when ask /
    # tts --listen speaks (B095). Default on; does not affect radio partials.
    print_prompt: bool = True


@dataclass
class ConfirmConfig:
    mode: str = "auto"


@dataclass
class SafetyConfig:
    deny_patterns: list[str] = field(default_factory=list)


@dataclass
class DashboardConfig:
    host: str = "127.0.0.1"
    port: int = 4136
    token: str | None = None
    require_token: bool = False
    tls_terminated: bool = False
    history_limit: int = 2000


@dataclass
class UpdateConfig:
    """GitHub release self-update check (B088). Notice only — no auto-install."""

    enabled: bool = True
    # owner/repo for https://api.github.com/repos/{repo}/releases/latest
    repo: str = "ultradyn/hark"


@dataclass
class AgentsConfig:
    """Coding CLI overrides for voice spawn (I005 / B055–B059)."""

    prefer_aliases: bool = True
    # agent key → command string or first PATH token (absolute path OK)
    cli: dict[str, str] = field(default_factory=dict)


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
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    update: UpdateConfig = field(default_factory=UpdateConfig)
    agents: AgentsConfig = field(default_factory=AgentsConfig)
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
pane_capture = true          # embed full pane text on blocked/needs_input/question_changed
pane_capture_lines = 100     # herdr agent read --source recent-unwrapped --lines N
pane_capture_max_chars = 12000  # cap body size in HEP / monitor compact

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
answer_arm_cue = true        # after TTS: beep when listen ready (not when speech opens)
# mute_edge_pad_ms = 300     # B084: discard after TTS unmute (not counted as user silence)
# Hold full TTS during Zoom/Teams/Meet (process list + optional audio streams)
hold_during_conference = true
conference_chime_only = true # soft cue while held; full question after call ends
# conference_process_names = ["zoom", "teams", "webex", "discord", "slack"]
conference_fail_open = true  # missing /proc or tools → allow TTS
conference_check_audio = true
# Browser Teams/Meet: Chromium Playback + Chromium input RecordStream → hold (B118)
conference_browser_av_heuristic = true
conference_poll_ms = 2000
conference_max_hold_s = 30  # seconds; 0 = wait until free (can hang if Discord idle-matched)
# Duck other media while TTS/STT runs (I002 / B045–B047) — never changes master/sink volume
# Env defaults (TOML absent only): HARK_DUCK_MEDIA_DURING_TTS/STT, HARK_PAUSE_MEDIA_DURING_TTS/STT,
# HARK_DUCK_LEVEL, HARK_MEDIA_CHECK_MPRIS. Needs pactl (volume duck); playerctl optional (MPRIS).
duck_media_during_tts = true
pause_media_during_tts = false  # true: playerctl Pause Playing players + duck rest
duck_media_during_stt = true    # duck during answer / post-wake listen (not idle wake)
pause_media_during_stt = true   # true: MPRIS Pause during STT (dogfood default on)
duck_level = 0.15               # fraction of prior per-stream volume (0.0–1.0); not 0.2
# duck_exclude_apps = ["easyeffects"]  # optional app name / binary substrings
media_check_mpris = true        # secondary media signal via playerctl
# B097: do not play TTS / mute mic while operator listen/radio is open
defer_tts_while_listening = true
defer_tts_max_wait_s = 45       # then speak anyway; 0 = wait until capture ends
defer_tts_poll_ms = 100
defer_tts_quiet_ms = 200        # settle after stream finalizes before speaking

# Bound answer windows — how spoken replies end
# Defaults are product-scoped so normal speech does not trigger control.
[listen]
end_mode = "silence"         # silence | radio
# end_mode = "radio"         # keep listening until end phrase (long pauses OK)
end_silence_s = 2.1          # quiet seconds before ending silence-mode capture
# radio_partial_silence_s = 0.6  # radio only: quiet before interim STT/partial (B037)
# radio_segment_pad_ms = 250     # radio only: silence pad each side of segment STT (B075)
# radio_segment_overlap_ms = 300 # radio only: real PCM lookback into next STT window (B085)
# radio_end_silence_s = 2.5      # legacy; segment cadence is radio_partial_silence_s
# radio_idle_end_silence_s = 6.3 # radio answer only: post-speech quiet → auto-finish
#                                # (default 3× end_silence_s; before first open: no-op)
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
# "that's all", prosign "over"). Bare "over" is end (never cancel) unless a
# phrasal-verb prev word blocks it ("turn it over", "take over"). Does NOT
# match mid-clause ("that's all I know about X", "over the weekend").
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
# Pre-speech lead-in when the gate opens (B079). Clamped 250–500 ms.
pre_roll_ms = 300

# Ambient: when NOT replying to a blocked agent question
# Continuous mic stream + overlapping local windows scan for activation;
# cloud STT only after wake (B079).
# Setup: ./scripts/setup-ambient.sh
#
# Wake customization — pick ONE style (see docs/CUSTOM_WAKE.md):
#
# 1) Name-based (default): set product names; greating+name / bare name wake.
#    Near-misses auto-learn alternate name tokens (no restart).
#      wake_mode = "names"
#      names = ["iris", "mercury", "hark", "herald"]
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
names = ["iris", "mercury", "hark", "herald"]
# extra_names = ["alice"]
# Persona TTS pairing (setup): Iris→eve, Mercury→leo
# wake_mode = "phrases"
# trigger_phrases = ["start prompt"]
# extra_trigger_phrases = ["begin dictation"]
learn_from_near_misses = true
engine = "vosk"              # vosk (default) | sherpa_kws | text_probe (tests)
# model_path auto-detected under ~/.local/share/hark/models/ when present:
#   vosk     → vosk-model-small-en-us-0.15
#   sherpa_kws → sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01
# model_path = "~/.local/share/hark/models/vosk-model-small-en-us-0.15"  # default small
# Optional larger Vosk (same engine; still needs aliases — docs/AUDIO_DESIGN.md):
# model_path = "~/.local/share/hark/models/vosk-model-en-us-0.22-lgraph"  # ~128M
# model_path = "~/.local/share/hark/models/vosk-model-en-us-0.22"         # ~1.8G
# Download: ./scripts/download-vosk-model.sh --model lgraph|0.22
# For Sherpa KWS (B070): ./scripts/download-sherpa-kws-model.sh
#   engine = "sherpa_kws"
#   # model_path = "~/.local/share/hark/models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
snippet_s = 2.5
# Overlapping wake windows (B079): hop < snippet so "hey <name>" is not chopped.
# Default hop ≈ 0.3 * snippet_s when omitted (e.g. 2.5 → 0.75 s).
# snippet_hop_s = 0.75
# Continuous mic ring capacity (seconds) while ambient is armed.
ring_s = 5.0
# One-shot wake wait / continuous idle cycle length (seconds). 0 = wait forever
# (no ambient.timeout). Continuous handsfree re-enters the wake wait each timeout_s.
timeout_s = 300
# Surface ambient.timeout on continuous idle cycles (NDJSON + syslog). Default on
# as a heartbeat (useful for provider cache / dogfood). Set false to quiet handsfree.
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
# Streaming mode (B098): short live TTS on ambient.partial (default OFF = HOLD).
# When true, partial HEP instructions allow brief interruptible acks; hark holds
# play until operator quiet ≥ streaming_ack_min_quiet_s (B105, default 2s) or
# listen ends — continuous speech without that pause is not stepped on.
# Quiet-gate mid-capture TTS is radio-only (B108): silence end_mode still
# auto-finalizes on end_silence_s and forces HOLD TTS until capture ends.
# Also clamps radio idle auto-finish so ambient.prompt lands after a natural
# pause (~end_silence_s) instead of the classic ~6.3s hold (B112).
# streaming = false
# streaming_ack_min_quiet_s = 2.0
# Once the quiet gate is dogfooded safe, operators may leave streaming = true.

# Coding CLI resolution for voice spawn (I005 / B055–B059)
# Prefer short aliases (cc/cx/gk/cr) when they are *safe* PATH binaries — not
# gcc-as-cc or CodeRabbit-as-cr. Override with absolute path or command token.
# [agents]
# prefer_aliases = true
# claude = "claude"          # or path to a cc shim
# codex = "codex"
# grok = "gk"
# cursor_agent = "cursor-agent"
# Env override: HARK_CONFIG_WATCH=0 to disable, =1 to force on.

[stt]
provider = "auto"
# Optional local full-STT for offline / privacy (B072). Cloud stays default.
# Do NOT use Whisper for continuous ambient wake (see B069 / B070 for KWS).
# provider = "faster_whisper"   # or "moonshine" (stretch) | "local" alias
# local_model = "tiny.en"       # or base.en; int8 CPU RTF ~0.1–0.15 (tiny.en, B069)
# local_device = "cpu"
# local_compute_type = "int8"
# local_model_path = ""         # optional on-disk CT2 model dir
# local_fail_open = true        # fall back to cloud auto if local unavailable
# local_download = true         # allow Hugging Face model download on first use
# Env: HARK_STT_PROVIDER, HARK_STT_LOCAL_MODEL, HARK_STT_LOCAL_FAIL_OPEN, …

[tts]
provider = "auto"
voice = "eve"                # xAI: eve ara leo rex sal … — hark providers voices
language = "en"
# voice = "ara"
# max_chars = 0                # total cap per TTS call; 0 = unlimited (speak full agent text)
# chunk_chars = 1500           # per synth request; multi-chunk plays in full (B091)
print_prompt = true          # print question text to terminal on ask / tts --listen (B095)

[confirm]
mode = "auto"

[safety]
deny_patterns = []

[dashboard]
# Live web dashboard (`hark webui` / `hark dashboard` / `hark serve`) — see docs/DASHBOARD.md
host = "127.0.0.1"           # non-localhost requires a token
port = 4136
# token = ""                 # generate: hark webui --print-token
# require_token = false      # force auth even on localhost
# tls_terminated = false     # true behind `tailscale serve` (Secure cookies)
# history_limit = 2000       # backfill window per source

[update]
# GitHub release self-update check (B088) — notice only, never auto-installs
enabled = true
# repo = "ultradyn/hark"     # owner/repo for releases/latest
# disable: enabled = false, or env HARK_UPDATE_CHECK=0
"""


def _radio_idle_end_silence_s(
    listen_raw: dict[str, Any], *, end_silence_s: float
) -> float:
    """Post-speech radio idle auto-finish (B074). Default 3× end_silence_s."""
    if "radio_idle_end_silence_s" in listen_raw:
        return float(listen_raw["radio_idle_end_silence_s"])
    return 3.0 * float(end_silence_s)


def _as_list_str(value: Any, default: list[str]) -> list[str]:
    if value is None:
        return list(default)
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    return list(default)


def _clamp_pre_roll_ms(value: Any, *, default: int = 300) -> int:
    """Clamp listen.pre_roll_ms to the B079 range (250–500 ms)."""
    if value is None:
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(250, min(500, v))


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


def _env_bool(name: str, default: bool) -> bool:
    """Read a HARK_* bool env var; unset/empty → default.

    True: 1/true/yes/on (case-insensitive). Everything else → False when set.
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


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
        model_path=_resolve_ambient_model_path(
            str(ambient_raw["model_path"])
            if ambient_raw.get("model_path")
            else (
                os.environ.get("HARK_WAKE_MODEL")
                or os.environ.get("HARK_VOSK_MODEL")
            ),
            engine=str(ambient_raw.get("engine", "vosk")),
        ),
        snippet_s=float(ambient_raw.get("snippet_s", 2.5)),
        snippet_hop_s=(
            float(ambient_raw["snippet_hop_s"])
            if ambient_raw.get("snippet_hop_s") is not None
            else None
        ),
        ring_s=float(ambient_raw.get("ring_s", 5.0)),
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
        streaming=_as_bool(ambient_raw.get("streaming"), default=False),
        streaming_ack_min_quiet_s=float(
            ambient_raw.get("streaming_ack_min_quiet_s", 2.0)
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


def _models_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "hark" / "models"


def default_vosk_model_path() -> Path:
    return _models_root() / "vosk-model-small-en-us-0.15"


def default_sherpa_kws_model_path() -> Path:
    return _models_root() / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"


def _resolve_vosk_model_path(raw: str | None) -> str | None:
    """Legacy helper — prefer :func:`_resolve_ambient_model_path`."""
    return _resolve_ambient_model_path(raw, engine="vosk")


def _resolve_ambient_model_path(
    raw: str | None,
    *,
    engine: str = "vosk",
) -> str | None:
    """Resolve ambient.model_path for the selected wake engine.

    Explicit ``model_path`` always wins. Otherwise auto-detect the usual
    XDG install location for vosk or sherpa_kws when present.
    """
    if raw:
        p = Path(os.path.expanduser(raw))
        return str(p)
    eng = (engine or "vosk").strip().lower()
    if eng in ("sherpa_kws", "sherpa", "kws"):
        auto = default_sherpa_kws_model_path()
        try:
            from hark.wake import is_sherpa_kws_model_dir

            if is_sherpa_kws_model_dir(auto):
                return str(auto)
        except Exception:
            if auto.is_dir() and (auto / "tokens.txt").is_file():
                return str(auto)
        return None
    # vosk / text_probe / default
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
    dashboard_raw = raw.get("dashboard") if isinstance(raw.get("dashboard"), dict) else {}
    update_raw = raw.get("update") if isinstance(raw.get("update"), dict) else {}

    stt_provider = os.environ.get("HARK_STT_PROVIDER") or str(
        stt_raw.get("provider", "auto")
    )
    stt_local_model = os.environ.get("HARK_STT_LOCAL_MODEL") or str(
        stt_raw.get("local_model", "tiny.en")
    )
    stt_local_device = os.environ.get("HARK_STT_LOCAL_DEVICE") or str(
        stt_raw.get("local_device", "cpu")
    )
    stt_local_compute = os.environ.get("HARK_STT_LOCAL_COMPUTE_TYPE") or str(
        stt_raw.get("local_compute_type", "int8")
    )
    stt_local_model_path = os.environ.get("HARK_STT_LOCAL_MODEL_PATH") or (
        str(stt_raw["local_model_path"])
        if stt_raw.get("local_model_path")
        else None
    )
    if stt_local_model_path is not None and not str(stt_local_model_path).strip():
        stt_local_model_path = None
    env_fail_open = os.environ.get("HARK_STT_LOCAL_FAIL_OPEN")
    if env_fail_open is not None:
        stt_local_fail_open = env_fail_open.strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    else:
        stt_local_fail_open = _as_bool(stt_raw.get("local_fail_open"), default=True)
    env_download = os.environ.get("HARK_STT_LOCAL_DOWNLOAD")
    if env_download is not None:
        stt_local_download = env_download.strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
    else:
        stt_local_download = _as_bool(stt_raw.get("local_download"), default=True)
    tts_provider = str(tts_raw.get("provider", "auto"))

    end_mode_raw = str(listen_raw.get("end_mode", "silence"))
    if os.environ.get("HARK_LISTEN_END_MODE"):
        end_mode_raw = os.environ["HARK_LISTEN_END_MODE"]
    try:
        end_mode = parse_end_mode(end_mode_raw).value
    except ValueError as exc:
        warnings.append(str(exc))
        end_mode = EndMode.SILENCE.value

    end_silence_s = float(listen_raw.get("end_silence_s", 2.1))
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
            pane_capture=_as_bool(watch_raw.get("pane_capture"), default=True),
            pane_capture_lines=max(1, int(watch_raw.get("pane_capture_lines", 100))),
            pane_capture_max_chars=max(
                256, int(watch_raw.get("pane_capture_max_chars", 12000))
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
            answer_arm_cue=bool(audio_raw.get("answer_arm_cue", True)),
            mute_edge_pad_ms=int(audio_raw.get("mute_edge_pad_ms", 300)),
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
            conference_browser_av_heuristic=bool(
                audio_raw.get("conference_browser_av_heuristic", True)
            ),
            conference_poll_ms=int(audio_raw.get("conference_poll_ms", 2000)),
            conference_max_hold_s=float(audio_raw.get("conference_max_hold_s", 0)),
            # Media ducking (I002 / B045–B047). Env HARK_* supplies defaults
            # when the TOML key is absent (same pattern as HARK_CUE_VOLUME /
            # HARK_HOLD_DURING_CONFERENCE) — explicit TOML wins.
            duck_media_during_tts=_as_bool(
                audio_raw.get("duck_media_during_tts"),
                default=_env_bool("HARK_DUCK_MEDIA_DURING_TTS", True),
            ),
            pause_media_during_tts=_as_bool(
                audio_raw.get("pause_media_during_tts"),
                default=_env_bool("HARK_PAUSE_MEDIA_DURING_TTS", False),
            ),
            duck_media_during_stt=_as_bool(
                audio_raw.get("duck_media_during_stt"),
                default=_env_bool("HARK_DUCK_MEDIA_DURING_STT", True),
            ),
            pause_media_during_stt=_as_bool(
                audio_raw.get("pause_media_during_stt"),
                default=_env_bool("HARK_PAUSE_MEDIA_DURING_STT", True),
            ),
            duck_level=float(
                audio_raw.get(
                    "duck_level",
                    os.environ.get("HARK_DUCK_LEVEL", 0.15),
                )
            ),
            duck_exclude_apps=_as_list_str(
                audio_raw.get("duck_exclude_apps"),
                [],
            ),
            media_check_mpris=_as_bool(
                audio_raw.get("media_check_mpris"),
                default=_env_bool("HARK_MEDIA_CHECK_MPRIS", True),
            ),
            defer_tts_while_listening=_as_bool(
                audio_raw.get("defer_tts_while_listening"),
                default=True,
            ),
            defer_tts_max_wait_s=float(
                audio_raw.get("defer_tts_max_wait_s", 45.0)
            ),
            defer_tts_poll_ms=int(audio_raw.get("defer_tts_poll_ms", 100)),
            defer_tts_quiet_ms=int(audio_raw.get("defer_tts_quiet_ms", 200)),
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
            end_silence_s=end_silence_s,
            radio_end_silence_s=float(listen_raw.get("radio_end_silence_s", 2.5)),
            radio_partial_silence_s=float(
                listen_raw.get("radio_partial_silence_s", 0.6)
            ),
            radio_idle_end_silence_s=_radio_idle_end_silence_s(
                listen_raw, end_silence_s=end_silence_s
            ),
            radio_segment_pad_ms=int(listen_raw.get("radio_segment_pad_ms", 250)),
            radio_segment_overlap_ms=int(listen_raw.get("radio_segment_overlap_ms", 300)),
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
            pre_roll_ms=_clamp_pre_roll_ms(listen_raw.get("pre_roll_ms", 300)),
        ),
        ambient=_build_ambient_config(
            ambient_raw if isinstance(ambient_raw, dict) else {},
            ambient_enabled=ambient_enabled,
        ),
        stt=SttConfig(
            provider=stt_provider,
            local_model=stt_local_model,
            local_device=stt_local_device,
            local_compute_type=stt_local_compute,
            local_model_path=stt_local_model_path,
            local_fail_open=stt_local_fail_open,
            local_download=stt_local_download,
        ),
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
            max_chars=int(tts_raw.get("max_chars", 0)),
            chunk_chars=int(tts_raw.get("chunk_chars", 1500)),
            allow_espeak_fallback=bool(tts_raw.get("allow_espeak_fallback", False)),
            print_prompt=_as_bool(tts_raw.get("print_prompt"), default=True),
        ),
        confirm=ConfirmConfig(mode=str(confirm_raw.get("mode", "auto"))),
        safety=SafetyConfig(
            deny_patterns=_as_list_str(safety_raw.get("deny_patterns"), [])
        ),
        dashboard=DashboardConfig(
            host=str(dashboard_raw.get("host", "127.0.0.1")),
            port=int(dashboard_raw.get("port", 4136)),
            token=(
                str(dashboard_raw["token"])
                if dashboard_raw.get("token")
                else os.environ.get("HARK_DASHBOARD_TOKEN")
            ),
            require_token=_as_bool(
                dashboard_raw.get("require_token"), default=False
            ),
            tls_terminated=_as_bool(
                dashboard_raw.get("tls_terminated"), default=False
            ),
            history_limit=int(dashboard_raw.get("history_limit", 2000)),
        ),
        update=_build_update_config(update_raw),
        agents=_build_agents_config(
            raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
        ),
        path=cfg_path if cfg_path.is_file() else None,
        warnings=warnings,
    )


def _build_update_config(update_raw: dict[str, Any]) -> UpdateConfig:
    """Parse ``[update]`` (B088 GitHub release check)."""
    enabled = _as_bool(update_raw.get("enabled"), default=True)
    env_en = os.environ.get("HARK_UPDATE_CHECK")
    if env_en is not None:
        enabled = env_en.strip().lower() not in ("0", "false", "no", "off", "disabled")
    repo = str(update_raw.get("repo") or "ultradyn/hark").strip() or "ultradyn/hark"
    env_repo = os.environ.get("HARK_UPDATE_REPO")
    if env_repo and env_repo.strip():
        repo = env_repo.strip()
    return UpdateConfig(enabled=enabled, repo=repo.lstrip("/"))


def _build_agents_config(agents_raw: dict[str, Any]) -> AgentsConfig:
    """Parse ``[agents]`` CLI overrides for voice spawn."""
    prefer = bool(agents_raw.get("prefer_aliases", True))
    cli: dict[str, str] = {}

    def _store(key: str, value: Any) -> None:
        if value is None:
            return
        val = str(value).strip()
        if not val:
            return
        k = str(key).strip()
        if k.replace("-", "_") == "cursor_agent":
            k = "cursor-agent"
        cli[k] = val

    nested = agents_raw.get("cli")
    if isinstance(nested, dict):
        for k, v in nested.items():
            _store(str(k), v)
    for flat in (
        "claude",
        "codex",
        "grok",
        "opencode",
        "pi",
        "agy",
        "cursor_agent",
        "cursor-agent",
    ):
        if flat in agents_raw:
            _store(flat, agents_raw[flat])
    return AgentsConfig(prefer_aliases=prefer, cli=cli)


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
            "pane_capture": cfg.watch.pane_capture,
            "pane_capture_lines": cfg.watch.pane_capture_lines,
            "pane_capture_max_chars": cfg.watch.pane_capture_max_chars,
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
            "answer_arm_cue": cfg.audio.answer_arm_cue,
            "mute_edge_pad_ms": cfg.audio.mute_edge_pad_ms,
            "hold_during_conference": cfg.audio.hold_during_conference,
            "conference_chime_only": cfg.audio.conference_chime_only,
            "conference_process_names": list(cfg.audio.conference_process_names),
            "conference_fail_open": cfg.audio.conference_fail_open,
            "conference_check_audio": cfg.audio.conference_check_audio,
            "conference_browser_av_heuristic": cfg.audio.conference_browser_av_heuristic,
            "conference_poll_ms": cfg.audio.conference_poll_ms,
            "conference_max_hold_s": cfg.audio.conference_max_hold_s,
            "duck_media_during_tts": cfg.audio.duck_media_during_tts,
            "pause_media_during_tts": cfg.audio.pause_media_during_tts,
            "duck_media_during_stt": cfg.audio.duck_media_during_stt,
            "pause_media_during_stt": cfg.audio.pause_media_during_stt,
            "duck_level": cfg.audio.duck_level,
            "duck_exclude_apps": list(cfg.audio.duck_exclude_apps),
            "media_check_mpris": cfg.audio.media_check_mpris,
            "defer_tts_while_listening": cfg.audio.defer_tts_while_listening,
            "defer_tts_max_wait_s": cfg.audio.defer_tts_max_wait_s,
            "defer_tts_poll_ms": cfg.audio.defer_tts_poll_ms,
            "defer_tts_quiet_ms": cfg.audio.defer_tts_quiet_ms,
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
            "radio_idle_end_silence_s": cfg.listen.radio_idle_end_silence_s,
            "radio_segment_pad_ms": cfg.listen.radio_segment_pad_ms,
            "radio_segment_overlap_ms": cfg.listen.radio_segment_overlap_ms,
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
            "pre_roll_ms": cfg.listen.pre_roll_ms,
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
            "snippet_hop_s": cfg.ambient.snippet_hop_s,
            "ring_s": cfg.ambient.ring_s,
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
            "streaming": cfg.ambient.streaming,
            "streaming_ack_min_quiet_s": cfg.ambient.streaming_ack_min_quiet_s,
        },
        "stt": {
            "provider": cfg.stt.provider,
            "local_model": cfg.stt.local_model,
            "local_device": cfg.stt.local_device,
            "local_compute_type": cfg.stt.local_compute_type,
            "local_model_path": cfg.stt.local_model_path,
            "local_fail_open": cfg.stt.local_fail_open,
            "local_download": cfg.stt.local_download,
        },
        "tts": {
            "provider": cfg.tts.provider,
            "voice": cfg.tts.voice,
            "language": cfg.tts.language,
            "max_chars": cfg.tts.max_chars,
            "chunk_chars": cfg.tts.chunk_chars,
            "print_prompt": cfg.tts.print_prompt,
        },
        "confirm": {"mode": cfg.confirm.mode},
        "dashboard": {
            "host": cfg.dashboard.host,
            "port": cfg.dashboard.port,
            # never the token itself (docs/DASHBOARD.md redaction contract)
            "token_configured": bool(cfg.dashboard.token),
            "require_token": cfg.dashboard.require_token,
            "tls_terminated": cfg.dashboard.tls_terminated,
            "history_limit": cfg.dashboard.history_limit,
        },
        "update": {
            "enabled": cfg.update.enabled,
            "repo": cfg.update.repo,
        },
        "agents": {
            "prefer_aliases": cfg.agents.prefer_aliases,
            "cli": dict(cfg.agents.cli),
        },
    }


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)

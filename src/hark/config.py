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
    EndMode,
    parse_end_mode,
)
from hark.paths import default_config_path, default_herdr_socket
from hark.wake import DEFAULT_ACTIVATION_PHRASES


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
    "watch": frozenset({"statuses", "debounce_ms", "transport", "poll_ms", "heartbeat_s"}),
    "audio": frozenset({
        "half_duplex",
        "post_tts_guard_ms",
        "listen_pre_arm_ms",
        "mute_mic_during_tts",
        "sync_hw_unmute",
        "cue_volume",
        "cue_start_path",
        "cue_stop_path",
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
        "stream_partials",
        "empty_stt_retry",
        "empty_stt_nudge",
    }),
    "ambient": frozenset({
        "enabled",
        "activation_phrases",
        "trigger_phrases",  # alias of activation_phrases
        "extra_activation_phrases",  # append to defaults (or to base list)
        "extra_trigger_phrases",  # alias of extra_activation_phrases
        "engine",
        "model_path",
        "snippet_s",
        "timeout_s",
        "debug",
        "debug_retention_days",
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


@dataclass
class AudioConfig:
    half_duplex: bool = True
    # After TTS ends, wait this long before arming capture (tight handoff)
    post_tts_guard_ms: int = 100
    # Fire near-end callback this many ms before TTS finishes (pre-arm signal)
    listen_pre_arm_ms: int = 300
    # Mute system default capture during TTS (Wave ring white→red via pactl)
    mute_mic_during_tts: bool = True
    # Watch Wave/ALSA/Pulse mute edges; hardware unmute → force OS unmute
    sync_hw_unmute: bool = True
    # Record start/stop cue volume for generated blips (0.0–1.0)
    cue_volume: float = 0.22
    # Optional custom WAV/MP3 paths (empty = assets/cues defaults)
    cue_start_path: str | None = None
    cue_stop_path: str | None = None


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
    # Longer hang for radio mode segment boundaries
    radio_end_silence_s: float = 2.5
    # Radio mode: emit interim STT to agent with HOLD warnings (before end phrase)
    stream_partials: bool = True
    # After empty STT (gate opened but no text): one automatic re-listen
    empty_stt_retry: bool = True
    # If still empty after retry: TTS "Sorry, I didn't catch that." then re-listen once
    empty_stt_nudge: bool = True


@dataclass
class AmbientConfig:
    """When not answering a bound question: listen for activation → new prompt."""

    enabled: bool = False
    activation_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_ACTIVATION_PHRASES)
    )
    # local | vosk | text_probe — never cloud during wake scan
    engine: str = "vosk"
    model_path: str | None = None
    snippet_s: float = 2.5
    timeout_s: float = 300.0
    # Dev: save wake audio+text under state/debug/wake (7-day cleanup)
    debug: bool = False
    debug_retention_days: float = 7.0


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

[audio]
half_duplex = true
post_tts_guard_ms = 100      # after TTS ends → start listen (tight handoff)
listen_pre_arm_ms = 300      # signal ~0.3s before TTS ends
mute_mic_during_tts = true   # pactl mute → Elgato Wave ring red while speaking
sync_hw_unmute = true        # Wave/ALSA unmute button → force OS/Pulse unmute
cue_volume = 0.22            # generated start/stop beep volume (0–1)
# cue_start_path = "/path/to/record-start.wav"
# cue_stop_path  = "/path/to/record-stop.wav"

# Bound answer windows — how spoken replies end
# Defaults are product-scoped so normal speech does not trigger control.
[listen]
end_mode = "silence"         # silence | radio
# end_mode = "radio"         # keep listening until end phrase (long pauses OK)
end_silence_s = 2.1          # quiet seconds before ending silence-mode capture
# radio_end_silence_s = 2.5
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
strip_phrase = true
max_listen_s = 300
empty_stt_retry = true       # re-listen once if STT returns empty transcript
empty_stt_nudge = true       # TTS "Sorry, I didn't catch that." then re-listen once more

# Ambient: when NOT replying to a blocked agent question
# Local 2–3s snippets scan for activation; cloud STT only after wake.
# Setup: ./scripts/setup-ambient.sh
#
# Custom trigger / wake phrases (any of these):
#   activation_phrases / trigger_phrases  — full list (replaces defaults if set)
#   extra_activation_phrases / extra_trigger_phrases — appended to base list
# After edit: kill -HUP <ambient-pid> reloads phrases without full restart
# (see docs/CUSTOM_WAKE.md). Restart also works.
#
# Examples:
#   extra_trigger_phrases = ["start prompt", "begin dictation"]
#   trigger_phrases = ["start prompt"]   # ONLY this wake (no hey hark)
[ambient]
enabled = false
activation_phrases = [
  "hey hark",
  "hey herald",
  "hello hark",
  "hello herald",
  "okay hark",
  "ok hark",
]
# extra_trigger_phrases = ["start prompt"]
engine = "vosk"              # vosk | text_probe (tests)
# model_path = "~/.local/share/hark/models/vosk-model-small-en-us-0.15"
snippet_s = 2.5
timeout_s = 300
debug = true                 # save wake wav+text under ~/.local/state/hark/debug/wake
debug_retention_days = 7

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


def resolve_activation_phrases(ambient_raw: dict[str, Any]) -> list[str]:
    """Build ambient wake/trigger phrase list from config.

    Keys (any combination):
      activation_phrases / trigger_phrases — replace defaults when set
      extra_activation_phrases / extra_trigger_phrases — append always

    Example — keep defaults and add a custom wake::

        [ambient]
        extra_trigger_phrases = ["start prompt", "begin dictation"]

    Example — only custom wakes (no hey hark)::

        [ambient]
        trigger_phrases = ["start prompt", "begin recording"]
    """
    primary = ambient_raw.get("activation_phrases")
    if primary is None:
        primary = ambient_raw.get("trigger_phrases")
    if primary is None:
        base = list(DEFAULT_ACTIVATION_PHRASES)
    else:
        base = _as_list_str(primary, [])

    extras: list[str] = []
    for key in ("extra_activation_phrases", "extra_trigger_phrases"):
        if key in ambient_raw and ambient_raw[key] is not None:
            extras.extend(_as_list_str(ambient_raw[key], []))

    return _dedupe_phrases(base + extras)


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
        ),
        audio=AudioConfig(
            half_duplex=bool(audio_raw.get("half_duplex", True)),
            post_tts_guard_ms=int(audio_raw.get("post_tts_guard_ms", 100)),
            listen_pre_arm_ms=int(audio_raw.get("listen_pre_arm_ms", 300)),
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
            stream_partials=bool(listen_raw.get("stream_partials", True)),
            empty_stt_retry=bool(listen_raw.get("empty_stt_retry", True)),
            empty_stt_nudge=bool(listen_raw.get("empty_stt_nudge", True)),
        ),
        ambient=AmbientConfig(
            enabled=ambient_enabled,
            activation_phrases=resolve_activation_phrases(
                ambient_raw if isinstance(ambient_raw, dict) else {}
            ),
            engine=str(ambient_raw.get("engine", "vosk")),
            model_path=_resolve_vosk_model_path(
                str(ambient_raw["model_path"])
                if ambient_raw.get("model_path")
                else os.environ.get("HARK_VOSK_MODEL")
            ),
            snippet_s=float(ambient_raw.get("snippet_s", 2.5)),
            timeout_s=float(ambient_raw.get("timeout_s", 300)),
            debug=bool(
                ambient_raw.get(
                    "debug",
                    os.environ.get("HARK_DEBUG", "").lower()
                    in ("1", "true", "yes", "on"),
                )
            ),
            debug_retention_days=float(ambient_raw.get("debug_retention_days", 7)),
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
        },
        "audio": {
            "half_duplex": cfg.audio.half_duplex,
            "post_tts_guard_ms": cfg.audio.post_tts_guard_ms,
            "listen_pre_arm_ms": cfg.audio.listen_pre_arm_ms,
            "mute_mic_during_tts": cfg.audio.mute_mic_during_tts,
            "sync_hw_unmute": cfg.audio.sync_hw_unmute,
            "cue_volume": cfg.audio.cue_volume,
            "cue_start_path": cfg.audio.cue_start_path,
            "cue_stop_path": cfg.audio.cue_stop_path,
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
            "stream_partials": cfg.listen.stream_partials,
            "empty_stt_retry": cfg.listen.empty_stt_retry,
            "empty_stt_nudge": cfg.listen.empty_stt_nudge,
        },
        "ambient": {
            "enabled": cfg.ambient.enabled,
            "activation_phrases": list(cfg.ambient.activation_phrases),
            "engine": cfg.ambient.engine,
            "model_path": cfg.ambient.model_path,
            "snippet_s": cfg.ambient.snippet_s,
            "timeout_s": cfg.ambient.timeout_s,
            "debug": cfg.ambient.debug,
            "debug_retention_days": cfg.ambient.debug_retention_days,
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

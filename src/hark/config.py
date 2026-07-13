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


KNOWN_TOP_KEYS = frozenset(
    {
        "version",
        "herdr",
        "watch",
        "audio",
        "listen",
        "stt",
        "tts",
        "confirm",
        "safety",
    }
)


@dataclass
class SessionConfig:
    id: str
    socket: str | None = None
    ssh: str | None = None
    herdr_bin: str | None = None
    label: str | None = None


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
    post_tts_guard_ms: int = 350


@dataclass
class ListenConfig:
    """How a spoken reply ends (global: ~/.config/hark/config.toml).

    end_mode:
      silence — end on Smart Turn / end-silence (default)
      radio   — keep listening through long pauses until an end phrase
                (like radio "over"), e.g. "okay send it" / "end prompt"
    """

    end_mode: str = EndMode.SILENCE.value
    end_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_END_PHRASES)
    )
    cancel_phrases: list[str] = field(
        default_factory=lambda: list(DEFAULT_CANCEL_PHRASES)
    )
    strip_phrase: bool = True
    # hard cap even in radio mode (operator safety)
    max_listen_s: float = 300.0
    # optional spoken nudge after this much trailing silence (0 = off)
    nudge_silence_s: float = 0.0


@dataclass
class SttConfig:
    provider: str = "auto"


@dataclass
class TtsConfig:
    provider: str = "auto"
    max_chars: int = 500
    allow_espeak_fallback: bool = False


@dataclass
class ConfirmConfig:
    mode: str = "auto"  # auto | always | never — R2/R3 always force confirm


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
# Hark config — see docs/SPEC.md
version = 1

[[herdr.sessions]]
id = "local"
# socket = "~/.config/herdr/herdr.sock"  # default if unset
# label = "local herdr"

[watch]
statuses = ["blocked", "done"]
debounce_ms = 250
transport = "auto"
poll_ms = 1000

[audio]
half_duplex = true
post_tts_guard_ms = 350

# How spoken replies end (see docs/AUDIO_DESIGN.md § Radio end)
# silence = Smart Turn / end-silence (default)
# radio   = keep listening through long pauses until an end phrase
[listen]
end_mode = "silence"
# end_mode = "radio"
end_phrases = [
  "okay send it",
  "ok send it",
  "send it",
  "end prompt",
  "end of prompt",
  "end of message",
  "over",
]
cancel_phrases = [
  "cancel that",
  "never mind",
  "scratch that",
  "abort send",
]
strip_phrase = true
max_listen_s = 300
# nudge_silence_s = 45   # optional "still listening" after long quiet (0 = off)

[stt]
provider = "auto"

[tts]
provider = "auto"
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


def load_config(path: Path | None = None) -> HarkConfig:
    """Load config from path or default; missing file → defaults with local session."""
    cfg_path = path or default_config_path()
    raw: dict[str, Any] = {}
    warnings: list[str] = []

    if cfg_path.is_file():
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh) or {}
        for key in raw:
            if key not in KNOWN_TOP_KEYS:
                warnings.append(f"unknown config key: {key!r}")
    # else: defaults only

    herdr = raw.get("herdr") or {}
    sessions_raw = herdr.get("sessions") if isinstance(herdr, dict) else None
    sessions: list[SessionConfig] = []
    if isinstance(sessions_raw, list):
        for item in sessions_raw:
            if not isinstance(item, dict) or "id" not in item:
                warnings.append(f"skipping invalid session entry: {item!r}")
                continue
            sessions.append(
                SessionConfig(
                    id=str(item["id"]),
                    socket=item.get("socket"),
                    ssh=item.get("ssh"),
                    herdr_bin=item.get("herdr_bin"),
                    label=item.get("label"),
                )
            )
    if not sessions:
        sessions = [SessionConfig(id="local")]

    # Env HERDR_SOCKET_PATH overrides first/local session socket when set
    env_sock = os.environ.get("HERDR_SOCKET_PATH")
    if env_sock:
        for s in sessions:
            if s.id == "local" or s.socket is None:
                s.socket = env_sock
                break

    watch_raw = raw.get("watch") or {}
    audio_raw = raw.get("audio") or {}
    listen_raw = raw.get("listen") or {}
    stt_raw = raw.get("stt") or {}
    tts_raw = raw.get("tts") or {}
    confirm_raw = raw.get("confirm") or {}
    safety_raw = raw.get("safety") or {}

    stt_provider = os.environ.get("HARK_STT_PROVIDER") or (
        str(stt_raw.get("provider", "auto")) if isinstance(stt_raw, dict) else "auto"
    )
    tts_provider = (
        str(tts_raw.get("provider", "auto")) if isinstance(tts_raw, dict) else "auto"
    )

    # listen.end_mode: config, then env HARK_LISTEN_END_MODE
    end_mode_raw = "silence"
    if isinstance(listen_raw, dict) and listen_raw.get("end_mode") is not None:
        end_mode_raw = str(listen_raw.get("end_mode"))
    if os.environ.get("HARK_LISTEN_END_MODE"):
        end_mode_raw = os.environ["HARK_LISTEN_END_MODE"]
    try:
        end_mode = parse_end_mode(end_mode_raw).value
    except ValueError as exc:
        warnings.append(str(exc))
        end_mode = EndMode.SILENCE.value

    if isinstance(listen_raw, dict):
        end_phrases = _as_list_str(
            listen_raw.get("end_phrases"), list(DEFAULT_END_PHRASES)
        )
        cancel_phrases = _as_list_str(
            listen_raw.get("cancel_phrases"), list(DEFAULT_CANCEL_PHRASES)
        )
        strip_phrase = bool(listen_raw.get("strip_phrase", True))
        max_listen_s = float(listen_raw.get("max_listen_s", 300))
        nudge_silence_s = float(listen_raw.get("nudge_silence_s", 0))
    else:
        end_phrases = list(DEFAULT_END_PHRASES)
        cancel_phrases = list(DEFAULT_CANCEL_PHRASES)
        strip_phrase = True
        max_listen_s = 300.0
        nudge_silence_s = 0.0

    return HarkConfig(
        version=int(raw.get("version", 1)) if isinstance(raw.get("version", 1), int) else 1,
        sessions=sessions,
        watch=WatchConfig(
            statuses=_as_list_str(
                watch_raw.get("statuses") if isinstance(watch_raw, dict) else None,
                ["blocked", "done"],
            ),
            debounce_ms=int(watch_raw.get("debounce_ms", 250))
            if isinstance(watch_raw, dict)
            else 250,
            transport=str(watch_raw.get("transport", "auto"))
            if isinstance(watch_raw, dict)
            else "auto",
            poll_ms=int(watch_raw.get("poll_ms", 1000))
            if isinstance(watch_raw, dict)
            else 1000,
            heartbeat_s=float(watch_raw.get("heartbeat_s", 30.0))
            if isinstance(watch_raw, dict)
            else 30.0,
        ),
        audio=AudioConfig(
            half_duplex=bool(audio_raw.get("half_duplex", True))
            if isinstance(audio_raw, dict)
            else True,
            post_tts_guard_ms=int(audio_raw.get("post_tts_guard_ms", 350))
            if isinstance(audio_raw, dict)
            else 350,
        ),
        listen=ListenConfig(
            end_mode=end_mode,
            end_phrases=end_phrases,
            cancel_phrases=cancel_phrases,
            strip_phrase=strip_phrase,
            max_listen_s=max_listen_s,
            nudge_silence_s=nudge_silence_s,
        ),
        stt=SttConfig(provider=stt_provider),
        tts=TtsConfig(
            provider=tts_provider,
            max_chars=int(tts_raw.get("max_chars", 500))
            if isinstance(tts_raw, dict)
            else 500,
            allow_espeak_fallback=bool(tts_raw.get("allow_espeak_fallback", False))
            if isinstance(tts_raw, dict)
            else False,
        ),
        confirm=ConfirmConfig(
            mode=str(confirm_raw.get("mode", "auto"))
            if isinstance(confirm_raw, dict)
            else "auto"
        ),
        safety=SafetyConfig(
            deny_patterns=_as_list_str(
                safety_raw.get("deny_patterns") if isinstance(safety_raw, dict) else None,
                [],
            )
        ),
        path=cfg_path if cfg_path.is_file() else None,
        warnings=warnings,
    )


def resolve_session_socket(session: SessionConfig) -> Path:
    if session.socket:
        return Path(os.path.expanduser(session.socket))
    if session.id == "local":
        return default_herdr_socket()
    # Named Herdr sessions
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
        "listen": {
            "end_mode": cfg.listen.end_mode,
            "end_phrases": list(cfg.listen.end_phrases),
            "cancel_phrases": list(cfg.listen.cancel_phrases),
            "strip_phrase": cfg.listen.strip_phrase,
            "max_listen_s": cfg.listen.max_listen_s,
            "nudge_silence_s": cfg.listen.nudge_silence_s,
        },
        "stt": {"provider": cfg.stt.provider},
        "tts": {
            "provider": cfg.tts.provider,
            "max_chars": cfg.tts.max_chars,
        },
        "confirm": {"mode": cfg.confirm.mode},
    }


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)

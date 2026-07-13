"""Non-verbal audio cues (record start/stop beeps) + on-disk TTS phrase cache."""

from __future__ import annotations

import hashlib
import io
import math
import os
import re
import struct
import wave
from pathlib import Path
from typing import TYPE_CHECKING

from hark.audio.playback import play_audio
from hark.syslog import log as syslog

if TYPE_CHECKING:
    from hark.config import AudioConfig, HarkConfig

# Repo-relative defaults (overridable)
_REPO_ROOT = Path(__file__).resolve().parents[3]
CUES_DIR = _REPO_ROOT / "assets" / "cues"
TTS_CACHE_DIR = _REPO_ROOT / "assets" / "tts"

# Single soft blips (one tone each)
RECORD_START_HZ = 880.0
RECORD_STOP_HZ = 660.0
BEEP_MS = 55
SAMPLE_RATE = 22050
DEFAULT_VOLUME = 0.22

# Runtime overrides from config (set via configure_cues)
_cue_volume: float = DEFAULT_VOLUME
_cue_start_path: Path | None = None
_cue_stop_path: Path | None = None


def configure_cues(
    *,
    volume: float | None = None,
    start_path: str | Path | None = None,
    stop_path: str | Path | None = None,
) -> None:
    """Apply cue settings (usually from HarkConfig.audio)."""
    global _cue_volume, _cue_start_path, _cue_stop_path
    if volume is not None:
        _cue_volume = max(0.0, min(1.0, float(volume)))
    if start_path is not None:
        p = Path(os.path.expanduser(str(start_path))) if str(start_path).strip() else None
        _cue_start_path = p if p and str(start_path).strip() else None
    if stop_path is not None:
        p = Path(os.path.expanduser(str(stop_path))) if str(stop_path).strip() else None
        _cue_stop_path = p if p and str(stop_path).strip() else None


def configure_cues_from_config(cfg: "HarkConfig | AudioConfig") -> None:
    audio = getattr(cfg, "audio", cfg)
    configure_cues(
        volume=getattr(audio, "cue_volume", DEFAULT_VOLUME),
        start_path=getattr(audio, "cue_start_path", None) or "",
        stop_path=getattr(audio, "cue_stop_path", None) or "",
    )


def _sine_pcm(
    freq: float,
    ms: int,
    sr: int = SAMPLE_RATE,
    vol: float | None = None,
) -> bytes:
    vol = DEFAULT_VOLUME if vol is None else vol
    n = int(sr * ms / 1000)
    attack = max(1, int(sr * 0.012))
    out = bytearray()
    for i in range(n):
        t = i / sr
        env = 1.0
        if i < attack:
            env = i / attack
        elif i > n - attack:
            env = max(0.0, (n - i) / attack)
        sample = int(32767 * vol * env * math.sin(2 * math.pi * freq * t))
        out += struct.pack("<h", max(-32767, min(32767, sample)))
    return bytes(out)


def _pcm_to_wav(pcm: bytes, sr: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm)
    return buf.getvalue()


def build_beep(freq: float, ms: int = BEEP_MS, vol: float | None = None) -> bytes:
    """One short soft sine blip as WAV bytes."""
    return _pcm_to_wav(_sine_pcm(freq, ms, vol=vol if vol is not None else _cue_volume))


def build_beep_pair(freqs: tuple[float, float] | float) -> bytes:
    if isinstance(freqs, (tuple, list)):
        return build_beep(float(freqs[0]))
    return build_beep(float(freqs))


def ensure_cue_files(*, force: bool = False) -> dict[str, Path]:
    """Write default baked cue WAVs if no custom path and file missing."""
    CUES_DIR.mkdir(parents=True, exist_ok=True)
    paths = {
        "record_start": CUES_DIR / "record-start.wav",
        "record_stop": CUES_DIR / "record-stop.wav",
    }
    specs = {
        "record_start": RECORD_START_HZ,
        "record_stop": RECORD_STOP_HZ,
    }
    for key, path in paths.items():
        if force or not path.is_file() or path.stat().st_size < 100 or path.stat().st_size > 6000:
            path.write_bytes(build_beep(specs[key], vol=_cue_volume))
    return paths


def resolve_cue_path(name: str) -> Path | None:
    """Resolve custom config path or default asset."""
    if name == "record_start" and _cue_start_path is not None:
        return _cue_start_path if _cue_start_path.is_file() else None
    if name == "record_stop" and _cue_stop_path is not None:
        return _cue_stop_path if _cue_stop_path.is_file() else None
    defaults = ensure_cue_files()
    return defaults.get(name)


def play_cue(name: str) -> None:
    """Play a named cue (record_start | record_stop). Never raises."""
    try:
        path = resolve_cue_path(name)
        if path is None or not path.is_file():
            # Fall back to in-memory generated beep at current volume
            freq = RECORD_START_HZ if name == "record_start" else RECORD_STOP_HZ
            play_audio(build_beep(freq, vol=_cue_volume))
            syslog(
                "cue.play",
                component="audio",
                cue=name,
                source="generated",
                volume=_cue_volume,
            )
            return
        play_audio(path.read_bytes())
        syslog(
            "cue.play",
            component="audio",
            cue=name,
            path=str(path),
            volume=_cue_volume,
        )
    except Exception as exc:
        syslog(
            "cue.error",
            component="audio",
            level="warn",
            cue=name,
            error=str(exc)[:120],
        )


def play_record_start() -> None:
    play_cue("record_start")


def play_record_stop() -> None:
    play_cue("record_stop")


def phrase_slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    s = s.strip("-")[:60] or "phrase"
    return s


def tts_cache_path(voice: str, text: str) -> Path:
    voice = (voice or "eve").lower()
    slug = phrase_slug(text)
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return TTS_CACHE_DIR / voice / f"{slug}-{h}.mp3"


COMMON_PHRASES: tuple[str, ...] = (
    "Hark ambient is listening. Say hey hark when you need me.",
    "Cancelled.",
    "Listening.",
    "Got it.",
    "Okay.",
    "Ready.",
    "Sorry, I didn't catch that.",
    "Hark shutting down.",
    "Hark restarting.",
)


def lookup_cached_tts(voice: str | None, text: str) -> bytes | None:
    path = tts_cache_path(voice or "eve", text)
    if path.is_file() and path.stat().st_size > 64:
        return path.read_bytes()
    return None


def store_cached_tts(voice: str | None, text: str, audio: bytes) -> Path:
    path = tts_cache_path(voice or "eve", text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio)
    return path

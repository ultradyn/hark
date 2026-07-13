"""Non-verbal audio cues (record start/stop beeps) + on-disk TTS phrase cache."""

from __future__ import annotations

import hashlib
import io
import math
import re
import struct
import wave
from pathlib import Path

from hark.audio.playback import play_audio
from hark.syslog import log as syslog

# Repo-relative defaults (overridable)
_REPO_ROOT = Path(__file__).resolve().parents[3]
CUES_DIR = _REPO_ROOT / "assets" / "cues"
TTS_CACHE_DIR = _REPO_ROOT / "assets" / "tts"

# Single soft blips (one tone each — not multi-beep sequences)
RECORD_START_HZ = 880.0   # A5 — recording armed
RECORD_STOP_HZ = 660.0    # E5 — recording done (slightly lower)
BEEP_MS = 55
SAMPLE_RATE = 22050
VOLUME = 0.22


def _sine_pcm(freq: float, ms: int, sr: int = SAMPLE_RATE, vol: float = VOLUME) -> bytes:
    n = int(sr * ms / 1000)
    # soft attack/release to avoid clicks
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


def build_beep(freq: float, ms: int = BEEP_MS) -> bytes:
    """One short soft sine blip as WAV bytes."""
    return _pcm_to_wav(_sine_pcm(freq, ms))


# Back-compat name used by tests
def build_beep_pair(freqs: tuple[float, float] | float) -> bytes:
    if isinstance(freqs, (tuple, list)):
        return build_beep(float(freqs[0]))
    return build_beep(float(freqs))


def ensure_cue_files(*, force: bool = False) -> dict[str, Path]:
    """Write baked cue WAVs into assets/cues/ (overwrite if force)."""
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
            # rewrite oversized multi-beep leftovers
            path.write_bytes(build_beep(specs[key]))
    return paths


def play_cue(name: str) -> None:
    """Play a named cue (record_start | record_stop). Never raises."""
    try:
        paths = ensure_cue_files()
        path = paths.get(name) or (CUES_DIR / f"{name}.wav")
        if not path.is_file():
            return
        # Cues are short and local — do not mute mic for them (mute would flash ring)
        play_audio(path.read_bytes())
        syslog("cue.play", component="audio", cue=name)
    except Exception as exc:
        syslog("cue.error", component="audio", level="warn", cue=name, error=str(exc)[:120])


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
    # content hash suffix avoids collisions on truncated slugs
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return TTS_CACHE_DIR / voice / f"{slug}-{h}.mp3"


# Common operator-facing phrases we pre-cache per voice
COMMON_PHRASES: tuple[str, ...] = (
    "Hark ambient is listening. Say hey hark when you need me.",
    "Cancelled.",
    "Listening.",
    "Got it.",
    "Okay.",
    "Ready.",
    "Sorry, I didn't catch that.",
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

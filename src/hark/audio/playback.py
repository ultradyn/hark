"""Playback helpers — WAV/PCM via sounddevice; MP3/other via ffplay/ffmpeg."""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None  # type: ignore


def sniff_audio_format(data: bytes) -> str:
    """Return a short format tag: wav | mp3 | ogg | flac | pcm | unknown."""
    if len(data) < 4:
        return "unknown"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":
        return "mp3"
    # MPEG ADTS sync (xAI TTS returns this)
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    return "pcm"


def write_audio(path: Path | str, data: bytes, *, sample_rate: int = 16000) -> Path:
    """Write audio bytes; convert to WAV via ffmpeg when needed for .wav targets."""
    path = Path(path)
    fmt = sniff_audio_format(data)
    suffix = path.suffix.lower()

    if fmt == "wav" or (fmt == "pcm" and suffix in (".wav", "")):
        if fmt == "wav":
            path.write_bytes(data)
        else:
            path.write_bytes(_pcm16_to_wav(data, sample_rate))
        return path

    # Native container matches extension
    if fmt == "mp3" and suffix in (".mp3", ".mpeg", ""):
        if not suffix:
            path = path.with_suffix(".mp3")
        path.write_bytes(data)
        return path

    # Convert with ffmpeg when writing .wav from mp3/etc.
    if suffix == ".wav" or not suffix:
        if not suffix:
            path = path.with_suffix(".wav")
        return _ffmpeg_convert(data, path, fmt)

    path.write_bytes(data)
    return path


# Back-compat name used by speech.py / CLI
def write_wav(path: Path | str, pcm_or_wav: bytes, sample_rate: int = 16000) -> Path:
    return write_audio(path, pcm_or_wav, sample_rate=sample_rate)


def play_audio(data: bytes, *, sample_rate: int | None = None) -> None:
    """Play provider audio safely (never treat MP3 as PCM — that is static)."""
    fmt = sniff_audio_format(data)
    if fmt == "wav":
        pcm, sr = _wav_to_pcm16(data)
        _play_pcm16(pcm, sr)
        return
    if fmt == "pcm":
        _play_pcm16(data, sample_rate or 24000)
        return

    # Compressed (mp3/ogg/flac): external player only
    ext = {".mp3": ".mp3", "mp3": ".mp3", "ogg": ".ogg", "flac": ".flac"}.get(fmt, ".bin")
    if fmt == "mp3":
        ext = ".mp3"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        p = Path(tmp.name)
    try:
        p.write_bytes(data)
        _play_file(p)
    finally:
        p.unlink(missing_ok=True)


def play_wav_bytes(data: bytes, *, sample_rate: int | None = None) -> None:
    """Alias kept for existing call sites."""
    play_audio(data, sample_rate=sample_rate)


def _play_pcm16(pcm: bytes, sample_rate: int) -> None:
    if sd is not None:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return
        sd.play(samples, sample_rate)
        sd.wait()
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        p = Path(tmp.name)
    try:
        p.write_bytes(_pcm16_to_wav(pcm, sample_rate))
        _play_file(p)
    finally:
        p.unlink(missing_ok=True)


def _play_file(path: Path) -> None:
    for cmd in (
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(path)],
        ["paplay", str(path)],
        ["aplay", str(path)],
    ):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    # Last resort: decode with ffmpeg → wav → sounddevice
    if shutil.which("ffmpeg") and sd is not None:
        wav_path = path.with_suffix(".decoded.wav")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-ac",
                    "1",
                    "-f",
                    "wav",
                    str(wav_path),
                ],
                check=True,
                capture_output=True,
            )
            pcm, sr = _wav_to_pcm16(wav_path.read_bytes())
            _play_pcm16(pcm, sr)
            return
        except (subprocess.CalledProcessError, OSError):
            pass
        finally:
            wav_path.unlink(missing_ok=True)
    raise RuntimeError(
        "could not play audio — need ffplay/ffmpeg for MP3 TTS, or WAV input"
    )


def _ffmpeg_convert(data: bytes, dest: Path, fmt: str) -> Path:
    if not shutil.which("ffmpeg"):
        # Fall back: write raw bytes with correct extension so user can open it
        alt = dest.with_suffix(".mp3" if fmt == "mp3" else dest.suffix or ".bin")
        alt.write_bytes(data)
        return alt
    with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as tmp:
        src = Path(tmp.name)
    try:
        src.write_bytes(data)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ac", "1", str(dest)],
            check=True,
            capture_output=True,
        )
        return dest
    finally:
        src.unlink(missing_ok=True)


def _pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        width = wf.getsampwidth()
        ch = wf.getnchannels()
        if width != 2:
            # Expand/narrow is rare; require ffmpeg path for non-16-bit
            raise RuntimeError(f"unsupported WAV sample width {width}")
        if ch > 1:
            mono = bytearray()
            step = width * ch
            for i in range(0, len(frames), step):
                mono.extend(frames[i : i + width])
            frames = bytes(mono)
        return frames, sr

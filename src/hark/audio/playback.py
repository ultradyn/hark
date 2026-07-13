"""Playback helpers — WAV/PCM via sounddevice; MP3/other via ffplay/ffmpeg."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import numpy as np

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None  # type: ignore


@dataclass
class PlayResult:
    duration_ms: int
    format: str


# Cross-process exclusive speaker: synth may run in parallel; play is serial (B092).
_play_tls = threading.local()
_play_lock_name = "tts_play.lock"


def tts_play_lock_path() -> Path:
    from hark.paths import state_dir

    return state_dir() / _play_lock_name


@contextmanager
def exclusive_playback() -> Iterator[None]:
    """Hold the global TTS speaker (fcntl flock). Re-entrant in the same thread.

    Concurrent ``hark tts`` processes synthesize freely, then block here so
    audio plays back-to-back without overlap. Nested calls (multi-chunk under
    one outer hold) do not re-open the file.
    """
    depth = int(getattr(_play_tls, "depth", 0) or 0)
    if depth > 0:
        _play_tls.depth = depth + 1
        try:
            yield
        finally:
            _play_tls.depth = depth
        return

    import fcntl

    path = tts_play_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _play_tls.depth = 1
        try:
            yield
        finally:
            _play_tls.depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def sniff_audio_format(data: bytes) -> str:
    """Return a short format tag: wav | mp3 | ogg | flac | pcm | unknown."""
    if len(data) < 4:
        return "unknown"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    return "pcm"


def estimate_duration_ms(data: bytes, sample_rate: int | None = None) -> int:
    """Best-effort duration from bytes (WAV exact; else ffprobe; else bitrate guess)."""
    fmt = sniff_audio_format(data)
    if fmt == "wav":
        try:
            with wave.open(io.BytesIO(data), "rb") as wf:
                return int(1000 * wf.getnframes() / max(1, wf.getframerate()))
        except Exception:
            pass
    if fmt == "pcm" and sample_rate:
        # 16-bit mono
        return int(1000 * (len(data) / 2) / sample_rate)
    # ffprobe
    if shutil.which("ffprobe"):
        with tempfile.NamedTemporaryFile(suffix=f".{fmt if fmt != 'unknown' else 'bin'}", delete=False) as tmp:
            p = Path(tmp.name)
        try:
            p.write_bytes(data)
            r = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(p),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return max(0, int(float(r.stdout.strip()) * 1000))
        except Exception:
            pass
        finally:
            p.unlink(missing_ok=True)
    # MP3 crude: assume ~16 KB/s at 128kbps
    if fmt == "mp3" and len(data) > 0:
        return int(1000 * len(data) / 16000)
    return 0


def write_audio(path: Path | str, data: bytes, *, sample_rate: int = 16000) -> Path:
    path = Path(path)
    fmt = sniff_audio_format(data)
    suffix = path.suffix.lower()

    if fmt == "wav" or (fmt == "pcm" and suffix in (".wav", "")):
        if fmt == "wav":
            path.write_bytes(data)
        else:
            path.write_bytes(_pcm16_to_wav(data, sample_rate))
        return path

    if fmt == "mp3" and suffix in (".mp3", ".mpeg", ""):
        if not suffix:
            path = path.with_suffix(".mp3")
        path.write_bytes(data)
        return path

    if suffix == ".wav" or not suffix:
        if not suffix:
            path = path.with_suffix(".wav")
        return _ffmpeg_convert(data, path, fmt)

    path.write_bytes(data)
    return path


def write_wav(path: Path | str, pcm_or_wav: bytes, sample_rate: int = 16000) -> Path:
    return write_audio(path, pcm_or_wav, sample_rate=sample_rate)


def play_audio(
    data: bytes,
    *,
    sample_rate: int | None = None,
    on_near_end: Callable[[], None] | None = None,
    near_end_ms: int = 0,
    exclusive: bool = True,
) -> PlayResult:
    """Play audio. Optional on_near_end fires ~near_end_ms before playback ends.

    Used so listen can arm ~0.3s before TTS finishes.

    ``exclusive`` (default True): take the cross-process TTS play lock (B092) so
    concurrent speakers queue. Pass False only when the caller already holds
    :func:`exclusive_playback`.
    """
    if exclusive:
        with exclusive_playback():
            return _play_audio_unlocked(
                data,
                sample_rate=sample_rate,
                on_near_end=on_near_end,
                near_end_ms=near_end_ms,
            )
    return _play_audio_unlocked(
        data,
        sample_rate=sample_rate,
        on_near_end=on_near_end,
        near_end_ms=near_end_ms,
    )


def _play_audio_unlocked(
    data: bytes,
    *,
    sample_rate: int | None = None,
    on_near_end: Callable[[], None] | None = None,
    near_end_ms: int = 0,
) -> PlayResult:
    fmt = sniff_audio_format(data)
    duration_ms = estimate_duration_ms(data, sample_rate)

    def _maybe_schedule_near_end() -> threading.Timer | None:
        if not on_near_end or near_end_ms <= 0 or duration_ms <= 0:
            return None
        delay = max(0.0, (duration_ms - near_end_ms) / 1000.0)
        t = threading.Timer(delay, on_near_end)
        t.daemon = True
        t.start()
        return t

    timer = _maybe_schedule_near_end()
    t0 = time.monotonic()
    try:
        if fmt == "wav":
            pcm, sr = _wav_to_pcm16(data)
            _play_pcm16(pcm, sr)
        elif fmt == "pcm":
            _play_pcm16(data, sample_rate or 24000)
        else:
            ext = ".mp3" if fmt == "mp3" else f".{fmt}"
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                p = Path(tmp.name)
            try:
                p.write_bytes(data)
                _play_file(p)
            finally:
                p.unlink(missing_ok=True)
    finally:
        if timer is not None:
            timer.cancel()

    wall_ms = int(1000 * (time.monotonic() - t0))
    return PlayResult(duration_ms=duration_ms or wall_ms, format=fmt)


def play_wav_bytes(
    data: bytes,
    *,
    sample_rate: int | None = None,
    on_near_end: Callable[[], None] | None = None,
    near_end_ms: int = 0,
    exclusive: bool = True,
) -> PlayResult:
    return play_audio(
        data,
        sample_rate=sample_rate,
        on_near_end=on_near_end,
        near_end_ms=near_end_ms,
        exclusive=exclusive,
    )


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
            raise RuntimeError(f"unsupported WAV sample width {width}")
        if ch > 1:
            mono = bytearray()
            step = width * ch
            for i in range(0, len(frames), step):
                mono.extend(frames[i : i + width])
            frames = bytes(mono)
        return frames, sr

"""Microphone capture with adaptive energy gate and single-mic lease."""

from __future__ import annotations

import io
import fcntl
import os
import struct
import threading
import time
import wave
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from hark.endpointing import EndpointFrame, EndpointStrategy, SilenceEndpointer
from hark.paths import state_dir

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None  # type: ignore


class MicBusyError(RuntimeError):
    pass


class MicLease:
    """Process- and system-wide single mic lease."""

    _lock = threading.Lock()
    _holder: str | None = None

    def __init__(self, name: str = "hark") -> None:
        self.name = name
        self._held = False
        self._lock_fd: int | None = None

    def __enter__(self) -> MicLease:
        with MicLease._lock:
            if MicLease._holder is not None:
                raise MicBusyError(f"mic busy ({MicLease._holder})")
            lock_path = state_dir() / "mic.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                raise MicBusyError("mic busy (held by another Hark process)") from None
            except OSError:
                os.close(fd)
                raise
            MicLease._holder = self.name
            self._held = True
            self._lock_fd = fd
        return self

    def __exit__(self, *args: object) -> None:
        with MicLease._lock:
            fd = self._lock_fd
            self._lock_fd = None
            if self._held and MicLease._holder == self.name:
                MicLease._holder = None
            self._held = False
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


def _require_sd() -> None:
    if sd is None:
        raise RuntimeError(
            "sounddevice not installed — run: uv sync  (needs PortAudio)"
        )


def pcm16_mono_bytes(samples: np.ndarray) -> bytes:
    samples = np.clip(samples, -1.0, 1.0)
    ints = (samples * 32767.0).astype(np.int16)
    return ints.tobytes()


def write_wav_bytes(pcm16: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16)
    return buf.getvalue()


def record_seconds(
    seconds: float,
    *,
    sample_rate: int = 16000,
    device: int | str | None = None,
) -> bytes:
    """Record fixed duration mono float→PCM16."""
    _require_sd()
    frames = int(seconds * sample_rate)
    audio = sd.rec(
        frames,
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        device=device,
    )
    sd.wait()
    return pcm16_mono_bytes(audio.reshape(-1))


@dataclass
class CaptureResult:
    pcm16: bytes
    sample_rate: int
    duration_ms: int
    speech_ms: int
    # Time spent waiting for speech open (leading silence not in pcm16)
    wait_speech_ms: int = 0
    # Peak energy while capture was armed (helps diagnose residual TTS / mute races)
    peak_rms: float = 0.0
    peak_db: float = -120.0

    @property
    def wav(self) -> bytes:
        return write_wav_bytes(self.pcm16, self.sample_rate)


def _still_discarding(
    *,
    open_mono: float,
    discard_leading_ms: int,
    audio_ok_after: Callable[[], float | None] | None,
) -> bool:
    """True while leading audio should be dropped (overlap pre-arm echo guard).

    ``audio_ok_after`` returns a monotonic deadline (or None while TTS is still
    playing). Fixed ``discard_leading_ms`` applies from stream open.
    """
    now = time.monotonic()
    if audio_ok_after is not None:
        ok_at = audio_ok_after()
        if ok_at is None or now < ok_at:
            return True
    if discard_leading_ms > 0 and now < open_mono + discard_leading_ms / 1000.0:
        return True
    return False


def capture_utterance(
    *,
    sample_rate: int = 16000,
    max_s: float = 120.0,
    end_silence_s: float = 2.1,
    min_speech_s: float = 0.25,
    open_margin_db: float = 8.0,
    # Absolute floor: speech louder than this opens even if relative margin fails
    abs_open_db: float = -48.0,
    open_confirm_blocks: int = 4,  # ~80 ms
    # Keep this much audio immediately before speech open (trims long leading silence)
    preroll_ms: int = 200,
    initial_timeout_s: float = 45.0,
    device: int | str | None = None,
    should_stop: Callable[[bytes, float], bool] | None = None,
    on_opened: Callable[[], None] | None = None,
    post_tts_guard_s: float = 0.0,
    # Drop leading frames (fixed window from open and/or until audio_ok_after)
    discard_leading_ms: int = 0,
    audio_ok_after: Callable[[], float | None] | None = None,
    # Pluggable endpointing (B007). None strategy == legacy fixed-silence gate.
    endpoint_strategy: EndpointStrategy | None = None,
    endpoint_probe_silence_s: float | None = None,
    endpoint_max_silence_s: float | None = None,
    on_endpoint_event: Callable[[str, dict], None] | None = None,
) -> CaptureResult:
    """Energy-gated capture until turn end or should_stop or max.

    The turn-end decision is delegated to :class:`~hark.endpointing.SilenceEndpointer`.
    With ``endpoint_strategy=None`` this is exactly the legacy fixed-silence gate
    (end after ``end_silence_s`` of quiet once ``min_speech_s`` speech seen). A
    smarter strategy may end earlier (reducing long waits) or wait longer up to
    ``endpoint_max_silence_s`` (reducing mid-thought cutoffs).

    Leading silence / background noise is **not** kept: the gate waits until
    speech is confirmed, then starts the capture buffer with a short pre-roll
    only (so word onsets are not clipped). ``on_opened`` fires once when speech
    is confirmed — use it for the record-start cue / stream arming.

    should_stop(pcm_so_far, elapsed_s) → True to end (e.g. agent listen-end).

    Overlap pre-arm: open the stream early and discard frames while
    ``audio_ok_after()`` is None or before its deadline (TTS still ending /
    residual echo). Gate timeout clocks start only after discard completes.
    """
    _require_sd()
    if post_tts_guard_s > 0:
        time.sleep(post_tts_guard_s)

    block = int(sample_rate * 0.02)  # 20 ms
    noise_floor = 1e-4
    open_thresh = None
    opened = False
    speech_blocks = 0
    silent_blocks = 0
    end_silence_blocks = max(1, int(end_silence_s / 0.02))
    min_speech_blocks = max(1, int(min_speech_s / 0.02))
    endpointer = (
        SilenceEndpointer(
            end_silence_s=end_silence_s,
            min_speech_s=min_speech_s,
            strategy=endpoint_strategy,
            probe_silence_s=endpoint_probe_silence_s,
            max_silence_s=endpoint_max_silence_s,
            on_event=on_endpoint_event,
        )
        if endpoint_strategy is not None
        else None
    )
    max_blocks = int(max_s / 0.02)
    timeout_blocks = int(initial_timeout_s / 0.02)
    preroll_blocks = max(1, int(preroll_ms / 20.0))
    peak_db = -120.0
    peak_rms = 0.0

    chunks: list[np.ndarray] = []
    # Short ring of recent frames while waiting for speech (discarded if timeout)
    preroll: deque[np.ndarray] = deque(maxlen=preroll_blocks)
    wait_speech_ms = 0
    # Safety cap for discard phase (TTS tail + residual + long mute)
    discard_max_s = max(30.0, initial_timeout_s)

    with sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=block,
        device=device,
    ) as stream:
        open_mono = time.monotonic()
        # Phase 0: drop leading audio (overlap pre-arm / fixed discard window)
        if discard_leading_ms > 0 or audio_ok_after is not None:
            while _still_discarding(
                open_mono=open_mono,
                discard_leading_ms=discard_leading_ms,
                audio_ok_after=audio_ok_after,
            ):
                if time.monotonic() - open_mono > discard_max_s:
                    raise TimeoutError(
                        "overlap discard window exceeded before audio became usable"
                    )
                data, overflowed = stream.read(block)
                del overflowed, data

        # Gate clock starts only after discard so TTS tail does not burn timeout
        start = time.monotonic()
        for i in range(max_blocks):
            data, overflowed = stream.read(block)
            del overflowed
            samples = data.reshape(-1)
            rms = float(np.sqrt(np.mean(samples**2)) + 1e-12)
            db = 20.0 * np.log10(rms)
            if db > peak_db:
                peak_db = db
                peak_rms = rms

            if not opened:
                preroll.append(samples.copy())
                # adapt noise floor while closed (slow attack)
                noise_floor = 0.98 * noise_floor + 0.02 * rms
                rel_thresh = 20.0 * np.log10(noise_floor + 1e-12) + open_margin_db
                open_thresh = max(rel_thresh, abs_open_db)
                if db >= open_thresh:
                    speech_blocks += 1
                    if speech_blocks >= open_confirm_blocks:
                        opened = True
                        silent_blocks = 0
                        # Seed buffer with short pre-roll only (not full leading silence)
                        chunks.extend(preroll)
                        preroll.clear()
                        wait_speech_ms = int(1000 * (time.monotonic() - start))
                        if on_opened is not None:
                            try:
                                on_opened()
                            except Exception:
                                pass
                else:
                    speech_blocks = max(0, speech_blocks - 1)
                if i >= timeout_blocks and not opened:
                    raise TimeoutError(
                        f"no speech detected "
                        f"(peak_db={peak_db:.1f} peak_rms={peak_rms:.5f} "
                        f"open_thresh≈{open_thresh:.1f}dB — try speaking louder "
                        f"or set a different input device)"
                    )
            else:
                chunks.append(samples.copy())
                if open_thresh is not None and db >= open_thresh - 4:
                    silent_blocks = 0
                    speech_blocks += 1
                    if endpointer is not None:
                        endpointer.on_speech()
                else:
                    silent_blocks += 1
                    if endpointer is None:
                        if (
                            silent_blocks >= end_silence_blocks
                            and speech_blocks >= min_speech_blocks
                        ):
                            break
                    else:
                        def _endpoint_frame() -> EndpointFrame:
                            pcm = pcm16_mono_bytes(np.concatenate(chunks)) if chunks else b""
                            return EndpointFrame(
                                pcm16=pcm,
                                sample_rate=sample_rate,
                                trailing_silence_s=silent_blocks * 0.02,
                                speech_s=speech_blocks * 0.02,
                            )

                        if endpointer.should_end(
                            silent_blocks=silent_blocks,
                            speech_blocks=speech_blocks,
                            audio_fn=_endpoint_frame,
                        ):
                            break

            if should_stop is not None:
                pcm = pcm16_mono_bytes(np.concatenate(chunks)) if chunks else b""
                if should_stop(pcm, time.monotonic() - start):
                    break

    if not chunks:
        raise TimeoutError(
            f"no speech captured (peak_db={peak_db:.1f} peak_rms={peak_rms:.5f})"
        )

    all_s = np.concatenate(chunks)
    pcm = pcm16_mono_bytes(all_s)
    dur_ms = int(1000 * len(all_s) / sample_rate)
    speech_ms = int(1000 * speech_blocks * 0.02)
    return CaptureResult(
        pcm16=pcm,
        sample_rate=sample_rate,
        duration_ms=dur_ms,
        speech_ms=speech_ms,
        wait_speech_ms=wait_speech_ms,
        peak_rms=float(peak_rms),
        peak_db=float(peak_db),
    )


def list_input_devices() -> list[dict]:
    _require_sd()
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d.get("max_input_channels", 0) > 0:
            out.append(
                {
                    "id": i,
                    "name": d.get("name"),
                    "channels": d.get("max_input_channels"),
                    "default_sr": d.get("default_samplerate"),
                }
            )
    return out

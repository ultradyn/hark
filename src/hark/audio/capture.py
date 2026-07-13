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
from typing import Any, Callable

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


def pad_pcm16_silence(
    pcm16: bytes,
    *,
    pad_ms: int = 0,
    sample_rate: int = 16000,
    lead_ms: int | None = None,
    trail_ms: int | None = None,
) -> bytes:
    """Expand mono PCM16 bounds with zero samples (silence).

    Mid-buffer samples are preserved byte-identical. Used for radio segment
    STT so edge phonemes are not hard-cut at the energy-gate boundary (B075).
    Silence-only pad does not invent speech for STT.
    """
    lead = int(pad_ms if lead_ms is None else lead_ms)
    trail = int(pad_ms if trail_ms is None else trail_ms)
    if lead <= 0 and trail <= 0:
        return pcm16
    n_lead = max(0, int(sample_rate * lead / 1000.0))
    n_trail = max(0, int(sample_rate * trail / 1000.0))
    # 16-bit mono: 2 bytes per sample
    return (b"\x00\x00" * n_lead) + pcm16 + (b"\x00\x00" * n_trail)


def radio_stt_window_pcm(
    seg_pcm: bytes,
    overlap_tail: bytes,
    *,
    overlap_ms: int,
    sample_rate: int = 16000,
) -> tuple[bytes, bytes]:
    """Build an STT window with real PCM lookback (B085) and next-segment tail.

    Returns ``(stt_pcm, new_overlap_tail)``. Prefers real previous-segment PCM
    over synthetic silence; empty ``overlap_tail`` yields the segment alone.
    """
    ov_ms = max(0, int(overlap_ms))
    if ov_ms <= 0:
        return seg_pcm, b""
    stt_pcm = (overlap_tail + seg_pcm) if overlap_tail else seg_pcm
    ov_bytes = int(sample_rate * (ov_ms / 1000.0)) * 2
    if ov_bytes > 0 and len(seg_pcm) >= ov_bytes:
        new_tail = seg_pcm[-ov_bytes:]
    elif seg_pcm:
        new_tail = seg_pcm
    else:
        new_tail = b""
    return stt_pcm, new_tail


def effective_radio_segment_pad_ms(
    pad_ms: int | float,
    radio_partial_silence_s: float,
    *,
    absolute_max_ms: int = 300,
) -> int:
    """Clamp radio boundary pad so it stays well under inter-segment quiet.

    Budget: ``min(absolute_max_ms, radio_partial_silence_s * 1000 * 0.4)``.
    Pad ≪ segment silence → STT sees hush, not prior speech / phantom words.
    """
    raw = int(pad_ms)
    if raw <= 0:
        return 0
    silence_budget = max(0, int(float(radio_partial_silence_s) * 1000.0 * 0.4))
    return max(0, min(raw, int(absolute_max_ms), silence_budget))


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


class PcmRingBuffer:
    """Fixed-capacity mono PCM16 ring (sample-interleaved int16).

    Used by continuous ambient capture so wake scoring and pre-roll read
    from a sliding window without reopening the device.
    """

    BYTES_PER_SAMPLE = 2

    def __init__(self, capacity_s: float, sample_rate: int = 16000) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        capacity_s = max(0.05, float(capacity_s))
        self.sample_rate = int(sample_rate)
        self.capacity = max(1, int(capacity_s * self.sample_rate))
        self._buf = np.zeros(self.capacity, dtype=np.int16)
        self._write = 0  # next write index
        self._available = 0  # samples currently held (≤ capacity)

    @property
    def available_samples(self) -> int:
        return self._available

    @property
    def available_s(self) -> float:
        return self._available / float(self.sample_rate)

    @property
    def capacity_s(self) -> float:
        return self.capacity / float(self.sample_rate)

    def clear(self) -> None:
        self._write = 0
        self._available = 0
        self._buf.fill(0)

    def write_samples(self, samples: np.ndarray) -> None:
        """Append int16 mono samples (overwrites oldest when full)."""
        if samples.size == 0:
            return
        flat = np.ascontiguousarray(samples.reshape(-1), dtype=np.int16)
        n = int(flat.shape[0])
        if n >= self.capacity:
            # Keep only the newest capacity samples
            self._buf[:] = flat[-self.capacity :]
            self._write = 0
            self._available = self.capacity
            return
        end = self._write + n
        if end <= self.capacity:
            self._buf[self._write : end] = flat
        else:
            first = self.capacity - self._write
            self._buf[self._write :] = flat[:first]
            self._buf[: n - first] = flat[first:]
        self._write = (self._write + n) % self.capacity
        self._available = min(self.capacity, self._available + n)

    def write_pcm16(self, data: bytes) -> None:
        if not data:
            return
        self.write_samples(np.frombuffer(data, dtype=np.int16))

    def write_float32(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        clipped = np.clip(samples.reshape(-1), -1.0, 1.0)
        self.write_samples((clipped * 32767.0).astype(np.int16))

    def tail_samples(self, n: int) -> np.ndarray:
        """Return the last *n* samples in chronological order (oldest→newest)."""
        if n <= 0 or self._available <= 0:
            return np.zeros(0, dtype=np.int16)
        n = min(int(n), self._available)
        end = self._write  # next write = one past newest
        # Full capacity span: oldest sits at write index
        if n == self.capacity:
            return np.concatenate((self._buf[end:], self._buf[:end]))
        start = (end - n) % self.capacity
        if start < end:
            return self._buf[start:end].copy()
        # Wrapped: [start:] + [:end]
        return np.concatenate((self._buf[start:], self._buf[:end]))

    def tail(self, duration_s: float) -> bytes:
        """Last ``duration_s`` of audio as PCM16 bytes."""
        n = int(max(0.0, float(duration_s)) * self.sample_rate)
        return self.tail_samples(n).tobytes()

    def tail_ms(self, ms: int) -> bytes:
        return self.tail(max(0, int(ms)) / 1000.0)

    def window(self, duration_s: float, *, end_offset_s: float = 0.0) -> bytes:
        """Score window of ``duration_s`` ending ``end_offset_s`` before the tip.

        ``end_offset_s=0`` is the newest audio (same as :meth:`tail`).
        """
        end_off = max(0, int(float(end_offset_s) * self.sample_rate))
        n = max(0, int(float(duration_s) * self.sample_rate))
        if n <= 0 or self._available <= 0:
            return b""
        # Drop end_off newest samples, then take n before that
        total_from_tip = end_off + n
        if total_from_tip > self._available:
            # Not enough history: take what we can before end_off
            avail_before = max(0, self._available - end_off)
            n = min(n, avail_before)
            if n <= 0:
                return b""
        samples = self.tail_samples(end_off + n)
        if end_off > 0:
            samples = samples[: len(samples) - end_off]
        return samples.tobytes()


def clamp_pre_roll_ms(ms: int | float | None, *, default: int = 300) -> int:
    """Clamp pre-roll to the B079 target range (250–500 ms)."""
    if ms is None:
        return default
    try:
        v = int(ms)
    except (TypeError, ValueError):
        return default
    return max(250, min(500, v))


def score_window_plan(
    snippet_s: float,
    hop_s: float | None = None,
    *,
    min_snippet_s: float = 0.8,
    max_snippet_s: float = 2.5,
    default_hop_ratio: float = 0.3,
) -> tuple[float, float]:
    """Normalize wake window + hop so hop is always strictly less than snippet.

    Default hop ≈ 30% of snippet (e.g. 2.5 s → 0.75 s) for overlapping cuts so
    a greeting+name rarely straddles non-overlapping boundaries.
    """
    snippet = max(min_snippet_s, min(float(snippet_s), max_snippet_s))
    if hop_s is None:
        hop = snippet * default_hop_ratio
    else:
        hop = float(hop_s)
    # Keep hop in (0, snippet): at least 100 ms, at most 75% of snippet
    hop = max(0.1, min(hop, snippet * 0.75))
    if hop >= snippet:
        hop = max(0.1, snippet * 0.5)
    return snippet, hop


class ContinuousMicStream:
    """Hold MicLease + InputStream open; fill a :class:`PcmRingBuffer`.

    Ambient wake keeps one of these for the whole arm (or until pause/yield).
    Score overlapping windows via :meth:`window_pcm16` without open/close thrash.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        ring_s: float = 5.0,
        device: int | str | None = None,
        lease_name: str = "ambient",
        block_ms: float = 20.0,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.ring = PcmRingBuffer(ring_s, self.sample_rate)
        self.device = device
        self.lease_name = lease_name
        self._block = max(1, int(self.sample_rate * (block_ms / 1000.0)))
        self._lease: MicLease | None = None
        self._stream: Any = None
        self._open = False

    @property
    def available_s(self) -> float:
        return self.ring.available_s

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> ContinuousMicStream:
        if self._open:
            return self
        _require_sd()
        lease = MicLease(self.lease_name)
        lease.__enter__()
        try:
            stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self._block,
                device=self.device,
            )
            stream.start()
        except Exception:
            lease.__exit__(None, None, None)
            raise
        self._lease = lease
        self._stream = stream
        self._open = True
        return self

    def close(self) -> None:
        stream = self._stream
        self._stream = None
        lease = self._lease
        self._lease = None
        self._open = False
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if lease is not None:
            try:
                lease.__exit__(None, None, None)
            except Exception:
                pass

    def __enter__(self) -> ContinuousMicStream:
        return self.open()

    def __exit__(self, *args: object) -> None:
        self.close()

    def read_for(
        self,
        duration_s: float,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> bool:
        """Block while reading ``duration_s`` of audio into the ring.

        Returns False if ``should_stop`` became true before the full duration
        (caller should check pause/shutdown). Raises if the stream is closed.
        """
        if not self._open or self._stream is None:
            raise RuntimeError("ContinuousMicStream is not open")
        duration_s = max(0.0, float(duration_s))
        if duration_s <= 0:
            return True
        deadline = time.monotonic() + duration_s
        # Ambient spectrum: non-recording feed so the webui can stay live (B087)
        last_spec = 0.0
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                return False
            data, overflowed = self._stream.read(self._block)
            del overflowed
            samples = data.reshape(-1)
            self.ring.write_float32(samples)
            now = time.monotonic()
            if now - last_spec >= 0.032:  # ~30 fps ambient (cheaper than listen)
                last_spec = now
                try:
                    from hark.audio.spectrum import publish_spectrum

                    publish_spectrum(
                        samples,
                        sample_rate=self.sample_rate,
                        recording=False,
                        source="ambient",
                    )
                except Exception:
                    pass
        return True

    def window_pcm16(self, duration_s: float, *, end_offset_s: float = 0.0) -> bytes:
        return self.ring.window(duration_s, end_offset_s=end_offset_s)

    def tail_ms(self, ms: int) -> bytes:
        return self.ring.tail_ms(ms)

    def tail(self, duration_s: float) -> bytes:
        return self.ring.tail(duration_s)


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
    # Keep this much audio immediately before speech open (trims long leading silence).
    # B079: default ≥250 ms so word onsets are not clipped when the gate lags.
    # Values outside 250–500 are clamped (except 0 which disables pre-roll).
    preroll_ms: int = 300,
    # B084: after TTS mute releases, discard this many ms and freeze silence clocks
    mute_edge_pad_ms: int = 300,
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
    # 0 disables; otherwise clamp to B079 range so config mistakes stay safe
    effective_preroll = 0 if preroll_ms <= 0 else clamp_pre_roll_ms(preroll_ms)
    preroll_blocks = max(1, int(effective_preroll / 20.0)) if effective_preroll > 0 else 0
    peak_db = -120.0
    peak_rms = 0.0

    chunks: list[np.ndarray] = []
    # Short ring of recent frames while waiting for speech (discarded if timeout)
    preroll: deque[np.ndarray] = deque(maxlen=max(1, preroll_blocks))
    wait_speech_ms = 0
    # Safety cap for discard phase (TTS tail + residual + long mute)
    discard_max_s = max(30.0, initial_timeout_s)
    # Spectrum window: ~40 ms of recent blocks for FFT (B087 live webui)
    spec_blocks = max(1, int(0.04 / 0.02))
    spec_ring: deque[np.ndarray] = deque(maxlen=spec_blocks)
    # Throttle file writes a bit when local publisher is absent (still ~50 fps)
    last_spec_pub = 0.0
    spec_interval_s = 0.016

    def _publish_spec(samples_block: np.ndarray) -> None:
        nonlocal last_spec_pub
        now = time.monotonic()
        if now - last_spec_pub < spec_interval_s:
            return
        last_spec_pub = now
        try:
            from hark.audio.spectrum import publish_spectrum

            spec_ring.append(samples_block)
            window = (
                np.concatenate(list(spec_ring))
                if len(spec_ring) > 1
                else samples_block
            )
            publish_spectrum(
                window,
                sample_rate=sample_rate,
                recording=True,
                source="listen",
            )
        except Exception:
            pass

    try:
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
            wait_blocks = 0  # only counts when not muted (B084)
            blocks_used = 0  # non-mute blocks against max_s (B084 freezes max too)
            mute_pad_blocks = 0
            was_tts_muted = False
            edge_pad_blocks = max(0, int(float(mute_edge_pad_ms) / 20.0))

            def _tts_muted() -> bool:
                try:
                    from hark.audio.mic_mute import tts_mute_depth

                    return tts_mute_depth() > 0
                except Exception:
                    return False

            # While-loop so TTS mute / edge-pad do not burn max_s or initial_timeout
            while blocks_used < max_blocks:
                data, overflowed = stream.read(block)
                del overflowed
                samples = data.reshape(-1)
                _publish_spec(samples)

                # B084: while Hark holds TTS mute, freeze open/silence/max clocks
                muted_now = _tts_muted()
                if muted_now:
                    was_tts_muted = True
                    if opened:
                        silent_blocks = 0
                    continue
                if was_tts_muted:
                    was_tts_muted = False
                    mute_pad_blocks = edge_pad_blocks
                    if opened:
                        silent_blocks = 0
                if mute_pad_blocks > 0:
                    mute_pad_blocks -= 1
                    if opened:
                        silent_blocks = 0
                    # Still seed preroll while waiting so post-pad open has history
                    if not opened and preroll_blocks > 0:
                        preroll.append(samples.copy())
                    continue

                blocks_used += 1
                rms = float(np.sqrt(np.mean(samples**2)) + 1e-12)
                db = 20.0 * np.log10(rms)
                if db > peak_db:
                    peak_db = db
                    peak_rms = rms

                if not opened:
                    if preroll_blocks > 0:
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
                            if preroll_blocks > 0:
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
                    wait_blocks += 1
                    if wait_blocks >= timeout_blocks and not opened:
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
    finally:
        try:
            from hark.audio.spectrum import clear_spectrum

            clear_spectrum(source="listen")
        except Exception:
            pass


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

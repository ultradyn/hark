"""Live voice spectrum (FFT bands) for the dashboard webui (B087).

Capture processes compute short-window FFT magnitudes and publish the *latest*
frame only — no JSONL history, no disk growth. ``hark serve`` polls that frame
and coalesces it onto the SSE stream (~60 fps) without advancing the event
cursor or crowding subscriber queues.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from hark.paths import state_dir

# Voice-oriented defaults: 32 log-ish bands up to ~6 kHz on 20–50 ms windows.
DEFAULT_N_BANDS = 32
DEFAULT_MAX_HZ = 6000.0
DEFAULT_WINDOW_MS = 40
SPECTRUM_FILENAME = "spectrum.latest"

# One throttle for every capture read loop (:class:`SpectrumTap`). Listen used
# 16 ms and ambient 32 ms; at the 20 ms frame cadence the first published every
# single frame, so the "throttle" only ever throttled ambient. ~31 Hz is well
# inside what the dashboard can render and halves the state-file write rate.
TAP_INTERVAL_S = 0.032
# Bounded handoff to the publish worker. Small on purpose: this is a
# latest-frame display, so a slow disk should cost intermediate frames.
TAP_QUEUE_FRAMES = 2

_local_publish: Callable[[dict[str, Any]], None] | None = None
_local_lock = threading.Lock()


def spectrum_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / SPECTRUM_FILENAME


def set_local_publisher(fn: Callable[[dict[str, Any]], None] | None) -> None:
    """In-process hook used by ``hark serve`` (bypass file round-trip)."""
    global _local_publish
    with _local_lock:
        _local_publish = fn


def compute_spectrum_bands(
    samples: np.ndarray | Sequence[float],
    *,
    sample_rate: int = 16000,
    n_bands: int = DEFAULT_N_BANDS,
    max_hz: float = DEFAULT_MAX_HZ,
) -> list[float]:
    """Return ``n_bands`` magnitudes in ``[0, 1]`` for frequencies up to ``max_hz``.

    Uses an rFFT on a Hann-windowed mono float window. Bands are linear in
    frequency (voice energy is still obvious; cheap on 20–50 ms frames).
    """
    n_bands = max(1, int(n_bands))
    max_hz = float(max_hz)
    sr = int(sample_rate)
    if sr <= 0:
        return [0.0] * n_bands

    arr = np.asarray(samples, dtype=np.float64).reshape(-1)
    if arr.size == 0:
        return [0.0] * n_bands

    # Cap window; pad short frames so rFFT has enough bins for max_hz.
    min_n = max(32, int(sr * DEFAULT_WINDOW_MS / 1000.0))
    if arr.size < min_n:
        pad = np.zeros(min_n, dtype=np.float64)
        pad[: arr.size] = arr
        arr = pad
    elif arr.size > min_n * 2:
        arr = arr[-min_n:]

    window = np.hanning(arr.size)
    spectrum = np.abs(np.fft.rfft(arr * window))
    if spectrum.size <= 1:
        return [0.0] * n_bands

    # Drop DC; map remaining bins into n_bands linear groups up to max_hz.
    freqs = np.fft.rfftfreq(arr.size, d=1.0 / sr)
    usable = spectrum[1:]
    usable_f = freqs[1:]
    cap = min(max_hz, sr / 2.0)
    mask = usable_f <= cap
    usable = usable[mask]
    if usable.size == 0:
        return [0.0] * n_bands

    # Group consecutive bins into n_bands buckets (last bucket absorbs remainder).
    edges = np.linspace(0, usable.size, n_bands + 1, dtype=int)
    bands = np.zeros(n_bands, dtype=np.float64)
    for i in range(n_bands):
        lo, hi = int(edges[i]), int(edges[i + 1])
        if hi <= lo:
            hi = min(lo + 1, usable.size)
        if lo >= usable.size:
            break
        bands[i] = float(np.mean(usable[lo:hi]))

    peak = float(np.max(bands))
    if peak <= 1e-12:
        return [0.0] * n_bands
    # Soft compress so quiet speech still moves bars without pegging on plosives.
    normed = np.sqrt(bands / peak)
    return [float(max(0.0, min(1.0, v))) for v in normed]


def make_spectrum_payload(
    bands: Sequence[float],
    *,
    recording: bool,
    ts: float | None = None,
    sample_rate: int = 16000,
    max_hz: float = DEFAULT_MAX_HZ,
    source: str | None = None,
) -> dict[str, Any]:
    """Dashboard stream payload (``serve.spectrum``)."""
    payload: dict[str, Any] = {
        "kind": "serve.spectrum",
        "bands": [float(max(0.0, min(1.0, float(b)))) for b in bands],
        "ts": float(time.time() if ts is None else ts),
        "recording": bool(recording),
        "sample_rate": int(sample_rate),
        "max_hz": float(max_hz),
    }
    if source:
        payload["source"] = str(source)
    return payload


def _write_latest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    # No fsync: latest-frame only, prefer low latency over crash durability.
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.is_file():
                tmp.unlink()
        except OSError:
            pass
        # best-effort; never break capture
        return


def read_latest_spectrum(root: Path | None = None) -> dict[str, Any] | None:
    """Read the latest spectrum frame from the shared file (or None)."""
    path = spectrum_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict) or obj.get("kind") != "serve.spectrum":
        return None
    bands = obj.get("bands")
    if not isinstance(bands, list):
        return None
    return obj


def publish_spectrum(
    samples: np.ndarray | Sequence[float],
    *,
    sample_rate: int = 16000,
    recording: bool = True,
    n_bands: int = DEFAULT_N_BANDS,
    max_hz: float = DEFAULT_MAX_HZ,
    source: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Compute bands and publish the latest frame (local callback + shared file)."""
    bands = compute_spectrum_bands(
        samples, sample_rate=sample_rate, n_bands=n_bands, max_hz=max_hz
    )
    payload = make_spectrum_payload(
        bands,
        recording=recording,
        sample_rate=sample_rate,
        max_hz=max_hz,
        source=source,
    )
    with _local_lock:
        cb = _local_publish
    if cb is not None:
        try:
            cb(payload)
        except Exception:
            pass
    try:
        _write_latest(spectrum_path(root), payload)
    except Exception:
        pass
    return payload


def clear_spectrum(
    *,
    root: Path | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Publish a silent non-recording frame (end of capture)."""
    payload = make_spectrum_payload(
        [0.0] * DEFAULT_N_BANDS,
        recording=False,
        source=source,
    )
    with _local_lock:
        cb = _local_publish
    if cb is not None:
        try:
            cb(payload)
        except Exception:
            pass
    try:
        _write_latest(spectrum_path(root), payload)
    except Exception:
        pass
    return payload


class SpectrumTap:
    """Publish spectrum frames for a capture read loop, off the audio thread.

    The read loops (``capture_utterance`` and ``ContinuousMicStream.read_for``,
    via :class:`hark.audio.frames.MicFrameSource`) hand every 20 ms frame to
    :meth:`__call__`, which only throttles and copies it into a bounded queue.
    The FFT, the window concatenation and the ``spectrum.latest`` write all run
    on this tap's worker, so a slow disk drops frames instead of stalling
    capture — the file write used to be synchronous on the audio thread at up
    to 50 Hz against a 50 Hz read cadence.

    One tap per open stream: build it when the stream opens, :meth:`close` it
    when the stream closes (the join matters — a detached worker would write the
    state file after its caller is gone).
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        source: str | None = None,
        recording: bool = True,
        interval_s: float = TAP_INTERVAL_S,
        window_s: float = DEFAULT_WINDOW_MS / 1000.0,
        n_bands: int = DEFAULT_N_BANDS,
        max_hz: float = DEFAULT_MAX_HZ,
        root: Path | None = None,
        queue_frames: int = TAP_QUEUE_FRAMES,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.source = source
        self.recording = bool(recording)
        self.interval_s = max(0.0, float(interval_s))
        self.n_bands = int(n_bands)
        self.max_hz = float(max_hz)
        self._root = root
        # ~40 ms FFT window at the 20 ms frame cadence == 2 frames.
        frame_s = 0.02
        self._ring_frames = max(1, int(round(max(frame_s, float(window_s)) / frame_s)))
        self._queue: queue.Queue[np.ndarray | None] = queue.Queue(
            maxsize=max(1, int(queue_frames))
        )
        self._last_pub = 0.0
        self._dropped = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._run, name="hark-spectrum-tap", daemon=True
        )
        self._thread.start()

    # -- audio thread ------------------------------------------------------

    def __call__(self, samples: np.ndarray) -> None:
        """Offer one frame. Never blocks, never raises."""
        if self._closed:
            return
        now = time.monotonic()
        if now - self._last_pub < self.interval_s:
            return
        self._last_pub = now
        try:
            frame = np.asarray(samples, dtype=np.float32).reshape(-1).copy()
        except Exception:
            return
        self._offer(frame)

    def _offer(self, frame: np.ndarray) -> None:
        try:
            self._queue.put_nowait(frame)
            return
        except queue.Full:
            pass
        # Evict the oldest, not the newest: the dashboard shows the latest frame,
        # so a backlog should cost history rather than freshness. Single producer,
        # so this get/put pair cannot lose the slot to a competing writer.
        try:
            self._queue.get_nowait()
            self._dropped += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            self._dropped += 1

    @property
    def dropped(self) -> int:
        """Frames the bounded queue discarded (slow disk / slow worker)."""
        return self._dropped

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def close(self, *, timeout_s: float = 1.0) -> None:
        """Stop accepting frames and join the worker; safe to call twice."""
        if self._closed:
            return
        self._closed = True
        # Make room for the sentinel: a queued frame is worth less than a joined
        # worker, which is what keeps the state file inside the caller's lifetime.
        for _ in range(self._queue.maxsize + 1):
            try:
                self._queue.put_nowait(None)
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                except queue.Empty:
                    pass
        thread = self._thread
        if thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout_s)))

    # -- worker thread -----------------------------------------------------

    def _run(self) -> None:
        ring: deque[np.ndarray] = deque(maxlen=self._ring_frames)
        while True:
            frame = self._queue.get()
            if frame is None:
                return
            try:
                ring.append(frame)
                window = (
                    np.concatenate(list(ring)) if len(ring) > 1 else frame
                )
                publish_spectrum(
                    window,
                    sample_rate=self.sample_rate,
                    recording=self.recording,
                    n_bands=self.n_bands,
                    max_hz=self.max_hz,
                    source=self.source,
                    root=self._root,
                )
            except Exception:
                # best-effort telemetry; never let it kill the worker
                pass

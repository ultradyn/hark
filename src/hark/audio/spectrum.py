"""Live voice spectrum (FFT bands) for the dashboard webui (B087).

Capture processes compute short-window FFT magnitudes and publish the *latest*
frame only — no JSONL history, no disk growth. ``hark serve`` polls that frame
and coalesces it onto the SSE stream (~60 fps) without advancing the event
cursor or crowding subscriber queues.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from hark.paths import state_dir

# Voice-oriented defaults: 32 log-ish bands up to ~6 kHz on 20–50 ms windows.
DEFAULT_N_BANDS = 32
DEFAULT_MAX_HZ = 6000.0
DEFAULT_WINDOW_MS = 40
SPECTRUM_FILENAME = "spectrum.latest"

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

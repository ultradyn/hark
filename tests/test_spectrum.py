"""B087: FFT spectrum helper + shared latest-frame bus for the webui."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from hark.audio.spectrum import (
    DEFAULT_N_BANDS,
    clear_spectrum,
    compute_spectrum_bands,
    make_spectrum_payload,
    publish_spectrum,
    read_latest_spectrum,
    set_local_publisher,
    spectrum_path,
)


def _sine(hz: float, *, sr: int = 16000, ms: int = 40, amp: float = 0.5) -> np.ndarray:
    n = int(sr * ms / 1000.0)
    t = np.arange(n, dtype=np.float64) / sr
    return (amp * np.sin(2 * math.pi * hz * t)).astype(np.float64)


def test_compute_spectrum_silence_is_zero() -> None:
    bands = compute_spectrum_bands(np.zeros(640), sample_rate=16000, n_bands=16)
    assert len(bands) == 16
    assert all(b == 0.0 for b in bands)


def test_compute_spectrum_empty() -> None:
    assert compute_spectrum_bands([], n_bands=8) == [0.0] * 8


def test_compute_spectrum_tone_peaks_in_low_band() -> None:
    # 200 Hz tone should light lower bands more than upper ones (0–6 kHz)
    low = compute_spectrum_bands(_sine(200), sample_rate=16000, n_bands=16, max_hz=6000)
    high = compute_spectrum_bands(_sine(4500), sample_rate=16000, n_bands=16, max_hz=6000)
    assert max(low) == pytest.approx(1.0, abs=1e-6)
    assert max(high) == pytest.approx(1.0, abs=1e-6)
    low_mass = sum(low[:4])
    high_mass_on_low = sum(high[:4])
    high_mass = sum(high[-4:])
    low_mass_on_high = sum(low[-4:])
    assert low_mass > high_mass_on_low
    assert high_mass > low_mass_on_high


def test_bands_clamped_unit_interval() -> None:
    bands = compute_spectrum_bands(_sine(1000, amp=1.0), n_bands=32)
    assert len(bands) == DEFAULT_N_BANDS
    assert all(0.0 <= b <= 1.0 for b in bands)


def test_make_spectrum_payload_shape() -> None:
    p = make_spectrum_payload([0.1, 0.5, 1.2, -0.1], recording=True, source="listen")
    assert p["kind"] == "serve.spectrum"
    assert p["recording"] is True
    assert p["source"] == "listen"
    assert p["bands"] == [0.1, 0.5, 1.0, 0.0]
    assert isinstance(p["ts"], float)
    assert p["sample_rate"] == 16000
    assert p["max_hz"] == 6000.0


def test_publish_and_read_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    seen: list[dict] = []
    set_local_publisher(lambda p: seen.append(p))
    try:
        payload = publish_spectrum(_sine(440), sample_rate=16000, recording=True, source="listen")
        assert payload["kind"] == "serve.spectrum"
        assert payload["recording"] is True
        assert len(payload["bands"]) == DEFAULT_N_BANDS
        assert seen and seen[-1]["kind"] == "serve.spectrum"

        path = spectrum_path()
        assert path.is_file()
        disk = json.loads(path.read_text(encoding="utf-8"))
        assert disk["kind"] == "serve.spectrum"
        assert disk["bands"] == payload["bands"]

        got = read_latest_spectrum()
        assert got is not None
        assert got["bands"] == payload["bands"]
        assert got["recording"] is True
    finally:
        set_local_publisher(None)


def test_clear_spectrum_sets_not_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    set_local_publisher(None)
    publish_spectrum(_sine(300), recording=True)
    cleared = clear_spectrum(source="listen")
    assert cleared["recording"] is False
    assert all(b == 0.0 for b in cleared["bands"])
    got = read_latest_spectrum()
    assert got is not None
    assert got["recording"] is False


def test_read_latest_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert read_latest_spectrum() is None


def test_hub_spectrum_coalesce() -> None:
    """Dashboard Hub holds latest spectrum without enqueueing events."""
    from hark.dashboard.server import Hub

    hub = Hub()
    q = hub.subscribe()
    hub.set_spectrum(make_spectrum_payload([0.2] * 8, recording=True))
    hub.set_spectrum(make_spectrum_payload([0.9] * 8, recording=True))
    seq, frame = hub.get_spectrum()
    assert seq == 2
    assert frame is not None
    assert frame["bands"][0] == 0.9
    # must not have been put on the event queue
    import queue

    with pytest.raises(queue.Empty):
        q.get_nowait()

"""Unit tests for B079 continuous ambient ring + pre-roll (no hardware)."""

from __future__ import annotations

import struct
import time
from types import SimpleNamespace

import numpy as np
import pytest

from hark.audio.capture import (
    ContinuousMicStream,
    PcmRingBuffer,
    clamp_pre_roll_ms,
    score_window_plan,
)


def _pcm16_tone(n_samples: int, value: int = 1000) -> bytes:
    return struct.pack("<" + "h" * n_samples, *([value] * n_samples))


def test_ring_write_and_tail_ms():
    ring = PcmRingBuffer(1.0, sample_rate=16000)
    # 500 ms of known samples
    n = 8000
    ring.write_pcm16(_pcm16_tone(n, 42))
    assert ring.available_samples == n
    assert abs(ring.available_s - 0.5) < 1e-6

    # 250 ms tail
    tail = ring.tail_ms(250)
    assert len(tail) == 4000 * 2
    samples = np.frombuffer(tail, dtype=np.int16)
    assert samples.shape == (4000,)
    assert int(samples[0]) == 42


def test_ring_overwrite_keeps_newest():
    ring = PcmRingBuffer(0.1, sample_rate=100)  # capacity 10 samples
    ring.write_samples(np.arange(1, 8, dtype=np.int16))  # 1..7
    ring.write_samples(np.arange(8, 16, dtype=np.int16))  # 8..15 → keep 6..15
    assert ring.available_samples == 10
    got = ring.tail_samples(10)
    assert list(got) == list(range(6, 16))


def test_ring_window_with_end_offset():
    ring = PcmRingBuffer(1.0, sample_rate=100)
    # 100 samples: values 0..99
    ring.write_samples(np.arange(100, dtype=np.int16))
    # window 0.2 s ending 0.1 s before tip → samples 70..89
    w = ring.window(0.2, end_offset_s=0.1)
    samples = np.frombuffer(w, dtype=np.int16)
    assert list(samples) == list(range(70, 90))


def test_score_window_plan_hop_less_than_snippet():
    snippet, hop = score_window_plan(2.5, None)
    assert snippet == 2.5
    assert hop < snippet
    assert hop == pytest.approx(0.75)

    snippet, hop = score_window_plan(2.5, 2.5)  # hop must be forced down
    assert hop < snippet
    assert hop <= snippet * 0.75

    snippet, hop = score_window_plan(3.0, 0.5)  # clamp snippet max 2.5
    assert snippet == 2.5
    assert hop == 0.5


def test_overlapping_hops_cover_boundary_phrase():
    """Synthetic stream: phrase straddles former non-overlapping cut.

    Non-overlapping 2.5 s cuts would put 'hey' in window A and 'iris' in B.
    With hop 0.75, some scored window contains both.
    """
    sr = 16000
    snippet_s = 2.5
    hop_s = 0.75
    snippet, hop = score_window_plan(snippet_s, hop_s)
    assert hop < snippet

    # Build a 5 s marker stream: second boundary at t=2.5
    # Mark "hey" in [2.3, 2.5) and "iris" in [2.5, 2.7)
    total_s = 5.0
    n = int(total_s * sr)
    buf = np.zeros(n, dtype=np.int16)
    hey_lo = int(2.3 * sr)
    hey_hi = int(2.5 * sr)
    iris_lo = int(2.5 * sr)
    iris_hi = int(2.7 * sr)
    buf[hey_lo:hey_hi] = 1
    buf[iris_lo:iris_hi] = 2

    ring = PcmRingBuffer(total_s + 0.1, sample_rate=sr)
    # Feed in hop-sized chunks and score last snippet each time (after full)
    hop_n = int(hop * sr)
    snip_n = int(snippet * sr)
    windows_with_both = 0
    scored = 0
    pos = 0
    while pos < n:
        end = min(pos + hop_n, n)
        ring.write_samples(buf[pos:end])
        pos = end
        if ring.available_samples < snip_n:
            continue
        w = np.frombuffer(ring.window(snippet), dtype=np.int16)
        scored += 1
        if (w == 1).any() and (w == 2).any():
            windows_with_both += 1

    assert scored >= 3
    assert windows_with_both >= 1, "overlap should catch hey+iris in one window"

    # Non-overlapping control: only score at 0..2.5 and 2.5..5.0
    w0 = buf[0:snip_n]
    w1 = buf[snip_n : 2 * snip_n]
    non_overlap_both = ((w0 == 1).any() and (w0 == 2).any()) or (
        (w1 == 1).any() and (w1 == 2).any()
    )
    assert not non_overlap_both, "fixture must split hey/iris across non-overlap cuts"


def test_clamp_pre_roll_ms():
    assert clamp_pre_roll_ms(300) == 300
    assert clamp_pre_roll_ms(100) == 250
    assert clamp_pre_roll_ms(900) == 500
    assert clamp_pre_roll_ms(None) == 300
    assert clamp_pre_roll_ms("bad") == 300


def test_capture_preroll_seeded_on_gate_open(monkeypatch):
    """Synthetic ring + gate open → utterance buffer starts with pre-roll samples."""
    from hark.audio import capture as cap_mod

    class FakeStream:
        def __init__(self):
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, block):
            self.reads += 1
            time.sleep(0.005)
            # ~15 quiet frames then speech, then silence to end
            if self.reads <= 15:
                # Quiet but with unique ramp so we can detect pre-roll presence
                level = 0.001 + 0.0001 * self.reads
                samples = np.full(block, level, dtype=np.float32)
            elif self.reads <= 40:
                samples = np.full(block, 0.4, dtype=np.float32)
            else:
                samples = np.zeros(block, dtype=np.float32)
            return samples.reshape(-1, 1), False

    fake = FakeStream()
    monkeypatch.setattr(cap_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        cap_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **kw: fake),
    )

    result = cap_mod.capture_utterance(
        max_s=3.0,
        end_silence_s=0.12,
        min_speech_s=0.05,
        open_confirm_blocks=2,
        open_margin_db=6.0,
        abs_open_db=-60.0,
        initial_timeout_s=2.0,
        preroll_ms=300,
        post_tts_guard_s=0,
    )
    # 300 ms @ 16 kHz mono int16 = 4800 samples * 2 bytes
    assert result.duration_ms >= 250
    assert len(result.pcm16) >= int(0.25 * 16000) * 2
    # Pre-roll region should include the quiet ramp (low amplitude), not start cold at speech
    samples = np.frombuffer(result.pcm16, dtype=np.int16).astype(np.float32) / 32767.0
    # First 200 ms should be quieter than speech peak (pre-roll of near-silence/ramp)
    head = samples[: int(0.2 * 16000)]
    mid = samples[int(0.2 * 16000) : int(0.5 * 16000)]
    assert float(np.max(np.abs(head))) < float(np.max(np.abs(mid))) * 0.5


def test_continuous_mic_stream_fills_ring(monkeypatch, tmp_path):
    """ContinuousMicStream writes blocks into the ring without reopening."""
    from hark.audio import capture as cap_mod

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class FakeInput:
        def __init__(self, **kw):
            self.n = 0

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

        def read(self, block):
            self.n += 1
            # Distinct value per block for ordering checks
            samples = np.full(block, 0.01 * (self.n % 50), dtype=np.float32)
            return samples.reshape(-1, 1), False

    monkeypatch.setattr(cap_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(cap_mod, "sd", SimpleNamespace(InputStream=FakeInput))

    with ContinuousMicStream(sample_rate=16000, ring_s=2.0, block_ms=20.0) as mic:
        assert mic.is_open
        # Fake InputStream is non-blocking: wall-clock 0.05 s still fills many blocks
        ok = mic.read_for(0.05)
        assert ok is True
        assert mic.available_s > 0
        pcm = mic.window_pcm16(min(0.08, mic.available_s))
        assert len(pcm) > 0
        pre = mic.tail_ms(250)
        # tail_ms is capped by available samples and by requested ms
        max_samples = min(mic.ring.available_samples, int(0.25 * 16000))
        assert len(pre) == max_samples * 2

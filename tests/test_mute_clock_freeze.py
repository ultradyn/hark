"""B084: listen clocks freeze while TTS mute is held; B085 STT window overlap."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from hark.audio import capture as cap
from hark.audio.capture import radio_stt_window_pcm
from hark.config import HarkConfig, config_to_dict, load_config


def test_tts_mute_depth_nestable():
    from hark.audio import mic_mute

    assert mic_mute.tts_mute_depth() == 0
    with mic_mute.mic_muted_during_tts(enabled=False):
        assert mic_mute.tts_mute_depth() == 0
    assert mic_mute.tts_mute_depth() == 0


def test_capture_freezes_timeout_while_tts_muted(monkeypatch):
    """Muted blocks must not burn initial_timeout_s."""
    block = 320  # 20ms @ 16k
    silent = np.zeros(block, dtype=np.float32)
    loud = np.ones(block, dtype=np.float32) * 0.2
    quiet = np.ones(block, dtype=np.float32) * 0.0001

    state = {"i": 0, "muted": True}

    def fake_depth():
        return 1 if state["muted"] else 0

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            i = state["i"]
            state["i"] += 1
            # After 30 muted reads, unmute
            if i == 30:
                state["muted"] = False
            if i < 35:
                data = silent.reshape(-1, 1).copy()
            elif i < 50:
                data = loud.reshape(-1, 1).copy()
            else:
                data = quiet.reshape(-1, 1).copy()
            return data, False

    monkeypatch.setattr(cap, "sd", SimpleNamespace(InputStream=lambda **k: FakeStream()))
    monkeypatch.setattr(cap, "_require_sd", lambda: None)
    monkeypatch.setattr("hark.audio.mic_mute.tts_mute_depth", fake_depth)

    # short timeout: if mute burned clock, would fail before speech
    result = cap.capture_utterance(
        sample_rate=16000,
        max_s=5.0,
        end_silence_s=0.15,
        min_speech_s=0.08,
        initial_timeout_s=0.4,  # only 0.4s wait if clocks freeze during mute
        mute_edge_pad_ms=40,  # 2 blocks
        preroll_ms=0,
        open_confirm_blocks=2,
        abs_open_db=-40.0,
        open_margin_db=1.0,
    )
    assert result.pcm16
    assert result.peak_rms > 0.01


def test_capture_freezes_max_s_while_tts_muted(monkeypatch):
    """Long TTS mute must not exhaust max_s before speech opens."""
    block = 320
    silent = np.zeros(block, dtype=np.float32)
    loud = np.ones(block, dtype=np.float32) * 0.25
    quiet = np.ones(block, dtype=np.float32) * 0.0001
    state = {"i": 0, "muted": True}

    def fake_depth():
        return 1 if state["muted"] else 0

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            i = state["i"]
            state["i"] += 1
            # ~80 muted frames (1.6s) then speech — max_s is only 0.5s of usable
            if i == 80:
                state["muted"] = False
            if i < 85:
                data = silent.reshape(-1, 1).copy()
            elif i < 100:
                data = loud.reshape(-1, 1).copy()
            else:
                data = quiet.reshape(-1, 1).copy()
            return data, False

    monkeypatch.setattr(cap, "sd", SimpleNamespace(InputStream=lambda **k: FakeStream()))
    monkeypatch.setattr(cap, "_require_sd", lambda: None)
    monkeypatch.setattr("hark.audio.mic_mute.tts_mute_depth", fake_depth)

    result = cap.capture_utterance(
        sample_rate=16000,
        max_s=0.5,  # would die during mute if max counted muted blocks
        end_silence_s=0.12,
        min_speech_s=0.06,
        initial_timeout_s=2.0,
        mute_edge_pad_ms=40,
        preroll_ms=0,
        open_confirm_blocks=2,
        abs_open_db=-40.0,
        open_margin_db=1.0,
    )
    assert result.pcm16
    assert result.peak_rms > 0.01


def test_mute_freezes_silence_counter_while_opened(monkeypatch):
    """During open speech, TTS mute must not count as end_silence (B084 freeze)."""
    block = 320
    loud = np.ones(block, dtype=np.float32) * 0.25
    quiet = np.ones(block, dtype=np.float32) * 0.00005
    # Timeline: open with loud, then quiet (would end), but inject mute mid-quiet
    # so silence counter freezes — then more speech after unmute.
    plan = (
        [loud] * 8  # open + min speech
        + [quiet] * 5  # building silence
        + [quiet] * 20  # muted quiet — must not end
        + [loud] * 6  # more speech after unmute
        + [quiet] * 20  # real end silence
    )
    state = {"i": 0}

    def fake_depth():
        # mute during plan indices 13..32
        return 1 if 13 <= state["i"] < 33 else 0

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            i = min(state["i"], len(plan) - 1)
            state["i"] += 1
            return plan[i].reshape(-1, 1).copy(), False

    monkeypatch.setattr(cap, "sd", SimpleNamespace(InputStream=lambda **k: FakeStream()))
    monkeypatch.setattr(cap, "_require_sd", lambda: None)
    monkeypatch.setattr("hark.audio.mic_mute.tts_mute_depth", fake_depth)

    result = cap.capture_utterance(
        sample_rate=16000,
        max_s=10.0,
        end_silence_s=0.2,  # 10 blocks
        min_speech_s=0.08,
        initial_timeout_s=2.0,
        mute_edge_pad_ms=40,
        preroll_ms=0,
        open_confirm_blocks=2,
        abs_open_db=-40.0,
        open_margin_db=1.0,
    )
    # Second speech burst should be in the buffer (mute froze silence)
    assert result.duration_ms > 400
    assert result.peak_rms > 0.01


def test_mute_preserves_silence_progress_for_finalize(monkeypatch):
    """B112: streaming TTS mute mid-pause must not wipe silence progress.

    Operator finishes speaking; silence builds toward end_silence_s; a mid-listen
    TTS ack mutes the mic. After unmute (quiet), capture should finish using
    prior silence progress — not restart the full hang (perceived prompt delay).
    """
    block = 320
    loud = np.ones(block, dtype=np.float32) * 0.25
    quiet = np.ones(block, dtype=np.float32) * 0.00005
    # end_silence = 10 blocks (0.2s). Build 8 quiet, mute 15 quiet, unmute + 4 quiet → end.
    # If mute *reset* silence, would need 10 more after unmute (not enough in plan).
    plan = (
        [loud] * 8  # open + min speech
        + [quiet] * 8  # almost at end silence
        + [quiet] * 15  # muted — freeze, do not reset
        + [quiet] * 6  # post-unmute (incl. edge pad) → should end
        + [loud] * 30  # must NOT be reached if finalize preserved progress
    )
    state = {"i": 0}
    mute_from, mute_to = 16, 31  # after 8 loud + 8 quiet

    def fake_depth():
        return 1 if mute_from <= state["i"] < mute_to else 0

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            i = min(state["i"], len(plan) - 1)
            state["i"] += 1
            return plan[i].reshape(-1, 1).copy(), False

    monkeypatch.setattr(cap, "sd", SimpleNamespace(InputStream=lambda **k: FakeStream()))
    monkeypatch.setattr(cap, "_require_sd", lambda: None)
    monkeypatch.setattr("hark.audio.mic_mute.tts_mute_depth", fake_depth)

    result = cap.capture_utterance(
        sample_rate=16000,
        max_s=10.0,
        end_silence_s=0.2,  # 10 blocks
        min_speech_s=0.08,
        initial_timeout_s=2.0,
        mute_edge_pad_ms=40,  # 2 blocks
        preroll_ms=0,
        open_confirm_blocks=2,
        abs_open_db=-40.0,
        open_margin_db=1.0,
    )
    # Finished on silence — did not swallow the trailing loud markers
    assert result.pcm16
    assert state["i"] < len(plan) - 10
    # Duration ≈ speech + pre-mute quiet only (mute frames not appended)
    assert result.duration_ms < 600


def test_speech_during_mute_resets_silence_and_logs(monkeypatch):
    """B112: energy during mute/pad resets silence so we do not false-finalize."""
    block = 320
    loud = np.ones(block, dtype=np.float32) * 0.25
    quiet = np.ones(block, dtype=np.float32) * 0.00005
    plan = (
        [loud] * 8
        + [quiet] * 8  # almost done
        + [loud] * 10  # muted but loud → speech_during_mute
        + [loud] * 6  # post-unmute more speech
        + [quiet] * 20  # real end
    )
    state = {"i": 0}
    logs: list[tuple[str, dict]] = []

    def fake_depth():
        return 1 if 16 <= state["i"] < 26 else 0

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            i = min(state["i"], len(plan) - 1)
            state["i"] += 1
            return plan[i].reshape(-1, 1).copy(), False

    monkeypatch.setattr(cap, "sd", SimpleNamespace(InputStream=lambda **k: FakeStream()))
    monkeypatch.setattr(cap, "_require_sd", lambda: None)
    monkeypatch.setattr("hark.audio.mic_mute.tts_mute_depth", fake_depth)
    monkeypatch.setattr(
        "hark.syslog.log",
        lambda event, **data: logs.append((event, data)),
    )

    result = cap.capture_utterance(
        sample_rate=16000,
        max_s=10.0,
        end_silence_s=0.2,
        min_speech_s=0.08,
        initial_timeout_s=2.0,
        mute_edge_pad_ms=40,
        preroll_ms=0,
        open_confirm_blocks=2,
        abs_open_db=-40.0,
        open_margin_db=1.0,
    )
    assert result.pcm16
    assert any(e == "listen.speech_during_mute" for e, _ in logs)
    # Post-mute speech kept the turn open long enough for full second burst + silence
    assert result.duration_ms > 300


def test_radio_stt_window_overlap_real_pcm():
    """B085: adjacent STT windows share ≥250ms of real prior-segment PCM."""
    sr = 16000
    # 500ms of marker A then 500ms of marker B (distinct sample values)
    a = (np.full(int(sr * 0.5), 1000, dtype=np.int16)).tobytes()
    b = (np.full(int(sr * 0.5), 2000, dtype=np.int16)).tobytes()
    stt1, tail = radio_stt_window_pcm(a, b"", overlap_ms=300, sample_rate=sr)
    assert stt1 == a
    # tail should be last 300ms of A
    assert len(tail) == int(sr * 0.3) * 2
    stt2, tail2 = radio_stt_window_pcm(b, tail, overlap_ms=300, sample_rate=sr)
    # stt2 starts with real A tail then full B
    assert stt2.startswith(tail)
    assert stt2.endswith(b)
    assert len(stt2) == len(tail) + len(b)
    # markers: first samples of overlap are 1000, not silence
    head = np.frombuffer(stt2[:200], dtype=np.int16)
    assert int(head[0]) == 1000
    # next tail is from B
    assert len(tail2) == int(sr * 0.3) * 2
    assert np.frombuffer(tail2[:2], dtype=np.int16)[0] == 2000


def test_radio_stt_window_no_overlap_when_disabled():
    a = b"\x01\x00" * 100
    stt, tail = radio_stt_window_pcm(a, b"\xff\xff" * 50, overlap_ms=0, sample_rate=16000)
    assert stt == a
    assert tail == b""


def test_mute_edge_pad_and_overlap_config_defaults():
    cfg = HarkConfig()
    assert cfg.audio.mute_edge_pad_ms == 300
    assert cfg.listen.radio_segment_overlap_ms == 300
    d = config_to_dict(cfg)
    assert d["audio"]["mute_edge_pad_ms"] == 300
    assert d["listen"]["radio_segment_overlap_ms"] == 300


def test_config_loads_mute_and_overlap(tmp_path, monkeypatch):
    p = tmp_path / "cfg.toml"
    p.write_text(
        """
[audio]
mute_edge_pad_ms = 250

[listen]
radio_segment_overlap_ms = 400
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    cfg = load_config(p)
    assert cfg.audio.mute_edge_pad_ms == 250
    assert cfg.listen.radio_segment_overlap_ms == 400
    dumped = config_to_dict(cfg)
    assert dumped["audio"]["mute_edge_pad_ms"] == 250
    assert dumped["listen"]["radio_segment_overlap_ms"] == 400


def test_join_overlap_pcm_prepend_is_bytes():
    """B083 text join still works with multi segments (smoke)."""
    from hark.speech import join_radio_stt_segments

    assert "a b c" in join_radio_stt_segments(["a b", "b c"])

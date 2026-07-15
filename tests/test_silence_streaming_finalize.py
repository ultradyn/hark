"""B108: silence end_mode still auto-finalizes when ambient.streaming is on.

Root causes covered:
1. High-gain / elevated room noise above a frozen abs_open hang floor must not
   keep the energy gate open forever (relative-to-peak hang).
2. Streaming quiet-gate TTS is radio-only; silence captures force HOLD so
   mid-capture mute cannot race end_silence_s (B084 freeze).
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from hark.audio import capture as cap
from hark.mic_coord import wait_until_tts_play_allowed


def _fake_stream(plan: list[np.ndarray], state: dict):
    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            i = min(state["i"], len(plan) - 1)
            state["i"] += 1
            return plan[i].reshape(-1, 1).copy(), False

    return FakeStream


def test_high_gain_room_noise_still_silence_finalizes(monkeypatch):
    """Peak speech well above open_thresh; elevated room must still hang-end.

    Reproduces dogfood where abs_open_db is low (-48) but high input gain leaves
    room noise forever above open_thresh-4, so classic hang never fires.
    """
    block = 320  # 20 ms @ 16 kHz
    # Very quiet pre-open (so open_thresh ≈ abs_open_db = -48)
    pre = np.ones(block, dtype=np.float32) * 1e-5  # ~-100 dB
    # Loud close-talk speech (high gain)
    loud = np.ones(block, dtype=np.float32) * 0.3  # ~-10 dB
    # Elevated room noise after speech — still well above -48-4, but far below peak
    room = np.ones(block, dtype=np.float32) * 0.02  # ~-34 dB

    plan = (
        [pre] * 10  # closed gate / noise floor
        + [loud] * 15  # open + min speech (~300 ms)
        + [room] * 30  # ~600 ms elevated "noise" — must count as silence
    )
    state = {"i": 0}

    monkeypatch.setattr(
        cap, "sd", SimpleNamespace(InputStream=lambda **k: _fake_stream(plan, state)())
    )
    monkeypatch.setattr(cap, "_require_sd", lambda: None)
    monkeypatch.setattr("hark.audio.mic_mute.tts_mute_depth", lambda: 0)

    result = cap.capture_utterance(
        sample_rate=16000,
        max_s=5.0,
        end_silence_s=0.2,  # 10 blocks
        min_speech_s=0.08,
        initial_timeout_s=2.0,
        mute_edge_pad_ms=0,
        preroll_ms=0,
        open_confirm_blocks=2,
        abs_open_db=-48.0,
        open_margin_db=8.0,
        hang_margin_db=4.0,
        speech_drop_db=18.0,
        peak_gate_slack_db=12.0,
    )
    assert result.pcm16
    assert result.peak_rms > 0.1
    # Must have ended on hang silence, not exhausted max_s (~5s of audio)
    assert result.duration_ms < 1500
    # Consumed speech + end silence, not the whole plan past end
    assert state["i"] < len(plan)


def test_soft_speech_near_threshold_still_ends(monkeypatch):
    """Relative hang must not break soft speech near abs_open (legacy path)."""
    block = 320
    pre = np.ones(block, dtype=np.float32) * 1e-5
    # Soft speech just above -48 dB (~0.004 → ~-48 dB; use higher for confirm)
    soft = np.ones(block, dtype=np.float32) * 0.008  # ~-42 dB
    quiet = np.ones(block, dtype=np.float32) * 1e-5

    plan = [pre] * 8 + [soft] * 12 + [quiet] * 25
    state = {"i": 0}

    monkeypatch.setattr(
        cap, "sd", SimpleNamespace(InputStream=lambda **k: _fake_stream(plan, state)())
    )
    monkeypatch.setattr(cap, "_require_sd", lambda: None)
    monkeypatch.setattr("hark.audio.mic_mute.tts_mute_depth", lambda: 0)

    result = cap.capture_utterance(
        sample_rate=16000,
        max_s=5.0,
        end_silence_s=0.15,
        min_speech_s=0.08,
        initial_timeout_s=2.0,
        mute_edge_pad_ms=0,
        preroll_ms=0,
        open_confirm_blocks=2,
        abs_open_db=-48.0,
        open_margin_db=8.0,
    )
    assert result.pcm16
    assert result.duration_ms < 1500


def test_streaming_true_silence_mode_forces_hold_gate():
    """ambient.streaming=true must not quiet-gate TTS over a silence capture."""
    clock = {"t": 0.0}
    active = {"on": True}
    quiet = {"s": 5.0}  # plenty of quiet — would allow streaming gate immediately

    def probe():
        if not active["on"]:
            return SimpleNamespace(
                active=False,
                reason=None,
                sources=(),
                stream_id=None,
                mode=None,
                pid=None,
            )
        return SimpleNamespace(
            active=True,
            reason="listen:silence:s-sil",
            sources=("listen.active",),
            stream_id="s-sil",
            mode="silence",
            pid=999,
        )

    def sleep(s: float) -> None:
        clock["t"] += s
        # Capture ends after 0.4s of HOLD waiting (not quiet-gate allow)
        if clock["t"] >= 0.4:
            active["on"] = False

    result = wait_until_tts_play_allowed(
        streaming=True,
        min_quiet_s=2.0,
        max_wait_s=10.0,
        poll_ms=100,
        quiet_ms=0,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock["t"],
        probe_fn=probe,
        quiet_fn=lambda: quiet["s"],
    )
    assert result.deferred is True
    assert result.gate == "idle"  # HOLD, not streaming quiet
    assert result.timed_out is False
    assert clock["t"] >= 0.4  # waited for capture end, not immediate quiet allow


def test_streaming_true_radio_mode_still_quiet_gates():
    """Radio + streaming still uses the B105 quiet gate (unchanged)."""
    clock = {"t": 0.0}
    quiet = {"s": 0.3}
    active = {"on": True}

    def probe():
        if not active["on"]:
            return SimpleNamespace(
                active=False,
                reason=None,
                sources=(),
                stream_id=None,
                mode=None,
                pid=None,
            )
        return SimpleNamespace(
            active=True,
            reason="listen:radio:s1",
            sources=("listen.active",),
            stream_id="s1",
            mode="radio",
            pid=999,
        )

    def sleep(s: float) -> None:
        clock["t"] += s
        if clock["t"] >= 0.5:
            quiet["s"] = 2.2

    result = wait_until_tts_play_allowed(
        streaming=True,
        min_quiet_s=2.0,
        max_wait_s=10.0,
        poll_ms=100,
        quiet_ms=0,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock["t"],
        probe_fn=probe,
        quiet_fn=lambda: quiet["s"],
    )
    assert result.deferred is True
    assert result.gate == "quiet"
    assert result.timed_out is False

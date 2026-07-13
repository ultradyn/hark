"""B007: pluggable endpointing strategy seam + smart turn wrapper."""

from __future__ import annotations

import numpy as np
import pytest

from hark.endpointing import (
    BLOCK_S,
    EndpointFrame,
    SilenceEndpointer,
    SmartTurnStrategy,
    build_endpoint_strategy,
)


def _frame(pcm: bytes = b"", *, sr: int = 16000, sil: float = 0.4, sp: float = 1.0) -> EndpointFrame:
    return EndpointFrame(pcm16=pcm, sample_rate=sr, trailing_silence_s=sil, speech_s=sp)


def _audio_fn(pcm: bytes = b"") -> object:
    return lambda: _frame(pcm)


# ---------------------------------------------------------------------------
# Default (no strategy) reproduces the legacy fixed-silence energy gate
# ---------------------------------------------------------------------------

def test_default_matches_legacy_block_maths():
    # Legacy: end_silence_blocks = int(2.1/0.02)=105, min_speech=int(0.25/0.02)=12
    ep = SilenceEndpointer(end_silence_s=2.1, min_speech_s=0.25)
    assert ep.end_silence_blocks == 105
    assert ep.min_speech_blocks == 12


def test_default_ends_only_at_fixed_silence_and_min_speech():
    ep = SilenceEndpointer(end_silence_s=2.1, min_speech_s=0.25)
    # not enough silence yet
    assert not ep.should_end(silent_blocks=104, speech_blocks=50, audio_fn=_audio_fn())
    # enough silence but not enough speech
    assert not ep.should_end(silent_blocks=105, speech_blocks=11, audio_fn=_audio_fn())
    # both satisfied
    assert ep.should_end(silent_blocks=105, speech_blocks=12, audio_fn=_audio_fn())


def test_default_never_calls_audio_fn():
    called = {"n": 0}

    def boom() -> EndpointFrame:
        called["n"] += 1
        raise AssertionError("audio_fn must not be called on the energy-gate path")

    ep = SilenceEndpointer(end_silence_s=2.1, min_speech_s=0.25)
    assert ep.should_end(silent_blocks=105, speech_blocks=50, audio_fn=boom)
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Smart strategy verdicts
# ---------------------------------------------------------------------------

class _FixedStrategy:
    name = "fake"

    def __init__(self, verdict: bool | None) -> None:
        self.verdict = verdict
        self.calls = 0

    def should_end(self, frame: EndpointFrame) -> bool | None:
        self.calls += 1
        return self.verdict


def test_strategy_true_ends_early_before_fixed_silence():
    strat = _FixedStrategy(True)
    ep = SilenceEndpointer(
        end_silence_s=2.1, min_speech_s=0.25, strategy=strat,
        probe_silence_s=0.4, max_silence_s=3.0,
    )
    probe = ep.probe_blocks  # 20
    # below probe: not consulted, keep listening
    assert not ep.should_end(silent_blocks=probe - 1, speech_blocks=50, audio_fn=_audio_fn())
    assert strat.calls == 0
    # at probe (far below fixed 105 blocks): strategy says complete -> end early
    assert ep.should_end(silent_blocks=probe, speech_blocks=50, audio_fn=_audio_fn())
    assert strat.calls == 1


def test_strategy_false_waits_until_max_cap():
    strat = _FixedStrategy(False)
    ep = SilenceEndpointer(
        end_silence_s=2.1, min_speech_s=0.25, strategy=strat,
        probe_silence_s=0.4, max_silence_s=3.0,
    )
    # incomplete at fixed-silence point -> keep waiting (reduces cutoffs)
    assert not ep.should_end(silent_blocks=105, speech_blocks=50, audio_fn=_audio_fn())
    # hits hard cap (3.0s -> 150 blocks) -> end regardless
    assert ep.should_end(silent_blocks=ep.max_silence_blocks, speech_blocks=50, audio_fn=_audio_fn())


def test_strategy_none_defers_to_fixed_silence():
    strat = _FixedStrategy(None)
    ep = SilenceEndpointer(
        end_silence_s=2.1, min_speech_s=0.25, strategy=strat,
        probe_silence_s=0.4, max_silence_s=3.0,
    )
    # undecided before fixed silence -> keep listening
    assert not ep.should_end(silent_blocks=50, speech_blocks=50, audio_fn=_audio_fn())
    # undecided at/after fixed silence -> defer path ends
    assert ep.should_end(silent_blocks=105, speech_blocks=50, audio_fn=_audio_fn())


def test_strategy_exception_disables_and_falls_back():
    events: list[tuple[str, dict]] = []

    class _Boom:
        name = "boom"

        def should_end(self, frame: EndpointFrame) -> bool | None:
            raise RuntimeError("model exploded")

    ep = SilenceEndpointer(
        end_silence_s=2.1, min_speech_s=0.25, strategy=_Boom(),
        probe_silence_s=0.4, max_silence_s=3.0,
        on_event=lambda e, f: events.append((e, f)),
    )
    # First probe raises -> fall back to fixed silence (not yet reached)
    assert not ep.should_end(silent_blocks=20, speech_blocks=50, audio_fn=_audio_fn())
    assert ep._strategy_failed is True
    assert any(e == "endpoint.strategy_error" for e, _ in events)
    # After failure, purely the energy gate: ends at fixed silence, no more calls
    assert ep.should_end(silent_blocks=105, speech_blocks=50, audio_fn=_audio_fn())


def test_min_speech_gate_blocks_strategy():
    strat = _FixedStrategy(True)
    ep = SilenceEndpointer(
        end_silence_s=2.1, min_speech_s=0.25, strategy=strat, probe_silence_s=0.4,
    )
    # too little speech -> never ends, strategy not consulted
    assert not ep.should_end(silent_blocks=100, speech_blocks=5, audio_fn=_audio_fn())
    assert strat.calls == 0


def test_max_silence_floor_is_end_silence():
    # max_silence_s smaller than end_silence must not lower the ceiling
    ep = SilenceEndpointer(
        end_silence_s=2.1, min_speech_s=0.25, strategy=_FixedStrategy(False),
        max_silence_s=0.5,
    )
    assert ep.max_silence_blocks >= ep.end_silence_blocks


# ---------------------------------------------------------------------------
# SmartTurnStrategy wrapper (injected predictor)
# ---------------------------------------------------------------------------

def test_smart_turn_threshold():
    calls: list[int] = []

    def predict(samples: np.ndarray, sr: int) -> float:
        calls.append(samples.size)
        return 0.8

    strat = SmartTurnStrategy(predict, threshold=0.5)
    pcm = (np.zeros(1600, dtype=np.int16)).tobytes()
    assert strat.should_end(_frame(pcm)) is True

    strat_hi = SmartTurnStrategy(predict, threshold=0.9)
    assert strat_hi.should_end(_frame(pcm)) is False


def test_smart_turn_empty_audio_undecided():
    strat = SmartTurnStrategy(lambda s, sr: 1.0)
    assert strat.should_end(_frame(b"")) is None


def test_smart_turn_trims_to_window():
    seen: dict[str, int] = {}

    def predict(samples: np.ndarray, sr: int) -> float:
        seen["n"] = samples.size
        return 1.0

    strat = SmartTurnStrategy(predict, window_s=1.0)  # keep 16000 samples
    pcm = (np.zeros(16000 * 3, dtype=np.int16)).tobytes()  # 3s
    strat.should_end(_frame(pcm, sr=16000))
    assert seen["n"] == 16000


def test_frame_samples_f32_roundtrip():
    ints = np.array([0, 16384, -16384, 32767], dtype=np.int16)
    fr = _frame(ints.tobytes())
    out = fr.samples_f32()
    assert out.dtype == np.float32
    assert abs(out[1] - 0.5) < 1e-3
    assert abs(out[2] + 0.5) < 1e-3


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def test_factory_energy_returns_none():
    assert build_endpoint_strategy(strategy_name="energy") is None
    assert build_endpoint_strategy(strategy_name="") is None
    assert build_endpoint_strategy(strategy_name="none") is None


def test_factory_unknown_warns_and_falls_back():
    warns: list[str] = []
    out = build_endpoint_strategy(strategy_name="magic", on_warn=warns.append)
    assert out is None
    assert warns and "magic" in warns[0]


def test_factory_smart_turn_with_injected_predictor():
    out = build_endpoint_strategy(
        strategy_name="smart_turn",
        predict_fn=lambda s, sr: 0.9,
        smart_turn_threshold=0.6,
    )
    assert isinstance(out, SmartTurnStrategy)
    assert out.threshold == 0.6


def test_factory_smart_turn_missing_model_falls_back():
    warns: list[str] = []
    out = build_endpoint_strategy(
        strategy_name="smart_turn",
        smart_turn_model_path="/nonexistent/model.onnx",
        on_warn=warns.append,
    )
    assert out is None
    assert warns and "smart_turn" in warns[0]


def test_block_constant():
    assert BLOCK_S == pytest.approx(0.02)

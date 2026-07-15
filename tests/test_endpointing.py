"""B007: pluggable endpointing strategy seam + smart turn wrapper."""

from __future__ import annotations

import inspect
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from hark.endpointing import (
    BLOCK_S,
    EndpointFrame,
    SilenceEndpointer,
    SmartTurnStrategy,
    build_endpoint_strategy,
    load_smart_turn_predictor,
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


def test_capture_energy_path_keeps_legacy_condition_and_avoids_per_block_concat(monkeypatch):
    """The default path must retain the exact old gate and avoid smart audio work."""
    from hark.audio import capture as cap_mod

    class FakeStream:
        def __init__(self) -> None:
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, block):
            self.reads += 1
            # Two blocks open the gate, then five quiet blocks hit 0.1 s.
            samples = np.full(
                block,
                0.5 if self.reads <= 2 else 0.0,
                dtype=np.float32,
            )
            return samples.reshape(-1, 1), False

    stream = FakeStream()
    monkeypatch.setattr(cap_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        cap_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **kwargs: stream),
    )
    real_concatenate = np.concatenate
    concatenate_calls = 0

    def counting_concatenate(parts):
        nonlocal concatenate_calls
        concatenate_calls += 1
        return real_concatenate(parts)

    monkeypatch.setattr(cap_mod.np, "concatenate", counting_concatenate)
    result = cap_mod.capture_utterance(
        max_s=1.0,
        end_silence_s=0.1,
        min_speech_s=0.04,
        open_confirm_blocks=2,
        initial_timeout_s=1.0,
        post_tts_guard_s=0,
    )

    assert result.duration_ms > 0
    assert stream.reads == 7
    # Only finalization concatenates. A default turn never builds an endpoint frame.
    assert concatenate_calls == 1
    source = inspect.getsource(cap_mod.capture_utterance)
    # Energy path still uses the legacy fixed-silence compound condition
    # (whitespace may vary with surrounding mute-hold logic).
    assert "silent_blocks >= end_silence_blocks" in source
    assert "speech_blocks >= min_speech_blocks" in source
    assert "endpointer is None" in source


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


def test_strategy_runs_once_per_contiguous_silence_run():
    strat = _FixedStrategy(False)
    ep = SilenceEndpointer(
        end_silence_s=2.1, min_speech_s=0.25, strategy=strat,
        probe_silence_s=0.4, max_silence_s=3.0,
    )

    assert not ep.should_end(silent_blocks=20, speech_blocks=50, audio_fn=_audio_fn())
    assert not ep.should_end(silent_blocks=21, speech_blocks=50, audio_fn=_audio_fn())
    assert strat.calls == 1

    ep.on_speech()
    assert not ep.should_end(silent_blocks=20, speech_blocks=50, audio_fn=_audio_fn())
    assert strat.calls == 2


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


def test_factory_invalid_threshold_falls_back():
    warns: list[str] = []
    out = build_endpoint_strategy(
        strategy_name="smart_turn",
        smart_turn_threshold=1.1,
        predict_fn=lambda s, sr: 0.9,
        on_warn=warns.append,
    )
    assert out is None
    assert warns and "threshold" in warns[0]


def test_smart_turn_loader_uses_v3_whisper_feature_contract(monkeypatch, tmp_path):
    """Exercise the real loader shape without installing optional dependencies."""
    model = tmp_path / "smart-turn-v3.onnx"
    model.touch()
    seen: dict[str, object] = {}

    class FakeSessionOptions:
        pass

    class FakeSession:
        def __init__(self, path, *, sess_options, providers):
            seen["path"] = path
            seen["session_options"] = sess_options
            seen["providers"] = providers

        def get_inputs(self):
            return [SimpleNamespace(name="input_features")]

        def run(self, output_names, inputs):
            seen["output_names"] = output_names
            seen["inputs"] = inputs
            return [np.array([[0.73]], dtype=np.float32)]

    fake_ort = ModuleType("onnxruntime")
    fake_ort.SessionOptions = FakeSessionOptions
    fake_ort.ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="sequential")
    fake_ort.GraphOptimizationLevel = SimpleNamespace(ORT_ENABLE_ALL="all")
    fake_ort.InferenceSession = FakeSession

    class FakeFeatureExtractor:
        def __init__(self, *, chunk_length):
            seen["chunk_length"] = chunk_length

        def __call__(self, samples, **kwargs):
            seen["samples"] = samples
            seen["feature_kwargs"] = kwargs
            return SimpleNamespace(
                input_features=np.ones((1, 80, 3000), dtype=np.float32)
            )

    fake_transformers = ModuleType("transformers")
    fake_transformers.WhisperFeatureExtractor = FakeFeatureExtractor
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)

    predict = load_smart_turn_predictor(str(model))
    assert predict(np.zeros(16000, dtype=np.float32), 16000) == pytest.approx(0.73)
    assert seen["chunk_length"] == 8
    assert seen["providers"] == ["CPUExecutionProvider"]
    assert seen["output_names"] is None
    assert seen["feature_kwargs"] == {
        "sampling_rate": 16000,
        "return_tensors": "np",
        "padding": "max_length",
        "max_length": 128000,
        "truncation": True,
        "do_normalize": True,
    }
    assert np.asarray(seen["inputs"]["input_features"]).shape == (1, 80, 3000)
    with pytest.raises(RuntimeError, match="16 kHz"):
        predict(np.zeros(8000, dtype=np.float32), 8000)


def test_radio_mode_neither_builds_nor_passes_an_endpoint_strategy(monkeypatch):
    """Radio capture remains the pre-B007 call surface even if smart turn is set."""
    import hark.speech as speech
    from hark.audio.capture import CaptureResult
    from hark.config import HarkConfig

    class NullContext:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeStore:
        def record_stt(self, **kwargs):
            pass

    cfg = HarkConfig()
    cfg.listen.endpoint_strategy = "smart_turn"
    calls: list[dict] = []

    def fake_capture(**kwargs):
        calls.append(kwargs)
        return CaptureResult(
            pcm16=b"\0\0",
            sample_rate=16000,
            duration_ms=20,
            speech_ms=20,
        )

    monkeypatch.setattr(speech, "build_endpoint_strategy", lambda **kwargs: (_ for _ in ()).throw(AssertionError("radio must not build endpoint strategy")))
    monkeypatch.setattr(speech, "resolve_stt", lambda *args, **kwargs: SimpleNamespace(
        name="fake",
        transcribe=lambda wav: SimpleNamespace(text="okay hark send", provider="fake"),
    ))
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)
    monkeypatch.setattr(speech, "pause_ambient_for_mic", lambda **kwargs: NullContext())
    monkeypatch.setattr(speech, "MicLease", lambda *args: NullContext())
    monkeypatch.setattr(speech, "BusySection", lambda *args: NullContext())
    monkeypatch.setattr(speech, "UsageStore", FakeStore)
    monkeypatch.setattr(speech, "configure_cues_from_config", lambda cfg: None)
    monkeypatch.setattr(speech, "register_active_listen", lambda *args, **kwargs: None)
    monkeypatch.setattr(speech, "clear_active_listen", lambda *args, **kwargs: None)
    monkeypatch.setattr(speech, "poll_listen_action", lambda *args: None)
    monkeypatch.setattr(speech, "consume_listen_action", lambda *args: None)
    monkeypatch.setattr(speech, "play_record_start", lambda: None)
    monkeypatch.setattr(speech, "play_record_stop", lambda: None)

    result = speech.run_listen(cfg, end_mode="radio", post_tts_guard_s=0)
    assert result.text == ""
    assert calls
    assert not {
        "endpoint_strategy",
        "endpoint_probe_silence_s",
        "endpoint_max_silence_s",
        "on_endpoint_event",
    }.intersection(calls[0])


def test_silence_mode_builds_and_wires_the_configured_endpoint_strategy(monkeypatch):
    """Config reaches capture only for silence mode, including the safety bounds."""
    import hark.speech as speech
    from hark.audio.capture import CaptureResult
    from hark.config import HarkConfig

    class NullContext:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeStore:
        def record_stt(self, **kwargs):
            pass

    cfg = HarkConfig()
    cfg.listen.endpoint_strategy = "smart_turn"
    cfg.listen.endpoint_probe_silence_s = 0.4
    cfg.listen.endpoint_max_silence_s = 3.0
    cfg.listen.smart_turn_model_path = "/models/smart-turn-v3.onnx"
    cfg.listen.smart_turn_threshold = 0.7
    strategy = _FixedStrategy(True)
    builds: list[dict] = []
    captures: list[dict] = []

    def fake_build(**kwargs):
        builds.append(kwargs)
        return strategy

    def fake_capture(**kwargs):
        captures.append(kwargs)
        return CaptureResult(
            pcm16=b"\0\0",
            sample_rate=16000,
            duration_ms=20,
            speech_ms=20,
        )

    monkeypatch.setattr(speech, "build_endpoint_strategy", fake_build)
    monkeypatch.setattr(speech, "resolve_stt", lambda *args, **kwargs: SimpleNamespace(
        name="fake",
        transcribe=lambda wav: SimpleNamespace(text="captured", provider="fake"),
    ))
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)
    monkeypatch.setattr(speech, "pause_ambient_for_mic", lambda **kwargs: NullContext())
    monkeypatch.setattr(speech, "MicLease", lambda *args: NullContext())
    monkeypatch.setattr(speech, "BusySection", lambda *args: NullContext())
    monkeypatch.setattr(speech, "UsageStore", FakeStore)
    monkeypatch.setattr(speech, "configure_cues_from_config", lambda cfg: None)
    monkeypatch.setattr(speech, "register_active_listen", lambda *args, **kwargs: None)
    monkeypatch.setattr(speech, "clear_active_listen", lambda *args, **kwargs: None)
    monkeypatch.setattr(speech, "poll_listen_action", lambda *args: None)
    monkeypatch.setattr(speech, "consume_listen_action", lambda *args: None)
    monkeypatch.setattr(speech, "play_record_start", lambda: None)
    monkeypatch.setattr(speech, "play_record_stop", lambda: None)

    result = speech.run_listen(cfg, post_tts_guard_s=0)
    assert result.text == "captured"
    assert len(builds) == 1
    assert builds[0]["strategy_name"] == "smart_turn"
    assert builds[0]["smart_turn_model_path"] == "/models/smart-turn-v3.onnx"
    assert builds[0]["smart_turn_threshold"] == 0.7
    assert callable(builds[0]["on_warn"])
    assert captures[0]["endpoint_strategy"] is strategy
    assert captures[0]["endpoint_probe_silence_s"] == 0.4
    assert captures[0]["endpoint_max_silence_s"] == 3.0
    assert callable(captures[0]["on_endpoint_event"])


def test_block_constant():
    assert BLOCK_S == pytest.approx(0.02)

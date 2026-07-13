"""Pluggable endpointing strategies for silence-mode capture (B007).

Silence-mode capture historically ended a turn purely on an **energy gate**:
once speech opened, a fixed ``end_silence_s`` of quiet always finalized the
utterance. That is robust but blunt — it cuts off speakers who pause
mid-thought and makes everyone wait the full fixed hang even when they have
clearly finished.

This module introduces a small **strategy seam** so a smarter turn detector
(e.g. a Smart Turn / semantic-VAD model) can decide *earlier* that a turn is
complete, or hold on *longer* when it is not — while the energy gate remains
the default and the always-available fallback.

Design goals:

* **Behaviour-preserving default.** With no strategy, ``audio.capture`` keeps
  its original fixed-silence condition byte-for-byte intact.
* **No mandatory heavy deps.** The Smart Turn integration is an *optional*
  import (``onnxruntime`` + ``transformers`` + a model file). If unavailable,
  the factory returns ``None`` and capture falls back to the energy gate.
* **Fail safe.** A strategy that raises is disabled for the rest of the capture
  and endpointing defers to the fixed energy-gate silence.

See ``docs/ENDPOINTING.md`` for the evaluation and rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

import numpy as np


BLOCK_S = 0.02  # 20 ms capture block (matches audio.capture)


@dataclass(frozen=True)
class EndpointFrame:
    """Snapshot handed to a strategy at a candidate turn boundary.

    ``pcm16`` is the mono 16-bit PCM captured since speech opened (leading
    silence already trimmed by the energy gate). Strategies that only need
    recent context should trim to their own window.
    """

    pcm16: bytes
    sample_rate: int
    trailing_silence_s: float
    speech_s: float

    def samples_f32(self) -> np.ndarray:
        """Decode ``pcm16`` to float32 in [-1, 1]."""
        if not self.pcm16:
            return np.zeros(0, dtype=np.float32)
        ints = np.frombuffer(self.pcm16, dtype=np.int16)
        return (ints.astype(np.float32) / 32768.0).copy()


@runtime_checkable
class EndpointStrategy(Protocol):
    """Decide whether a spoken turn is complete at a candidate boundary.

    ``should_end`` returns:

    * ``True``  — turn complete, finalize now (may be earlier than the fixed
      ``end_silence_s``, reducing long waits).
    * ``False`` — turn incomplete, keep listening (reduces mid-thought
      cutoffs); still bounded by ``endpoint_max_silence_s``.
    * ``None``  — undecided; defer to the fixed energy-gate silence.
    """

    name: str

    def should_end(self, frame: EndpointFrame) -> bool | None: ...


def _blocks(seconds: float, block_s: float) -> int:
    # Truncate (not round) to match the legacy energy-gate block maths exactly.
    return max(1, int(seconds / block_s))


class SilenceEndpointer:
    """Per-capture endpoint decision engine driven by the block loop.

    The capture loop feeds it the running ``silent_blocks`` / ``speech_blocks``
    counters plus a lazy audio provider; it returns whether to finalize. All
    time thresholds are given in seconds and converted to 20 ms blocks.

    ``audio.capture`` uses this only when a non-energy strategy is active; the
    default keeps the legacy condition inline.
    """

    def __init__(
        self,
        *,
        end_silence_s: float,
        min_speech_s: float,
        strategy: EndpointStrategy | None = None,
        probe_silence_s: float | None = None,
        max_silence_s: float | None = None,
        block_s: float = BLOCK_S,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.block_s = block_s
        self.end_silence_blocks = _blocks(end_silence_s, block_s)
        self.min_speech_blocks = _blocks(min_speech_s, block_s)
        self.strategy = strategy
        self.on_event = on_event

        # Probe: how much trailing silence before a smart strategy is consulted.
        # Smaller than end_silence lets a confident strategy finish early.
        if probe_silence_s is None or probe_silence_s <= 0:
            probe_silence_s = min(end_silence_s, 0.6)
        self.probe_blocks = _blocks(probe_silence_s, block_s)

        # Hard cap: max trailing silence to wait when a strategy says "incomplete".
        # Defaults to end_silence so the energy gate stays the fallback ceiling.
        if max_silence_s is None or max_silence_s <= 0:
            max_silence_s = end_silence_s
        self.max_silence_blocks = max(self.end_silence_blocks, _blocks(max_silence_s, block_s))

        self._strategy_failed = False
        self._probed_this_silence = False
        self._probe_verdict: bool | None = None

    def on_speech(self) -> None:
        """Start a new silence run after the capture gate sees speech again."""
        self._probed_this_silence = False
        self._probe_verdict = None

    def _emit(self, event: str, **fields: object) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event, dict(fields))
            except Exception:
                pass

    def should_end(
        self,
        *,
        silent_blocks: int,
        speech_blocks: int,
        audio_fn: Callable[[], EndpointFrame],
    ) -> bool:
        """Return True if the capture should finalize now.

        ``audio_fn`` builds an :class:`EndpointFrame` lazily. A strategy is
        consulted once per contiguous silence run, after ``probe_silence_s``;
        repeated 20 ms model inference would otherwise overrun the audio loop.
        """
        if speech_blocks < self.min_speech_blocks:
            return False

        strategy = None if self._strategy_failed else self.strategy
        if strategy is None:
            return silent_blocks >= self.end_silence_blocks

        # Never wait past the hard cap (energy-gate fallback ceiling).
        if silent_blocks >= self.max_silence_blocks:
            return True
        # Not enough trailing silence to consult the strategy yet.
        if silent_blocks < self.probe_blocks:
            return False
        if self._probed_this_silence:
            if self._probe_verdict is None:
                return silent_blocks >= self.end_silence_blocks
            return False

        try:
            frame = audio_fn()
            verdict = strategy.should_end(frame)
        except Exception as exc:  # pragma: no cover - defensive
            self._strategy_failed = True
            self._emit(
                "endpoint.strategy_error",
                strategy=getattr(strategy, "name", "?"),
                error=str(exc)[:200],
            )
            # Fall back to fixed silence for the remainder of this capture.
            return silent_blocks >= self.end_silence_blocks

        self._probed_this_silence = True
        self._probe_verdict = verdict
        if verdict is True:
            self._emit(
                "endpoint.end",
                strategy=getattr(strategy, "name", "?"),
                trailing_silence_s=round(silent_blocks * self.block_s, 3),
            )
            return True
        if verdict is False:
            return False
        # None → undecided: defer to the fixed energy-gate silence.
        return silent_blocks >= self.end_silence_blocks


# ---------------------------------------------------------------------------
# Smart Turn strategy (optional; requires onnxruntime + transformers + a model)
# ---------------------------------------------------------------------------

# Completion probability predictor: (mono float32 @ sample_rate) -> P(complete).
PredictFn = Callable[[np.ndarray, int], float]


class SmartTurnStrategy:
    """Semantic/acoustic turn-completion detector.

    Thin, dependency-free wrapper around an injected ``predict_fn`` that maps a
    mono float32 waveform to a completion probability in ``[0, 1]``. The real
    model (pipecat Smart Turn v3 ONNX) is loaded lazily by
    :func:`load_smart_turn_predictor`; tests inject a fake predictor.

    A confident *complete* verdict (``p >= threshold``) ends the turn — possibly
    before the fixed ``end_silence_s``. Otherwise it returns ``False`` (keep
    listening) so a mid-thought pause is not cut off, bounded by the caller's
    ``endpoint_max_silence_s``.
    """

    name = "smart_turn"

    def __init__(
        self,
        predict_fn: PredictFn,
        *,
        threshold: float = 0.5,
        window_s: float = 8.0,
    ) -> None:
        self.predict_fn = predict_fn
        self.threshold = float(threshold)
        self.window_s = float(window_s)

    def should_end(self, frame: EndpointFrame) -> bool | None:
        samples = frame.samples_f32()
        if samples.size == 0:
            return None
        # Only the trailing window matters for turn completion.
        keep = int(self.window_s * frame.sample_rate)
        if keep > 0 and samples.size > keep:
            samples = samples[-keep:]
        p = float(self.predict_fn(samples, frame.sample_rate))
        if not np.isfinite(p) or not 0.0 <= p <= 1.0:
            raise ValueError(f"smart_turn predictor returned invalid probability {p!r}")
        return p >= self.threshold


def load_smart_turn_predictor(model_path: str) -> PredictFn:
    """Build a predictor for the pipecat Smart Turn v3 ONNX export.

    The upstream v3 export takes Whisper input features, rather than raw PCM.
    This loader follows the upstream preprocessing contract: mono 16 kHz audio,
    the newest eight seconds, then ``WhisperFeatureExtractor`` padding and
    normalization. All imports stay behind the ``smart-turn`` extra, and any
    setup or inference failure is handled by the caller as an energy fallback.
    """
    try:
        import onnxruntime as ort  # type: ignore
        from transformers import WhisperFeatureExtractor  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "smart_turn endpoint needs the 'smart-turn' extra: "
            "pip install 'hark[smart-turn]'"
        ) from exc

    import os

    if not model_path or not os.path.isfile(os.path.expanduser(model_path)):
        raise RuntimeError(f"smart_turn model not found: {model_path!r}")

    session_options = ort.SessionOptions()
    session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    session_options.inter_op_num_threads = 1
    session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = ort.InferenceSession(
        os.path.expanduser(model_path),
        sess_options=session_options,
        providers=["CPUExecutionProvider"],
    )
    input_name = sess.get_inputs()[0].name
    feature_extractor = WhisperFeatureExtractor(chunk_length=8)

    def predict(samples: np.ndarray, sample_rate: int) -> float:  # pragma: no cover - needs model
        if sample_rate != 16000:
            raise RuntimeError(
                f"smart_turn v3 requires 16 kHz mono audio, got {sample_rate} Hz"
            )
        inputs = feature_extractor(
            np.asarray(samples, dtype=np.float32),
            sampling_rate=sample_rate,
            return_tensors="np",
            padding="max_length",
            max_length=8 * sample_rate,
            truncation=True,
            do_normalize=True,
        )
        input_features = np.asarray(inputs.input_features, dtype=np.float32)
        out = sess.run(None, {input_name: input_features})[0]
        val = float(np.asarray(out).reshape(-1)[0])
        if not np.isfinite(val) or not 0.0 <= val <= 1.0:
            raise RuntimeError(f"smart_turn v3 returned invalid probability {val!r}")
        return val

    return predict


def build_endpoint_strategy(
    *,
    strategy_name: str,
    smart_turn_model_path: str | None = None,
    smart_turn_threshold: float = 0.5,
    predict_fn: PredictFn | None = None,
    on_warn: Callable[[str], None] | None = None,
) -> EndpointStrategy | None:
    """Resolve a configured endpoint strategy, falling back to the energy gate.

    Returns ``None`` for the energy gate (the default and the fallback). Any
    failure to build a smarter strategy emits a warning via ``on_warn`` and
    returns ``None`` so capture keeps working with no heavy deps.
    """
    name = (strategy_name or "energy").strip().lower()
    if name in ("", "energy", "energy_gate", "gate", "off", "none"):
        return None
    if name in ("smart_turn", "smart-turn", "smartturn"):
        try:
            threshold = float(smart_turn_threshold)
            if not np.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
                raise ValueError("smart_turn_threshold must be between 0 and 1")
            fn = predict_fn or load_smart_turn_predictor(smart_turn_model_path or "")
            return SmartTurnStrategy(fn, threshold=threshold)
        except Exception as exc:
            if on_warn is not None:
                on_warn(
                    f"smart_turn endpoint unavailable ({exc}); "
                    "falling back to energy gate"
                )
            return None
    if on_warn is not None:
        on_warn(
            f"unknown endpoint_strategy {strategy_name!r}; using energy gate"
        )
    return None

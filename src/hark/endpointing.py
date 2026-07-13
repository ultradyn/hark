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

* **Behaviour-preserving default.** With no strategy (``strategy is None``) the
  :class:`SilenceEndpointer` reproduces the previous fixed-silence rule exactly
  (``silent_blocks >= end_silence_blocks and speech_blocks >= min_speech``).
* **No mandatory heavy deps.** The Smart Turn integration is an *optional*
  import (``onnxruntime`` + a model file). If unavailable, the factory returns
  ``None`` and capture falls back to the energy gate.
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

    With ``strategy is None`` this is exactly the legacy energy gate.
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

        ``audio_fn`` builds an :class:`EndpointFrame` lazily; it is only called
        when an active strategy actually needs the audio, so the common energy
        gate path stays allocation-free.
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
# Smart Turn strategy (optional; requires onnxruntime + a model file)
# ---------------------------------------------------------------------------

# Completion probability predictor: (mono float32 @ sample_rate) -> P(complete).
PredictFn = Callable[[np.ndarray, int], float]


class SmartTurnStrategy:
    """Semantic/acoustic turn-completion detector.

    Thin, dependency-free wrapper around an injected ``predict_fn`` that maps a
    mono float32 waveform to a completion probability in ``[0, 1]``. The real
    model (pipecat Smart Turn v2 ONNX) is loaded lazily by
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
        return p >= self.threshold


def load_smart_turn_predictor(model_path: str) -> PredictFn:
    """Build a :data:`PredictFn` backed by a Smart Turn v2 ONNX model.

    Requires the optional ``smart-turn`` extra (``onnxruntime``). The model is
    the pipecat-ai ``smart-turn-v2`` export: it takes a raw 16 kHz mono
    waveform and emits a single completion logit. Raises ``RuntimeError`` /
    ``ImportError`` on any problem so the factory can fall back to the energy
    gate.

    Experimental: exact I/O tensor names vary by export; failures are caught by
    the factory and downgraded to the energy gate. Validate on your own setup.
    """
    try:
        import onnxruntime as ort  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "smart_turn endpoint needs the 'smart-turn' extra: "
            "pip install 'hark[smart-turn]'"
        ) from exc

    import os

    if not model_path or not os.path.isfile(os.path.expanduser(model_path)):
        raise RuntimeError(f"smart_turn model not found: {model_path!r}")

    sess = ort.InferenceSession(
        os.path.expanduser(model_path),
        providers=["CPUExecutionProvider"],
    )
    input_name = sess.get_inputs()[0].name
    output_name = sess.get_outputs()[0].name

    def _sigmoid(x: float) -> float:
        return 1.0 / (1.0 + float(np.exp(-x)))

    def predict(samples: np.ndarray, sample_rate: int) -> float:  # pragma: no cover - needs model
        del sample_rate
        arr = np.asarray(samples, dtype=np.float32).reshape(1, -1)
        out = sess.run([output_name], {input_name: arr})[0]
        val = float(np.asarray(out).reshape(-1)[0])
        # Treat as probability if already in [0,1], else squash the logit.
        return val if 0.0 <= val <= 1.0 else _sigmoid(val)

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
            fn = predict_fn or load_smart_turn_predictor(smart_turn_model_path or "")
        except Exception as exc:
            if on_warn is not None:
                on_warn(
                    f"smart_turn endpoint unavailable ({exc}); "
                    "falling back to energy gate"
                )
            return None
        return SmartTurnStrategy(fn, threshold=smart_turn_threshold)
    if on_warn is not None:
        on_warn(
            f"unknown endpoint_strategy {strategy_name!r}; using energy gate"
        )
    return None

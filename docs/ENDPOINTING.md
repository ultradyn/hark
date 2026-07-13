# Endpointing / turn detection (B007)

How Hark decides a spoken **answer window** (silence mode) is finished — an
evaluation of smarter turn detection, and the pluggable seam that lets you use
it while keeping the energy gate as the default and fallback.

## Problem

Silence mode (`[listen] end_mode = "silence"`) ended a turn on a pure **energy
gate**: once speech opened, a fixed `end_silence_s` (default 2.1 s) of quiet
always finalized the utterance (see `audio/capture.py`).

That single fixed hang forces a trade-off with no good setting:

- **Too short** → cuts people off when they pause to think mid-sentence
  ("so the function should… *[pause]* …take a timeout arg"). A cut-off turn
  means a truncated prompt sent to the agent.
- **Too long** → everyone waits ~2 s of dead air after they have obviously
  finished a short answer ("yes", "option two"), which feels sluggish.

A smarter detector can read *how* someone stopped (falling intonation, a
complete clause, filler vs. trailing-off) and end early when they are clearly
done, or wait when they are mid-thought — collapsing the trade-off.

## Options evaluated

| Approach | What it is | Smarter than energy gate? | Deps / cost | Verdict for Hark |
|---|---|---|---|---|
| **Energy gate + fixed `end_silence_s`** (current) | RMS threshold + adaptive noise floor + fixed silence hang | baseline | none (numpy) | Keep as default + fallback. Robust, zero-dep, offline. |
| **WebRTC VAD** (`webrtcvad`) | Per-frame voice/no-voice classifier | No — still needs a fixed silence timer on top; only cleaner speech/noise split | C extension | Not worth it. Doesn't answer "is the *turn* over", only "is this frame speech". |
| **Silero VAD** | Small ONNX speech-activity model | Marginal — better VAD, still not turn-level semantics | `onnxruntime` + ~2 MB model | Better VAD, but same fixed-hang problem. Skip for turn detection. |
| **[pipecat-ai Smart Turn v3](https://github.com/pipecat-ai/smart-turn)** | Open-source (BSD) Whisper-Tiny-based **turn-completion** model; its ONNX export consumes Whisper features from the current 16 kHz turn | **Yes** — trained specifically to distinguish "finished" vs "pausing" from acoustic + prosodic cues | `onnxruntime` + `transformers` + model (8 MB CPU / 32 MB GPU); under 100 ms CPU inference | **Recommended** as the opt-in smart strategy. Local, no cloud, permissive licence. |
| **LLM / cloud "semantic VAD"** (e.g. streaming STT endpointing, OpenAI Realtime turn detection) | Provider decides turn boundaries server-side | Yes, but | network round-trips, provider lock-in, cost, privacy | Rejected for the local answer-window path. Hark deliberately keeps the wake/answer gate local (see `docs/AUDIO_DESIGN.md`). |

## Recommendation

1. **Default stays the energy gate.** Zero dependencies, fully offline, and the
   guaranteed fallback. The default install must not grow heavy deps and must
   behave exactly as before when the feature is off.
2. **Ship a pluggable seam** so a smarter detector is a config switch, not a
   rewrite.
3. **Offer Smart Turn v3 as an optional extra** (`pip install 'hark[smart-turn]'`
   + a model file). It is the best local, privacy-preserving, permissively
   licensed turn-completion model available. If it can't load, capture
   transparently falls back to the energy gate.

We did **not** make Smart Turn mandatory: `onnxruntime` + `transformers` + a
model is heavy for a lightweight voice bridge, and the energy gate is good
enough for most close-talk answer windows.

## Design — the strategy seam

Implemented in `src/hark/endpointing.py`.

```text
capture block loop (audio/capture.py)
    │  per 20 ms block once speech opened
    ▼
SilenceEndpointer.should_end(silent_blocks, speech_blocks, audio_fn)
    │
    ├─ speech_blocks < min_speech      → keep listening
    ├─ strategy is None (energy gate)  → end at fixed end_silence_s   ← DEFAULT
    └─ strategy present:
         ├─ silent_blocks ≥ max_silence_s        → end (hard cap = fallback ceiling)
         ├─ silent_blocks <  probe_silence_s     → keep listening (too early to ask)
         └─ strategy.should_end(frame):
              True  → end now (may be < end_silence_s → less waiting)
              False → keep listening (up to max_silence_s → fewer cutoffs)
              None  → defer to fixed end_silence_s
```

Key properties:

- **Behaviour-preserving.** With `strategy is None` the decision reduces to
  `silent_blocks >= end_silence_blocks and speech_blocks >= min_speech_blocks`
  using the *same* truncating block maths as before. The original capture-loop
  condition remains byte-for-byte intact, and no per-block audio concatenation
  occurs on this path. Covered by `tests/test_endpointing.py::test_default_*`
  and `test_capture_energy_path_keeps_legacy_condition_and_avoids_per_block_concat`.
- **Fail safe.** A strategy that raises is disabled for the rest of that capture
  and endpointing defers to the fixed silence; an `endpoint.strategy_error`
  event is emitted once.
- **Bounded.** `endpoint_max_silence_s` (default = `end_silence_s`) caps how long
  a "keep listening" verdict can stall, so a mis-behaving model can never hang
  the mic. Raise it above `end_silence_s` to let Smart Turn hold longer through
  mid-thought pauses.
- **Realtime-safe probing.** A strategy runs once after `endpoint_probe_silence_s`
  for each contiguous quiet run. Further 20 ms blocks reuse an incomplete or
  undecided result; speech resets the probe for the next pause.
- **Silence mode only.** Radio mode (end-phrase / `listen-end`) is untouched.

### `EndpointStrategy` protocol

```python
class EndpointStrategy(Protocol):
    name: str
    def should_end(self, frame: EndpointFrame) -> bool | None: ...
    #   True  → turn complete (end)
    #   False → incomplete (keep listening)
    #   None  → undecided (defer to fixed end_silence_s)
```

`SmartTurnStrategy` wraps an injected `predict_fn(samples_f32, sample_rate) ->
P(complete)`; `p >= threshold` → `True`, else `False`. Tests inject a fake
predictor; `load_smart_turn_predictor(model_path)` builds the real ONNX-backed
predictor behind the optional import.

## Configuration

```toml
[listen]
end_mode = "silence"
end_silence_s = 2.1              # fixed-gate hang / fallback ceiling
endpoint_strategy = "energy"     # "energy" (default) | "smart_turn"

# Smart turn (only used when endpoint_strategy = "smart_turn"):
# endpoint_probe_silence_s = 0.4 # trailing quiet before first model probe (0 = auto: min(end_silence_s, 0.6))
# endpoint_max_silence_s   = 3.0 # max quiet to wait on an "incomplete" verdict (0 = end_silence_s)
# smart_turn_model_path    = "~/.local/share/hark/models/smart-turn-v3.onnx"
# smart_turn_threshold     = 0.5 # P(complete) at/above which the turn ends
```

Env overrides: `HARK_LISTEN_ENDPOINT_STRATEGY`, `HARK_SMART_TURN_MODEL`.

Install the extra and fetch a Smart Turn v3 ONNX export:

```bash
pip install 'hark[smart-turn]'
# point smart_turn_model_path at a pipecat-ai smart-turn-v3 .onnx export
```

## Status / caveats

- **Validated:** the seam, the energy-gate default (behaviour-preserving), the
  decision logic (early-end / keep-listening / defer / fail-safe), config
  plumbing, and the `SmartTurnStrategy` wrapper — all under
  `tests/test_endpointing.py` and `tests/test_config_endpoint.py` with an
  injected fake predictor, plus a fake optional runtime that checks the Smart
  Turn v3 preprocessing contract (no model or optional dependency needed for
  CI).
- **Operational caveat:** Smart Turn requires a 16 kHz mono capture and a v3
  ONNX export. Any load or inference failure is intentionally downgraded to the
  energy gate. Validate probabilities on your own model/mic before relying on
  it.

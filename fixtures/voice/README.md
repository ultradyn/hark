# Voice fixtures (wake / STT)

Short operator-voice clips and derived variants for wake regression and offline
engine comparison (B071) without a live mic.

## Layout

```text
fixtures/voice/
  README.md
  wake/
    cases.jsonl             # eval index: id → wav?, vosk_text?, expect_match, tags
    live/                   # real captures from ambient debug snips
      *.wav                 # 16 kHz mono PCM (~2.5 s)
      *.json                # sidecar (text, matched, phrase, rms, backend)
    derived/                # B071: noise/gain/pad/silence from live + synth PCM
      *.wav
  stt/                      # optional future full-utterance STT clips
```

## Case schema (`cases.jsonl`)

| Field | Required | Meaning |
|-------|----------|---------|
| `id` | yes | Stable case id |
| `expect_match` | yes | Whether production wake should fire |
| `wav` | audio rows | Path relative to `wake/` |
| `meta` | live rows | Sidecar JSON (optional for derived) |
| `vosk_text` / `text` | text path | Transcript for `match_activation` parity |
| `expect_phrase_contains` | hits | Substring of matched phrase (`hark` / `herald` / …) |
| `tags` | recommended | Dimensions: `live`, `derived`, `text-only`, `positive`/`negative`, `greeting`/`bare`, `noise-*`, `custom-name`, `speaker-op1`, … |
| `parent` | derived | Source live id |
| `source` | recommended | `live-capture-…`, `derived-from-live`, `synthetic-text`, … |
| `notes` | optional | Human rationale |

### Outcome taxonomy (eval harness)

| Label | Meaning |
|-------|---------|
| **hit** | expect wake + got wake (TP) |
| **miss** | expect wake + no wake (FN) |
| **fa** | no wake expected + got wake (false accept / FP) |
| **reject** | no wake expected + no wake (TN) |

```bash
# Summary table (text_path always; Vosk/Sherpa when installed)
uv run python scripts/eval-wake-fixtures.py
uv run python scripts/eval-wake-fixtures.py --audio-only -v
uv run pytest tests/test_wake_eval_harness.py -q
uv run pytest tests/test_wake_eval_harness.py -m vosk -s    # optional
uv run pytest tests/test_wake_eval_harness.py -m sherpa_kws -s  # optional (B070+)
```

Sherpa KWS is **optional**: default CI must not require the model or
`sherpa-onnx`. When B070 lands `SherpaKwsWakeBackend`, the same harness scores
it automatically.

Regenerate derived WAVs + refresh `cases.jsonl` / MANIFEST:

```bash
uv run python scripts/gen-wake-eval-fixtures.py
```

## Live corpus (2026-07-13 Wave captures)

| id | vosk text | expect |
|----|-----------|--------|
| hey-harold-as-herald-hit | hey harold | match → herald |
| hey-hook-as-hark-hit | hey hook | match → hark |
| hey-hawk-as-hark-hit | hey hawk | match → hark |
| a-hawk-miss | a hawk | no match |
| hey-hoc-miss | hey hoc | no match |
| hello-alone-miss | hello | no match |
| hey-ho-miss | hey ho | no match |

Parity tests use **`vosk_text`** (or sidecar `text`) through `match_activation` —
same as production after local ASR. Optional offline Vosk / Sherpa re-decode of
the WAV is in `tests/test_vosk_wake_fixtures.py` and `tests/test_wake_eval_harness.py`.

## Capture method (expanding the live set)

### A. Ambient debug snips (preferred)

1. Enable ambient with debug snips (default in dev): `[ambient] debug_snips = true`
2. Speak targets at normal distance (one utterance per attempt):
   - **Greeting hits:** hey/hello/ok + hark|herald|iris|mercury
   - **Bare hits:** name alone (and soft “um name”)
   - **Custom names:** whatever is in `ambient.names`
   - **Negatives:** incomplete greating, near-misses, room talk, media bleed
   - **Noise:** same phrase with fan / keyboard / far-field
3. Export into the repo:

```bash
./scripts/export-fixtures.sh --with-wake
uv run python scripts/gen-wake-eval-fixtures.py   # refresh derived variants
uv run python scripts/eval-wake-fixtures.py -v
```

Snips land under `~/.local/state/hark/debug/wake/<YYYY-MM-DD>/` then
`fixtures/voice/wake/live/`.

### B. Direct ffmpeg (Elgato Wave / Pulse)

Prefer **16 kHz mono PCM WAV**, ~2–3 s (matches `ambient.snippet_s`):

```bash
ffmpeg -f pulse -i default -ac 1 -ar 16000 -t 2.5 \
  fixtures/voice/wake/live/hey-iris-clean.wav
```

Add a row to `cases.jsonl` (or re-run export if you also write a sidecar JSON),
then regenerate derived variants.

### Tips

| Clip | Content | Length |
|------|---------|--------|
| clean wake | “hey hark” / “hey herald” / “hey iris” | 1–2.5 s |
| mishear hit | natural speech vosk garbles (hook/hawk/harold) | 1–2.5 s |
| bare name | “herald”, “iris” | 1–2 s |
| negative | “what about the hard drive” / bare “hello” | 2–3 s |
| noise | same as clean with room noise | 2–3 s |
| multi-speaker | second operator, same script | 2–3 s |

Speak at normal distance from the Wave. One utterance per file. Do not commit
third-party voices without consent.

### Derived corpus (no mic)

`scripts/gen-wake-eval-fixtures.py` builds from each live clip:

| Variant | Purpose |
|---------|---------|
| `*-noise-light` | ~18 dB SNR white noise |
| `*-noise-heavy` | ~8 dB SNR white noise |
| `*-quiet` | gain 0.35 (distance / soft talk) |
| `*-pad150` | 150 ms leading silence shift |
| `silence-2.5s-miss` / `noise-only-miss` | energy-floor / FA guards |

Plus **text-only** rows for bare vs greating and custom names (Iris/Mercury)
without multi-speaker audio — scored on `text_path` only.

## Using in tests

```python
import json
from pathlib import Path
from hark.wake import match_activation
from hark.wake_eval import load_cases, evaluate_cases, format_summary_table

root = Path("fixtures/voice/wake")
for case in load_cases(root / "cases.jsonl"):
    if case.get("vosk_text") is None:
        continue
    hit = match_activation(case["vosk_text"], anywhere=True)
    assert bool(hit) == case["expect_match"], case["id"]

# Full summary (optional engines):
# uv run python scripts/eval-wake-fixtures.py
```

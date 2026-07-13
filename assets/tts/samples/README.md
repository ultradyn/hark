# Hark TTS voice samples

Curated **same-phrase** clips for comparing TTS voices during setup / dogfood.

This is **not** the runtime cache (`assets/tts/{voice}/…` content-hashed files).

## Layout

```
assets/tts/samples/
  README.md
  phrase.txt                 # template with {Voice} (said ≥2× per sample)
  manifest.json              # inventory + per-voice filled phrase
  xai/
    male/{voice_id}.mp3
    female/{voice_id}.mp3
  openai/
    male/ | female/ | unknown/
  minimax/
    male/ | female/ | unknown/
```

| Segment | Meaning |
|---------|---------|
| `{provider}` | `xai` \| `openai` \| `minimax` (future backends as needed) |
| `{gender}` | Catalog gender when known; else `unknown` |
| `{voice_id}.mp3` | Provider voice id as filename (no date folders) |

Rules:

1. Shared **phrase template** (`phrase.txt`) with `{Voice}` filled per sample so
   each clip **says the voice name at least twice** (e.g. “This is Leo… listening to Leo…”).
2. Re-generate **in place** to refresh; do not create `male-YYYYMMDD` trees.
3. Keep runtime cache out of `samples/`.
4. OpenAI / MiniMax trees stay empty until xAI dogfood feedback; then use the
   **same template** and update `manifest.json`.

## Play (local)

```bash
# inventory
jq -r '.samples[] | "\(.provider)/\(.gender)/\(.voice_id)\t\(.bytes)"' \
  assets/tts/samples/manifest.json

# play one
mpv assets/tts/samples/xai/male/leo.mp3
ffplay -autoexit -nodisp assets/tts/samples/xai/female/eve.mp3
```

## Regenerate

```bash
# rebuild all xAI from catalog (requires hark auth)
python3 scripts/gen-tts-samples.py --provider xai

# single voice (script injects the name twice)
python3 scripts/gen-tts-samples.py --provider xai --voice leo
```

## Naming note

- **Wake name** `iris` (“hey iris”) is independent of TTS **voice_id** `iris`.
- Default product pairing under discussion: wake **Iris** + TTS voice **eve** (not necessarily TTS `iris`).

## OpenAI / MiniMax (later)

Same layout and `phrase.txt` template (`{Voice}` twice). Typical OpenAI ids:
`alloy`, `ash`, `ballad`, `coral`, `echo`, `fable`, `onyx`, `nova`, `sage`,
`shimmer` — put under `unknown/` if gender is unclear. MiniMax uses their
`voice_id` strings (default in Hark: `English_expressive_narrator`).

# Voice fixtures (wake / STT)

Short operator-voice clips for regression tests without a live mic.

## Layout

```text
fixtures/voice/
  README.md
  wake/
    cases.jsonl             # index: id → wav, vosk_text, expect_match
    live/                   # real captures from ambient debug snips
      *.wav                 # 16 kHz mono PCM
      *.json                # sidecar (text, matched, phrase, rms, backend)
  stt/                      # optional future full-utterance STT clips
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

Parity tests use **`vosk_text`** (or sidecar `text`) through `match_activation` — same as production after local ASR. Full offline Vosk re-decode of the WAV is optional later.

## Recording more (Elgato Wave)

Prefer **16 kHz mono PCM WAV**:

```bash
ffmpeg -f pulse -i default -ac 1 -ar 16000 -t 3 \
  fixtures/voice/wake/live/hey-hark-clean.wav
```

Or enable ambient debug snips and export:

```bash
# config: [ambient] debug_snips = true  (already default in dev)
# say wake phrases, then:
./scripts/export-fixtures.sh --with-wake
```

### Tips

| Clip | Content | Length |
|------|---------|--------|
| clean wake | just “hey hark” or “hey herald” | 1–2 s |
| mishear hit | natural speech vosk garbles (hook/hawk/harold) | 1–2 s |
| negative | “what about the hard drive” / bare “hello” | 2–3 s |
| stt prompt | full short command after wake | 2–5 s |

Speak at normal distance from the Wave. One utterance per file.

## Using in tests

```python
import json
from pathlib import Path
from hark.wake import match_activation

for line in Path("fixtures/voice/wake/cases.jsonl").read_text().splitlines():
    case = json.loads(line)
    hit = match_activation(case["vosk_text"], anywhere=True)
    assert bool(hit) == case["expect_match"], case["id"]
```

# Local wake engines & STT (operator + handsfree guide)

Hark ambient **wake** uses a **local** short-snippet model only. After activation,
prompt body / radio STT stays **cloud-first** (xAI etc.). Full survey:
[docs/plans/B069-local-stt-survey.md](https://github.com/ultradyn/hark/blob/master/docs/plans/B069-local-stt-survey.md).

Setup chooser: [SETUP.md](SETUP.md) · `hark setup --wake-engine …`

---

## Why Sherpa is better for wake (vs Vosk)

**Short version:** Vosk is *open speech recognition* (transcribe whatever was said).
Sherpa KWS is *keyword spotting* (listen for the activation phrases we configure).
Wake is a keyword problem, so KWS fits much better.

| | **Vosk** (open-vocab ASR) | **Sherpa KWS** (keyword spotting) |
|--|---------------------------|-----------------------------------|
| **Job** | Turn audio → arbitrary English text | Score configured keyword phrases |
| **Product names** | Often mangles short/rare names (`hark`→hook/hawk, `iris`→irish/iraq) | Matches phrase graph (`hey iris`, `hey hark`, …) |
| **Near-misses** | Needs alias tables + learning to paper over ASR errors | Still useful, but less “luck” for first-try wake |
| **Cost** | Higher RSS / RTF on continuous ambient | ~half Vosk RSS; RTF ≈ 0.02 on short snips (B069) |
| **When it fails** | Wrong *words* in the transcript | Keyword below threshold / not in keyword set |

**Dogfood takeaway:** With Vosk, activation can feel random on **iris** / **mercury**.
With Sherpa, the same “hey iris” (even slightly mangled as *irris* at the KWS layer)
tends to fire first try because the engine is hunting for that keyword, not guessing
a general sentence.

**What wake is not:** Full conversation STT. After wake, utterance STT still goes
to cloud (or optional local Whisper — not for always-on wake).

**Recommend for most operators:** `engine = "sherpa_kws"` once the model +
`wake-sherpa` extra are installed. Keep **Vosk** only if you need the smaller
dep set or are debugging without ONNX.

---

## Engines

| Engine | Config | When to pick | Size / cost (approx) |
|--------|--------|--------------|----------------------|
| **Sherpa KWS** | `engine = "sherpa_kws"` | **Preferred** for product names + reliability | ~20 M tree; RSS ~half Vosk; RTF ≈ 0.02 |
| **Vosk** (stock default) | `engine = "vosk"` | Constrained disk/deps; already set up; alias learning OK | ~40 M zip / ~68 M installed; RSS ~150 MiB |
| **text_probe** | `engine = "text_probe"` | Tests only | — |

Config default in the package remains **Vosk** for backward compatibility; handsfree
setup / dogfood should **recommend Sherpa** when download is OK.

---

## Vosk

- Model: `vosk-model-small-en-us-0.15` (Apache-2.0)
- Install: `./scripts/setup-ambient.sh` or `./scripts/download-vosk-model.sh`
- Python: `uv sync --extra wake`
- Path (auto): `~/.local/share/hark/models/vosk-model-small-en-us-0.15`
- Matching: open-vocab ASR → text → `match_activation` (seed aliases + near-miss learning)
- Larger models: optional `model_path` (0.22-lgraph / 0.22; see docs); still ASR, not true KWS

---

## Sherpa-ONNX open-vocab KWS

- Model: `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` (English, prefer **int8**)
- Install model: `./scripts/download-sherpa-kws-model.sh`
- Python: `uv sync --extra wake-sherpa`  
  (`sherpa-onnx` + `sentencepiece` + **`onnxruntime`** — provides `libonnxruntime.so`)
- Path (auto): `~/.local/share/hark/models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01`
- Keywords built from `WakePolicy` (names×prefixes + exact phrases); **rebuild on config reload / SIGHUP**
- Doctor: `hark doctor` reports `status=ready|missing_model|package_missing` for `engine=sherpa_kws`
- Handsfree launcher (`scripts/run-mode-a.sh`) puts onnxruntime’s `capi/` on `LD_LIBRARY_PATH`
  so `import sherpa_onnx` can resolve the shared library (Hark also re-execs once if needed)

```toml
[ambient]
engine = "sherpa_kws"
# model_path = "~/.local/share/hark/models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
```

```bash
./scripts/download-sherpa-kws-model.sh
uv sync --extra wake-sherpa
# or: hark setup --yes --wake-engine sherpa_kws
./scripts/run-mode-a.sh   # restart ambient after engine change
```

**Fail-open:** missing model → clear error / doctor MISSING; leave `engine = "vosk"` or install
the model. Ambient does **not** silently use cloud for wake.

If logs say `sherpa-onnx not installed` but the package is present, the usual cause is
**missing `libonnxruntime.so`** (install `onnxruntime` via `wake-sherpa`, restart workers).

---

## Optional local full-STT (post-wake / offline) — not default

**B072** — optional utterance STT behind the existing provider interface.
Cloud remains product default (`stt.provider = "auto"`, ADR-004). **Do not** use
Whisper-family models for continuous ambient wake (name mangling + always-on cost).

```bash
uv sync --extra local-stt          # faster-whisper (from a repo checkout)
# or: pip install '.[local-stt]'
```

```toml
[stt]
provider = "faster_whisper"   # or "local" / "moonshine"
local_model = "tiny.en"       # or base.en
local_fail_open = true        # → cloud when model/dep missing
```

Env: `HARK_STT_PROVIDER`, `HARK_STT_LOCAL_MODEL`, `HARK_STT_LOCAL_FAIL_OPEN`.
Details and B069 RTF notes: [docs/PROVIDERS.md](https://github.com/ultradyn/hark/blob/master/docs/PROVIDERS.md).

| Option | Role |
|--------|------|
| **faster-whisper** `tiny.en` / `base.en` int8 | Primary local full-STT (CPU) |
| **Moonshine** | Stretch / experimental path |
| **whisper.cpp** | Ops alternative (not first-class in hark yet) |

---

## Eval & enrollment

- Hit/miss/FA table: `scripts/eval-wake-fixtures.py` (B071)
- Beep-paced practice samples: `hark wake-enroll` (I006) — local WAVs under
  `~/.local/state/hark/wake_enroll/`; optional scoring can seed learned aliases
  (B077 denylist blocks junk like `is` / place-name confusables)

---

## Related

| ID | Topic |
|----|--------|
| B069 | Local STT survey (done) |
| B070 | Sherpa backend + setup (done) |
| B071 | Wake eval harness (done) |
| B072 | Optional local full-STT (done) |
| B073 | Larger Vosk model docs (done) |
| I006 | Wake enrollment samples (done) |

# Local wake engines & STT (operator guide)

Hark ambient **wake** uses a **local** short-snippet model only. After activation,
prompt body / radio STT stays **cloud-first** (xAI etc.). Full survey:
[docs/plans/B069-local-stt-survey.md](../../docs/plans/B069-local-stt-survey.md).

Setup chooser: [SETUP.md](SETUP.md) · `hark setup --wake-engine …`

---

## Engines

| Engine | Config | When to pick | Size / cost (approx) |
|--------|--------|--------------|----------------------|
| **Vosk** (default) | `engine = "vosk"` | Already set up; constrained disk/deps; fine with alias learning | ~40 M zip / ~68 M installed; RSS ~150 MiB |
| **Sherpa KWS** | `engine = "sherpa_kws"` | Better product names (`hark`/`herald`/`iris`…); low RTF | ~20 M tree; RSS ~half Vosk; RTF ≈ 0.02 |
| **text_probe** | `engine = "text_probe"` | Tests only | — |

**Default stays Vosk** until dogfood says otherwise (B070 does not flip the default).

---

## Vosk

- Model: `vosk-model-small-en-us-0.15` (Apache-2.0)
- Install: `./scripts/setup-ambient.sh` or `./scripts/download-vosk-model.sh`
- Python: `uv sync --extra wake`
- Path (auto): `~/.local/share/hark/models/vosk-model-small-en-us-0.15`
- Matching: open-vocab ASR → text → `match_activation` (seed aliases + near-miss learning)
- Larger models: optional `model_path` (see B073); still ASR, not true KWS

---

## Sherpa-ONNX open-vocab KWS

- Model: `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` (English, prefer **int8**)
- Install: `./scripts/download-sherpa-kws-model.sh`
- Python: `uv sync --extra wake-sherpa` (`sherpa-onnx` + `sentencepiece`)
- Path (auto): `~/.local/share/hark/models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01`
- Keywords built from `WakePolicy` (names×prefixes + exact phrases); **rebuild on config reload / SIGHUP**
- Doctor: `hark doctor` reports `status=ready|missing_model|package_missing` for `engine=sherpa_kws`

```toml
[ambient]
engine = "sherpa_kws"
# model_path = "~/.local/share/hark/models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
```

**Fail-open:** missing model → clear error / doctor MISSING; leave `engine = "vosk"` or install the model. Ambient does not silently use cloud for wake.

---

## Optional local full-STT (post-wake / offline) — not default

**B072 landed** — optional utterance STT behind the existing provider interface.
Cloud remains product default (`stt.provider = "auto"`, ADR-004). **Do not** use
Whisper-family models for continuous ambient wake (name mangling + always-on cost).

```bash
pip install 'hark[local-stt]'   # faster-whisper
```

```toml
[stt]
provider = "faster_whisper"   # or "local" / "moonshine"
local_model = "tiny.en"       # or base.en
local_fail_open = true        # → cloud when model/dep missing
```

Env: `HARK_STT_PROVIDER`, `HARK_STT_LOCAL_MODEL`, `HARK_STT_LOCAL_FAIL_OPEN`.
Details and B069 RTF notes: [docs/PROVIDERS.md](../../docs/PROVIDERS.md).

| Option | Role |
|--------|------|
| **faster-whisper** `tiny.en` / `base.en` int8 | Primary local full-STT (CPU) |
| **Moonshine** | Stretch / experimental path |
| **whisper.cpp** | Ops alternative (not first-class in hark yet) |

---

## Comparison snapshot (B069 probe)

On early live wake fixtures: Vosk/Whisper mangle `hark`→hook/hawk; **Sherpa KWS**
hit/miss-separated product phrases without alias tables. Expand eval with
`scripts/eval-wake-fixtures.py` (B071).

---

## Related tasks

| ID | Topic |
|----|--------|
| B069 | Survey (done) |
| B070 | Sherpa backend + setup (done) |
| B071 | Wake eval harness (done) |
| B072 | Optional local full-STT (done) |
| B073 | Larger Vosk model docs (done) |

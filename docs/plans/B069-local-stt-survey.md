# B069 — Light/fast local STT & wake-ASR survey

**Status:** investigation complete (2026-07-14)  
**Related:** idea **I004** (local models / streaming Whisper), ADR-004 (tiny local wake only; full STT cloud-first), `docs/AUDIO_DESIGN.md` ambient pipeline, `fixtures/voice/wake/`.

This is a **research / recommendation** slice — not multi-engine product UI.

---

## 1. Hark constraints (shape of the problem)

| Constraint | Detail |
|------------|--------|
| **Wake path** | Continuous or near-continuous **2–3 s** local snippets (`ambient.snippet_s` default 2.5). **No cloud** until activation fires. |
| **Hardware** | Laptop-class **CPU OK**; GPU optional, **must not require** GPU. This probe host: AMD Ryzen 7 255 (16 threads), 60 GiB RAM, **no NVIDIA**. |
| **Idle cost** | Low CPU/RSS when quiet; energy floor already skips silent frames (`VoskWakeBackend.energy_floor`). |
| **Post-wake STT** | Prompt body / answer windows stay **cloud-first** (xAI etc.). Local full-STT is optional (offline, privacy, latency). Target feel: **≲ 1–1.5 s** after speech end on mid hardware if local. |
| **Fail-open / pluggable** | Keep cloud STT. Engine should be swappable (`WakeBackend` protocol already exists). |
| **Open models preferred** | Python API or CLI/subprocess. Apache/MIT preferred over AGPL / non-commercial for product defaults. |
| **Product names** | Wake words are rare English tokens (`hark`, `herald`). Generic ASR systematically maps them to common words (`hook`, `hawk`, `harold`). Today compensated by **seed aliases + near-miss learning** (`docs/CUSTOM_WAKE.md`). |

### Current baseline path

```text
mic → 2–3 s snippets → Vosk small en-us → text → match_activation / fuzzy aliases
                                                    → on hit: cloud STT for prompt body
```

Implementation: `VoskWakeBackend` in `src/hark/wake.py`, ambient loop in `src/hark/ambient.py`. Fixtures under `fixtures/voice/wake/` (live WAVs + `cases.jsonl`).

---

## 2. Probe environment & methods

| Item | Value |
|------|--------|
| Host | AMD Ryzen 7 255, 16 threads, no discrete NVIDIA GPU |
| OS Python via | `uv run` project venv |
| Fixtures | 7× 2.50 s mono wake WAVs in `fixtures/voice/wake/live/` |
| **Measured here** | Vosk small; faster-whisper `tiny.en` / `base.en` int8 CPU; sherpa-onnx English KWS int8 |
| **Cited (not re-run)** | Official Vosk model table WER/size; whisper.cpp / faster-whisper published RTF; Moonshine paper/README latency claims; Porcupine/openWakeWord license notes |

**RTF** = wall decode time / audio duration. RTF ≪ 1 is faster than real-time.

---

## 3. Candidate comparison table

Effort scale: **L** = drop-in / config; **M** = new backend + deps + tests; **H** = training pipeline or commercial licensing work.

| Candidate | On-disk size (approx) | RTF / latency notes | Wake quality (names / short phrases) | Integration effort | License |
|-----------|----------------------|---------------------|--------------------------------------|--------------------|---------|
| **Vosk small en-us 0.15** (baseline) | **~40 M zip / ~68 M installed** | **Measured:** load ~330 ms; 2.5 s snip decode **~280–380 ms**, **RTF ≈ 0.11–0.15**; peak RSS **~158 MiB** | Weak on rare names: fixtures yield `hey hook` / `hey hawk` / `hey harold` / `hey hoc`. Product lives on **aliases + learning**. Official WER ~9.9 % Librispeech test-clean. | **Already shipped** | Apache-2.0 (API + this model) |
| **Vosk en-us 0.22** (larger) | **1.8 G** | Published as server-class; still online Kaldi, typically real-time on desktop. ~20 % relative WER win over small in third-party evals; **not measured here** (download cost). | Better generic English; **unlikely to invent “hark”** without LM bias — alias layer still needed. High RAM (vendor: big models up to multi‑GB). | **L** (`model_path` already config) | Apache-2.0 |
| **Vosk en-us 0.22-lgraph** | **128 M** | Middle ground; dynamic graph; slower than small in some reports | Similar story: better WER, still open-vocab ASR not KWS | **L** | Apache-2.0 |
| **faster-whisper tiny.en** int8 CPU | Model cache ~**75 M** params / CT2 files small | **Measured:** cold load ~5.5 s; 2.5 s snip **~250–350 ms** typical (**RTF ≈ 0.10–0.14**), one outlier ~1.2 s (RTF 0.47) | **Still mangles product names** on same fixtures: `Hey, Hawk.` / `Hey, Hulk.` / `Hey hot.` — no free lunch from Whisper for “hark”. Hallucination risk on short/quiet clips. | **M** (Python, optional dep) | MIT (code); Whisper weights MIT |
| **faster-whisper base.en** int8 CPU | Larger CT2 | **Measured:** load ~13.5 s; decode **~460–570 ms** (RTF ≈ 0.19–0.23), hello clip **~2.2 s** | Same name class errors (`Hawk`/`Harold`); not wake-specialized | **M** | MIT |
| **whisper.cpp tiny.en** | ~**75 M** ggml | **Cited:** tiny often **RTF ~0.05–0.1** on laptop CPU; excellent on Apple Silicon Metal. Same model family as above for quality. | Same Whisper quality profile | **M** (CLI subprocess or bindings) | MIT |
| **Sherpa-ONNX streaming Zipformer ASR** (full online ASR) | English streaming models typically **~60–200 M** depending on int8/fp32 | Designed for **streaming RTF ≪ 1** on CPU; continuous partials possible | Better streaming STT candidate than Whisper for always-on **transcription**; open-vocab still mangles rare names | **M–H** | Apache-2.0 (sherpa-onnx) |
| **Sherpa-ONNX open-vocab KWS** (Zipformer GigaSpeech **3.3 M**, English) | **~20 M** tree; int8 encoder **~4.6 M** | **Measured:** load ~0.95 s; 2.5 s snip **~47–63 ms**, **RTF ≈ 0.019–0.025**; peak RSS **~63 MiB** | **Best on fixture set:** hits `HEY_HARK` / `HEY_HERALD` on the three “hit” WAVs; **empty** on four “miss” WAVs (no alias table). Custom keywords via BPE `keywords_file`. | **M** (new `WakeBackend`, optional dep `sherpa-onnx`) | Apache-2.0 |
| **Moonshine tiny / streaming** | **~27 M** params (tiny); English **MIT** | **Cited:** edge-focused; latency scales with audio duration (unlike Whisper’s 30 s pad). Vendor tables: Moonshine Tiny Streaming **tens–hundreds of ms** on short clips vs Whisper tiny multi‑100 ms–seconds. | Strong **short-form STT** candidate; **not** a dedicated KWS. English open weights OK; non-English often community/non-commercial. | **M** (Python / ONNX) | MIT (EN models + much of code) |
| **Porcupine-class wake-only** (Picovoice) | Very small runtime | Excellent KWS latency on embedded | Strong commercial KWS; **custom “hey hark” needs paid path**; free tier / access-key model is a product risk (community reports free-tier sunset pressure). | **H** (license + keys) | Proprietary (+ limited free) |
| **openWakeWord** | Small ONNX classifiers | Real-time on CPU; Home Assistant popular | Needs **trained custom model** for “hey hark” (synthetic + negatives). Pretrained models often **CC BY-NC-SA**. Code Apache-2.0. | **H** (train + ship model) | Apache-2.0 code; models often NC |
| **NeMo CTC light / other ONNX ASR** | Varies | GPU-biased ecosystem | Overkill for wake; optional full STT research | **H** | Apache-2.0 typically |

### Fixture decode snapshot (this machine)

| WAV (expect) | Vosk small text | faster-whisper tiny.en | Sherpa KWS keywords |
|--------------|-----------------|------------------------|---------------------|
| hey-hook-as-hark-hit (**wake**) | `hey hook` | `Hey, Hawk.` | **`HEY_HARK`** |
| hey-hawk-as-hark-hit (**wake**) | `hey hawk` | `Hey, Hawk.` | **`HEY_HARK`** |
| hey-harold-as-herald-hit (**wake**) | `hey harold` | `Hey Harold.` | **`HEY_HERALD`** |
| a-hawk-miss | `a hawk` | `Hey, Hulk.` | _(none)_ |
| hey-hoc-miss | `hey hoc` | `Hey, Hawk.` | _(none)_ |
| hello-alone-miss | `hello` | `Hello there.` | _(none)_ |
| hey-ho-miss | `hey ho` | `Hey hot.` | _(none)_ |

Takeaway: **generic ASR (Vosk and Whisper) both fail the “hark” token**; Hark’s alias layer papers over Vosk. **Open-vocab KWS matched the product phrase class without aliases** on this small live set (N=7 — indicative, not a full ROC study).

---

## 4. Streaming notes

| Approach | Streaming fit for Hark |
|----------|------------------------|
| **Snippet ASR (today)** | Decode whole 2–3 s buffer each cycle. Simple; latency = full-snippet RTF. |
| **Online ASR (Sherpa Zipformer / Whisper streaming forks)** | Partial tokens while speaking; good for **post-wake local dictation**, heavier always-on CPU if used for idle wake. |
| **Open-vocab KWS (Sherpa)** | True streaming keyword paths; **fires when phrase completes** — ideal ambient gate. Keywords rebuild when config names/phrases change (same live-reload story as today). |
| **Whisper family** | Batch-oriented; short-clip behavior can include padding/hallucination. Prefer for **utterance STT**, not always-on wake, unless a dedicated streaming stack is validated. |
| **Radio / answer window** | Remains **cloud STT** after wake (partials, end phrases). Local STT would only replace that path under an explicit offline/privacy mode. |

---

## 5. Recommendation

### Primary wake engine (next implement)

**Sherpa-ONNX open-vocabulary keyword spotting** (English GigaSpeech 3.3 M int8, custom keywords for configured names/phrases).

**Why:**

1. **Right problem class** — wake is KWS, not dictation. Fighting Vosk aliases forever is a product smell; KWS scores configured phrases directly.
2. **Measured fit** — RTF ≈ **0.02**, RSS ≈ **half of Vosk small**, model **~20 M** on disk vs 68 M (or 1.8 G for big Vosk).
3. **Fixture behavior** — clean hit/miss separation on current live set without `hook`/`hawk` seed lists.
4. **Open stack** — Apache-2.0, Python package `sherpa-onnx`, CPU provider, no GPU required.
5. **Pluggable** — implements the existing `WakeBackend.score_snippet` seam; keep **Vosk as default** until the new engine is dogfooded; fail-open if model missing.

**Caveats (explicit):**

- N=7 fixtures; need a larger wake eval (multi-speaker, noise, custom names) before flipping the default.
- Keyword file must be regenerated when `names` / `trigger_phrases` change (wire into config watch / SIGHUP).
- Bare-name vs greating+name thresholds need tuning to preserve “no mid-sentence false wake” policy.
- Near-miss learning UX may need a KWS-specific story (low acoustic score near threshold vs ASR text aliases).

### Optional local full-STT (post-wake / offline)

**Do not replace cloud STT as default.** Optional path:

1. **Primary optional:** **faster-whisper** `tiny.en` or `base.en` (int8 CPU) — mature Python, good enough for offline prompts; or **distil-small.en** if quality > speed.
2. **Alt edge:** **Moonshine** tiny/streaming when packaging/ONNX path is clean — better short-audio latency curve than Whisper.
3. **CLI-only ops:** **whisper.cpp** for subprocess users / non-Python hosts.

Keep cloud for Mode A dogfood quality and radio partials.

### What not to do now

| Option | Verdict |
|--------|---------|
| Swap default wake to **Whisper tiny** | **No** — similar name mangling, higher variance, worse always-on story. |
| **Big Vosk only** as the long-term fix | **Defer as sole strategy** — easy `model_path` experiment, but still open-vocab ASR + multi‑GB cost. OK as **optional** quality knob. |
| **Porcupine** as default | **No** — licensing / access-key product risk; conflicts with open preference. |
| **openWakeWord** as default without training investment | **Defer** — custom train is the real work; NC pretrained models awkward. |
| Drop cloud STT | **Out of scope** (ADR-004 / product). |

### Interim posture

**Keep Vosk small + cloud STT** as the **shipping default** until Sherpa KWS is implemented and dogfooded. Alias/learning remains valuable for Vosk and as a safety net. Document that larger Vosk models are a **config-only** experiment for operators with disk/RAM.

---

## 6. Integration sketch (for follow-up implement)

```text
[ambient]
engine = "vosk"            # default until dogfood
# engine = "sherpa_kws"    # follow-up
# model_path = "..."       # vosk dir or sherpa KWS dir
snippet_s = 2.5
```

```python
# New backend (sketch) — same Protocol as VoskWakeBackend
class SherpaKwsWakeBackend:
    name = "sherpa_kws"
    def score_snippet(self, pcm16_le: bytes, sample_rate: int = 16000) -> WakeHit | None:
        # energy floor → stream.accept_waveform → KeywordSpotter.decode
        # map keyword id → WakeHit(phrase=..., backend=self.name)
        ...
```

On policy reload: rebuild `keywords_file` from `WakePolicy` (names × prefixes + exact phrases), BPE-encode with model `bpe.model` / `tokens.txt`.

Tests: extend `tests/test_vosk_wake_fixtures.py` pattern with optional `@pytest.mark.sherpa_kws`; keep text-path fixture parity tests engine-agnostic.

---

## 7. Suggested follow-up bugs

Filed from this survey (IDs filled when `bl bug` runs; see B069 todo body if this table is stale):

| ID | Slice | Intent | Est. |
|----|-------|--------|------|
| **B070** | Sherpa-ONNX KWS wake backend | Optional `engine=sherpa_kws`, model download script, keyword rebuild on config, fixture tests, doctor readiness | M (~5 h) |
| **B071** | Wake eval harness expansion | More WAVs / speakers / noise; score hit-rate & false-accept for Vosk vs KWS | M (~3 h) |
| **B072** | Optional local STT provider | `faster-whisper` (or Moonshine) behind existing STT provider interface for offline/privacy; default remains cloud | M (~4 h) |
| **B073** | Docs: larger Vosk model_path | Operator note + optional download helper for 0.22-lgraph / 0.22 | S (~1 h) |

---

## 8. Sources

### Measured (this worktree, 2026-07-14)

- Vosk `vosk-model-small-en-us-0.15` via `vosk` 0.3.45 on `fixtures/voice/wake/live/*.wav`
- `faster-whisper` 1.2.1, models `tiny.en` / `base.en`, `device=cpu`, `compute_type=int8`, `beam_size=1`
- `sherpa-onnx` 1.13.4, model `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` int8, custom keywords for HEY_HARK / HEY_HERALD / …

### Cited

- [Vosk models table](https://alphacephei.com/vosk/models) — sizes, WER, Apache-2.0 for en-us small/0.22
- [sherpa-onnx KWS docs](https://k2-fsa.github.io/sherpa/onnx/kws/index.html) — open-vocab KWS, GigaSpeech English 3.3 M
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — CTranslate2 CPU int8 benchmarks
- Moonshine / Useful Sensors materials — tiny ~27 M params, MIT English weights, short-audio latency vs Whisper
- openWakeWord / Porcupine community notes — train-your-own vs proprietary KWS licensing

---

## 9. Acceptance checklist (B069)

- [x] Survey against Hark constraints (wake snippets, CPU, fail-open, cloud kept)
- [x] Concrete numbers: local probes for Vosk / faster-whisper / Sherpa KWS + cited sizes/WER
- [x] Written recommendation (primary KWS + optional local STT; keep Vosk default interim)
- [x] Follow-up implement bugs filed; I004 + CHANGELOG cross-links

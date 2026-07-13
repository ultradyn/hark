# STT / TTS providers

## Policy

| Rule | Detail |
|------|--------|
| **Cloud STT/TTS default** | Full dictation stays cloud-first (ADR-004). Local neural STT is **optional** (B072), never required. |
| **No required local neural STT/TTS** | Do not require Whisper.cpp, Piper, etc. for a working install. |
| **Max reuse of operator accounts** | Pluggable providers: xAI, OpenAI, Anthropic, Google, MiniMax |
| **Local allowed** | Mic capture, RMS gate, classic WebRTC VAD, playback, espeak-ng emergency TTS; optional post-wake local STT (`faster_whisper`) |
| **Avoid as primary** | Playwright website “Dictate” (optional last-resort provider only) |
| **Not for ambient wake** | Do **not** use Whisper / full STT for continuous ambient wake — that path is Vosk / Sherpa KWS (B069–B070). |

## Capability matrix (honest)

| Provider | STT | TTS | Streaming STT | Auth for this project | Notes |
|----------|-----|-----|---------------|----------------------|--------|
| **xAI Grok** | Yes — REST + WS | Yes — REST + WS | **Yes** (Smart Turn) | **Grok Build OAuth** (`~/.grok/auth.json`) preferred; `XAI_API_KEY` fallback | **Default primary** |
| **OpenAI** | Yes — transcriptions + Realtime Whisper | Yes | Yes | `OPENAI_API_KEY` / ChatGPT API key | Strong fallback |
| **Anthropic** | **No public STT API** as of plan time | Product voice / TTS not a general TTS API for this | N/A | Claude Code Max is UI voice (`/voice`) | **Orchestrator**, not STT engine. Provider stub: status `unsupported` with message; optional experimental browser/product bridge later |
| **Google (Gemini / Antigravity)** | **Yes** — Gemini audio understanding (file → transcript); Cloud STT if GCP | Yes — Gemini TTS / Cloud TTS | Partial (Live API / Cloud streaming) | `GOOGLE_API_KEY` / `GEMINI_API_KEY` / ADC | Good batch path after VAD segment |
| **MiniMax** | **ASR not clearly public** on main API docs | **Yes** — T2A (`/v1/t2a_v2`) | TTS streaming yes | `MINIMAX_API_KEY` | Use for **TTS**; STT = probe + stub until official ASR endpoint confirmed |

**Principle:** implement every *documented* speech API we can; for missing STT (Anthropic, MiniMax ASR), ship a provider module that `hark doctor` reports as **unavailable** with a clear reason, not a silent fallback.

---

## Default resolution order

### STT (`stt.provider = "auto"`)

1. **xAI** if Grok OAuth token or `XAI_API_KEY` works  
2. **OpenAI** if key present  
3. **Google/Gemini** if key present  
4. Else error listing how to configure  

### TTS (`tts.provider = "auto"`)

1. **xAI** (same auth as STT)  
2. **OpenAI**  
3. **MiniMax** (strong dedicated TTS)  
4. **Google** Gemini/Cloud TTS  
5. Else espeak-ng emergency only if `tts.allow_espeak_fallback = true`

Operator may pin: `provider = "xai" | "openai" | "google" | "minimax" | "anthropic"`.

Optional local (explicit only — never via `auto`):

`provider = "faster_whisper" | "local" | "moonshine"` — see [Optional local full-STT](#optional-local-full-stt-b072) below.

---

## Optional local full-STT (B072)

For **offline** or **privacy-local** utterance transcription (answer windows / post-wake prompt body). **Cloud remains the product default.** Install is opt-in:

```bash
pip install 'hark[local-stt]'   # faster-whisper
# Moonshine stretch (separate package; packaging less stable):
#   pip install useful-moonshine
```

### Config / env

```toml
[stt]
provider = "faster_whisper"   # or "local" / "whisper" aliases; "moonshine" stretch
local_model = "tiny.en"       # or base.en (quality vs speed)
local_device = "cpu"           # GPU optional, never required
local_compute_type = "int8"
# local_model_path = "/path/to/ct2-model"  # optional on-disk override
local_fail_open = true        # if local missing → cloud auto (recommended)
local_download = true         # allow Hugging Face download on first use
```

| Env | Maps to |
|-----|---------|
| `HARK_STT_PROVIDER` | `stt.provider` |
| `HARK_STT_LOCAL_MODEL` | `stt.local_model` |
| `HARK_STT_LOCAL_DEVICE` | `stt.local_device` |
| `HARK_STT_LOCAL_COMPUTE_TYPE` | `stt.local_compute_type` |
| `HARK_STT_LOCAL_MODEL_PATH` | `stt.local_model_path` |
| `HARK_STT_LOCAL_FAIL_OPEN` | `stt.local_fail_open` (`0`/`1`) |
| `HARK_STT_LOCAL_DOWNLOAD` | `stt.local_download` |

When `local_fail_open = true` (default) and the local engine or model cannot load, resolution falls back to cloud `auto` with a warning log.

### RTF expectations (from B069 survey)

Measured on a mid laptop CPU (Ryzen 7 class, no discrete NVIDIA), int8, short ~2.5 s clips — see `docs/plans/B069-local-stt-survey.md`:

| Engine | RTF / latency notes |
|--------|---------------------|
| **faster-whisper `tiny.en` int8 CPU** | **RTF ≈ 0.10–0.14** typical (~250–350 ms decode); cold load ~5.5 s; one outlier ~RTF 0.47 |
| **faster-whisper `base.en` int8 CPU** | **RTF ≈ 0.19–0.23** (~460–570 ms); cold load ~13.5 s |
| **Moonshine tiny** (cited) | Edge-focused; short-clip latency often tens–hundreds of ms (better short-audio curve than Whisper’s 30 s pad) |

Target feel for local post-wake: **≲ 1–1.5 s** after speech end on mid hardware when the model is warm.

### What local STT is *not*

- **Not** the ambient wake scanner — open-vocab Whisper still mangles product names (`hark` → hawk/hook); continuous snippet decode is the wrong problem class. Use Vosk / Sherpa KWS for wake.
- **Not** selected by `provider = "auto"`.

`hark doctor` and `hark providers` report local engine import readiness (soft; missing extra is not a hard fail).

---

## xAI (primary)

### Auth — Grok Build OAuth (preferred)

Grok Build stores session credentials in:

```text
~/.grok/auth.json
```

Interactive login: `grok login` (OAuth at `auth.x.ai`). Tokens refresh automatically.

**handsfree must:**

1. Prefer reading a **usable access token** from `~/.grok/auth.json` (same precedence Grok CLI uses: session token over API key).  
2. Fall back to `XAI_API_KEY` / `providers.xai.api_key`.  
3. On 401, print: run `grok login` or set `XAI_API_KEY`.  
4. **Never log the token.**

Exact JSON fields can drift; implement by matching Grok’s documented behavior (session token takes precedence over API key; see Grok auth user guide). Optional: call a small helper if Grok ever exposes `grok auth print-token` — do not shell out to scrape TUI.

### STT

- REST: `POST https://api.x.ai/v1/stt` (multipart file)  
- Streaming: `wss://api.x.ai/v1/stt?sample_rate=16000&encoding=pcm&interim_results=true&smart_turn=0.7&smart_turn_timeout=3000`  
- Keyterms for agent vocabulary  

### TTS

- `POST https://api.x.ai/v1/tts` — voices e.g. `eve`, `ara`, …  
- Streaming WS for long text optional  

Docs: https://docs.x.ai/developers/model-capabilities/audio/speech-to-text  
https://docs.x.ai/developers/model-capabilities/audio/text-to-speech  

---

## OpenAI

| Need | Path |
|------|------|
| File STT | `POST /v1/audio/transcriptions` |
| Streaming STT | Realtime API / gpt-realtime-whisper |
| TTS | `POST /v1/audio/speech` |

Auth: `OPENAI_API_KEY`.

---

## Anthropic

| Need | Reality (plan-time) |
|------|---------------------|
| Public STT API | **Not available** for third-party apps the way xAI/OpenAI are |
| Claude Code `/voice` | Product UI; tokens free in product; **not** a stable external STT endpoint for `hark listen` |
| Role in system | **Mode A orchestrator** (Claude Code Max) calling `hark` tools |

**Implementation:**

```text
providers/anthropic_stt.py  → raises ProviderUnsupported("anthropic: no public STT API; use xai|openai|google")
providers/anthropic_tts.py  → same unless a public TTS API appears
```

`hark doctor` shows:

```text
anthropic stt: unsupported (use for orchestration only)
```

Optional Phase 4: experimental `provider = "claude-code-voice"` via product automation — **out of scope v1**.

---

## Google (Gemini / Antigravity stack)

Operator uses Antigravity a little → treat **Gemini API** as the practical path.

### STT (batch — v1 target)

1. Local VAD segment → WAV/MP3  
2. Upload or inline audio to Gemini  
3. Prompt: `Transcribe this audio verbatim. Output only the transcript.`  
4. Return text  

Refs: Gemini audio understanding / `generateContent` with audio; Files API upload.

### STT (streaming)

- Prefer Cloud Speech-to-Text streaming **or** Gemini Live transcription if operator has it  
- v1 may ship **batch-only** for Google and still list provider as available  

### TTS

- Gemini TTS speech generation and/or Cloud Text-to-Speech  

Auth: `GEMINI_API_KEY` or `GOOGLE_API_KEY` (and document ADC for Cloud STT if used).

---

## MiniMax

### TTS (v1 — implement)

- `POST https://api.minimax.io/v1/t2a_v2` (region variants: `minimaxi.chat`, `api-uw.minimax.io`)  
- Models such as `speech-2.8-hd` / current Speech lineup — pin via config  
- Auth: `MINIMAX_API_KEY` + any required GroupId header if their API still needs it (verify against live docs at implement time)

### STT

Public docs emphasize **T2A (text→audio)**, not ASR. Community notes suggest ASR may exist but is not a stable documented endpoint.

**v1:** `minimax` STT provider = `unsupported` until endpoint confirmed; `hark doctor` says so.  
**When confirmed:** add `minimax_stt` without changing CLI.

---

## Optional escape hatch: browser dictation

`provider = "browser-chatgpt"` / `browser-claude` — Playwright dictate. **Not default.** High overhead; only if APIs fail and operator opts in.

---

## Local audio I/O (not models)

| Piece | Notes |
|-------|-------|
| Capture | 16 kHz mono PCM16; PortAudio / sounddevice / cpal |
| Playback | `paplay` / `ffplay` / Pulse |
| Gate | RMS open/close + hangover |
| VAD | Optional classic `webrtcvad` |
| Devices | `hark devices` |

---

## Credentials summary

| Source | Use |
|--------|-----|
| `~/.grok/auth.json` | xAI OAuth (preferred) |
| `XAI_API_KEY` | xAI fallback |
| `OPENAI_API_KEY` | OpenAI |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Google |
| `MINIMAX_API_KEY` | MiniMax TTS |
| Anthropic keys | Not required for voice I/O; used by orchestrator host |

---

## Cost control

- Open STT stream only while listening (after gate or on `hark listen`)  
- Truncate TTS to `tts.max_chars`  
- Prefer xAI Smart Turn over long idle WS  
- Verbose mode may log estimated billable seconds, never audio by default  

# STT / TTS providers

## Policy

| Rule | Detail |
|------|--------|
| **Cloud STT/TTS default** | Full dictation stays cloud-first (ADR-004). Local neural STT is **optional** (B072), never required. |
| **No required local neural STT/TTS** | Do not require Whisper.cpp, Piper, etc. for a working install. |
| **Max reuse of operator accounts** | Pluggable providers: xAI, OpenAI, Anthropic, Google, MiniMax |
| **Local allowed** | Mic capture, RMS gate, playback; optional post-wake local STT (`faster_whisper`). Config has `tts.allow_espeak_fallback` but **auto TTS does not use it yet** (unimplemented escape hatch). |
| **Avoid as primary** | Browser Playwright “Dictate” providers are **not implemented** (out of scope today). |
| **Not for ambient wake** | Do **not** use Whisper / full STT for continuous ambient wake — that path is Vosk / Sherpa KWS (B069–B070). |

## Capability matrix (honest)

Shipped I/O is **batch REST** (`httpx` POST). Upstream Realtime/WS/Cloud streaming APIs are **not wired** in hark. **Smart Turn** is local listen endpointing (`listen.endpoint_strategy`), not an xAI streaming STT feature.

| Provider | STT | TTS | Streaming STT (hark) | Auth for this project | Notes |
|----------|-----|-----|----------------------|----------------------|--------|
| **xAI Grok** | Yes — batch REST | Yes — batch REST | **No** (upstream WS exists; unused) | **Grok Build OAuth** (`~/.grok/auth.json`) preferred; `XAI_API_KEY` fallback | **Default primary** |
| **OpenAI** | Yes — `gpt-4o-mini-transcribe` (default; Codex ChatGPT OAuth) + `whisper-1` fallback for API keys when `OPENAI_STT_MODEL` unset | Yes — batch REST | **No** (Realtime API not implemented) | `OPENAI_API_KEY`; also Codex / OpenCode / Pi CLI stores | Strong fallback; Codex OAuth lacks `whisper-1` / TTS scopes today |
| **Custom STT** | Yes — OpenAI-compatible batch REST (configurable base URL) | **No** (this variant is STT-only) | **No** | Bearer via env / key file / key command | Explicit pin only (`provider = "custom"`); never via `auto` |
| **Custom TTS** | **No** (this variant is TTS-only) | Yes — OpenAI-compatible batch REST (configurable base URL) | **No** | Bearer via env / key file / key command | Explicit pin only (`provider = "custom"`); never via `auto` |
| **Anthropic** | **No public STT API** as of plan time | Product voice / TTS not a general TTS API for this | N/A | Claude Code Max is UI voice (`/voice`) | **Orchestrator**, not STT engine. Provider stub: status `unsupported` with message |
| **Google (Gemini / Antigravity)** | **Yes** — Gemini audio understanding (file → transcript); Cloud STT **not implemented** | Yes — Gemini TTS; Cloud TTS **not implemented** | **No** (Live/Cloud streaming unused) | `GOOGLE_API_KEY` / `GEMINI_API_KEY`; also **agy** OAuth + OpenCode / Pi | Good batch path after RMS/energy (or Smart Turn) segment |
| **MiniMax** | **ASR not clearly public** on main API docs | **Yes** — T2A (`/v1/t2a_v2`), batch (`stream: false`) | N/A (TTS not streamed in hark) | `MINIMAX_API_KEY`; also `mmx` CLI / Pi / OpenCode / legacy `~/.minimax` | Use for **TTS**; STT = `unsupported` until official ASR endpoint confirmed |

**Principle:** implement every *documented* speech API we can; for missing STT (Anthropic, MiniMax ASR), ship a provider module that `hark doctor` reports with a clear reason. Doctor checks **credential discovery** (secret / CLI store presence), not live STT/TTS capability — e.g. MiniMax ✓ means keys found, not that STT works. `speech_ok` today is an **xAI-credential** gate, not “any fallback key ready”.

---

## Default resolution order

Disabled providers are skipped even when credentials exist:

```toml
[stt]
disabled = ["google"]          # never use for STT (auto or pin)

[tts]
disabled = []                  # e.g. ["minimax"]
minimax_ok = false             # must be true before MiniMax TTS runs
```

Env: `HARK_STT_DISABLED`, `HARK_TTS_DISABLED` (comma-separated), `HARK_TTS_MINIMAX_OK=1`.

MiniMax TTS: on first interactive use when MiniMax would be selected, Hark asks for consent and persists `tts.minimax_ok = true`. Non-interactive runs fail with a clear hint until the flag is set. `hark doctor` / setup **display** `minimax_ok` but do not set it — agents pinning MiniMax must set the flag or `HARK_TTS_MINIMAX_OK=1` before non-interactive `hark tts` (B170).

**Auto means credential presence at resolve time**, not runtime viability. Selection walks the order below and picks the first provider whose `*_auth().available` is true. Auth failures / HTTP 401–403 raise `ProviderError` and **do not** fall through to the next provider today (open B164). The only intra-provider fallback is OpenAI STT model (`gpt-4o-mini-transcribe` → `whisper-1` when `OPENAI_STT_MODEL` is unset).

### STT (`stt.provider = "auto"`)

1. **xAI** if Grok OAuth token or `XAI_API_KEY` is present  
2. **OpenAI** if key/token present (default model `gpt-4o-mini-transcribe`; falls back to `whisper-1` for API keys when `OPENAI_STT_MODEL` unset — Codex ChatGPT OAuth supports GPT-4o transcribe but not `whisper-1`)  
3. **Google/Gemini** if key or agy/OpenCode/Pi token present  
4. Else error listing how to configure (`grok login` / `agy` / `XAI_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`)  

### TTS (`tts.provider = "auto"`)

1. **xAI** (same auth as STT)  
2. **OpenAI**  
3. **MiniMax** (only when `tts.minimax_ok` is true, or after interactive consent)  
4. **Google** Gemini TTS (when credentials present)  
5. Else error — hint lists `grok login` / `XAI_API_KEY` / `OPENAI_API_KEY` / `MINIMAX_API_KEY` (Google/agy is still in the order above but **omitted from that hint string** today)

Operator may pin: `provider = "xai" | "openai" | "google" | "minimax" | "anthropic" | "custom"` (`HARK_TTS_PROVIDER` env override; `custom` maps to **Custom TTS** below).

Optional local (explicit only — never via `auto`):

`provider = "faster_whisper" | "local" | "moonshine"` — see [Optional local full-STT](#optional-local-full-stt-b072) below.

Optional Custom STT gateway (explicit only — never via `auto`):

`provider = "custom"` — see [Custom STT](#custom-stt-openai-compatible-gateway) below.

Optional Custom TTS gateway (explicit only — never via `auto`):

`provider = "custom"` (in `[tts]`) — see [Custom TTS](#custom-tts-openai-compatible-gateway) below.

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

`hark doctor` and `hark providers` report local engine **import readiness** (soft; missing extra is not a hard fail). Cloud provider rows remain credential-discovery only.

---

## Custom STT (OpenAI-compatible gateway)

Pin an HTTP speech-to-text endpoint that speaks the **OpenAI audio transcriptions** wire format (batch REST only). This is for operators who front STT behind their own reverse proxy or internal gateway. **Not selected by `auto`.**

### Client API Hark adheres to

Hark is a client. The gateway must accept:

| | |
|---|---|
| Method / path | `POST {base_url}{custom_path}` |
| Default path | `/audio/transcriptions` (OpenAI surface) |
| Alternate path | `/stt` (native dual-mount style; same multipart body) |
| Auth | `Authorization: Bearer <token>` |
| Content-Type | `multipart/form-data` |
| Form fields | **`file`** — WAV bytes, filename `audio.wav`, content-type `audio/wav`; **`model`** — string (required on `/audio/transcriptions`; optional on `/stt` when the gateway injects a default); optional **`language`** (BCP-47 / ISO code when the gateway supports it) |
| Success body | JSON with a transcript string in `text` (preferred), or `transcript`, or `result.text`. Word arrays (`words[]`) are accepted as a fallback join. Plain-text bodies are accepted if JSON parse fails. |
| Errors | Non-2xx → operator-visible `ProviderError` with HTTP status + short body excerpt. **Audio bytes and bearer tokens are never logged.** |
| Non-goals | Realtime / WebSocket STT; streaming multipart; OpenAI `response_format` variants beyond default JSON/text; TTS |

`base_url` is the OpenAI-style API root **including** the version segment, e.g. `https://gateway.example.com/v1`. Hark joins `{base_url}{custom_path}` with no extra `/v1` insertion.

Example request shape (equivalent to what Hark sends):

```bash
curl -sS -X POST "$BASE/audio/transcriptions" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@answer.wav;type=audio/wav" \
  -F "model=gpt-4o-mini-transcribe" \
  -F "language=en"
# → {"text":"…"}
```

Native dual-mount style (same auth + multipart; model may be optional server-side):

```bash
curl -sS -X POST "$BASE/stt" \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@answer.wav;type=audio/wav" \
  -F "model=grok-stt"
```

### Gateway-side prerequisites

Hark is only the client. Gateways typically gate audio **per upstream provider
and per model id**, so a working chat key does not imply working speech:

| Response | Meaning | Fix |
|----------|---------|-----|
| `501 not_supported` | route reached; that upstream has the audio capability disabled | operator enables STT/TTS on the provider |
| `404` provider/model not found | `custom_model` matched no configured route (or the id is not granted) | use a model id the gateway documents |
| `401` / `403` | key rejected — Hark does **not** fall through to another provider (B164) | check the key source |

Model-routed gateways may also expose synthetic ids for their dual-mount paths
(e.g. an STT default injected when `model` is omitted on `/stt`).

### Config / env

```toml
[stt]
provider = "custom"
custom_base_url = "https://gateway.example.com/v1"
custom_model = "gpt-4o-mini-transcribe"   # or a gateway-documented model id
# custom_path = "/audio/transcriptions"  # default; set "/stt" for native dual-mount
# Key sources (first hit wins) — keep secrets out of config when possible:
# custom_api_key = "…"                   # or HARK_STT_CUSTOM_API_KEY
# custom_api_key_file = "~/.llmp"        # or HARK_STT_CUSTOM_API_KEY_FILE
# custom_api_key_command = "cat ~/.llmp" # or HARK_STT_CUSTOM_API_KEY_COMMAND
```

| Env | Maps to |
|-----|---------|
| `HARK_STT_PROVIDER=custom` | pin Custom STT |
| `HARK_STT_CUSTOM_BASE_URL` | `stt.custom_base_url` |
| `HARK_STT_CUSTOM_API_KEY` | bearer token (highest priority) |
| `HARK_STT_CUSTOM_API_KEY_FILE` | `stt.custom_api_key_file` — path to a file holding the token (whole file, stripped) |
| `HARK_STT_CUSTOM_API_KEY_COMMAND` | `stt.custom_api_key_command` — shell command; stdout first non-empty line is the token |
| `HARK_STT_CUSTOM_MODEL` | `stt.custom_model` |
| `HARK_STT_CUSTOM_PATH` | `stt.custom_path` (`/audio/transcriptions` or `/stt`) |

**Key precedence:** literal env/config key → key file → key command.

`hark doctor` / `hark providers` report **config readiness** (base URL + key source + model-when-required), not a live probe. `config_to_dict` / JSON dumps expose `custom_api_key_configured` (true if any key source is set) plus the file path / command string — never the secret itself.

---

## Custom TTS (OpenAI-compatible gateway)

Pin an HTTP text-to-speech endpoint that speaks the **OpenAI audio speech** wire format (batch REST only). This is for operators who front TTS behind their own reverse proxy or internal gateway. **Not selected by `auto`.**

### Client API Hark adheres to

Hark is a client. The gateway must accept:

| | |
|---|---|
| Method / path | `POST {base_url}{custom_path}` |
| Default path | `/audio/speech` (OpenAI surface) |
| Alternate path | `/tts` (native dual-mount style; same JSON body — `model` optional when the gateway injects a default) |
| Auth | `Authorization: Bearer ***` |
| Content-Type | `application/json` |
| JSON body | **`input`** — text to synthesize (string); **`voice`** — voice id string (always sent; default `alloy`); optional **`model`** (required on `/audio/speech`; omitted on `/tts` when unset) |
| Success | **Raw audio bytes** (not JSON); Hark uses the response `content-type` header (e.g. `audio/mpeg`) |
| Errors | Non-2xx → operator-visible `ProviderError` with HTTP status + short body excerpt. **Bearer tokens are never logged.** |
| Non-goals | Streaming/chunked TTS; WebSocket sessions; OpenAI `instructions` / `response_format` variants beyond default; STT |

`base_url` is the OpenAI-style API root **including** the version segment, e.g. `https://gateway.example.com/v1`. Hark joins `{base_url}{custom_path}` with no extra `/v1` insertion.

Voice resolution order: explicit `--voice` / `tts.voice` → `custom_voice` → `alloy` (OpenAI parity).

**Voice ids belong to the gateway, not to OpenAI.** The `alloy` default is kept
for OpenAI parity, and a gateway fronting a different upstream will reject it
(e.g. `404 Voice alloy not found`). Set `custom_voice` to an id that gateway
documents whenever the upstream is not OpenAI.

Example request shape (equivalent to what Hark sends):

```bash
curl -sS -X POST "$BASE/audio/speech" \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini-tts","input":"hello world","voice":"alloy"}'
# → raw audio bytes (audio/mpeg)
```

Native dual-mount style (same auth + JSON; model may be optional server-side):

```bash
curl -sS -X POST "$BASE/tts" \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{"input":"hello world","voice":"eve"}'
```

### Gateway-side prerequisites

Hark is only the client. Gateways typically gate audio **per upstream provider
and per model id**, so a working chat key does not imply working speech:

| Response | Meaning | Fix |
|----------|---------|-----|
| `501 not_supported` | route reached; that upstream has the audio capability disabled | operator enables STT/TTS on the provider |
| `404` provider/model not found | `custom_model` matched no configured route (or the id is not granted) | use a model id the gateway documents |
| `401` / `403` | key rejected — Hark does **not** fall through to another provider (B164) | check the key source |

Model-routed gateways may also expose synthetic ids for their dual-mount paths
(e.g. an STT default injected when `model` is omitted on `/stt`).

### Config / env

```toml
[tts]
provider = "custom"
custom_base_url = "https://gateway.example.com/v1"
custom_model = "gpt-4o-mini-tts"   # or a gateway-documented model id
custom_voice = "alloy"             # default voice when none given
# custom_path = "/audio/speech"    # default; set "/tts" for native dual-mount
# Key sources (first hit wins) — keep secrets out of config when possible:
# custom_api_key = "…"             # or HARK_TTS_CUSTOM_API_KEY
# custom_api_key_file = "~/.llmp"  # or HARK_TTS_CUSTOM_API_KEY_FILE
# custom_api_key_command = "cat ~/.llmp" # or HARK_TTS_CUSTOM_API_KEY_COMMAND
```

| Env | Maps to |
|-----|---------|
| `HARK_TTS_PROVIDER=custom` | pin Custom TTS |
| `HARK_TTS_CUSTOM_BASE_URL` | `tts.custom_base_url` |
| `HARK_TTS_CUSTOM_API_KEY` | bearer token (highest priority) |
| `HARK_TTS_CUSTOM_API_KEY_FILE` | `tts.custom_api_key_file` — path to a file holding the token (whole file, stripped) |
| `HARK_TTS_CUSTOM_API_KEY_COMMAND` | `tts.custom_api_key_command` — shell command; stdout first non-empty line is the token |
| `HARK_TTS_CUSTOM_MODEL` | `tts.custom_model` |
| `HARK_TTS_CUSTOM_VOICE` | `tts.custom_voice` |
| `HARK_TTS_CUSTOM_PATH` | `tts.custom_path` (`/audio/speech` or `/tts`) |

**Key precedence:** literal env/config key → key file → key command.

`hark doctor` / `hark providers` report **config readiness** (base URL + key source + model-when-required), not a live probe. `config_to_dict` / JSON dumps expose `custom_api_key_configured` (true if any key source is set) plus the file path / command string — never the secret itself.

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
2. Fall back to `XAI_API_KEY` (env only — no config key fallback).  
3. On 401, print: run `grok login` or set `XAI_API_KEY`.  
4. **Never log the token.**

Exact JSON fields can drift; implement by matching Grok’s documented behavior (session token takes precedence over API key; see Grok auth user guide). Optional: call a small helper if Grok ever exposes `grok auth print-token` — do not shell out to scrape TUI.

### STT

- **Shipped:** REST `POST https://api.x.ai/v1/stt` (multipart file)  
- Upstream also documents a streaming WS endpoint; **hark does not use it**. Listen endpointing (energy / optional Smart Turn) is local, before the batch upload.  
- Keyterms for agent vocabulary  

### TTS

- **Shipped:** `POST https://api.x.ai/v1/tts` — voices e.g. `eve`, `ara`, …  
- Streaming WS for long text is upstream-only; unused in hark  

Docs: https://docs.x.ai/developers/model-capabilities/audio/speech-to-text  
https://docs.x.ai/developers/model-capabilities/audio/text-to-speech  

---

## OpenAI

| Need | Path |
|------|------|
| File STT | `POST /v1/audio/transcriptions` (shipped) |
| Streaming STT | Realtime API / gpt-realtime-whisper — **not implemented** |
| TTS | `POST /v1/audio/speech` (shipped) |

Default STT model is `gpt-4o-mini-transcribe`. Set `OPENAI_STT_MODEL` to pin a model; when set, the automatic `whisper-1` fallback on 400/401/403/404 is **disabled**.

Auth discovery (env first, then CLI stores — fail-open):

1. `OPENAI_API_KEY`
2. Codex CLI `~/.codex/auth.json` (`OPENAI_API_KEY` field, else `tokens.access_token`; honors `CODEX_HOME`)
3. OpenCode `$XDG_DATA_HOME/opencode/auth.json` (default `~/.local/share/opencode/auth.json`) — `openai` entry
4. Pi agent `~/.pi/agent/auth.json` — `openai` / `openai-codex` entries

Never log the token. ChatGPT OAuth access tokens may not work for all `api.openai.com` audio routes; prefer API keys when available.

---

## Anthropic

| Need | Reality (plan-time) |
|------|---------------------|
| Public STT API | **Not available** for third-party apps the way xAI/OpenAI are |
| Claude Code `/voice` | Product UI; tokens free in product; **not** a stable external STT endpoint for `hark listen` |
| Role in system | **Handsfree orchestrator** (Claude Code Max) calling `hark` tools |

**Implementation:** handled inline in `providers/resolve.py` — selecting
`provider = "anthropic"` raises `ProviderUnsupported`:

```text
STT: anthropic: no public STT API; use xai|openai|google
TTS: anthropic: no public TTS API for hark; use xai|openai|minimax|google
```

`hark doctor` shows (no `ANTHROPIC_API_KEY`):

```text
· anthropic: unsupported STT/TTS (use as orchestrator only)
```

When `ANTHROPIC_API_KEY` is set:

```text
· anthropic: key set but public STT/TTS unsupported for hark
```

Optional Phase 4: experimental `provider = "claude-code-voice"` via product automation — **out of scope v1**.

---

## Google (Gemini / Antigravity stack)

Operator uses Antigravity a little → treat **Gemini API** as the practical path.

### STT (batch — shipped)

1. Local RMS/energy (or Smart Turn) segment → WAV/MP3  
2. Upload or inline audio to Gemini  
3. Prompt: `Transcribe this audio verbatim. Output only the transcript.`  
4. Return text  

Refs: Gemini audio understanding / `generateContent` with audio; Files API upload.

### STT (streaming)

- Upstream: Cloud Speech-to-Text streaming or Gemini Live — **not implemented** in hark  
- Shipped path is **batch-only**

### TTS

- **Shipped:** Gemini TTS speech generation  
- Cloud Text-to-Speech — **not implemented**

Auth discovery (env first, then CLI stores — fail-open):

1. `GEMINI_API_KEY` or `GOOGLE_API_KEY`
2. Antigravity (`agy`) OAuth `~/.gemini/oauth_creds.json` (`access_token` / `api_key`)
3. OpenCode `$XDG_DATA_HOME/opencode/auth.json` — `google` / `gemini` keys
4. Pi agent `~/.pi/agent/auth.json` — `google` / `gemini` keys

---

## MiniMax

### TTS (v1 — implement)

- `POST https://api.minimax.io/v1/t2a_v2` (fallback region: `api-uw.minimax.io`)  
- Model default `speech-2.6-hd` — pin via `MINIMAX_TTS_MODEL` env  
- Request body sets `"stream": false` (batch only)  
- Auth discovery (env first, then CLI stores — fail-open):
  1. `MINIMAX_API_KEY` (+ optional `MINIMAX_GROUP_ID` header when required)
  2. MiniMax CLI **`mmx`**: `~/.mmx/config.json` (`api_key` or `oauth.access_token`; honors `MMX_CONFIG_DIR`)
  3. Pi agent `~/.pi/agent/auth.json` — `minimax` key
  4. OpenCode auth — `minimax*` / `minimax-coding-plan` keys
  5. Legacy `~/.minimax` (raw key file or dir with `config.json` / `api_key`)
- Never log the token. Interactive login: `mmx auth login`.
- **Consent:** MiniMax is not used until `tts.minimax_ok = true` (or interactive yes / `HARK_TTS_MINIMAX_OK=1`). Use `[tts] disabled = ["minimax"]` to ban it entirely. Doctor reports the flag; it does not grant consent.

### STT

Public docs emphasize **T2A (text→audio)**, not ASR. Community notes suggest ASR may exist but is not a stable documented endpoint.

**v1:** `minimax` STT provider = `unsupported` until endpoint confirmed; doctor shows credentials when found, but STT calls still raise `ProviderUnsupported`.  
**When confirmed:** add `minimax_stt` without changing CLI.

---

## Optional escape hatch: browser dictation

`provider = "browser-chatgpt"` / `browser-claude` — Playwright dictate. **Not implemented** (no resolve names or modules). Prior-art / stretch only; not selectable today.

---

## Local audio I/O (not models)

| Piece | Notes |
|------|--------|
| Capture | 16 kHz mono PCM16; PortAudio / sounddevice / cpal |
| Playback | `paplay` / `ffplay` / Pulse |
| Gate | RMS open/close + hangover |
| Endpointing | Energy (default) or optional Smart Turn (`listen.endpoint_strategy`); no `webrtcvad` dependency |
| Devices | `hark devices` |

---

## Credentials summary

| Source | Use |
|--------|-----|
| `~/.grok/auth.json` | xAI OAuth (preferred) |
| `XAI_API_KEY` | xAI fallback |
| `OPENAI_API_KEY` | OpenAI (preferred explicit) |
| `~/.codex/auth.json` | OpenAI via Codex CLI |
| `~/.local/share/opencode/auth.json` | OpenAI / MiniMax / **Google** via OpenCode |
| `~/.pi/agent/auth.json` | OpenAI / MiniMax / **Google** via Pi agent |
| `MINIMAX_API_KEY` | MiniMax TTS (preferred explicit) |
| `~/.mmx/config.json` | MiniMax via **`mmx`** CLI |
| `~/.minimax` | Legacy MiniMax key file/dir |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `~/.gemini/oauth_creds.json` | Google (explicit key or agy OAuth); also OpenCode/Pi above |
| `HARK_STT_CUSTOM_API_KEY` / `_FILE` / `_COMMAND` (+ `HARK_STT_CUSTOM_BASE_URL`) | Custom STT gateway bearer (explicit `provider = "custom"`) |
| Anthropic keys | Not required for voice I/O; used by orchestrator host |

---

## Cost control

- Open STT only while listening (after gate or on `hark listen`) — batch upload after segment, not a long-lived provider WS  
- Truncate TTS to `tts.max_chars`  
- Prefer tight local endpointing (energy / Smart Turn) over leaving the mic open  
- Verbose mode may log estimated billable seconds, never audio by default  

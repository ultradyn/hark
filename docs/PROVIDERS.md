# STT / TTS providers

## Policy

| Rule | Detail |
|------|--------|
| **No local neural STT/TTS** | Do not require Whisper.cpp, Piper, etc. |
| **Max reuse of operator accounts** | Pluggable providers: xAI, OpenAI, Anthropic, Google, MiniMax |
| **Local allowed** | Mic capture, RMS gate, classic WebRTC VAD, playback, espeak-ng emergency TTS |
| **Avoid as primary** | Playwright website “Dictate” (optional last-resort provider only) |

## Capability matrix (honest)

| Provider | STT | TTS | Streaming STT | Auth for this project | Notes |
|----------|-----|-----|---------------|----------------------|--------|
| **xAI Grok** | Yes — REST + WS | Yes — REST + WS | **Yes** (Smart Turn) | **Grok Build OAuth** (`~/.grok/auth.json`) preferred; `XAI_API_KEY` fallback | **Default primary** |
| **OpenAI** | Yes — transcriptions + Realtime Whisper | Yes | Yes | `OPENAI_API_KEY`; also Codex / OpenCode / Pi CLI stores | Strong fallback |
| **Anthropic** | **No public STT API** as of plan time | Product voice / TTS not a general TTS API for this | N/A | Claude Code Max is UI voice (`/voice`) | **Orchestrator**, not STT engine. Provider stub: status `unsupported` with message; optional experimental browser/product bridge later |
| **Google (Gemini / Antigravity)** | **Yes** — Gemini audio understanding (file → transcript); Cloud STT if GCP | Yes — Gemini TTS / Cloud TTS | Partial (Live API / Cloud streaming) | `GOOGLE_API_KEY` / `GEMINI_API_KEY` / ADC | Good batch path after VAD segment |
| **MiniMax** | **ASR not clearly public** on main API docs | **Yes** — T2A (`/v1/t2a_v2`) | TTS streaming yes | `MINIMAX_API_KEY`; also `mmx` CLI / Pi / OpenCode / legacy `~/.minimax` | Use for **TTS**; STT = probe + stub until official ASR endpoint confirmed |

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
- Auth discovery (env first, then CLI stores — fail-open):
  1. `MINIMAX_API_KEY` (+ optional `MINIMAX_GROUP_ID` header when required)
  2. MiniMax CLI **`mmx`**: `~/.mmx/config.json` (`api_key` or `oauth.access_token`; honors `MMX_CONFIG_DIR`)
  3. Pi agent `~/.pi/agent/auth.json` — `minimax` key
  4. OpenCode auth — `minimax*` / `minimax-coding-plan` keys
  5. Legacy `~/.minimax` (raw key file or dir with `config.json` / `api_key`)
- Never log the token. Interactive login: `mmx auth login`.

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
| `OPENAI_API_KEY` | OpenAI (preferred explicit) |
| `~/.codex/auth.json` | OpenAI via Codex CLI |
| `~/.local/share/opencode/auth.json` | OpenAI / MiniMax via OpenCode |
| `~/.pi/agent/auth.json` | OpenAI / MiniMax via Pi agent |
| `MINIMAX_API_KEY` | MiniMax TTS (preferred explicit) |
| `~/.mmx/config.json` | MiniMax via **`mmx`** CLI |
| `~/.minimax` | Legacy MiniMax key file/dir |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | Google |
| Anthropic keys | Not required for voice I/O; used by orchestrator host |

---

## Cost control

- Open STT stream only while listening (after gate or on `hark listen`)  
- Truncate TTS to `tts.max_chars`  
- Prefer xAI Smart Turn over long idle WS  
- Verbose mode may log estimated billable seconds, never audio by default  

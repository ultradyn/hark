# Implementation plan — Hark

## Phase 0 — Spec merge (done)

- [x] Rename product to **Hark** / CLI `hark`  
- [x] Fold prior-agent safety, HEP, audio, risk classes  
- [x] Mode A primary + multi-session  
- [x] Event schema + Herdr socket probe prototype  
- [x] Skill `hark` + alias `handsfree`  
- [x] Repo renamed to `hark`; **Mode A first** (no `harkd` in v1)  

## Phase 1 — Python prototype (from checkout)

```bash
uv sync
uv run hark doctor
uv run hark status --json
uv run hark watch --once --for-monitor
python prototype/herdr_event_monitor.py --socket "$HERDR_SOCKET_PATH"
```

### Order

1. [x] Package layout `src/hark/`, entrypoint `hark`  
2. [x] Config + multi-session + doctor (Grok OAuth detect)  
3. [x] Herdr client: CLI wrap (`agent list` / send / read)  
4. [x] `watch` poll merge → HEP + `--for-monitor`  
5. [x] `context` / `status` / fingerprint helper  
6. [x] `tts` / `listen` (xAI OAuth + gate) + radio end phrases  
7. [x] `ask` + confirm policy (R2 always)  
8. [x] `answer` bound delivery store + stale reject; `keys`  
9. [x] OpenAI + Google STT; MiniMax TTS; anthropic unsupported stubs  
10. [x] SSH tunnel helper  
11. [x] Socket subscribe path (auto; poll fallback)  
12. [x] Ambient wake (`hey hark` / `hey herald`) via local short snippets  
13. *(explicitly not v1 product)* `harkd` — Python scaffold + boundary: [HARKD.md](HARKD.md)  

### Dev rule

Always run from **latest checkout** (`uv run hark`), not a stale global install.

## Phase 2 — Harden (still Mode A)

- Dedupe/debounce integration tests  
- Idempotent delivery store (sqlite or jsonl)  
- Echo-overlap rejection tests  
- Doctor redaction  
- Acceptance suite  

## Phase 3 — Native CLI

Rust `hark` (Mode A tools + watch); same HEP and CLI. **Still no daemon required.**

## Phase 4 — Optional

- **`harkd` Mode B** (after Mode A is solid) — see [HARKD.md](HARKD.md)  
- MCP tools (prior `MCP_TOOLS.md` as draft)  
- Herdr plugin packaging  
- Wake-prefix mode  
- MiniMax ASR if documented  
- Barge-in / AEC  

## Risks

| Risk | Mitigation |
|------|------------|
| Grok OAuth not accepted by api.x.ai STT | Fall back `XAI_API_KEY`; doctor reports both |
| protocol 14 missing subscribe filters | Poll path |
| Mode A agent invents answers | Skill + monitor instructions |
| Double-send on blip | `answer` + delivery_id |

## Language

Python prototype → **Rust** production (matches prior ADRs and Herdr ecosystem).  

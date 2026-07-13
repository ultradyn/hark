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
python prototype/herdr_event_monitor.py --socket "$HERDR_SOCKET_PATH"
```

### Order

1. Package layout `src/hark/`, entrypoint `hark`  
2. Config + multi-session + doctor (Grok OAuth detect)  
3. Herdr client: CLI wrap + socket (reuse probe)  
4. `watch` poll merge → HEP + `--for-monitor`  
5. `context` / `status` / fingerprint helper  
6. `tts` / `listen` (xAI OAuth) + adaptive gate basics  
7. `ask` + confirm policy (R2 always)  
8. `answer` bound delivery + stale reject; `keys`  
9. OpenAI + Google STT; MiniMax TTS  
10. SSH tunnel helper  
11. Socket subscribe path  
12. *(explicitly not v1)* `harkd`  

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

- **`harkd` Mode B** (after Mode A is solid)  
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

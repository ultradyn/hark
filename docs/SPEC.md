# Hark — normative software specification

**Package:** `hark`  
**CLI:** `hark`  
**Optional daemon:** `harkd`  
**Skill:** `hark`  
**Config:** `~/.config/hark/config.toml`  
**State:** `~/.local/state/hark/`  
**Env prefix:** `HARK_`  
**Min Herdr:** 0.7.1 (protocol ≥ 14 on known hosts)  
**Event schema:** `hark.event.v1` — [PROTOCOL.md](PROTOCOL.md), `schemas/event-v1.schema.json`

RFC words: **MUST** / **MUST NOT** / **SHOULD** / **MAY**.

---

## 1. Intent

Enable a human within earshot of a microphone to supervise multiple Herdr coding agents by voice. The system surfaces blocked (and optionally completed) agent state, speaks questions, captures deliberate replies, transcribes via **cloud** STT, and delivers text or keys with **auditable, race-safe routing**.

Not a general voice assistant. An LLM is **not** required on the critical path for routing identity (Mode B / library). Handsfree uses a supervisory agent for **judgment** (false done, menus, summaries) while still calling library-safe delivery APIs.

## 2. Placement

| Component | Location |
|-----------|----------|
| Handsfree orchestrator | Local, **outside** Herdr |
| `hark` CLI + mic/speakers | Local |
| Herdr server(s) | Local and/or remote (multi-session) |
| Coding agents | Inside Herdr panes |

## 3. Modes

| Mode | Priority | Description |
|------|----------|-------------|
| **A — Agent + tools** | **v1 only path** | Monitor on `hark watch`; agent runs `context` / `ask` / `answer` / `keys` |
| **B — `harkd`** | **Post-v1** | Same library; full voice loop without orchestrator — **not in v1** |
| **C — one-shot** | Always | `tts`, `listen`, freeform `reply` for debug |

v1 **MUST** complete handsfree without shipping `harkd`. Library design **SHOULD** leave room for Mode B later.

Handsfree requires a long-lived wake path on `hark monitor --for-monitor` (or at least `hark watch`). Native Monitor in Claude Code / Grok; on Pi use [`pi-monitor`](https://github.com/clankercode/pi-monitor) (`pi install npm:pi-monitor`), on OpenCode use [`opencode-monitor-bg`](https://github.com/clankercode/opencode-monitor-bg), on **Antigravity (`agy`)** use experimental **agentapi deliver** (`hark agentapi deliver --follow-monitor` — see [AGY.md](AGY.md)). See [ARCHITECTURE.md](ARCHITECTURE.md#monitor--harness-compatibility).

## 4. CLI (v1 surface)

```
hark doctor
hark watch [--session ID]... [--statuses blocked,done] [--for-monitor] [--transport auto|socket|poll]
hark status [--session ID]... [--status …] [--read-excerpt] [--json]
hark queue [--json]                    # pending interactions if tracked
hark context <target> [--lines N] [--source …]
hark tts …
hark listen … [--end-mode silence|radio]
hark ask … [--confirm auto|always|never] [--end-mode silence|radio]
hark reply <target> …                  # freeform (debug / simple)
hark keys <target> <key> [key…]
hark answer <event_id> (--text … | --keys …)   # bound, preferred
hark skip <event_id>
hark mute | unmute
hark devices
hark providers [test NAME]
hark config path|init|show
```

**Target:** `session_id/pane_id` or `--session` + pane.

**Exit codes:** 0 ok · 1 error · 2 usage · 3 Herdr · 4 provider · 5 audio · 6 timeout · 7 abort/stale/policy.

## 5. Library invariants (all modes)

1. At most one mic lease at a time.  
2. Capture binds to an `event_id` (or explicit freeform flag).  
3. TTS and mic capture do not overlap by default (half-duplex). Optional
   `audio.overlap_prearm` may open capture near TTS end while discarding audio
   until TTS ends + `overlap_discard_ms` (echo guard).  
4. A transcript does not retarget because a newer event arrives mid-utterance.  
5. Delivery checks pane identity, revision, and question fingerprint when bound.  
6. Uncertain writes reconcile before retry; no blind double-send.  
7. Pane content is untrusted (see [SAFETY.md](SAFETY.md)).  

## 6. Herdr client

### Startup per session

1. Resolve socket (local path or SSH tunnel).  
2. `ping` / version gate ≥ 0.7.1.  
3. Capability probe (`session.snapshot` if available; else `agent list`).  
4. Subscribe `events.subscribe` when possible; else poll.  
5. Reconcile already-blocked panes from snapshot before announcing “new.”  

### Subscriptions (when available)

- `pane.agent_status_changed`  
- `pane.agent_detected`  
- `pane.closed` / `pane.exited` / `pane.moved`  
- optional workspace/tab closed  

### Delivery

Prefer `agent.send`; else `pane.send_text` + `send_keys`.  
Menus: `hark keys` / `answer --keys`.  

### Multi-session

Events always carry `session_id`. See [HERDR.md](HERDR.md).

## 7. Question extraction

On blocked:

1. `agent.read` / `pane.read` (`visible` or `recent-unwrapped`, default 40–80 lines).  
2. Optional `detection` source if available.  
3. Strip ANSI; take trailing ask block.  
4. Classify `question.kind` + `risk` (R0–R3).  
5. Compute **fingerprint** over normalized question text + choices.  
6. Speak concise form; keep longer excerpt for `context` / “read verbatim.”  

Agent-specific parsers MAY improve confidence; low confidence → longer verbatim + stricter confirm.

**MUST NOT** use an unconstrained LLM to rewrite permission scope.

## 8. Risk and confirmation

See [SAFETY.md](SAFETY.md).

- R0/R1: confirm **auto** (when unsure).  
- R2/R3: confirm **always**.  

## 9. Audio

See [AUDIO_DESIGN.md](AUDIO_DESIGN.md). Event-driven answer windows only in MVP. Adaptive gate; post-TTS guard; no continuous cloud streaming of ambient audio.

**Listen end modes** (`[listen].end_mode` in `~/.config/hark/config.toml`, env `HARK_LISTEN_END_MODE`):

| Mode | Finalize when |
|------|----------------|
| `silence` (default) | Energy gate + end-silence; optionally Smart Turn |
| `radio` | Spoken end phrase (product-scoped defaults + soft closers when enabled; e.g. `okay hark send`, `send it`, sentence-final `over`); short mid-turn pauses must not cut off; long post-speech idle auto-finishes |

**Radio partial cadence** (`[listen].radio_partial_silence_s`, default **0.6 s**): in radio mode only, trailing quiet of this length ends a capture **segment** and runs interim STT (emitted as `ambient.partial` when `stream_partials` is true). It does **not** end the turn by itself. End phrases, soft closers, agent `listen-end` / cancel, `max_listen_s`, or **post-speech idle** still finalize. Silence mode continues to use `end_silence_s` (default 2.1 s) for answer-window end; do not change that for radio partial frequency. See [AUDIO_DESIGN.md](AUDIO_DESIGN.md).

**Radio idle auto-finish** (`[listen].radio_idle_end_silence_s`, default **3× `end_silence_s`** ≈ **6.3 s**): for **answer / ask windows** in radio mode only (not ambient wake), after the energy gate has opened at least once, continuous quiet longer than this value auto-finishes the capture on the same path as soft-end (finalize STT + close stream; **not** cancel). Before speech opens, existing initial timeout / nudge behavior applies. Short thinking pauses (~2 s) stay open. Soft/product end phrases still finish sooner when said.

Cancel phrases abort without delivery (`hark cancel`, not casual “cancel that”). Hard `max_listen_s` always applies.

**Endpointing strategy** (`[listen].endpoint_strategy`, default **`energy`**, env `HARK_LISTEN_ENDPOINT_STRATEGY`): silence-mode turn detection is pluggable. `energy` reduces exactly to the fixed `end_silence_s` gate (default; also the fallback). `smart_turn` consults a Smart Turn v3 model (optional `[smart-turn]` extra + `smart_turn_model_path`) to finish early or hold through mid-thought pauses, bounded by `endpoint_max_silence_s`; if it cannot load, capture falls back to the energy gate. Full evaluation + seam: [ENDPOINTING.md](ENDPOINTING.md).

**Soft end phrases** (`[listen].soft_end_phrases_enabled`, default **`true`**): in radio mode, also finalize on a conservative list of informal closers (`send it`, `send that`, `that's all`, `end of message`, `over and out`, `okay over`, bare `over`, …) **only** when the phrase is utterance-final (word-bounded transcript suffix) after segment silence. Bare `over` is a radio prosign (always **end**, never cancel): sole utterance, after sentence punct, or other utterance-final use unless the previous word is a phrasal-verb cue (“turn it over”, “take over”); mid-clause “over the weekend” never finishes. Multi-word `okay over` covers STT that drops the comma in “okay, over”. Mid-clause “that's all I know about X” and “send it to production” must not finish. Env: `HARK_SOFT_END_PHRASES_ENABLED` (`0`/`false` disables). The orchestrator **must** call `hark listen-end` from partials when a done signal is clear and the stream is still active (backup if soft-end misses). Full lists: [AUDIO_DESIGN.md](AUDIO_DESIGN.md).

**Ambient** (`[ambient]`): when not in an answer window, optional local 2–3 s snippet wake (`hey hark` / `hey herald`); **no cloud STT until activation**.

**Ambient streaming** (`[ambient].streaming`, default **`false`**, B098): when **false**, radio `ambient.partial` HEP instructions are hard HOLD (think privately; no TTS full answer). When **true**, partial instructions allow **short live TTS** acks / brief interim replies while capture continues; pane delivery and full answers still wait for `ambient.prompt` / `final=true`. Event field `streaming` mirrors the flag. **Quiet gate (B105):** `hark tts` holds play until operator quiet ≥ `[ambient].streaming_ack_min_quiet_s` (default **2.0 s**) or the listen ends — continuous speech without that pause is not stepped on (coordinates with mute-during-TTS / B097).

## 10. Providers

See [PROVIDERS.md](PROVIDERS.md). xAI via Grok OAuth preferred. No local neural STT/TTS. No Playwright as production STT.

## 11. Events

See [PROTOCOL.md](PROTOCOL.md). Dedupe + debounce required. `--for-monitor` compact profile required for handsfree.

## 12. Config (sketch)

```toml
version = 1

[[herdr.sessions]]
id = "local"

[[herdr.sessions]]
id = "work"
ssh = "workbox"

[watch]
statuses = ["blocked", "done"]
debounce_ms = 250
transport = "auto"
detect_false_done = true   # done/idle + menu-like pane → agent.needs_input

[audio]
# adaptive gate params — see AUDIO_DESIGN
half_duplex = true
post_tts_guard_ms = 350
# listen_pre_arm_ms = 300
# overlap_prearm = false       # true: concurrent capture near TTS end
# overlap_discard_ms = 150     # echo discard after TTS ends (overlap mode)

[listen]
# silence | radio — radio = keep listening until end phrase / post-speech idle
end_mode = "silence"
# end_silence_s = 2.1               # silence mode: quiet that ends the answer window
# radio_partial_silence_s = 0.6     # radio only: quiet before interim STT/partial
# radio_idle_end_silence_s = 6.3    # radio answer: post-speech quiet → auto-finish (3× end_silence)
# stream_partials = true
endpoint_strategy = "energy"        # energy (default/fallback) | smart_turn
# smart_turn_model_path = "~/.local/share/hark/models/smart-turn-v3.onnx"
# endpoint_max_silence_s = 3.0      # smart turn: max wait on "incomplete" (0 = end_silence_s)
# end_phrases / cancel_phrases — see AUDIO_DESIGN defaults
# soft_end_phrases_enabled = true   # informal closers (default on; set false for product-only)
strip_phrase = true
max_listen_s = 300

[stt]
provider = "auto"
# xai oauth via ~/.grok/auth.json

[tts]
provider = "auto"
max_chars = 500

[confirm]
mode = "auto"   # for R0/R1; R2/R3 force always

[safety]
deny_patterns = []  # optional hard blocks
```

Env: `HARK_CONFIG`, `HARK_LISTEN_END_MODE`, `HARK_SOFT_END_PHRASES_ENABLED`, `HARK_STT_PROVIDER`, `XAI_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `MINIMAX_API_KEY`, `HERDR_SOCKET_PATH`.

## 13. Performance targets (excluding provider RTT)

| Metric | Target |
|--------|--------|
| Idle watch CPU | < 1–2% one core |
| Idle RSS | < 50 MiB (native); Python prototype higher OK |
| Normalize blocked event | p95 < 250 ms after receipt |
| No STT sockets while idle | required |

## 14. Diagnostics

`hark doctor` MUST check: Herdr sessions/tunnels, version, mic, playback, provider auth (Grok OAuth), control permissions, redacted output.

## 15. Dev workflow

Python prototype: always `uv run hark` from latest git checkout.  
Production: Rust rewrite, same CLI + HEP.

## 16. Definition of done (v1 handsfree)

Without browser automation or local ML models:

1. Multi-session watch with HEP events + monitor profile.  
2. Context + risk classify + fingerprint.  
3. Speak/listen via at least xAI (OAuth) and one fallback.  
4. Bound `hark answer` with stale rejection.  
5. `hark keys` for menus.  
6. Done events wake agent; skill forbids blind announce.  
7. Skill alone runs handsfree.  
8. Acceptance tests in [ACCEPTANCE.md](ACCEPTANCE.md) pass or skip with documented reasons.  

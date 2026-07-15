# Architecture

## Topology

```
 ┌──────────────────────────────────────────────────────────────┐
 │  Local machine                                                 │
 │                                                                │
 │  Handsfree orchestrator (outside Herdr)                        │
 │       ▲  Monitor: hark watch --for-monitor                     │
 │       │  tools: context, ask, answer, keys                     │
 │       │                                                        │
 │  ┌────┴─────┐     optional later      ┌─────────────────────┐  │
 │  │ hark CLI │◄───────────────────────►│ harkd (Mode B)      │  │
 │  │ + lib    │   same safety library   │ full auto voice loop│  │
 │  └────┬─────┘                         └─────────────────────┘  │
 │       │ cloud STT/TTS · mic · speakers                           │
 │       ▼                                                          │
 │  Herdr sessions: local sock · SSH tunnel · multi               │
 │       ▼                                                          │
 │  Coding agents in panes                                          │
 └──────────────────────────────────────────────────────────────┘
```

## Library vs agent vs daemon

| Layer | Owns |
|-------|------|
| **hark library** | Herdr client, HEP normalize/dedupe, fingerprints, audio gate, providers, **safe delivery**, mic lease |
| **Handsfree agent** | Judgment: false done, menu mapping, summaries, when to dig into session files |
| **harkd** | Optional: priority queue + dialogue FSM without an orchestrator |

**Invariant:** an LLM **MUST NOT** choose the delivery target ID. Targets come from events + explicit human/agent tool args; the library validates fingerprints.

## Multi-session

One watch process merges N sessions; every event has `session_id`.  
Remote: SSH tunnel of Unix socket (preferred) or `ssh host herdr …` poll fallback.

## Event path

```text
Herdr wire event
  → debounce
  → normalize HEP (hark.event.v1)
  → dedupe by (session, pane, epoch, fingerprint)
  → priority queue
  → orchestrator: stdout --for-monitor
  → (Mode B: dialogue FSM)
```

## Interaction path (bound)

```text
blocked → extract question + risk + fingerprint
       → TTS identity + question
       → post-TTS guard → readiness
       → Answer Window open(policy) → ListenResult
       → echo reject / filler reject (silence session)
       → confirm if R2/R3 or auto-unsure
       → revalidate target
       → send text or keys
       → idempotent delivery record
```

## Answer Window (deep listen module)

Capture after TTS or ambient wake is a single deep module
(`hark.answer_window`): **`open(policy) → ListenResult`**.

| Layer | Owns |
|-------|------|
| **External interface** | `open_answer_window(policy, deps=…)`, `AnswerWindowPolicy` profiles (`bound_answer` / `post_wake` / `confirm`), `ListenResult` |
| **Implementation** | `RadioSession` (segments, partial HEP, soft/hard end, agent control, idle clamp) · `SilenceSession` (endpoint strategy, empty/no-open recovery, echo) |
| **Thin facades** | `speech.run_listen` builds policy + deps then opens the window; ambient post-wake and CLI listen pass **profiles**, not gate-kwargs soup |
| **Stays pure / separate** | `listen_end` phrase evaluation (no I/O); `listen_control` IPC for `hark listen-end` |

**Locality:** radio soft-end, streaming idle clamp, and partial HEP shapes live behind one seam. **Leverage:** Mode A CLI, ambient, speak-then-listen, and dashboard dictation share the same open path. Streaming / idle knobs are **policy fields** (not re-read from `[ambient]` inside the session loop). Design note: [plans/P1-M1-answer-window.md](plans/P1-M1-answer-window.md). Domain terms: root [CONTEXT.md](../CONTEXT.md).

## Bound Answerability (deep delivery gate)

Whether a bound event is still safe to answer is a single deep module
(`hark.answerability`): **`assess_snapshot(live) → Verdict`** (pure) plus
injectable **`read_live_snapshot` / `assess_live`** for status + fingerprint
re-read.

| Layer | Owns |
|-------|------|
| **External interface** | `assess_snapshot`, `LiveAnswerSnapshot`, `AnswerabilityVerdict`, reason codes; live helpers for Herdr-like clients |
| **Matrix** | Herdr `live.status` × HEP kind (`agent.blocked` / `agent.needs_input` / …) × pane heuristics → deliver\|refuse |
| **Orchestrators** | `answering.answer_bound_event` (store + send); `cli._queue_live_answerable`; dashboard `/answer` via the same answer path |
| **False-done** | `agent.needs_input` + idle-like status + menu still present + FP match → **deliver**; empty idle chrome → refuse |

**Compatible state** is codified here (not only `status==blocked`). SAFETY.md Routing and [plans/P1-M2-answerability.md](plans/P1-M2-answerability.md) are normative. Delivery store age/idempotency remain in `delivery.py`.

## Monitor / harness compatibility

Handsfree needs the orchestrator to wake on **`hark monitor --for-monitor`** (unified Herdr + ambient feed). Prefer that over bare `hark watch` alone. Availability by harness:

| Harness | Monitor / wake | How |
|---------|----------------|-----|
| Claude Code, Grok | Native | Built-in long-lived Monitor tool on `hark monitor --for-monitor` |
| Pi | Plugin | [`pi-monitor`](https://github.com/clankercode/pi-monitor) (`pi install npm:pi-monitor`) — runs a background command and delivers regex-matching stdout into the session |
| OpenCode | Plugin | [`opencode-monitor-bg`](https://github.com/clankercode/opencode-monitor-bg) — `monitor_start` / `monitor_list` / `monitor_fetch` / `monitor_kill` deliver background output back into the owning session |
| **Antigravity (`agy`)** | **agentapi inject (experimental)** | No native Monitor. Register env + run `hark agentapi deliver --follow-monitor` (or `./scripts/hark-agy-deliver.sh`). See [AGY.md](AGY.md). |

Point plugins / agentapi at `hark monitor --for-monitor` (or at minimum `hark watch --for-monitor --statuses blocked,done`). Without a Monitor/inject path, blocks won't interrupt the session.

## Related docs

- [PROTOCOL.md](PROTOCOL.md) — HEP  
- [SAFETY.md](SAFETY.md) — risk, stale, distrust  
- [AUDIO_DESIGN.md](AUDIO_DESIGN.md) — gate / duplex  
- [PRIOR_ART.md](PRIOR_ART.md) — what we merged from other agents  
- [HARKD.md](HARKD.md) — optional `harkd` vs handsfree boundary (experimental)  
- [AGY.md](AGY.md) — Antigravity (`agy`) agentapi handsfree path (experimental)  
- [plans/B049-agy-agentapi.md](plans/B049-agy-agentapi.md) — B049 design + follow-ups

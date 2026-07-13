# Architecture

## Topology

```
 ┌──────────────────────────────────────────────────────────────┐
 │  Local machine                                                 │
 │                                                                │
 │  Mode A orchestrator (outside Herdr)                           │
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
| **Mode A agent** | Judgment: false done, menu mapping, summaries, when to dig into session files |
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
  → Mode A: stdout --for-monitor
  → (Mode B: dialogue FSM)
```

## Interaction path (bound)

```text
blocked → extract question + risk + fingerprint
       → TTS identity + question
       → post-TTS guard → readiness
       → listen (adaptive gate / Smart Turn)
       → echo reject / filler reject
       → confirm if R2/R3 or auto-unsure
       → revalidate target
       → send text or keys
       → idempotent delivery record
```

## Monitor / harness compatibility

Mode A needs the orchestrator to wake on **`hark monitor --for-monitor`** (unified Herdr + ambient feed). Prefer that over bare `hark watch` alone. Availability by harness:

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
- [HARKD.md](HARKD.md) — optional `harkd` vs Mode A boundary (experimental)  
- [AGY.md](AGY.md) — Antigravity (`agy`) agentapi Mode A path (experimental)  
- [plans/B049-agy-agentapi.md](plans/B049-agy-agentapi.md) — B049 design + follow-ups

# Prior art merge log

Sources under `/home/xertrov/src/handsfree-agent/`:

| Artifact | Origin |
|----------|--------|
| `init-prompt.md` | Original product brief (same as this repo’s start) |
| `follow-up-1.md` | Mode A, multi-session, `hfa`, providers, open Q answers |
| `HARK-README-INTRO.md` | **Naming/branding: Hark** |
| `herdr-voice-bridge-spec.zip` | Full “hvb” normative spec (daemon-first) |
| `herdr-voice-spec-v0.1.0.zip` | Second “herdr-voice” normative spec + MCP + interaction FSM |

## Product direction after merge

| Topic | Prior specs | This project (after fold) |
|-------|-------------|---------------------------|
| Name | hvb / herdr-voice | **Hark** / CLI **`hark`** |
| Critical path | Daemon owns voice loop | **Mode A primary** (agent + tools); **same safety library**; optional **`harkd`** later |
| Orchestrator | Optional supervisor | **Required for Mode A**; local, outside Herdr |
| Multi-session / remote | MVP often single mic machine | **First-class multi-session + SSH** |
| Confirm | R2 always; R0/R1 optional | **auto** for ordinary; **always** for permission/destructive (R2/R3) |
| Skill | herdr-voice | **`hark`** + alias **`handsfree`** |
| Repo path | various | **`/home/xertrov/src/grok/hark`** |
| v1 scope | daemon often required | **Mode A only** (`harkd` later) |

## Ideas adopted (must-have)

1. **Stale-answer protection** — question fingerprint + pane revision + re-validate before send  
2. **Event dedupe** — `(session, pane, agent_session, blocked_epoch, question_fingerprint)`  
3. **Debounce** status edges 150–400 ms  
4. **Risk classes R0–R3** and permission readback  
5. **Target invalidation** on pane close/move/exit  
6. **Idempotent delivery** records (no double-send on reconnect)  
7. **Adaptive noise floor** + post-TTS guard + readiness cue  
8. **Echo transcript rejection** (high overlap with just-spoken TTS)  
9. **Question kinds** — free_text / choice / permission / confirmation  
10. **Monitor compact profile** — short lines, no secrets  
11. **JSON Schema** for watch/interaction events  
12. **Priority queue** when many agents blocked  
13. **Voice meta-commands** — repeat, skip, cancel, next, status  
14. **Agent-content distrust** — pane text is untrusted data  
15. **Local DSP allowed; neural STT/TTS not**  
16. **Socket-first Herdr client** with poll fallback + capability probe  
17. **Herdr socket probe prototype** — port into `prototype/`  
18. **SECURITY** controls and doctor redaction  
19. **Optional MCP** surface (Phase 3+), not required for Mode A  
20. **Branding** — tagline, verse, `harkd`, `~/.config/hark`, `HARK_` env  

## Ideas deferred (not rejected)

| Idea | Why deferred |
|------|----------------|
| SQLite full interaction store in v1 Mode A | Agent can keep state; add with `harkd` |
| Full dialogue FSM in a always-on daemon as *only* path | Conflicts with Mode A preference |
| MCP as primary agent interface | CLI + Monitor first; MCP later |
| Wake-prefix continuous local gate | Optional after event-driven mode works |
| System TTS as default | Prefer cloud (quality); espeak emergency only |
| Browser/Playwright STT | Explicitly experimental / out of v1 |
| Windows named pipes | After Linux solid |
| Biometric speaker ID | Never required |

## Conflicts resolved

### Daemon-owns-loop vs Mode A

**Resolution:** Implement **one library**. Mode A uses `hark watch|context|ask|reply|keys`. Optional `harkd` later runs the same library without an orchestrator. **Safety checks live in the library**, not only in the agent’s judgment.

### “Confirm only when unsure” vs “always confirm permissions”

**Resolution:**  
- **R0/R1** (ordinary free text / low-risk choice): confirm only when unsure.  
- **R2/R3** (permission, destructive, credentials, deploy, publish): **always** confirm.  

This matches both safety and the operator’s intent for routine answers.

## Branding locked

From `HARK-README-INTRO.md`:

- Project: **Hark**  
- CLI: **`hark`**  
- Daemon: **`harkd`** (optional)  
- Config: `~/.config/hark/`  
- State: `~/.local/state/hark/`  
- Env prefix: `HARK_`  
- Tagline: **When your agents need a word.**  

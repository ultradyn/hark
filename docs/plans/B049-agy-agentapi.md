# B049 — Support agy (Antigravity) via agentapi

## Problem

Mode A needs a **long-lived Monitor** on `hark monitor --for-monitor` so the
orchestrator wakes on `agent.blocked`, `ambient.prompt`, etc. without polling.

| Harness | Wake path |
|---------|-----------|
| Claude Code, Grok | Native Monitor tool |
| Pi / OpenCode | Plugin monitors |
| **Google Antigravity (`agy`)** | **No native Monitor** — needs **agentapi inject** |

Without inject, an idle agy session never sees Herdr blocks or ambient prompts
unless the human presses Enter or the agent polls (forbidden for Mode A).

## Inspiration (c2c, read-only)

Patterns copied in spirit (not as a dependency):

| c2c piece | Role |
|-----------|------|
| `AgyAdapter` | Client adapter: `agentapi_wake`, config under `~/.gemini` |
| `Mode_agy_inject` | Delivery mode selecting agentapi watcher |
| `c2c_agy_deliver` | Sidecar: inbox → `agy agentapi send-message` with `ANTIGRAVITY_LS_ADDRESS` |
| Harness skill `agy.md` | CLI-first; receive via agentapi inject; no MCP required |

Canonical inject:

```bash
ANTIGRAVITY_LS_ADDRESS=<http://127.0.0.1:port> \
  agy agentapi send-message --title="…" <conversation_id> <content>
```

Env observed in-session:

- `ANTIGRAVITY_LS_ADDRESS` — local language-service HTTP base
- `ANTIGRAVITY_CONVERSATION_ID` — conversation / recipient id

## Design (this PR — foundation)

### Module `src/hark/agentapi.py`

Pure + thin runtime helpers (testable without a live agy TUI):

| API | Purpose |
|-----|---------|
| `AgyEnv` | `ls_address` + `conversation_id` |
| `read_agy_env` / `write_agy_env` | `~/.local/state/hark/agy-env.json` |
| `resolve_agy_env_from_environ` | From `ANTIGRAVITY_*` |
| `format_wake_payload` | Preamble + HEP line |
| `build_send_message_argv` | Pure argv builder |
| `send_message` / `deliver_line` | Subprocess inject (mockable) |
| `follow_monitor_and_deliver` | Sidecar: spawn monitor → inject |

### CLI `hark agentapi`

```text
hark agentapi register [--ls-address …] [--conversation …]
hark agentapi status [--json]
hark agentapi send "…" [--dry-run]
hark agentapi deliver [--follow-monitor | stdin] [--dry-run]
```

### Operator path (Mode A on agy)

1. Install skill (`hark` / `handsfree`) into Antigravity skill dirs as needed.
2. `hark doctor` + `./scripts/run-mode-a.sh` (watch + ambient workers).
3. Inside an **agy** session (or with env exported):  
   `hark agentapi register`
4. Start the **deliver sidecar** (second terminal or nohup):  
   `hark agentapi deliver --follow-monitor`  
   or `./scripts/hark-agy-deliver.sh`
5. Load skill `/hark` — same TTS / answer loop. Wakes arrive as agentapi-injected
   user messages containing compact HEP JSON.

### Constraints / non-goals (this PR)

- **Not** a full c2c-style managed start with hooks auto-writing agy-env.
- **Not** MCP for Mode A on agy (CLI + agentapi only).
- **Not** replacing `hark monitor` for harnesses that already have Monitor.
- Deliver sidecar is **best-effort**; failed inject retries are a follow-up.
- Injected peer content is **data** — agent must still use `hark answer` with
  bound `event_id` (same SAFETY rules).

## Follow-ups

1. Optional SessionStart hook snippet under `~/.gemini` to auto-`register`.
2. Pidfile + graceful stop for deliver sidecar (share Mode A lifecycle).
3. Dedup / rate-limit rapid partial injects (radio `ambient.partial` flood).
4. Doctor check: agy on PATH, env registered, sidecar alive.
5. First-class skill install path for Antigravity skill root.
6. End-to-end dogfood with live agy TUI + blocked Herdr pane.

## Validation

- Unit tests for env R/W, payload format, argv, dry-run deliver, CLI register.
- Manual: `hark agentapi send --dry-run` with registered env; live inject when
  operator has agy running.

--- SUMMARY ---

- Agy has no native Monitor; Mode A wake uses `agy agentapi send-message` +
  `ANTIGRAVITY_LS_ADDRESS` / conversation id (c2c pattern).
- This PR ships `hark.agentapi` + CLI + docs + skill notes + thin deliver sidecar.
- Full managed lifecycle (hooks, doctor, partial rate-limit) is explicit follow-up.

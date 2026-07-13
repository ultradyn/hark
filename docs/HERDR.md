# Herdr integration contract

## Version floor

| Requirement | Value |
|-------------|-------|
| Herdr app | **≥ 0.7.1** |
| Socket protocol | **≥ 14** (0.7.1 on operator host reports 14) |
| Orchestrator location | **Outside** Herdr (local machine) |
| Herdr location | Local and/or remote; **multi-session** supported |

Probe always:

```bash
herdr --version
herdr status
# per session:
HERDR_SESSION=name herdr status
HERDR_SOCKET_PATH=/path herdr agent list
```

## Multi-session model

Each Hark **session** is a named handle to one Herdr server:

| Field | Meaning |
|-------|---------|
| `id` | Stable id in events (`local`, `work`, `laptop`) |
| `socket` | Unix socket path, or tunnel endpoint |
| `ssh` | Optional `user@host` to establish tunnel |
| `herdr_bin` | Optional remote `herdr` path |
| `label` | Spoken name (“work box”) |

### Local

```bash
herdr agent list
# socket: ~/.config/herdr/herdr.sock
# named:  ~/.config/herdr/sessions/<name>/herdr.sock
```

Env: `HERDR_SESSION`, `HERDR_SOCKET_PATH`.

### Remote (v1 approaches)

1. **SSH remote attach (human UI):** `herdr --remote workbox` — good for humans; automation may still prefer sockets.  
2. **SSH tunnel of the Unix socket (preferred for `hark`):**  
   ```bash
   ssh -L /tmp/herdr-work.sock:/home/you/.config/herdr/herdr.sock workbox -N
   # then:
   HERDR_SOCKET_PATH=/tmp/herdr-work.sock herdr agent list
   ```  
   `hark` should manage tunnels for `[[herdr.sessions]]` with `ssh = "workbox"` (spawn ControlMaster, local sock path under `~/.cache/hark/tunnels/`).  
3. **Remote CLI over SSH (fallback poll):**  
   ```bash
   ssh workbox herdr agent list
   ```  
   Higher latency; OK for poll transport.

### Event identity

Every event and reply target is **`(session_id, pane_id)`**.  
CLI forms:

```text
hark reply work/w1:p6 "yes"
hark reply --session work w1:p6 "yes"
hark keys work/w1:p6 2 enter
```

## Semantic states

| Status | Hark handling |
|--------|---------------------|
| `blocked` | Primary speak/listen trigger |
| `working` | Usually ignore |
| `idle` | Ignore unless asked |
| `done` | **Wake Mode A; do not auto-announce.** Agent reads short context and judges |
| `unknown` | Non-blocked |

False `done` / missed `blocked` → Mode A re-reads pane (`hark context` / `herdr agent read`) and optionally session files.

## CLI used by Hark

### Inspect

```bash
herdr agent list
herdr agent get <target>
herdr agent read <target> [--source visible|recent|recent-unwrapped] [--lines N]
herdr agent explain <target> [--json]
herdr pane read <pane_id> …
```

### Wait / send

```bash
herdr wait agent-status <pane_id> --status blocked
herdr agent send <target> <text>
herdr pane send-text <pane_id> <text>
herdr pane send-keys <pane_id> <key> [key ...]   # 2 enter down esc …
herdr pane run <pane_id> <command>               # text+Enter for shells, not chat
```

### Keys for menus

Herdr key-combo syntax: `enter`, `tab`, `esc`, `down`, `up`, digits as printable keys, `ctrl+c`, etc.

`hark keys` is a thin wrapper that sets the correct session socket and forwards to `pane send-keys` / `agent`-resolved pane.

## Socket API

NDJSON over Unix socket. Subscribe when available:

```json
{"id":"sub1","method":"events.subscribe","params":{"subscriptions":[{"type":"pane.agent_status_changed"}]}}
```

Bootstrap: `session.snapshot` if present, else `agent list` poll.

If subscribe missing on protocol 14, poll only — still correct multi-session via separate sockets.

## Context for judgment

| Source | Use |
|--------|-----|
| `agent read --source visible --lines 40` | Default “2–3 msgs” worth of bottom UI |
| `recent-unwrapped --lines 80` | Slightly more log-like context |
| `agent explain` | Why Herdr chose a state |
| `agent_session` path/id if present on agent record | Raw harness files when orchestrator wants deeper context |
| Full scrollback | Only on demand — keep default small for tokens + TTS |

## Environment (inside Herdr panes — coding agents)

| Variable | Meaning |
|----------|---------|
| `HERDR_ENV=1` | Inside Herdr |
| `HERDR_PANE_ID` | Pane id |
| `HERDR_SOCKET_PATH` | Socket |

The **Hark orchestrator does not need these**; it addresses remote/local sessions via config.

**Self-exclusion (B029):** when `hark watch` itself runs inside a Herdr pane,
hark's own pane appears in `agent list`. Watch reads `HERDR_ENV` +
`HERDR_PANE_ID` + `HERDR_SOCKET_PATH` to identify that pane and filters it out
before edge-detection — it emits no `agent.*` events for, and never reads, its
own pane (avoids a feedback loop). The excluded target is reported on
`watch.armed` as `self_target`. Set `HARK_WATCH_INCLUDE_SELF=1` to disable this.
Pane-id matches are scoped to the herdr server at `HERDR_SOCKET_PATH`; a pane on
a different (e.g. remote/tunnelled) session is never treated as self.

## Integrations

```bash
herdr integration install claude
herdr integration install codex
herdr integration status
```

Recommended on each host that runs agents.

## Out of scope

- Replacing Herdr detection manifests  
- Requiring orchestrator inside a Herdr pane  
- Phone-only audio without a local mic path (v1)  

# Herdr integration contract

## Version floor

| Requirement | Value |
|-------------|-------|
| Herdr app | **≥ 0.7.1** |
| Socket protocol | **≥ 14** (0.7.1 on operator host reports 14) |
| Orchestrator location | **Outside** Herdr (local machine) |
| Herdr location | Local and/or remote; **multi-session config** supported (see below) |

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
| `herdr_bin` | Optional `herdr` binary path; **local sessions only** — ignored when `ssh` is set (tunnel-backed ops use the local client against `HERDR_SOCKET_PATH`) |
| `remote_socket` | Optional remote socket path for SSH/tunnel sessions (default: Herdr's standard socket) |
| `label` | Spoken name (“work box”) |

**Config vs handsfree start:**

- **Config / targets / doctor:** multiple `[[herdr.sessions]]` entries are first-class. Event identity and `hark reply` / `keys` use `session/pane` across any configured id.
- **`hark watch`:** with no `--session`, watches **all** configured sessions (fallback id `local` if the list is empty). Pass `--session` once per id (`hark watch --session a --session b`) to select a subset.
- **`hark start` / `restart`:** spawn **one** watch worker for a single `--session` (also `HARK_SESSION` in `run-mode-a.sh`). CLI default is **`default`**, while the stock config template uses `id = "local"` — pass `--session local` (or rename the config id) so the id matches a configured session. Watching every configured session from one `hark start` is not current product behavior.

### Local

```bash
herdr agent list
# socket: ~/.config/herdr/herdr.sock
# named:  ~/.config/herdr/sessions/<name>/herdr.sock
```

Env: `HERDR_SESSION`, `HERDR_SOCKET_PATH`.

### Remote (v1 approaches)

1. **SSH remote attach (human UI):** `herdr --remote workbox` — good for humans; automation may still prefer sockets.  
2. **SSH tunnel of the Unix socket (preferred — what Hark automates):**  
   ```bash
   ssh -L /tmp/herdr-work.sock:/home/you/.config/herdr/herdr.sock workbox -N
   # then:
   HERDR_SOCKET_PATH=/tmp/herdr-work.sock herdr agent list
   ```  
   For `[[herdr.sessions]]` with `ssh = "…"`, Hark runs a dedicated `ssh -N -L` child and attaches a local `HerdrClient` to the forwarded socket. Preferred local path is under the XDG cache (`~/.cache/hark/tunnels/` by default) with a **transport-digest** filename (stable for session id + ssh + remote socket), not a bare session name. If that path exceeds the AF_UNIX length budget (~100 bytes), Hark falls back to a short path under `/tmp` or `/var/tmp` as `hark-{uid}-{namespace}/…`. Tunnels use crash-safe lease/owner markers (B152).  
3. **Remote CLI over SSH (manual / debug only):**  
   ```bash
   ssh workbox herdr agent list
   ```  
   Useful for humans debugging without a tunnel. **Not** a Hark poll transport — production watch/`HerdrSessionAccess` always uses approach 2 (tunnel + local socket client). `transport=poll` still talks to the local forwarded socket / local `herdr` CLI, not `ssh host herdr`.

### Event identity

Every event and reply target is **`(session_id, pane_id)`**.  
CLI forms:

```text
hark reply work/w1:p6 "yes"
hark reply --session work w1:p6 "yes"
hark keys work/w1:p6 2 enter
```

## Session profile (B125)

Handsfree startup interview answers live at
`~/.local/state/hark/session_profile.json` (XDG state). Fields: **scope**,
**autonomy**, **role**, **mode**.

| Scope | Meaning for `hark start` / `restart` |
|-------|--------------------------------------|
| `herdr` (default when no profile) | Start Herdr watch (forward agent events) |
| `session_local` | Skip Herdr watch — ambient/orchestrator only |

CLI:

```bash
hark session-profile show
hark session-profile set --scope herdr|session_local [--autonomy …] [--role …] [--mode …] [--apply]
hark session-profile apply    # write saved mode into config.toml
hark session-profile clear    # remove profile; start defaults return
```

Watch overrides on `hark start` / `restart`:

- `--no-watch` — never start watch (wins over profile and `--force-watch`)
- `--force-watch` — start watch even when scope is `session_local`
- no profile file → treat as watch-on (`herdr` scope)

`--apply` / `apply` map **mode** into `[ambient].streaming` / `[listen].end_mode`.
Skill docs point here for the CLI; this section is the public home.

## Semantic states

| Status | Hark handling |
|--------|---------------------|
| `blocked` | Primary speak/listen trigger |
| `working` | Usually ignore |
| `idle` | Ignore unless asked |
| `done` | **Wake orchestrator; do not auto-announce.** Agent reads short context and judges |
| `unknown` | Non-blocked |

False `done` / missed `blocked` → orchestrator re-reads pane (`hark context` / `herdr agent read`) and optionally session files.

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

### Sessions + start agent (I005 / voice spawn)

Prefer **`hark`** wrappers for handsfree (alias-safe CLI resolve + kickoff):

```bash
hark session list [--json]
hark session ensure <name> [--json]          # start headless server if needed
hark agent-start <agent> --cwd PATH [--herdr-session NAME] [--prompt "…"] [--json]
# agent: claude|cc, codex|cx, grok|gk, cursor-agent|cr, opencode, pi, agy, or --adhoc
```

Raw Herdr equivalents:

```bash
herdr session list [--json]
herdr --session <name> server                # headless named session
herdr agent start <name> [--cwd PATH] [--workspace ID] [--tab ID] [--split right|down] [--no-focus] -- <argv...>
```

See `docs/plans/I005-voice-herdr-agent-control.md` and `hark.agents.resolve` for alias reject rules (gcc-as-`cc`, CodeRabbit-as-`cr`).

**`[agents]` config** (voice spawn resolution):

```toml
[agents]
prefer_aliases = true   # default: try safe short aliases (cc/cx/gk/cr) before canonical
# claude = "claude"     # or absolute path / PATH token override per agent key
# codex = "codex"
```

`prefer_aliases = true` prefers short aliases only when they resolve to a **safe** PATH binary (not gcc-as-`cc` / CodeRabbit-as-`cr`). Set `false` to prefer canonical names first. Per-agent keys override the resolved command entirely.

### Keys for menus

Herdr key-combo syntax: `enter`, `tab`, `esc`, `down`, `up`, digits as printable keys, `ctrl+c`, etc.

`hark keys` is a thin wrapper that sets the correct session socket and forwards to `pane send-keys` / `agent`-resolved pane.

## Socket API

NDJSON over Unix socket. Subscribe when available:

```json
{"id":"sub1","method":"events.subscribe","params":{"subscriptions":[{"type":"pane.agent_status_changed"}]}}
```

Bootstrap: `agent list` poll (production `hark watch` reconciles from `agent list` at startup and after reconnects; `session.snapshot` is used only by `prototype/herdr_event_monitor.py`).

If subscribe missing on protocol 14, poll only — still correct multi-session via separate sockets (one client/socket per configured session id).

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
| `HERDR_SESSION` | Optional named session |

The **Hark orchestrator does not need these**; it addresses remote/local sessions via config.

**Self-exclusion (B029):** when `hark watch` itself runs inside a Herdr pane,
hark's own pane appears in `agent list`. Watch reads `HERDR_ENV` +
`HERDR_PANE_ID` + `HERDR_SOCKET_PATH` to identify that pane and filters it out
before regular edge and lifecycle handling — it emits no watch events for, and
never reads, its own pane (avoids a feedback loop). The excluded target is reported on
`watch.armed` as `self_target`. Set `HARK_WATCH_INCLUDE_SELF=1` to disable this.
Pane-id matches are scoped to the herdr server at `HERDR_SOCKET_PATH`; a pane on
a different (e.g. remote/tunnelled) session is never treated as self. If the
socket is absent or malformed, Hark leaves every pane included rather than
risking a collision with another server.

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

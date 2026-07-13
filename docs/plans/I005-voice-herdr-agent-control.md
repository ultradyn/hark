# I005 — Voice control for Herdr sessions + coding-agent start

**Status:** planned (decomposition only; implementation in B055–B059).  
**Idea:** Voice-create Herdr sessions, start coding agents (claude/codex/grok/cursor-agent + ad-hoc CLIs), and kick them off with an initial prompt. Prefer short aliases when present (`cc`, `cx`, `gk`, `cr`).

## Problem

Mode A today can **watch** and **answer** agents already running in Herdr panes. It cannot, by design surface:

1. Create / ensure a **named Herdr session** (`herdr --session name` / `herdr session …`).
2. **Start** a new coding agent pane (`herdr agent start … -- <argv>`).
3. Resolve the right **CLI argv** (alias vs full binary) reliably.
4. **Kick off** that agent with a spoken initial prompt after spawn.

Operators want hands-free: *“hey hark, start a codex in clawq on the swarm session and tell it to fix the flaky test.”*

## Feasibility

| Capability | Evidence | Verdict |
|------------|----------|---------|
| Start agent in pane | `herdr agent start <name> [--cwd …] [--workspace …] [--tab …] [--split …] -- <argv…>` | **Ready** |
| Named sessions | `herdr session list/attach/stop/delete`; `herdr --session <name>` creates/uses | **Ready** |
| Workspace / tab | `herdr workspace create`, `herdr tab create` | **Ready** (optional v1) |
| Inject kickoff prompt | Existing `HerdrClient.send_text` / `agent send` + Enter | **Ready** |
| Alias preference | Operator shell often has fish functions `cc`/`cx`/`gk`; PATH bins vary | **Needs careful resolver** |
| Voice path | Ambient wake → STT → Mode A `ambient.prompt` → agent tools | **Ready** (skill + CLI) |

**Verdict: feasible now.** No Herdr protocol gaps for the v1 slice. Main product work is (1) safe argv resolution, (2) thin library/CLI wrappers Mode A can call without freestyling shell, (3) skill playbook + confirmations, (4) doctor/docs.

### Alias resolution pitfalls (dogfood host)

Observed on a real Linux + fish host:

| Wanted | Alias | PATH reality |
|--------|-------|--------------|
| Claude Code | `cc` | `/usr/bin/cc` → **gcc** (must reject); fish function `cc` is Claude — **not** visible to non-interactive `herdr agent start` |
| Codex | `cx` | Often missing on PATH; fish function only |
| Grok | `gk` | Often a real `~/.local/bin/gk` wrapper |
| Cursor Agent | `cr` | `~/.local/bin/cr` may be **CodeRabbit**, not cursor-agent |

**Rule for the resolver:**

1. For each known agent, try **preferred aliases in order**, then **canonical command**.
2. Accept a candidate only if it is an executable on `PATH` **and** not on a **reject list** (e.g. basename `cc` that is gcc/cc→gcc; basename `cr` that is coderabbit).
3. Optional config overrides: absolute paths or explicit argv prefixes.
4. **Ad-hoc** CLIs: operator/Mode A supplies full argv; no alias table required.
5. Never shell out through interactive fish/zsh functions for spawn — only PATH binaries (or config paths). Document that operators who only have fish functions should install thin `~/.local/bin/{cc,cx,…}` shims or set config.

## Goals

1. Deterministic **CLI argv resolution** for known agents + ad-hoc.
2. Library + `hark` CLI to **ensure session** and **start agent** via Herdr.
3. Optional **kickoff prompt** after start (send text + Enter).
4. Mode A **skill** voice playbook (phrases, confirm cwd/session, TTS ack).
5. **Doctor** shows which CLIs resolve; docs for HERDR/skill/SPEC.

### Non-goals (this epic)

- Auto-approving agent tools or bypassing R2/R3 human confirm for *destructive* agent work.
- Starting remote Herdr servers over SSH (session must already exist or be local; tunnels stay as today).
- Full workspace/tab voice UX (API may accept workspace/tab ids; voice defaults to simple split).
- Teaching fish functions to Herdr (PATH/config only).
- Dashboard UI for spawn (I003) — may call the same library later.

## Architecture

```text
Operator speech
  → ambient wake + STT → ambient.prompt
  → Mode A orchestrator (judgment: which agent, cwd, session, prompt)
  → hark agent-start / hark session ensure  (library validates + runs herdr)
  → herdr agent start … -- <resolved argv>
  → optional hark reply / agent send kickoff
  → TTS ack ("Started codex in clawq on swarm")
```

**Invariant (aligned with SPEC):** Mode A may choose *which* agent/cwd/session from human speech, but the library **builds and runs** the herdr command (no silent double-spawn without ack; clear errors). Target identity after start comes from herdr’s returned agent record / re-list.

### Layers

| Layer | Owns |
|-------|------|
| `hark.agents.resolve` (new) | Catalog, alias preference, reject list, ad-hoc argv, config overrides |
| `HerdrClient` | `list_sessions`, `ensure_session` (document create path), `start_agent(...)` |
| `hark` CLI | `agent-start`, `session list/ensure` (names TBD in B057) |
| Mode A skill | Voice phrases, confirm policy, when to kickoff, TTS acks |
| doctor | Report resolved CLIs + missing aliases |

## Suggested spoken UX (skill; not library NLU)

Examples Mode A should honour (paraphrase OK):

- “Start claude in amaroo”
- “New codex in clawq on swarm”
- “Spin up grok” (cwd = last focused / ask)
- “Start cursor-agent with the prompt: review the last commit”
- “Create a herdr session called lab”
- “Run opencode in preview-md” (ad-hoc / less-common CLI)

### Clarification policy (audio)

When the operator wants a new coding-CLI session but **placement is unclear** — which
**Herdr session** (e.g. `default` / `swarm` / configured `[[herdr.sessions]]` id) and/or
which **Herdr space** (workspace / tab / split target) — Mode A **MUST NOT guess**.

1. Ask for clarification by voice (`hark ask` / `hark tts --listen`).
2. Offer a **brief** list of suggested options (from live `hark session list` /
   configured sessions / recent workspaces — not an exhaustive dump).
3. **One audio question at a time.** Never stack multiple questions in one TTS turn
   (e.g. do not ask session *and* cwd *and* kickoff prompt together). Resolve the
   highest-priority gap, then the next, until enough to spawn safely.

Same one-at-a-time rule applies to all audio questions in this flow (cwd, agent kind,
kickoff prompt confirm, etc.), not only session placement.

Confirm / clarify when:

- **Herdr session or space is unspecified or ambiguous** (required clarify + short options)
- cwd is ambiguous or outside known project roots
- creating a **new** named session
- ad-hoc argv the operator did not clearly name

## Decomposition

| ID | Title | Est | Depends |
|----|-------|-----|---------|
| **B055** | Coding CLI argv resolver (aliases + ad-hoc + reject list) | 2h | — |
| **B056** | HerdrClient: list/ensure session + agent start | 3h | B055 |
| **B057** | CLI: `hark agent-start` / session ensure + optional kickoff | 3h | B056 |
| **B058** | Mode A skill: voice playbook for session + agent spawn | 2h | B057 |
| **B059** | Config, doctor, docs (HERDR/SPEC/skill) for voice agent control | 2h | B057, B058 |

### Implement order

1. B055 pure resolver + unit tests (no herdr).  
2. B056 client methods + mocked subprocess tests.  
3. B057 CLI for Mode A tools.  
4. B058 skill + package skill copies.  
5. B059 doctor/docs/config polish.

## Config sketch (B059)

```toml
[agents]
# optional overrides (absolute path or first PATH token)
# claude = "cc"           # only if safe shim exists
# codex = "codex"
# grok = "gk"
# cursor_agent = "cursor-agent"
# prefer_aliases = true   # default true

# reject basenames that must never be used as coding CLIs
# reject = ["gcc", "cc"]  # built-in rejects still apply for gcc-linked cc
```

Exact keys locked in B059.

## Acceptance for the idea (I005)

- [x] Feasibility evaluated against live `herdr` CLI + Mode A path.  
- [x] Design note at `docs/plans/I005-voice-herdr-agent-control.md`.  
- [x] Concrete bugs B055–B059 with AC + dependencies.  
- [x] Idea file updated with created IDs and design summary.  
- [x] Clarification policy: ambiguous session/space → ask + short options; one audio Q at a time.

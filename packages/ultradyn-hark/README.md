# @ultradyn/hark

![Hark — when your agents need a word](https://hark.xk.io/og.png)

> **When your agents need a word.**

Handsfree voice for your coding-agent **herd** in [Herdr](https://herdr.dev/).

**Supervise the whole herd by voice.** When agents in [Herdr](https://herdr.dev/) block, swarm, or wait on you, Hark speaks the ask and takes your answer out loud—so the fleet keeps moving while you’re away from the keyboard.

This package ships the **`hark`** and **`handsfree`** agent skills. Run the skill; the agent arms Herdr watch, speech, and safe delivery. You stay on voice.

| | |
|--|--|
| **Site** | [hark.xk.io](https://hark.xk.io) |
| **Skills** | `hark` · alias `handsfree` |
| **CLI** | Separate Python package (see [Install the CLI](#install-the-cli-hark-binary)) |
| **Requires** | [Herdr](https://herdr.dev/) ≥ 0.7.1 · `hark` CLI on `PATH` · a long-lived **Monitor** |
| **Supports** | Claude Code · Grok Build · Antigravity · Pi · OpenCode · Codex |
| **Source** | [github.com/ultradyn/hark](https://github.com/ultradyn/hark) |

---

## Supports

Handsfree voice needs a coding CLI/agent that can run shell tools **and** keep a long-lived **Monitor** (or equivalent) on:

```bash
hark monitor
```

Compact Monitor lines are the default (no extra flags required). Without a persistent Monitor, blocks won’t interrupt the session.

| CLI / agent | Support | Notes |
|-------------|---------|--------|
| **Claude Code** | Native / Monitor | Skill + Monitor on the watch feed; full handsfree loop |
| **Grok Build** | Native / Monitor | Same pattern; skill install + persistent monitor |
| **Antigravity** | Native / AgentAPI | Experimental long-lived feed via agentapi inject |
| **Pi** | Plugin | Any plugin that keeps a long-lived Monitor on `hark monitor`. Example: [pi-monitor](https://github.com/clankercode/pi-monitor) (`pi install npm:pi-monitor`) |
| **OpenCode** | Plugin | Same idea. Example: [opencode-monitor-bg](https://github.com/clankercode/opencode-monitor-bg) (`monitor_start` / `monitor_fetch`) |
| **Codex / others** | If Monitor exists | Any agent with shell + long-lived watch can drive Hark |

Herdr hosts the worker agents. Your orchestrator (outside Herdr) runs the skill and speaks for the fleet.

---

## Install skills

### Skills CLI (recommended)

```bash
npx skills add ultradyn/hark -g -y
```

Pick agents explicitly:

```bash
npx skills add ultradyn/hark -g -a claude-code -a opencode -y
```

### This package (npm / pnpm / bun)

```bash
# npm
npm i -g @ultradyn/hark

# pnpm
pnpm add -g @ultradyn/hark

# bun
bun add -g @ultradyn/hark
```

Then:

```bash
hark-skill path    # absolute paths to skill directories
hark-skill list    # hark, handsfree
hark-skill help
```

Register from the installed package:

```bash
npx skills add "$(dirname "$(hark-skill path | head -1)")" -g -y
```

Or point your agent’s skills path at the directories printed by `hark-skill path`.

---

## Use

In a compatible agent:

```text
/hark
# or
/handsfree
```

**Run the skill. The agent sets up the rest.**

1. Run `hark doctor` / setup as needed  
2. Arm **`hark monitor`** (required — ambient alone misses `agent.blocked`)  
3. Speak blocked questions, listen, confirm when needed  
4. Deliver text or menu keys with stale-safe targeting  

Prefer voice after the skill loads (`hark tts`, `hark ask`, `hark listen`). Ambient wake: say *hey hark*, *hey herald*, or your custom trigger.

---

## Install the CLI (`hark` binary)

Skills need the **Python `hark` CLI** on your machine (not bundled in this npm package).

**One-liner** (CLI + skills into `~/.claude/skills`) — script hosted on the site:

```bash
curl -fsSL https://hark.xk.io/install.sh | bash
```

Safer (inspect, then run):

```bash
curl -fsSL https://hark.xk.io/install.sh -o /tmp/hark-install.sh
less /tmp/hark-install.sh
bash /tmp/hark-install.sh
```

Then:

```bash
hark doctor
```

From a monorepo checkout: `uv sync && uv run hark doctor`.

Full docs, architecture, and safety notes: **[hark.xk.io](https://hark.xk.io)** · **[GitHub](https://github.com/ultradyn/hark)**.

---

## The loop

```text
Agent becomes blocked in Herdr
        ↓
Hark / skill speaks the question
        ↓
You answer by voice
        ↓
Cloud STT → validate / confirm if needed
        ↓
Deliver to the correct Herdr target (stale-safe)
        ↓
Work continues
```

Fleet control by voice. Human-in-the-loop without hands on the keyboard.

**Risky actions always confirm.** Pane text is untrusted; fingerprint + revision refuse stale panes. The LLM never invents the delivery target.

---

## Package layout

```text
@ultradyn/hark
  skills/hark/SKILL.md
  skills/handsfree/SKILL.md
  bin/hark-skill.js
```

Skills are synced from the monorepo on pack (`npm pack` / publish).

---

## Links

- Site: [hark.xk.io](https://hark.xk.io)
- Source: [ultradyn/hark](https://github.com/ultradyn/hark)
- Herdr: [herdr.dev](https://herdr.dev/)
- Issues: [github.com/ultradyn/hark/issues](https://github.com/ultradyn/hark/issues)

---

## Releasing

Maintainers: see monorepo [`RELEASE.md`](../../RELEASE.md). Pushing tag `vX.Y.Z`
(matching this package’s version) runs GitHub Actions `release.yml` — OIDC
trusted publish to npm + GitHub Release. After push, agents run
`/watch-gh-populate-release`.

## License

MIT

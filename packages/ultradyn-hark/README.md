# @ultradyn/hark

> **When your agents need a word.**

Handsfree voice for your coding-agent **herd** in [Herdr](https://herdr.dev/).

This package ships the **`hark`** and **`handsfree`** agent skills (Mode A). Load the skill in Claude Code, Grok, Pi, OpenCode, or any agent with skill + Monitor support — the agent arms Herdr watch, speaks blocked questions, listens via cloud STT, and delivers replies safely.

| | |
|--|--|
| **Site** | [hark.xk.io](https://hark.xk.io) |
| **Skills** | `hark` · alias `handsfree` |
| **CLI** | Separate Python package (see [Install the CLI](#install-the-cli-hark-binary)) |
| **Requires** | [Herdr](https://herdr.dev/) ≥ 0.7.1 · `hark` CLI on `PATH` for Mode A |
| **Source** | [github.com/clankercode/hark](https://github.com/clankercode/hark) *(moving to `ultradyn/hark`)* |

---

## Install skills

### Skills CLI (recommended)

```bash
npx skills add clankercode/hark -g -y
```

Pick agents explicitly:

```bash
npx skills add clankercode/hark -g -a claude-code -a opencode -y
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

The skill tells the agent to:

1. Run `hark doctor` / setup as needed  
2. Arm **`hark watch --for-monitor`** (required — ambient alone misses `agent.blocked`)  
3. Speak blocked questions, listen, confirm when needed  
4. Deliver text or menu keys with stale-safe targeting  

Prefer voice after the skill loads (`hark tts`, `hark ask`, `hark listen`).

---

## Install the CLI (`hark` binary)

Skills need the **Python `hark` CLI** on your machine (not bundled in this npm package).

**One-liner** (CLI + skills into `~/.claude/skills`):

```bash
curl -fsSL https://raw.githubusercontent.com/clankercode/hark/master/install.sh | bash
```

Safer (inspect, then run):

```bash
curl -fsSL https://raw.githubusercontent.com/clankercode/hark/master/install.sh -o /tmp/hark-install.sh
less /tmp/hark-install.sh
bash /tmp/hark-install.sh
```

Then:

```bash
hark doctor
```

From a monorepo checkout: `uv sync && uv run hark doctor`.

Full docs, ambient wake (`hey hark`), and architecture: **[hark.xk.io](https://hark.xk.io)** · **[GitHub](https://github.com/clankercode/hark)**.

---

## What Mode A does

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
- Source: [clankercode/hark](https://github.com/clankercode/hark)
- Herdr: [herdr.dev](https://herdr.dev/)
- Issues: [github.com/clankercode/hark/issues](https://github.com/clankercode/hark/issues)

---

## Releasing

Maintainers: see monorepo [`RELEASE.md`](../../RELEASE.md). Pushing tag `vX.Y.Z`
(matching this package’s version) runs GitHub Actions `release.yml` — OIDC
trusted publish to npm + GitHub Release. After push, agents run
`/watch-gh-populate-release`.

## License

MIT

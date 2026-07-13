# @ultradyn/hark

Handsfree voice for your coding-agent **herd** in [Herdr](https://herdr.dev/).

This npm package ships the **hark** and **handsfree** agent skills (Mode A).
The Python `hark` CLI lives in the same monorepo: [clankercode/hark](https://github.com/clankercode/hark).

Site: [hark.xk.io](https://hark.xk.io)

## Install skills

### Via skills CLI (recommended)

```bash
npx skills add clankercode/hark -g -y
# or pick agents:
npx skills add clankercode/hark -g -a claude-code -a opencode -y
```

### Via this package

```bash
npm i -g @ultradyn/hark
hark-skill path    # absolute paths to skill dirs
hark-skill list
hark-skill help
```

Then point your agent at those skill directories, or:

```bash
npx skills add "$(dirname "$(hark-skill path | head -1)")" -g -y
```

## Use

In Claude Code / Grok / Pi / OpenCode (with Monitor support):

```text
/hark
# or /handsfree
```

The skill instructs the agent to arm `hark watch --for-monitor`, speech, and safe delivery.

## CLI

```bash
# from monorepo
uv sync && uv run hark doctor
# or install.sh (see B021)
```

## License

MIT

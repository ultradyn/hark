# Naming — locked

| Component | Name |
|-----------|------|
| **Project / package** | **Hark** (`hark`) |
| **CLI** | **`hark`** |
| **Daemon (optional)** | **`harkd`** |
| **Skill (primary)** | **`hark`** (`skill/hark/SKILL.md`) |
| **Skill (alias)** | **`handsfree`** (`skill/handsfree/SKILL.md`) — same Mode A loop |
| **Config dir** | `~/.config/hark/` |
| **State dir** | `~/.local/state/hark/` |
| **Env prefix** | `HARK_` |
| **Event schema id** | `hark.event.v1` |
| **Repo path** | `/home/xertrov/src/grok/hark` |

## Tagline

> **Hark — when your agents need a word.**

Alternatives (from branding brief): “Blocked agents call. Hark answers.” · “Human-in-the-loop, without hands on the keyboard.”

## Etymology note

**Hark** = listen attentively. Fits mic-side product better than “handsfree-agents” or generic `hfa`.

## Superseded names

| Old | Status |
|-----|--------|
| handsfree-agents | Working title only |
| handsfree (skill) | **Kept as alias skill** of `hark` |
| hfa / hf | Replaced by `hark` |
| hvb / herdr-voice | Prior-agent specs; concepts merged |

## CLI shape

```bash
hark doctor
hark watch [--for-monitor]
hark status
hark queue                 # pending interactions (when daemon/library tracks them)
hark context <target>
hark tts "…"
hark listen
hark ask "…"
hark reply <target> "…"
hark keys <target> 2 enter
hark answer <event_id> …   # bound delivery with fingerprint checks
hark mute | unmute
hark devices
hark providers
# later:
harkd                      # optional always-on Mode B (experimental; docs/HARKD.md)
hark daemon start|status|stop
```

## Verse (README flavor only)

See branding brief: playful verse is fine in marketing; **routing and confirmation stay deterministic and boring.**

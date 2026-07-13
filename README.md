# Hark

> **When your agents need a word.**

**Hark, the herald agents sing:  
“Human input, please, we bring.”  
Blocked in Herdr, questions rise;  
Hark relays your voice replies.**

Hark is a lightweight **voice bridge** for coding agents in [Herdr](https://herdr.dev/) (≥ 0.7.1).

When an agent becomes **blocked**, Hark (and/or a supervisory agent using the `hark` skill) can read the question aloud, listen for your spoken answer, transcribe via cloud STT, and deliver text or menu keys to the correct pane—so work continues while you are away from the keyboard.

```text
Agent becomes blocked
        ↓
Hark / orchestrator speaks the question
        ↓
You answer by voice
        ↓
Cloud STT → validate / confirm if needed
        ↓
Deliver to the correct Herdr target (stale-safe)
        ↓
Work continues
```

| | |
|--|--|
| **CLI** | `hark` |
| **Skill** | `hark` (alias: **`handsfree`**) |
| **Optional daemon** | `harkd` — **not v1** (Mode A first) |
| **Mode** | **A only for v1**: local agent outside Herdr + Monitor + tools |
| **Herdr** | ≥ 0.7.1 · multi-session (local + SSH) |
| **Speech** | Cloud only (xAI OAuth, OpenAI, Google, MiniMax TTS, …) |
| **Status** | Specification + probe prototype · app not fully built |

The verse is playful; **routing and confirmation are not.**

## Docs

| Doc | Purpose |
|-----|---------|
| [docs/PRIOR_ART.md](docs/PRIOR_ART.md) | Merge log from earlier agent specs |
| [docs/NAMING.md](docs/NAMING.md) | Locked names (`hark`, `harkd`, paths) |
| [docs/PRODUCT.md](docs/PRODUCT.md) | Goals |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Topology, Mode A, library vs daemon |
| [docs/SPEC.md](docs/SPEC.md) | Normative software spec |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | HEP event protocol |
| [docs/SAFETY.md](docs/SAFETY.md) | Routing, risk R0–R3, distrust |
| [docs/AUDIO_DESIGN.md](docs/AUDIO_DESIGN.md) | Gate, endpointing, half-duplex |
| [docs/HERDR.md](docs/HERDR.md) | Herdr / multi-session |
| [docs/PROVIDERS.md](docs/PROVIDERS.md) | STT/TTS providers |
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | Build plan |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | Acceptance criteria |
| [schemas/event-v1.schema.json](schemas/event-v1.schema.json) | Event JSON Schema |
| [skill/hark/SKILL.md](skill/hark/SKILL.md) | Primary agent skill |
| [skill/handsfree/SKILL.md](skill/handsfree/SKILL.md) | Alias skill (same loop) |
| [prototype/herdr_event_monitor.py](prototype/herdr_event_monitor.py) | Socket subscribe probe |

## Design goals

- Fast, low-overhead, always-on friendly  
- Event-driven Herdr integration (socket subscribe; poll fallback)  
- Reliable multi-agent / multi-session targeting with **fingerprint + revision** checks  
- Pluggable cloud STT/TTS (no local speech model)  
- Confirm ordinary answers only when unsure; **always** confirm permissions/destructive  
- Recoverable across disconnects (no silent double-send)  

## Dev (when implementing)

```bash
cd /path/to/hark   # or this checkout
uv run hark doctor
# probe Herdr events:
HERDR_SOCKET_PATH=~/.config/herdr/herdr.sock python prototype/herdr_event_monitor.py
```

Always run from **latest checkout** while developing the Python prototype.

## Repo

```text
/home/xertrov/src/grok/hark
```

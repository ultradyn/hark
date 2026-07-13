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
| **Status** | Python prototype (`uv run hark`) · Mode A tools + speech |

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

## Install (one-liner)

```bash
curl -fsSL https://raw.githubusercontent.com/clankercode/hark/master/install.sh | bash
```

Safer (inspect, then run):

```bash
curl -fsSL https://raw.githubusercontent.com/clankercode/hark/master/install.sh -o /tmp/hark-install.sh
less /tmp/hark-install.sh
bash /tmp/hark-install.sh
```

The installer is **idempotent** and HTTPS-only. It:

- Clones or updates the repo under `~/.local/share/hark/src` (override with `HARK_HOME` / `--dir`)
- Installs the `hark` CLI via **uv** (`uv tool install`) or **pip** (`--method pip`)
- Copies agent skills to `~/.claude/skills/{hark,handsfree}` (override with `HARK_SKILLS_DIR`)
- Supports `PREFIX` / `DESTDIR`, `HARK_REF` (branch/tag/commit), `--with-wake`, `--no-skills`, `--no-cli`

Then:

```bash
hark doctor
# In Claude Code / compatible agents:
#   /hark
```

From a local checkout: `./install.sh` (uses that tree; no re-clone).

## Dev / try it

```bash
cd /home/xertrov/src/grok/hark   # or your clone
uv sync
uv run hark doctor
uv run hark config init          # optional ~/.config/hark/config.toml
uv run hark status
uv run hark watch --for-monitor  # Mode A feed
uv run hark tts "hello"
uv run hark listen               # speak, then silence ends (or end_mode=radio)
uv run hark ask "What color?"
# ambient wake (needs vosk model if engine=vosk):
# uv run hark ambient
```

Dev tip: run from **latest checkout** (`uv run hark`). After `./install.sh`, the global `hark` on `PATH` is fine for day-to-day use.

### Ambient wake (`hey hark`)

```bash
./scripts/setup-ambient.sh          # uv sync --extra wake + download vosk model + enable config
# download only:
./scripts/download-vosk-model.sh    # methods: hf → curl → wget → browser
./scripts/download-vosk-model.sh --method curl
uv run hark ambient                 # say “hey hark”, then speak a prompt
```

Model lands at `~/.local/share/hark/models/vosk-model-small-en-us-0.15`.

### Config highlights (`~/.config/hark/config.toml`)

- `[listen] end_mode = "radio"` — long pauses OK until `okay hark send` / `end prompt`
- Cancel defaults are product-scoped: `hark cancel` (not “cancel that”)
- `[ambient]` — local 2–3s wake for `hey hark` / `hey herald` (no cloud until activated)
- `[audio] mute_mic_during_tts` — Wave mute ring while TTS plays

## Fixtures (Python ↔ Rust parity)

Shared golden corpora under [`fixtures/`](fixtures/README.md) for wake matching, radio end phrases, HEP event ingest, and live Wave wake snips.

```bash
uv run pytest tests/test_fixtures_parity.py -q
./scripts/export-fixtures.sh              # refresh HEP/syslog samples from live state
./scripts/export-fixtures.sh --with-wake  # also copy today's debug wake snips
```

## Repo

```text
/home/xertrov/src/grok/hark
  install.sh         # one-line installer (CLI + skills)
  fixtures/          # parity goldens + live wake audio
  schemas/           # HEP JSON Schema
  skill/             # agent skills (hark, handsfree)
  src/hark/          # Python Mode A bridge
  tests/
```

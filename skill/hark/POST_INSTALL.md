# Hark post-install (after `npx skills`)

**Audience:** operators and agents right after installing the **hark** / **handsfree**
skill. Skills install does **not** install the Python CLI or its dependencies.

Related: [SETUP.md](SETUP.md) (first-run product setup) · [WAKE_STT.md](WAKE_STT.md)
(local wake engines) · [SKILL.md](SKILL.md) (handsfree loop).

Site / installer: [https://hark.xk.io](https://hark.xk.io) · source:
[github.com/ultradyn/hark](https://github.com/ultradyn/hark)

---

## What `npx skills add` does (and does not)

```bash
npx skills add ultradyn/hark -g -y
# optional agents:
# npx skills add ultradyn/hark -g -a claude-code -a opencode -y
```

| Done by skills install | **Not** done |
|------------------------|--------------|
| Copies **skill markdown** (`SKILL.md`, this file, SETUP, WAKE_STT) into agent skill dirs | Install **`hark` Python CLI** on `PATH` |
| Discovers skill names `hark` and `handsfree` | Install Python packages (`httpx`, `numpy`, `sounddevice`, …) |
| | Install **system** audio libs (PortAudio / ALSA) |
| | Install optional **wake** extras (`vosk`, `sherpa-onnx`, models) |
| | Install / start **Herdr** |
| | Configure STT/TTS auth (`grok login`, API keys) |
| | Write `~/.config/hark/config.toml` |

Until the CLI is installed, `/hark` will fail at the first `hark doctor` / `hark tts`
with `command not found` or import errors. **Finish this checklist before arming
the Monitor.**

Same gap applies to `npm i -g @ultradyn/hark` (skills package only) and to copying
skill trees by hand.

---

## Agent rule (hard)

When this skill is loaded and **`hark` is missing**, **`hark doctor` fails**, or
imports complain about **sounddevice / PortAudio / vosk / sherpa**:

1. **Do not** invent a half-working handsfree loop.
2. Follow **Recommended path** below (or the one-liner installer).
3. Re-run `hark doctor` until the CLI is healthy enough for setup.
4. Then continue [SETUP.md](SETUP.md) / [SKILL.md](SKILL.md) bootstrap.

Prefer the **hosted installer** for operators; prefer **explicit uv steps** when
debugging deps.

---

## Recommended path (one-liner CLI + skills)

Idempotent HTTPS installer (clones/updates sources, installs editable CLI, can
refresh skills):

```bash
curl -fsSL https://hark.xk.io/install.sh -o /tmp/hark-install.sh
less /tmp/hark-install.sh          # inspect
bash /tmp/hark-install.sh
# optional ambient wake Python extra (vosk package only; not the model):
# bash /tmp/hark-install.sh --with-wake
```

Or pipe (trusts hark.xk.io):

```bash
curl -fsSL https://hark.xk.io/install.sh | bash
```

Then:

```bash
export PATH="$HOME/.local/bin:$PATH"
hark doctor
```

Source tree default: `~/.local/share/hark/src` (override: `HARK_HOME` / `--dir`).

---

## Manual path (what the installer is doing)

### 0. Prerequisites

| Need | Why |
|------|-----|
| **Python ≥ 3.11** | `hark` package requires it |
| **`uv`** (preferred) or **pip** | Install the CLI into a tool/user env |
| **git** + **curl** | Clone / fetch if not using a local checkout |
| **PortAudio** (system) | `sounddevice` needs it for mic + playback |
| **ffmpeg** (recommended) | TTS playback (mp3) via ffplay/ffmpeg fallbacks |
| **Herdr ≥ 0.7.1** | Handsfree watches Herdr sessions — separate product |

**PortAudio** (common distros):

```bash
# Debian / Ubuntu
sudo apt-get install -y portaudio19-dev python3-dev

# Fedora
sudo dnf install -y portaudio-devel

# Arch
sudo pacman -S portaudio

# macOS (Homebrew)
brew install portaudio
```

Without PortAudio, `uv tool install` may succeed but `hark listen` / `hark tts`
fail with sounddevice / PortAudio errors.

### 1. Install the `hark` CLI

**Preferred (editable tool install — tracks a git checkout):**

```bash
# clone once (or use installer default location)
git clone https://github.com/ultradyn/hark.git "${HARK_HOME:-$HOME/.local/share/hark/src}"
cd "${HARK_HOME:-$HOME/.local/share/hark/src}"

# core runtime: httpx, numpy, sounddevice
uv tool install -e . --force

export PATH="$HOME/.local/bin:$PATH"
command -v hark && hark --help
```

**From an existing monorepo checkout:**

```bash
cd /path/to/hark
uv tool install -e . --force
# or without installing:
uv run hark doctor
```

**pip (no uv):**

```bash
cd /path/to/hark
python3 -m pip install --user .
# with wake extra:
# python3 -m pip install --user '.[wake]'
export PATH="$HOME/.local/bin:$PATH"
```

**Core Python deps** (always, via the package):

- `httpx` — HTTP for providers / Herdr clients
- `numpy` — PCM / audio buffers
- `sounddevice` — capture + playback (needs **PortAudio**)

These come in automatically with `uv tool install` / `pip install .`. You do **not**
install them one-by-one unless debugging a broken venv.

### 2. Put `hark` on PATH

```bash
export PATH="$HOME/.local/bin:$PATH"
# fish: fish_add_path $HOME/.local/bin
```

Confirm:

```bash
which hark
hark doctor
```

### 3. Optional: ambient wake (local engine)

Core CLI works for **watch / tts / listen / answer** without wake packages.
**Ambient** “hey iris / hey hark” needs an extra + model.

| Engine | Python | Model script | When |
|--------|--------|--------------|------|
| **Vosk** (stock default) | PATH `hark` from `uv tool`: `uv tool install -e '.[wake]' --force`; checkout `uv run hark`: `uv sync --extra wake` | `./scripts/download-vosk-model.sh`; `setup-ambient.sh` for checkout setup | Smaller deps; ASR-based wake |
| **Sherpa KWS** (recommended) | PATH `hark` from `uv tool`: `uv tool install -e '.[wake-sherpa]' --force`; checkout `uv run hark`: `uv sync --extra wake-sherpa` | `./scripts/download-sherpa-kws-model.sh` | Better product-name wake |

Installer flag for Vosk **package** only (not model):

```bash
bash /tmp/hark-install.sh --with-wake
```

**Match the extra to the command you will run.** `uv sync` changes only the
checkout `.venv`; it does **not** add `vosk` or `sherpa-onnx` to a separate
PATH `hark` installed by `uv tool`. `scripts/setup-ambient.sh` also runs that
checkout sync, so use it only with `uv run hark` (or reinstall the PATH tool
afterward).

From the checkout that backs an editable **PATH `hark`**:

```bash
cd "${HARK_HOME:-$HOME/.local/share/hark/src}"

# Vosk path: install the extra into the uv tool environment, not only .venv
uv tool install -e '.[wake]' --force
./scripts/download-vosk-model.sh
hark doctor

# Sherpa path (preferred for iris/hark reliability)
uv tool install -e '.[wake-sherpa]' --force
./scripts/download-sherpa-kws-model.sh
hark doctor
```

Run [SETUP.md](SETUP.md) / `hark setup` to enable and configure the selected
ambient engine after downloading its model.

For checkout-only development with **`uv run hark`**:

```bash
cd "${HARK_HOME:-$HOME/.local/share/hark/src}"

# Vosk path: setup script syncs the checkout .venv, downloads the model,
# and enables the Vosk config
./scripts/setup-ambient.sh
uv run hark doctor

# Sherpa path (preferred for iris/hark reliability)
uv sync --extra wake-sherpa
./scripts/download-sherpa-kws-model.sh
uv run hark doctor
```

Models land under `~/.local/share/hark/models/` by default. Details: [WAKE_STT.md](WAKE_STT.md).

### 4. Speech auth + Herdr

| Piece | Action |
|-------|--------|
| **xAI STT/TTS** (preferred) | `grok login` (or set `XAI_API_KEY`) |
| **Other providers** | OpenAI / Google / MiniMax keys as configured — see site docs |
| **Herdr** | Install/start Herdr ≥ 0.7.1; local socket or `ssh` sessions in config |
| **Config** | `hark config init` → `~/.config/hark/config.toml` |

### 5. Verify

```bash
hark doctor
```

Expect roughly:

- CLI present, install not stuck on obvious missing cmds
- Herdr sessions reachable (local and/or tunnels)
- Speech auth OK for the provider you use
- Mic/devices listed when probing audio
- Ambient: if `engine=vosk` / `sherpa_kws`, doctor should not stay on
  `package_missing` / `missing_model` once extras + models are installed

Then product first-run: [SETUP.md](SETUP.md) or `hark setup`, then arm handsfree
per [SKILL.md](SKILL.md) (`hark start`, **one** `hark monitor --for-monitor`).

---

## Dependency map (cheat sheet)

```text
npx skills add ultradyn/hark
        │
        ▼
  agent skill markdown only
        │
        │  still need ↓
        ▼
┌───────────────────────────────────────────┐
│  System: Python 3.11+, PortAudio, ffmpeg  │
│  CLI:    uv tool install -e .   (or pip)  │
│  Core:   httpx, numpy, sounddevice        │
│  Auth:   grok login / provider keys       │
│  Herdr:  ≥ 0.7.1 running                  │
│  Wake*:  [wake] or [wake-sherpa] + model  │
└───────────────────────────────────────────┘
        │
        ▼
  hark doctor → SETUP.md → /hark loop
```

\* Wake is optional for pure blocked-agent voice answer; required for ambient
wake phrases.

### Optional Python extras (`pyproject.toml`)

| Extra | Packages (approx) | Use |
|-------|-------------------|-----|
| *(none — core)* | httpx, numpy, sounddevice | CLI, STT/TTS cloud, watch, deliver |
| `wake` | vosk | Ambient engine `vosk` |
| `wake-sherpa` | sherpa-onnx, sentencepiece, onnxruntime | Ambient engine `sherpa_kws` |
| `smart-turn` | onnxruntime, transformers | Optional Smart Turn endpointing |
| `local-stt` | faster-whisper | Optional offline post-wake STT (not ambient) |
| `dev` | pytest, jsonschema | Tests only |

---

## Common failures after skills-only install

| Symptom | Cause | Fix |
|---------|--------|-----|
| `hark: command not found` | Skills only; no CLI | Run installer or `uv tool install -e .` |
| `sounddevice` / PortAudio error | System lib missing | Install `portaudio19-dev` (or OS equivalent), reinstall CLI |
| `ModuleNotFoundError: httpx` / `numpy` | Broken or empty env | Reinstall with `uv tool install -e . --force` |
| Ambient `package_missing` (vosk) | PATH tool lacks `wake`, or only checkout `.venv` was synced | PATH `hark`: `uv tool install -e '.[wake]' --force`; checkout `uv run hark`: `uv sync --extra wake` |
| Ambient `package_missing` (sherpa) | PATH tool lacks `wake-sherpa`, or only checkout `.venv` was synced | PATH `hark`: `uv tool install -e '.[wake-sherpa]' --force`; checkout `uv run hark`: `uv sync --extra wake-sherpa` |
| Ambient `missing_model` | Model not downloaded | `setup-ambient.sh` or `download-sherpa-kws-model.sh` |
| `sherpa_onnx` import / libonnxruntime | onnxruntime not linked | Ensure `wake-sherpa` extra; see WAKE_STT / `LD_LIBRARY_PATH` notes |
| xAI 401 / auth missing | No Grok OAuth | `grok login` or `XAI_API_KEY` |
| Herdr / socket errors | Herdr down or wrong session | Install Herdr; fix `[[herdr.sessions]]` |
| `install: stale` / missing `start` | Frozen non-editable tool | `cd` source + `uv tool install -e . --force` |
| PATH works in shell, not in agent | Agent env lacks `~/.local/bin` | Export PATH in agent env / login shell; or use absolute path from `which hark` |

---

## Skills package only (`@ultradyn/hark`)

```bash
npm i -g @ultradyn/hark
hark-skill path    # where skill dirs live
hark-skill list
```

Still requires the **Python** CLI (this document). Skills registration:

```bash
npx skills add ultradyn/hark -g -y
# or point at packaged skills:
npx skills add "$(dirname "$(hark-skill path | head -1)")" -g -y
```

---

## After CLI is healthy

1. [SETUP.md](SETUP.md) — persona, sessions, wake engine, setup-complete flag
2. [SKILL.md](SKILL.md) — TTS mode, **one** Monitor on `hark monitor --for-monitor`, answer loop
3. [WAKE_STT.md](WAKE_STT.md) — Vosk vs Sherpa details

Do **not** skip `hark doctor` before speaking to the operator as if handsfree is live.

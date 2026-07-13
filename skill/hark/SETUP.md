# Hark first-run setup checklist

Canonical **question order** for agent-driven and CLI setup.  
CLI: `hark setup` (flags: `--yes`, `--persona`, `--wake-engine`, `--voice`, …).  
Flag file: `~/.local/state/hark/setup-complete.json` (includes **`hark_version`**).

Related: [WAKE_STT.md](WAKE_STT.md) (local wake; **prefer Sherpa KWS** for product names), [SKILL.md](SKILL.md),
`docs/CUSTOM_WAKE.md`, `docs/plans/B069-local-stt-survey.md`.

---

## When to run

| Condition | Action |
|-----------|--------|
| No `setup-complete.json` | Full setup (this checklist) |
| `setup_schema_version` older than current | Re-ask **only new** questions |
| Operator asks to reconfigure | Full or partial; use `--force` on CLI |
| Already complete, same schema | Skip; go arm handsfree (monitor + workers) |

Schema version lives in code as `SETUP_SCHEMA_VERSION` (`hark.setup_flow`).

---

## CLI must match the checkout (dogfood)

PATH `hark` from a **non-editable** `uv tool install` freezes site-packages. After
`git pull` / new subcommands on master (`start`/`stop`/`restart`, …), PATH can lag
while `uv run hark` works. **`hark doctor`** reports `install: stale|frozen` with a
reinstall hint (B100).

Prefer one of:

```bash
# from the checkout you dogfood (editable — pulls update the CLI)
uv tool install -e . --force
# or always run from the tree:
uv run hark …
# or re-run the installer (editable by default):
./install.sh
```

If `hark start` is “invalid choice”, reinstall editable before debugging Mode A.

## Question order (canonical)

1. **Health** — `hark doctor` (text OK). Fix Herdr / tunnels / speech keys if red.
   Check **install:** — if `stale` / `frozen` / missing cmds, reinstall editable first
   (see above).
2. **Herdr sessions** — local / SSH / mix  
   Write `[[herdr.sessions]]` (local without `ssh`, remote with `ssh = "…"`).  
   See SKILL.md **Herdr sessions**.
3. **Persona**  
   - **Feminine (default):** wake names include **Iris** (+ mercury/hark/herald); TTS **eve**  
   - **Masculine:** **Mercury** (+ iris/hark/herald); TTS **leo**  
   - **Custom:** operator-chosen primary name + voice
4. **TTS voice** — any voice from the active provider catalog.  
   Play a short sample when available (`assets/tts/samples/…` or provider preview).  
   **B076** multi-provider auth: if OpenAI/MiniMax keys missing, **gracefully degrade**
   (list bundled samples only; still accept a typed voice id). Do not hard-fail setup.
5. **Wake backend** — **Vosk** vs **Sherpa KWS** (extensible later):  
   - Recommend **Sherpa KWS** when download/install OK (keyword spotting ≫ Vosk ASR for iris/hark; see [WAKE_STT.md](WAKE_STT.md)).  
   - Prefer **Vosk** if constrained (already installed, no download, tiny deps).  
   - **Defer** → leave `engine = "vosk"` (product default until dogfood).  
   Config: `[ambient] engine = "vosk" | "sherpa_kws"`.
6. **Download model** if Sherpa selected:  
   `./scripts/download-sherpa-kws-model.sh`  
   `uv sync --extra wake-sherpa`  
   Fail-open: if download fails, fall back to Vosk and note in setup answers.
7. **Confirm wake test** — ask operator to say **hey iris** / **hey mercury** (or configured names).  
   Optional ambient once: `hark ambient --once` with ambient enabled.
8. **Write setup-complete flag** with:
   ```json
   {
     "hark_version": "<version that wrote the flag>",
     "setup_schema_version": 1,
     "completed_at": "<ISO-8601 Z>",
     "answers": {
       "persona": "feminine|masculine|custom",
       "wake_names": ["iris", "mercury", "hark", "herald"],
       "tts_voice": "eve",
       "tts_provider": "xai",
       "wake_engine": "vosk|sherpa_kws|defer",
       "sessions": [{"id": "local"}],
       "notes": ""
     }
   }
   ```
   No secrets in this file.
9. **Arm handsfree** — continue SKILL.md (monitor, TTS mode, queue announce).

**Voice-first:** after doctor, prefer `hark tts` / `hark ask` one question at a time when the Hark skill drives setup.

---

## Config keys touched

```toml
[tts]
voice = "eve"            # or leo / catalog id
# provider = "xai"

[ambient]
enabled = true           # when operator is ready for ambient
wake_mode = "names"
names = ["iris", "mercury", "hark", "herald"]
engine = "vosk"          # or "sherpa_kws"
# model_path = "…"       # auto under XDG when model installed

[[herdr.sessions]]
id = "local"
```

---

## CLI cheat

```bash
hark setup --yes --persona feminine --wake-engine vosk
hark setup --yes --persona masculine --wake-engine sherpa_kws
hark setup --force   # re-run full flow
```

---

## Enrollment samples (I006) — optional

After wake backend choice / confirm wake test, **optionally** run:

```bash
hark wake-enroll --phrase "hey iris" --count 7
# dry-run beeps only:
hark wake-enroll --dry-run --count 3
```

Beep loop: **ready** → say phrase once → **accept** (or **reject** + retry) → … → **end**.
Writes `~/.local/state/hark/wake_enroll/<phrase-slug>/<timestamp>/` (`01.wav`… + `manifest.json`).
Optional wake-backend scoring seeds `wake_learned.json` (B077 denylist). Local only — no cloud upload.
Beeps via `audio.cues`.

---

## Fail-open

| Problem | Behavior |
|---------|----------|
| Sherpa model missing | Doctor `status=missing_model`; keep/use `engine=vosk` |
| `sherpa-onnx` not installed | Doctor warns `uv sync --extra wake-sherpa` |
| Vosk model missing | `./scripts/setup-ambient.sh` / `download-vosk-model.sh` |
| TTS sample auth incomplete | Skip playback; still set voice id (B076 when ready) |

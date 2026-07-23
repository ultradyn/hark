# Hark first-run setup checklist

Two separate flows — do not conflate them:

| Flow | When | What |
|------|------|------|
| **A. First-run `hark setup`** | Missing/stale `setup-complete.json`, empty sessions, or `--force` | CLI question order below (sessions → persona → TTS → wake). Writes config + flag. |
| **B. Every `/hark` skill start** | Always, before arming | B125 structured interview (scope → autonomy → role → mode) + `session-profile set … --apply`; sessions only if `scope=herdr`; persona/wake only if setup incomplete. See [SKILL.md](SKILL.md) **Session + voice bootstrap**. |

CLI: `hark setup` (flags: `--yes`, `--force`, `--persona`, `--wake-engine`, `--voice`, `--names`, `--sessions`, `--skip-doctor`, `--skip-download`).  
Flag file: `~/.local/state/hark/setup-complete.json` (includes **`hark_version`**).

Related: [POST_INSTALL.md](POST_INSTALL.md) (CLI + Python/system deps after `npx skills`),
[WAKE_STT.md](WAKE_STT.md) (local wake; stock default **vosk**, recommend **Sherpa KWS** when model install succeeds),
[SKILL.md](SKILL.md), `docs/CUSTOM_WAKE.md`, `docs/plans/B069-local-stt-survey.md`.

---

## When to run

| Condition | Action |
|-----------|--------|
| No `setup-complete.json` | Full **Flow A** (`hark setup`) |
| `setup_schema_version` older than current | Re-run setup (**today: full Flow A**; future may ask only new keys) |
| Empty / missing `answers.sessions` | Re-ask **Herdr sessions** (local / SSH / mix); `hark doctor` warns |
| Operator asks to reconfigure | Full or partial; use `--force` on CLI |
| Already complete, same schema | Skip Flow A. On each `/hark` start still run **Flow B** (B125 interview + `session-profile set --apply`); ask sessions only if `scope=herdr`; persona/wake only if setup incomplete |

Schema version lives in code as `SETUP_SCHEMA_VERSION` (`hark.setup_flow`).

**Agent hard rule (B116 + B125):** on every `/hark` or `/handsfree` skill start, **before** ambient/watch/monitor, run Flow B. Do not arm with silent defaults. See SKILL.md **Session + voice bootstrap** and **Structured startup interview**.

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

## Flow A — First-run `hark setup` (CLI question order)

`hark setup` / `run_setup` asks and writes config + flag. It does **not** run wake-confirm, enrollment, or arm handsfree (those are post-CLI / agent — steps 7–9).

1. **Health** — `hark doctor` (text OK). Fix Herdr / tunnels / speech keys if red.
   Check **install:** — if `stale` / `frozen` / missing cmds, reinstall editable first
   (see above). Note **setup:** line if incomplete / empty sessions.
2. **Herdr sessions** — local / SSH / mix (**always asked in Flow A**; never skip here)  
   Write `[[herdr.sessions]]` (local without `ssh`, remote with `ssh = "…"`).  
   Voice: `hark ask --confirm never "Which Herdr sessions should I watch? Local only, a remote SSH host, or both?"`  
   CLI non-interactive: `hark setup --yes --sessions local` or  
   `--sessions local,work=ssh:workbox`.  
   See SKILL.md **Herdr sessions**. (On skill start, sessions are asked only when Flow B chose `scope=herdr`.)
3. **Persona**  
   - **Feminine (default):** wake names include **Iris** (+ mercury/hark/herald); TTS **eve**  
   - **Masculine:** **Mercury** (+ iris/hark/herald); TTS **leo**  
   - **Custom:** operator-chosen primary name + voice
4. **TTS voice** — any voice from the active provider catalog.  
   Play a short sample when available (`assets/tts/samples/…` or provider preview).  
   **B076** multi-provider auth: if OpenAI/MiniMax keys missing, **gracefully degrade**
   (list bundled samples only; still accept a typed voice id). Do not hard-fail setup.
5. **Wake backend** — **Vosk** vs **Sherpa KWS** (extensible later):  
   - **Stock default / `--yes` without `--wake-engine`:** `vosk` (CLI help: “default vosk until dogfood”).  
   - **Recommend Sherpa KWS** when the model is already on disk or download/install succeeds (keyword spotting ≫ Vosk ASR for iris/hark; see [WAKE_STT.md](WAKE_STT.md)).  
   - Prefer **Vosk** if constrained (already installed, no download, tiny deps) or `--skip-download`.  
   - **Defer** → leave `engine = "vosk"`.  
   Config: `[ambient] engine = "vosk" | "sherpa_kws"`.
6. **Download model** if Sherpa selected:  
   `./scripts/download-sherpa-kws-model.sh`  
   `uv sync --extra wake-sherpa`  
   Fail-open: if download fails, fall back to Vosk and note in setup answers.
7. **(Post-CLI / agent) Confirm wake test** — ask operator to say **hey iris** / **hey mercury** (or configured names).  
   Optional ambient once: `hark ambient --once` (workers arm ambient in-memory; disk `enabled` may still be false — see Config keys).
8. **Write setup-complete flag** — CLI does this at end of Flow A with:
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
   No secrets in this file. CLI then prints “Next: confirm wake… then arm handsfree…” and returns.
9. **(Post-CLI / agent) Arm handsfree** — Flow B if not done, then continue SKILL.md (monitor, TTS mode, queue announce).

**Voice-first (agent driving Flow A):** after doctor, prefer `hark tts` / `hark ask` **one question at a time**. Sessions → persona/wake → TTS voice → engine. Do not stack questions. Do not arm until Flow A answers are written and Flow B is done.

---

## Flow B — Session profile + start (every skill start)

Persist the B125 interview, then start workers. Deep tables live in SKILL.md; this is the minimum SETUP agents must not miss.

```bash
hark session-profile set \
  --scope session_local|herdr \
  --autonomy silent|blocked_only|proactive|babysit \
  --role "…" \
  --mode auto_end|radio|conversation \
  --apply
hark session-profile show   # path: ~/.local/state/hark/session_profile.json
# note start_watch= (false when scope=session_local)
```

Defaults when no profile file: `herdr` + `blocked_only` + `radio`.

```bash
hark start                  # watch + ambient per profile / flags
hark start --force-watch    # watch even if session_local
hark start --no-watch       # skip Herdr watch
hark start --no-ambient     # skip ambient wake loop
```

`scope=session_local` → `hark start` skips Herdr watch unless `--force-watch`.

---

## Config keys touched

`hark setup` / `apply_answers_to_config` writes the keys below. It does **not** set
`[ambient] enabled = true` (package default stays `false` on disk). Continuous ambient
via `hark start` / workers arms the loop in-memory; doctor may still report
`ambient: enabled=False` until an operator flips the disk flag.

```toml
[tts]
voice = "eve"            # or leo / catalog id
# provider = "xai"
# playback_speed = 1.0   # pitch-preserving tempo; non-default needs ffmpeg

[ambient]
# enabled = false        # setup does not write this; flip manually if desired
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
hark setup --yes --persona feminine --wake-engine vosk \
  --sessions local --voice eve
hark setup --yes --persona masculine --wake-engine sherpa_kws \
  --sessions local --names mercury,iris,hark,herald
hark setup --yes --skip-download --wake-engine vosk   # no Sherpa fetch
hark setup --force   # re-run full flow
hark setup --skip-doctor --yes --sessions local
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
| `sherpa-onnx` / vosk package missing | Doctor `status=package_missing` (+ install hint, e.g. `uv sync --extra wake-sherpa`) |
| Vosk model missing | `./scripts/setup-ambient.sh` / `download-vosk-model.sh` |
| TTS sample auth incomplete | Skip playback; still set voice id (B076 when ready) |

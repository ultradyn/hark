# Custom activation (wake) phrases

Ambient mode scans short **local** mic snippets for an activation, then opens
**cloud STT** for the prompt body (`ambient.prompt`).

There are **two customization styles**. Pick one.

## 1. Name-based (default)

Configure **product names** (defaults: `hark`, `herald`). Multiple names are
fine. Matching is structural:

- Greating + name: `hey` / `hello` / `hi` / `yo` / `ok` / `okay` / `sup` + name
- Bare name: `herald`, `harold`, `hark` (optional fillers `um` / `uh`)
- Seed mishears for built-in names (e.g. `hook`→hark, `harold`→herald)
- **Learned** name alternates from failed wake attempts (no restart)

```toml
[ambient]
wake_mode = "names"          # default; can omit
names = ["hark", "herald"]
# extra_names = ["alice"]    # append more canonical names
learn_from_near_misses = true
# Optional exact full-phrase extras still work alongside names:
# extra_trigger_phrases = ["start prompt"]
```

`hark back …` and mid-sentence uses (`the herald of spring`) do **not** wake.

## 2. Full-phrase

Configure **entire trigger phrases** only. No name fuzzy/bare. Matching is
exact (plus learned full-phrase alternates).

```toml
[ambient]
wake_mode = "phrases"
trigger_phrases = ["start prompt", "begin dictation"]
# extra_trigger_phrases = ["begin recording"]
learn_from_near_misses = true
```

Legacy: setting `trigger_phrases` / `activation_phrases` to a list that does
**not** mention hark/herald infers `phrases` mode. Lists that include
hey-hark-style product wakes stay in **names** mode.

## Dynamic learning (no restart)

Failed wake attempts that look intentional (`ambient.wake_near_miss`) **auto-
expand** alternates:

| Mode | Learns | Stored as |
|------|--------|-----------|
| names | Alternate **name tokens** (e.g. vosk `hoc`→`hark`) | `name_aliases` |
| phrases | Alternate **full phrases** (e.g. `start promt`) | `phrase_aliases` |

Persisted at:

```text
~/.local/state/hark/wake_learned.json
```

Ambient hot-reloads this file by mtime on each snippet and after each learn
write. Emits `ambient.wake_learned`. **No SIGHUP or process restart required.**

To pin a learned alias permanently in config:

- Names: add to `names` / `extra_names`, or keep relying on the learned file
- Phrases: add to `trigger_phrases` / `extra_trigger_phrases`

Disable with `learn_from_near_misses = false`.

## Config keys (summary)

| Key | Role |
|-----|------|
| `wake_mode` | `names` (default) or `phrases` |
| `names` / `activation_names` / `wake_names` | Canonical names (names mode) |
| `extra_names` | Append names |
| `trigger_phrases` / `activation_phrases` | Full phrase list (phrases mode) or legacy |
| `extra_trigger_phrases` / `extra_activation_phrases` | Append full phrases |
| `learn_from_near_misses` | Auto-expand from near-misses (default true) |

Edit `~/.config/hark/config.toml` (or `HARK_CONFIG` / `hark --config …`).

`hark config show` / doctor report active mode, names, and display phrases.

## Apply config edits: file-watch vs SIGHUP vs restart

| Method | When | What happens |
|--------|------|----------------|
| **Learning** | Near-miss while ambient runs | Writes `wake_learned.json`; applies next snippet |
| **File-watch** (default) | You save `config.toml` | mtime poll + debounce → same reload path as SIGHUP. Emits `ambient.reloaded` with `source: "config_watch"` |
| **SIGHUP** | You want an immediate reload | Send SIGHUP to the ambient process → same `apply_config_reload`. Emits `ambient.reloaded` with `source: "sighup"` |
| **Restart** | Always safe | Full process restart |

**File-watch defaults** (`[ambient]`):

| Key | Default | Role |
|-----|---------|------|
| `config_watch` | `true` | Enable mtime poll on the active config path (`HARK_CONFIG` / `~/.config/hark/config.toml` / `--config`) |
| `config_watch_poll_ms` | `1000` | How often to `stat` the file |
| `config_watch_debounce_ms` | `400` | Require mtime stable this long before reload (rapid editor writes) |

Env: `HARK_CONFIG_WATCH=0` disables; `=1` forces on (overrides TOML).

Both file-watch and SIGHUP call the same `apply_config_reload` (phrases, names, `listen.end_mode`, `surface_timeouts`, etc.). Phrase-only changes hot-update the backend; engine/model changes rebuild it.

```bash
# Optional: force reload without waiting for the next poll (PID from Mode A / hark daemon)
kill -HUP <ambient-pid>
```

## Debug snips

With `[ambient] debug = true`, wake hits/misses under:

```text
~/.local/state/hark/debug/wake/YYYY-MM-DD/
```

## Agent / skill configuration help

When the operator asks to change how they wake Hark, the Mode A skill should:

1. Ask **name-based** vs **full-phrase** if unclear.
2. Edit the right keys (table above); do not invent CLI flags that do not exist.
3. After config.toml edits: file-watch applies automatically (default); SIGHUP still works for immediate reload. Mention that **learning** needs neither.
4. Optionally inspect `wake_learned.json` and promote stable aliases into config.

## Tests

```bash
uv run pytest tests/test_wake_policy.py tests/test_custom_triggers.py tests/test_custom_wake_e2e.py tests/test_config_watch.py -q
```

## Related

- `src/hark/wake.py` — `WakePolicy`, match, near-miss, learn suggest
- `src/hark/wake_learn.py` — persist/load learned aliases
- `src/hark/config.py` — `resolve_wake_policy`
- `src/hark/ambient.py` — loop, learn hot-apply, SIGHUP / file-watch reload
- `src/hark/config_watch.py` — mtime poll + debounce → `request_reload`
- Skill: `skill/hark/SKILL.md` (Ambient + wake config)

# Custom activation (wake) phrases

Ambient mode scans short **local** mic snippets for an activation phrase, then
opens **cloud STT** for the prompt body (`ambient.prompt`). Defaults include
`hey hark` / `hey herald`. You can add or replace phrases without code changes.

## Config

Edit `~/.config/hark/config.toml` (or `HARK_CONFIG` / `hark --config …`):

```toml
[ambient]
enabled = true
engine = "vosk"   # production; use text_probe only in tests
# model_path = "~/.local/share/hark/models/vosk-model-small-en-us-0.15"

# Keep defaults and append custom wakes:
extra_trigger_phrases = ["start prompt", "begin dictation"]

# Or replace the entire list (no hey hark unless you include it):
# trigger_phrases = ["start prompt"]

# Aliases (same behavior):
# activation_phrases = [...]           # replace defaults
# extra_activation_phrases = [...]     # append
```

Resolved list order: base (`activation_phrases` / `trigger_phrases` or
defaults) then extras, de-duplicated case-insensitively.

`hark config show` / doctor report the active phrases after resolution.

## Apply changes: SIGHUP vs restart

| Method | When | What happens |
|--------|------|----------------|
| **SIGHUP** | Ambient loop already running | Re-reads config; updates wake phrases in place (vosk model kept). Emits `ambient.reloaded` NDJSON. Engine/model path changes rebuild the backend. |
| **Restart** | Always safe | Full process restart (`hark ambient` / Mode A ambient path again). |

```bash
# Find ambient / Mode A process, then:
kill -HUP "$(pgrep -f 'hark.*ambient' | head -1)"
# or: kill -HUP <pid>
```

SIGINT / SIGTERM still mean graceful stop (finish in-flight listen, then exit).
SIGHUP does **not** shut down.

If HUP is inconvenient (systemd unit without reload), edit config and restart
the ambient process the same way you started it.

## Debug snips

With `[ambient] debug = true` (default in the sample config), wake hits and
scored misses are saved under:

```text
~/.local/state/hark/debug/wake/YYYY-MM-DD/
  HHMMSS-mmm-hit.wav + .json
  HHMMSS-mmm-miss.wav + .json
```

Sidecar JSON includes `matched`, `phrase`, `text`, `rms`, `backend`. Retention
defaults to 7 days (`debug_retention_days`).

## Tests (CI, no mic/cloud)

```bash
uv run pytest tests/test_custom_triggers.py tests/test_custom_wake_e2e.py -q
```

Coverage:

- Config resolution (`extra_trigger_phrases` / `trigger_phrases`)
- `TextProbeBackend` scoring `TXT:start prompt …` (mock PCM)
- One ambient cycle: custom wake → mocked `run_listen` → `ambient.prompt`
- `apply_config_reload` hot phrase update + engine rebuild
- Ambient loop: reload flag → `ambient.reloaded` → custom wake → prompt

## Related

- `src/hark/config.py` — `resolve_activation_phrases`
- `src/hark/wake.py` — match + backends
- `src/hark/ambient.py` — loop, SIGHUP apply
- `src/hark/lifecycle.py` — `request_reload` / SIGHUP handler
- Skill note: `skill/hark/SKILL.md` (Ambient bullet)

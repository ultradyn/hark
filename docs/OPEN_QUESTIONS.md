# Open questions

## Resolved

| Topic | Decision |
|-------|----------|
| Name | **Hark** / CLI **`hark`** / skill **`hark`** |
| Skill alias | **`handsfree`** at `skill/handsfree/` (full alias) |
| Mode | **A primary**; **Mode A only for v1** |
| `harkd` | **Not in v1** — scaffold + boundary in [HARKD.md](HARKD.md); Mode A remains product path |
| Repo path | **`/home/xertrov/src/grok/hark`** |
| Orchestrator | Local, outside Herdr |
| Sessions | Multi; local + SSH |
| Confirm | auto for R0/R1; **always** for R2/R3 |
| Menus | `hark keys` / `answer --keys` |
| Done | Wake + agent judgment via short context |
| xAI auth | Grok Build OAuth first |
| Dev | `uv run` from latest checkout |
| Local ML | No neural STT/TTS |
| Prior specs | Merged — see PRIOR_ART.md |

## Still open (optional)

| # | Question | Default if silent |
|---|----------|-------------------|
| R3 | Default announce for `done` when judgment says finished? | Quiet unless agent chooses TTS |
| R4 | SQLite delivery store in Python v1? | JSONL under `~/.local/state/hark/` is enough first |

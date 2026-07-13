# assets/tts

| Path | Role |
|------|------|
| `{voice_id}/…` | **Runtime cache** — content-hashed spoken clips for that voice (e.g. `eve/`). Safe to delete; regenerated on use. |
| `samples/` | **Curated comparison set** — same phrase, organized by provider/gender for setup dogfood. See [samples/README.md](samples/README.md). |

Do not put ad-hoc dated folders at this level.

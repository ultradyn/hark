# Herdr wire fixtures

Place captured Herdr CLI / socket JSON here for contract tests (Python + Rust clients).

## Suggested layout

```text
fixtures/herdr/
  agent-list-empty.json       # no agents
  agent-list-blocked.json     # one blocked agent with question
  agent-list-working.json
  watch-stream-blocked.jsonl  # multi-line watch feed sample
```

Capture with:

```bash
# when herdr is running
herdr agent list --json > fixtures/herdr/agent-list-$(date +%Y%m%d).json
# or via hark scripts
./scripts/capture-herdr-schema.sh
```

Strip secrets / local paths before committing. Prefer redacted real captures over hand-written mocks when possible — wire quirks are the point.

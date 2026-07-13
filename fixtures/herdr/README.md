# Herdr wire fixtures

Redacted real captures of Herdr CLI / socket wire shapes for contract tests
(Python now; Rust client later). Prefer refreshing from a live session over
hand-written mocks — wire quirks are the point.

## Layout

```text
fixtures/herdr/
  agent-list-empty.json        # { result: { agents: [] } }
  agent-list-blocked.json      # ≥1 agent_status=blocked (redacted live)
  agent-list-working.json      # ≥1 agent_status=working (redacted live)
  agent-list-idle-sample.json  # small idle subset
  agent-list-mixed.json        # full session snapshot (redacted)
  watch-stream-blocked.jsonl   # redacted HEP lines from watch.jsonl (+ meta)
  watch-stream-hep.jsonl       # same as watch-stream-blocked (alias)
  watch-stream-wire.jsonl      # herdr socket notification envelopes
```

## Capture / refresh

```bash
# Live agent list → redacted fixtures (requires running herdr)
./scripts/capture-herdr-fixtures.sh

# Or manually:
herdr agent list > /tmp/herdr-agent-list-raw.json
# then run the capture script, which redacts /home/<user> → /home/operator
```

Watch HEP samples are taken from `~/.local/state/hark/watch.jsonl` when present.
Wire envelopes in `watch-stream-wire.jsonl` match shapes consumed by
`hark.watch._handle_lifecycle_event` / subscribe loop.

## Redaction rules

- Home directories → `/home/operator/...`
- No API keys / tokens (lines matching common secret patterns are dropped or truncated)
- Long `question` fields in HEP samples truncated

## Contract tests

```bash
uv run pytest tests/test_herdr_fixtures.py -q
```

Parser under test: `hark.herdr.client.parse_agent_list`.

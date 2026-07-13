# Hark fixtures (Python ↔ Rust parity)

Golden corpora for ingestion, phrase matching, HEP events, and wake audio.
Use the same files from Python tests and a future Rust client so bugs show up as
shared fixture failures rather than “works in one language only.”

## Layout

```text
fixtures/
  README.md                 # this file
  MANIFEST.json             # catalog + sha256 of key artifacts
  text/                     # pure text goldens (no audio, no network)
    wake_match.jsonl        # activation / fuzzy wake
    radio_end.jsonl         # radio end/cancel phrases
    fingerprint.jsonl       # question fingerprint stability
    risk.jsonl              # risk classification
  voice/
    README.md               # how to record more clips
    wake/
      cases.jsonl           # case index (links text + wav)
      live/*.wav + *.json   # real Wave captures + vosk sidecars
  events/
    hep/                    # Hark Event Protocol v1 samples
    syslog/                 # internal system.jsonl-shaped samples
  usage/
    sample.jsonl            # tts/stt usage ledger samples
  herdr/                    # external Herdr wire captures (fill when available)
```

## What each family is for

| Family | Tests | Rust client use |
|--------|-------|-----------------|
| `text/*` | Deterministic phrase/risk/fp logic | Port matchers; assert same outcomes |
| `voice/wake` | Fuzzy wake on **vosk_text**; optional offline STT later | Ingest snip meta; same match rules |
| `events/hep` | Schema shape, partial HOLD, stream supersede | Event ingest / bus consumers |
| `events/syslog` | Timeline wake→prompt | Optional internal log parsers |
| `usage` | Ledger fields | Metrics parity |
| `herdr` | Agent list / watch wire | Socket/CLI client parity |

## Conventions

- **JSONL**: one JSON object per line, UTF-8, no trailing comments.
- **Stable ids**: every golden case has an `id` string; tests fail by id.
- **Expect fields** use `expect_*` prefixes so loaders stay simple.
- **Audio**: 16 kHz mono PCM WAV (ambient/vosk native). Keep clips short (~2.5 s).
- **Privacy**: fixtures are operator voice from local dev; do not add third-party recordings without consent. Prefer redacted Herdr payloads.

## Running Python parity tests

```bash
uv run pytest tests/test_fixtures_parity.py -q
```

## Exporting fresh captures from live state

```bash
./scripts/export-fixtures.sh
# optional: also copy today's wake snips
./scripts/export-fixtures.sh --with-wake
```

Default sources:

- `~/.local/state/hark/{system,ambient,watch,usage}.jsonl`
- `~/.local/state/hark/debug/wake/<YYYY-MM-DD>/`

Exports **add/refresh** under `fixtures/`; they do not delete hand-curated goldens in `fixtures/text/`.

## Rust sketch

```rust
// Pseudocode — keep field names identical to Python loaders
for line in include_str!("../../fixtures/text/wake_match.jsonl").lines() {
    let case: WakeCase = serde_json::from_str(line)?;
    let hit = match_activation(&case.input, case.anywhere);
    assert_eq!(hit.is_some(), case.expect_match, "{}", case.id);
}
```

Prefer reading these fixtures as **data**, not re-encoding expectations in both codebases.

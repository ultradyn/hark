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
    README.md               # capture method + B071 eval harness notes
    wake/
      cases.jsonl           # eval index (live / derived / text-only)
      live/*.wav + *.json   # real Wave captures + vosk sidecars
      derived/*.wav         # noise/gain/pad/silence variants (B071)
  events/
    hep/                    # Hark Event Protocol v1 samples
    syslog/                 # internal system.jsonl-shaped samples
  usage/
    sample.jsonl            # tts/stt usage ledger samples
  herdr/                    # redacted Herdr agent-list + watch wire (B005)
```

## What each family is for

| Family | Tests | Rust client use |
|--------|-------|-----------------|
| `text/*` | Deterministic phrase/risk/fp logic | Port matchers; assert same outcomes |
| `voice/wake` | Fuzzy wake on **vosk_text**; offline Vosk/Sherpa hit-miss-FA eval (B071) | Ingest snip meta; same match rules |
| `events/hep` | Schema shape, partial HOLD, stream supersede | Event ingest / bus consumers |
| `events/syslog` | Timeline wake→prompt | Optional internal log parsers |
| `usage` | Ledger fields | Metrics parity |
| `herdr` | Agent list / watch wire (redacted live) | Socket/CLI client parity (`parse_agent_list`) |

**Media ducking (I002 / B044–B047):** realistic `pactl list sink-inputs` blobs
live as inline fixtures in `tests/test_media.py` (Spotify playing, corked
browser, muted VLC, Hark paplay/ffplay, conference Zoom, etc.) — not under
`fixtures/` — so unit tests mock subprocess without live Pulse/PipeWire.

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

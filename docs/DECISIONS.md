# Architecture decisions — Hark

## ADR-001: Product name Hark

CLI `hark`, optional `harkd` (post-v1), skills **`hark`** + alias **`handsfree`**, config `~/.config/hark/`.  
Repo path: `/home/xertrov/src/grok/hark`.

## ADR-002: Mode A primary; library owns safety; no harkd in v1

Supervisory agent outside Herdr is the default operator. **v1 ships Mode A only.**  
**Routing safety** (fingerprint, revision, mic lease, idempotent send) lives in the shared library so Mode B/`harkd` can reuse it later. LLMs do not pick opaque target IDs.

## ADR-003: Multi-session first-class

Local + remote Herdr sessions merge into one HEP feed with `session_id`.

## ADR-004: No local neural speech models

Cloud STT/TTS only. Local DSP (gate/VAD/resample) allowed.

## ADR-005: Confirm policy split by risk

R0/R1: confirm when unsure. R2/R3: always. Conservative classification.

## ADR-006: Socket-first Herdr; poll fallback

Subscribe when capable; poll otherwise. Capability probe over hard-coded protocol numbers alone.

## ADR-007: Bound delivery preferred

`hark answer <event_id>` over freeform `reply` for production loops.

## ADR-008: Event-driven listening only (MVP)

No continuous ambient cloud transcription. Optional wake-prefix later.

## ADR-009: Half-duplex + post-TTS guard

No barge-in in v1. Echo text rejection as backup.

## ADR-010: Python prototype → Rust production

Prior specs + this project agree. Dev: always latest checkout via `uv run`.

## ADR-011: Pane content untrusted

Speak and route; never execute. Supervisory agents must not treat pane text as human auth.

## ADR-012: No Playwright STT in v1

API/OAuth only for production speech.

## ADR-013: Prior art folded, not dual-tracked

hvb / herdr-voice specs are historical; Hark SPEC is authoritative going forward ([PRIOR_ART.md](PRIOR_ART.md)).

## ADR-014: Optional radio-style listen end (global config)

Operators who think aloud with long pauses need the mic to stay open until an explicit end phrase (like radio “over”), not until the first silence / Smart Turn.

- Config: `[listen] end_mode = "radio"` in `~/.config/hark/config.toml`  
- Env: `HARK_LISTEN_END_MODE`  
- CLI: `hark listen|ask --end-mode radio` (overrides)  
- Default remains `silence` for short answers.  
- End phrases are stripped; cancel phrases abort (exit 7).  
- Hard `max_listen_s` always caps capture.  
See [AUDIO_DESIGN.md](AUDIO_DESIGN.md).  

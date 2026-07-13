# I002 — TTS/STT ducking when music or other media is playing

Planning intake for idea **I002**. Implementation is split across **B044–B047**.
This document is design only — do not treat as shipped behavior.

## Problem

Operators often run music/podcasts while Mode A is armed. Today:

- TTS speaks at full level over media (hard to hear; socially disruptive).
- STT capture hears media bleed (false gate opens, empty/noisy transcripts).
- **B017 conference hold** already queues full questions during Zoom/Teams/etc.,
  but that is **hold**, not **duck**, and targets conference apps — not Spotify.

## Goals

1. Detect **non-conference media** playback (Pulse/PipeWire; optional MPRIS).
2. While Hark **TTS** plays: temporarily lower other sink-input volumes, then restore.
3. While Hark **listens** (answer window / post-wake STT): same duck (optional MPRIS pause).
4. Config kill-switches, docs, doctor readiness.

Non-goals for this epic slice:

- Biometric voice isolation or adaptive noise cancellation models.
- Changing default sink / master volume.
- Pausing conference apps (B017 remains authoritative).
- Fully implementing ducking in the I002 planning PR (decomposition only).

## Current audio paths (as of plan)

| Path | Module | Notes |
|------|--------|--------|
| TTS play | `speech.speak` → `audio.playback.play_audio` | sounddevice / ffplay / paplay; **no volume gain** on TTS bytes |
| Mic mute during TTS | `audio.mic_mute` | `pactl set-source-mute` (Wave ring) |
| Cues | `audio.cues` | `cue_volume` only for beeps |
| Conference | `conference.py` | proc + sink-input **name** match → hold TTS |
| Capture | `audio.capture` + `speech` listen | energy gate; no media awareness |

## Design

### Precedence

```text
conference active + hold_during_conference?
  yes → B017 hold / chime / queue (no media duck fight)
  no  → media ducking (I002) if enabled and sink-inputs RUNNING
```

Fail-open everywhere: missing `pactl` / parse errors → behave as today.

### Detection (B044) — shipped foundation

Module: `src/hark/audio/media.py` (`hark.audio.media`):

- Parse `pactl list sink-inputs` for index, volume, mute, corked, application.name / media.name.
- Duckable = RUNNING (or not corked), not muted, not Hark-owned (ffplay/paplay/hark/sounddevice when attributable).
- Optional: `playerctl` / MPRIS PlaybackStatus=Playing as a secondary signal.
- Conference app names stay in B017; ducking callers skip when conference hold would apply
  (`filter_duckable(..., exclude_conference=True)` / precedence in AUDIO_DESIGN).
- Public: `MediaMatch`, `is_media_active(cfg) -> MediaMatch`, `duckable_indices_and_volumes`.

### Duck during TTS (B045)

```text
with duck_media(level=cfg.audio.duck_level):
    play_wav_bytes(...)
```

- Snapshot per-input volume → `pactl set-sink-input-volume N <pct>%` → restore in `finally`.
- Default **on**: `duck_media_during_tts = true`, `duck_level ≈ 0.2`.
- Log `media.ducked` / restore counts to syslog for dogfood.

### Duck during STT (B046) — shipped

- Arm duck in `run_listen` for the full answer-window / post-wake capture; restore
  on end/cancel/timeout/exception.
- Default **on** for answer/post-wake listen; **not** for continuous idle Vosk wake.
- `pause_media_during_stt` (MPRIS Pause/Play), default **on** (user refinement / dogfood).
- Callers pass `enabled` / `pause_players` explicitly (do not inherit TTS defaults).

### Config / docs (B047)

```toml
[audio]
duck_media_during_tts = true
duck_media_during_stt = true
duck_level = 0.15
pause_media_during_stt = true   # dogfood default on (plan originally said false)
# duck_exclude_apps = ["easyeffects"]  # optional
```

Document in `docs/AUDIO_DESIGN.md`; doctor notes when pactl missing.

## Work items

| ID | Title | Est | Depends |
|----|-------|-----|---------|
| **B044** | Detect active media playback (Pulse/PipeWire + optional MPRIS) | 2h | — |
| **B045** | Duck other media volume during TTS playback | 3h | B044 |
| **B046** | Duck or pause media during STT capture windows | 3h | B044 |
| **B047** | Config, docs, and doctor checks for media ducking | 2h | B045, B046 |

Total ~10h (matches I002 estimate).

## Risks

| Risk | Mitigation |
|------|------------|
| Stuck low volume if process killed mid-duck | restore in `finally`; optional boot-time "no-op" (document operator `pactl` recovery) |
| Ducking conference streams while call active | conference hold first; exclude conference apps from duck list when match |
| Attributing Hark's own sink-input | exclude by app name + short time window around play |
| PipeWire without pactl | fail-open; doctor warns |

## Validation strategy

- Fixture-based unit tests for pactl blob parsing and volume math.
- Mock subprocess for set/restore; no live Spotify required in CI.
- Manual dogfood: music + `hark tts` / ambient ask.

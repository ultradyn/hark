# Audio, gating, and turn-taking

Adapted from prior `AUDIO_DESIGN.md` for Hark.

## Practical selectivity (not biometrics)

- close-talk headset / lapel / directional mic (recommended);  
- **answer windows** only after Hark asks (or after ambient activation);  
- mute/discard during TTS;  
- adaptive noise floor + speech hysteresis;  
- activation phrases for ambient (`hey hark` / `hey herald`);  
- **product-scoped** control phrases (no casual “cancel that” defaults);  
- risk-based confirmation.

## Capture pipeline (answer window)

```text
device
  → resample to 16 kHz mono PCM16
  → adaptive noise-floor (gate closed only)
  → energy gate + hangover
  → pre-roll
  → utterance → cloud STT
```

## Ambient pipeline (not answering a blocked agent)

```text
device
  → 2–3 s rolling snippets (local only)
  → tiny local model / vosk (NO cloud)
  → match activation phrase?
        no  → discard snippet
        yes → optional readiness cue
            → cloud STT for prompt body
            → same end_mode as [listen]
```

## Control phrase policy

**Defaults must not fire on ordinary technical speech.**

| Role | Default examples | Avoid as defaults |
|------|------------------|-------------------|
| End (radio) | `okay hark send`, `end prompt`, `hark over` | mid-clause bare `over`, `stop` |
| Cancel | `hark cancel`, `abort hark send` | `cancel that`, `never mind` |
| Activation | `hey hark`, `hey herald` | bare `hark` mid-sentence |

Operators may add casual phrases if they accept false triggers.

## End modes (`[listen]`)

| `end_mode` | Behavior |
|------------|----------|
| **`silence`** (default) | Energy gate + end-silence; optional Smart Turn (see [ENDPOINTING.md](ENDPOINTING.md)) |
| **`radio`** | Keep listening through long pauses until end phrase |

Silence-mode turn detection is pluggable via `listen.endpoint_strategy`
(`energy` default / `smart_turn` optional). See [ENDPOINTING.md](ENDPOINTING.md)
for the evaluation, the strategy seam, and config.

```toml
[listen]
end_mode = "radio"
end_phrases = ["okay hark send", "end prompt", "hark over"]
cancel_phrases = ["hark cancel", "cancel hark", "abort hark send"]
strip_phrase = true
max_listen_s = 300
# Quiet before interim STT / ambient.partial (radio only; does not finalize)
radio_partial_silence_s = 0.6
stream_partials = true
# Soft informal closers — DEFAULT ON (B039 radio dogfood; see soft end below)
soft_end_phrases_enabled = true
```

Env: `HARK_LISTEN_END_MODE=radio`. Disable soft end with
`soft_end_phrases_enabled = false` or `HARK_SOFT_END_PHRASES_ENABLED=0`.

### Radio partial cadence vs silence end

| Config | Mode | Default | Role |
|--------|------|---------|------|
| `end_silence_s` | **silence** only | 2.1 s | Quiet that **ends** the answer window |
| `radio_partial_silence_s` | **radio** only | 0.6 s | Quiet that ends a **segment** → cloud STT → optional `ambient.partial` (HOLD) |
| `radio_end_silence_s` | legacy | 2.5 s | Kept for config BC; segment cadence is `radio_partial_silence_s` |
| `stream_partials` | radio | `true` | Emit interim events when segment text grows |

Radio **never** finalizes on silence alone. After each short quiet, Hark runs STT on
accumulated audio: if an end/cancel/soft phrase hits, the stream finalizes; otherwise
(with `stream_partials`) it emits a partial and keeps listening. Shorter
`radio_partial_silence_s` → more frequent partials for Mode A; raise it (e.g. 1.0–1.5)
to cut STT cost when pauses are long. Do **not** lower `end_silence_s` to chase radio
partials — that would change normal silence-mode answer windows.

### Soft end phrases (default on)

Mode A agents can always finish a radio capture from partials with
`hark listen-end`. By default, Hark itself also auto-finishes on a **small
set** of informal closers without agent intervention (radio dogfood).

| Config | Default | Meaning |
|--------|---------|---------|
| `soft_end_phrases_enabled` | `true` | Master switch (on by default; set `false` for product phrases only) |
| `soft_end_phrases` | built-in safe list | Override/replace the default soft list |
| Env `HARK_SOFT_END_PHRASES_ENABLED` | unset | `1`/`true`/`yes`/`on` enables; `0`/`false`/`no`/`off` disables |

**Matching rules (must all hold):**

1. Radio mode only (evaluated after each segment; segment ends on
   `radio_partial_silence_s` quiet — trailing silence required).
2. Phrase is **utterance-final**: whole transcript equals the phrase, or the
   phrase is a word-bounded suffix after normalize + trailing punct strip.
3. Cancel and product `end_phrases` always win over soft phrases.
4. Mid-clause text does **not** match — e.g. `"that's all I know about X"`
   never finishes on `"that's all"`, and `"please just send it to production"`
   never finishes on `"send it"`.
5. Bare **`over`** is **sentence-final** as well as utterance-final: the sole
   utterance `"over"`, or a suffix after sentence-ending punctuation
   (`". over"`, `"! over"`, `"? over"`). Word-final but not sentence-final
   forms such as `"turn it over"` / `"hand it over"` do **not** finish.
   Mid-clause `"over the weekend"` / `"think it over and continue"` never finish.

**Default soft list:**

| Phrase | Notes |
|--------|--------|
| `send it` / `send that` | Bare radio-style send (B039); terminal only |
| `okay send it` / `ok send it` / `okay send` / `ok send` | Multi-word send variants |
| `that's all` / `that is all` / `thats all` | Common closer; apostrophe variants for STT |
| `end of message` / `end message` | Explicit message terminator |
| `end of transmission` | Radio-style formal closer |
| `over and out` | Multi-word radio closer (no sentence-punct required) |
| bare `over` | **Sentence-final only** (sole utterance or after `.`/`!`/`?`) |

**Not in the default list (unsafe / high false-finish risk):**

| Phrase | Why excluded |
|--------|----------------|
| `done` / `i'm done` | Mid-thought pauses after partial work |
| `that's it` | `"that's it for the migration"` after a pause |
| `finished` / `go` / `go ahead` | Too common mid-speech |
| `cancel that` | Cancel semantics — use product cancel phrases |

Residual risk when soft end is on: if the operator pauses *right after* a
terminal soft closer mid-thought (e.g. says `"please just send it"` then
stops before `"to production"`), radio may finalize. Use product phrases
(`okay hark send`) or set `soft_end_phrases_enabled = false` for stricter
control. Mode A agents may still call `hark listen-end` from partials.

## Ambient (`[ambient]`)

```toml
[ambient]
enabled = false
activation_phrases = ["hey hark", "hey herald", "okay hark"]
engine = "vosk"          # or text_probe for tests
# model_path = "/path/to/vosk-model-small-en-us"
snippet_s = 2.5
# One-shot wake wait / continuous idle cycle length (seconds).
# 0 = wait indefinitely (no ambient.timeout cycle).
timeout_s = 300
# Emit ambient.timeout on continuous idle cycles (NDJSON + syslog).
# Default on — useful as a heartbeat when watching provider cache / dogfood.
# Set false to quiet long-running Mode A (still re-enters the wake wait).
surface_timeouts = true
# emit_timeout_events = true  # alias of surface_timeouts
```

| Key | Default | Notes |
|-----|---------|--------|
| `timeout_s` | `300` | One-shot: max wait for a wake before `ambient.timeout`. Continuous Mode A: idle cycle length before re-entering the wake wait (and optionally emitting `ambient.timeout`). `0` = no deadline / no timeout event. |
| `surface_timeouts` | `true` | When **on**, continuous ambient surfaces `ambient.timeout` each idle cycle (monitor NDJSON + syslog) as a heartbeat. When **off**, continuous idle cycles stay quiet (no timeout event) — turn off for noisy long-running Mode A; leave on if you want cache-warmup / liveness visibility. Alias: `emit_timeout_events`. One-shot `hark ambient --once` always emits timeout when nothing is heard. |

CLI: `hark ambient` (forces a wake+listen cycle). Continuous: `hark ambient` without `--once`.

## Half-duplex sequence (answer)

1. Speak question (TTS).  
2. Post-TTS guard.  
3. Arm capture.  
4. Endpoint (silence or radio phrase) → STT → confirm if needed → deliver.  

Default remains **half-duplex**: capture starts only after TTS exits the mic-mute
context. `listen_pre_arm_ms` fires a near-end signal so the sequential listen can
skip/tighten `post_tts_guard_ms`, but the InputStream still opens after play.

### Optional overlap pre-arm

When low handoff latency matters more than strict half-duplex:

```toml
[audio]
listen_pre_arm_ms = 300
overlap_prearm = true        # default false — keep half-duplex
overlap_discard_ms = 150     # drop audio until TTS ends + this many ms
```

With `overlap_prearm = true`, capture starts near TTS end (same near-end timer).
While TTS is still finishing (and often while the mic is still muted), frames are
**discarded**. After TTS ends, another `overlap_discard_ms` of audio is dropped
so residual acoustic echo does not open the energy gate or reach STT. Speech
after the discard window is kept as usual.

## False-trigger defenses

- No cloud STT outside answer window or post-activation.  
- Product-scoped control lexicon by default.  
- Min speech duration; filler discard; TTS echo overlap reject.  
- One mic lease at a time.  

## Privacy

- Wake snippets processed locally; not uploaded.  
- Delete raw audio after STT unless debug capture enabled.  

## Conference hold vs media ducking

When a **conference** app is active (Zoom/Teams/Meet/…), **B017 conference hold**
wins: full TTS is held/chimed/queued (`hold_during_conference`) rather than
fighting the call with ducking. Media ducking (I002 / B044–B047) applies only
when conference hold does **not** take the path — i.e. music/podcasts and other
non-call sink-inputs.

Detection lives in `hark.audio.media` (`is_media_active` → `MediaMatch`): Pulse/
PipeWire **sink-inputs** (RUNNING / Corked=no; excludes Hark’s own ffplay/paplay
streams) plus optional MPRIS (`playerctl`). Fail-open if `pactl` is missing.
Conference streams may still appear in the match; callers must prefer B017 hold
over duck.

**TTS ducking (B045):** when `audio.duck_media_during_tts` is true (default),
`run_tts` wraps playback with `duck_media(...)`: snapshot non-Hark sink-input
volumes → lower each to `prior * duck_level` (default **0.15**) via
`pactl set-sink-input-volume` → always restore in `finally`. Optional
`pause_media_during_tts` pauses Playing MPRIS players (`playerctl -p NAME pause`)
then ducks remaining sources, and resumes those players on exit. Kill-switch:
`duck_media_during_tts = false`. Does **not** change default sink / master volume.
STT-window ducking is B046 (same `duck_media` primitive). Design detail:
[plans/I002-media-ducking.md](plans/I002-media-ducking.md).

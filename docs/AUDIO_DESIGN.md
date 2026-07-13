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

### Arm cue (record-start beep)

After TTS for `hark ask` / `tts --listen` / confirm, the **record-start beep plays when
listen is armed** — not when the energy gate first opens. That way the operator
knows they can speak without waiting through silence. Leading silence is still
trimmed from STT content.

| Knob | Default | Role |
|------|---------|------|
| `[audio] answer_arm_cue` | `true` | Beep when answer-window listen is ready (silence **and** radio) |
| `[ambient] post_wake_arm_cue` | `true` | Same after ambient wake → post-wake listen |

With arm cue on, speech-open only logs (`listen.speech_opened`); it does **not**
double-beep. With arm cue off, record-start still plays once when speech opens.

**Dogfood:** use the checkout (`uv run hark`) or `uv tool install -e .` so the
CLI matches repo arm-cue behaviour; a stale `uv tool` site-packages install can
still wait for speech before beeping.

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
| Soft end (radio, default on) | sentence-final `over`, `okay over`, `send it`, `that's all`, `over and out` | mid-clause `over the weekend`, `send it to staging` |
| Cancel | `hark cancel`, `abort hark send` | `cancel that`, `never mind` |
| Activation | `hey hark`, `hey herald` | bare `hark` mid-sentence |

Operators may add casual phrases if they accept false triggers.

### How to end radio (operator-facing)

When listen is in **radio** mode, the mic stays open through short thinking
pauses. Finish a turn with any of:

| Say… / do… | Kind |
|------------|------|
| **`okay hark send`** / **`hark over`** / **`end prompt`** | Product end (always on) |
| **`over`** (after a sentence / alone) / **`okay, over`** / **`okay over`** | Soft end (default on) |
| **`send it`**, **`that's all`**, **`over and out`**, **`message done`** | Soft end (default on) |
| Stay quiet ~**6.3 s** after you have started speaking | Idle auto-finish (default 3× `end_silence_s`; B074) |
| **`hark cancel`** | Abort without using the transcript |

Mode A agents also **must** run `hark listen-end` when a partial clearly shows
you are done (backup if soft-end misses). Skill bootstrap reminds operators:
*“when you’re done, say over or okay hark send.”*

## End modes (`[listen]`)

| `end_mode` | Behavior |
|------------|----------|
| **`silence`** (default) | Energy gate + end-silence; optional Smart Turn (see [ENDPOINTING.md](ENDPOINTING.md)) |
| **`radio`** | Keep listening through short pauses until end phrase or post-speech idle |

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
# After speech opens: continuous quiet → auto-finish (default 3× end_silence_s)
# radio_idle_end_silence_s = 6.3
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
| `radio_idle_end_silence_s` | **radio** answer only | **3× `end_silence_s`** (~6.3 s) | After speech has opened at least once, continuous quiet this long **auto-finishes** (soft-end path, not cancel). Before first open: no-op (initial timeout / nudges) |
| `radio_end_silence_s` | legacy | 2.5 s | Kept for config BC; segment cadence is `radio_partial_silence_s` |
| `stream_partials` | radio | `true` | Emit interim events when segment text grows |

Radio does **not** finalize on short silence alone. After each short quiet
(`radio_partial_silence_s`), Hark runs STT on accumulated audio: if an
end/cancel/soft phrase hits, the stream finalizes; otherwise (with
`stream_partials`) it emits a partial and keeps listening. **After speech has
opened**, if the operator stays quiet longer than `radio_idle_end_silence_s`
(~6.3 s by default), the answer window auto-finishes so forgotten radio closers
do not leave the mic open indefinitely (B074). Short thinking pauses (~2 s)
remain open. Before the first open, long quiet still uses the existing initial
timeout / nudge path. Shorter `radio_partial_silence_s` → more frequent
partials for Mode A; raise it (e.g. 1.0–1.5) to cut STT cost when pauses are
long. Do **not** lower `end_silence_s` to chase radio partials — that would
change normal silence-mode answer windows.

### Soft end phrases (default on)

Mode A agents **must** finish a radio capture from partials with
`hark listen-end` when the operator clearly ended (done-signal backup).
By default, Hark itself also auto-finishes on a **small set** of informal
closers without agent intervention (radio dogfood).

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
   (`". over"`, `"! over"`, `"? over"`, `", over"` — comma is a soft boundary
   so `"okay, over"` works). Word-final but not sentence-final forms such as
   `"turn it over"` / `"hand it over"` do **not** finish. Mid-clause
   `"over the weekend"` / `"think it over and continue"` never finish.
6. Multi-word **`okay over`** / **`ok over`** match without sentence punct
   (STT often drops the comma in “okay, over”).

**Default soft list:**

| Phrase | Notes |
|--------|--------|
| `send it` / `send that` | Bare radio-style send (B039); terminal only |
| `okay send it` / `ok send it` / `okay send` / `ok send` | Multi-word send variants |
| `that's all` / `that is all` / `thats all` | Common closer; apostrophe variants for STT |
| `end of message` / `end message` | Explicit message terminator |
| `end of transmission` | Radio-style formal closer |
| `message done` | Informal “I'm finished dictating” closer |
| `over and out` | Multi-word radio closer (no sentence-punct required) |
| `okay over` / `ok over` | STT of “okay, over” when comma is dropped |
| bare `over` | **Sentence-final only** (sole utterance or after `.`/`!`/`?`/`,`) |

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
control. Mode A agents **must** call `hark listen-end` from partials when a
done signal is clear and soft-end did not already finalize.

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
| `engine` | `vosk` | Local wake ASR. `text_probe` for tests. Future optional KWS engines (B070) do not change this default. |
| `model_path` | auto / setup | Directory of an unpacked Vosk model. `./scripts/setup-ambient.sh` installs the **small** default. |
| `timeout_s` | `300` | One-shot: max wait for a wake before `ambient.timeout`. Continuous Mode A: idle cycle length before re-entering the wake wait (and optionally emitting `ambient.timeout`). `0` = no deadline / no timeout event. |
| `surface_timeouts` | `true` | When **on**, continuous ambient surfaces `ambient.timeout` each idle cycle (monitor NDJSON + syslog) as a heartbeat. When **off**, continuous idle cycles stay quiet (no timeout event) — turn off for noisy long-running Mode A; leave on if you want cache-warmup / liveness visibility. Alias: `emit_timeout_events`. One-shot `hark ambient --once` always emits timeout when nothing is heard. |

CLI: `hark ambient` (forces a wake+listen cycle). Continuous: `hark ambient` without `--once`.

### Larger Vosk models (optional `model_path`)

**Default stays small.** Shipping / `setup-ambient.sh` use
`vosk-model-small-en-us-0.15` (~40 M zip / ~68 M installed, RSS ~150 MiB on a
laptop probe). Operators can point `ambient.model_path` at a larger official
English model for a **config-only quality experiment** — not the long-term wake
architecture (see B069 survey; dedicated KWS is B070).

| Model | Zip (approx) | Role | Trade-offs |
|-------|--------------|------|------------|
| `vosk-model-small-en-us-0.15` | ~40 M | **Default** always-on wake | Lowest disk/RAM; weak on rare product names → **aliases + learning** |
| `vosk-model-en-us-0.22-lgraph` | ~128 M | Middle ground (dynamic graph) | Better generic WER; higher RSS/CPU than small; still open-vocab ASR |
| `vosk-model-en-us-0.22` | ~1.8 G | Server-class English | Best Vosk WER of the three; multi‑GB download + multi‑GB RAM; slowest to fetch |

**Still needs aliases.** Larger models improve generic English; they do **not**
reliably invent tokens like `hark` / `herald`. Keep seed mishears and
`learn_from_near_misses` (see [`CUSTOM_WAKE.md`](CUSTOM_WAKE.md)). Do not expect
big Vosk alone to replace keyword spotting.

Download (optional helper; default model unchanged):

```bash
# middle ground (~128M)
./scripts/download-vosk-model.sh --model lgraph
# server-class (~1.8G) — large download
./scripts/download-vosk-model.sh --model 0.22 --method curl
```

Then set config (file-watch / SIGHUP reloads `model_path` and rebuilds the backend):

```toml
[ambient]
engine = "vosk"
# after: ./scripts/download-vosk-model.sh --model lgraph
model_path = "~/.local/share/hark/models/vosk-model-en-us-0.22-lgraph"
```

Sources: [Vosk models table](https://alphacephei.com/vosk/models), survey
[`docs/plans/B069-local-stt-survey.md`](plans/B069-local-stt-survey.md).

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

```text
conference active + hold_during_conference?
  yes → B017 hold / chime / queue (no media duck fight)
  no  → media ducking if enabled and duckable sink-inputs present
```

Detection lives in `hark.audio.media` (`is_media_active` → `MediaMatch`): Pulse/
PipeWire **sink-inputs** (RUNNING / Corked=no; excludes Hark’s own ffplay/paplay
streams) plus optional MPRIS (`playerctl`). Conference streams may still appear
in the match; duck lists use `exclude_conference=True` so callers do not
volume-fight Zoom/Teams while hold is authoritative.

### Fail-open and restore

- Missing **`pactl`**, parse errors, or failed `set-sink-input-volume` → no duck
  (TTS/STT behave as today). Never changes default sink / master volume.
- Prior per-stream volumes (and any MPRIS players we paused) are **always**
  restored in `finally`, including on exception / cancel / timeout.
- Missing **`playerctl`** degrades MPRIS detect/pause only; volume duck still
  works when `pactl` is present.
- `hark doctor` surfaces `pactl` / `playerctl` readiness as a soft warning
  (status `ready` / `degraded` / `disabled`) — never a hard doctor failure.

### TTS ducking (B045)

When `audio.duck_media_during_tts` is true (default), `run_tts` wraps playback
with `duck_media(...)`: snapshot non-Hark sink-input volumes → lower each to
`prior * duck_level` (default **0.15**) via `pactl set-sink-input-volume` →
always restore in `finally`. Optional `pause_media_during_tts` pauses Playing
MPRIS players (`playerctl -p NAME pause`) then ducks remaining sources, and
resumes those players on exit. Kill-switch: `duck_media_during_tts = false`.

### STT ducking (B046)

When `audio.duck_media_during_stt` is true (default), `run_listen` wraps the
full answer-window / post-wake capture with the same `duck_media` primitive,
passing **explicit** STT flags (`enabled=duck_media_during_stt`,
`pause_players=pause_media_during_stt`). Dogfood default:
`pause_media_during_stt = true` (MPRIS Pause + volume duck). Restores on end /
cancel / timeout / exception.

**Not** armed for continuous idle ambient wake (local Vosk half-duplex wake loop
uses `record_seconds` only — never enters `run_listen`). Independent of
`mute_mic_during_tts` (half-duplex: TTS mic mute ends, then listen ducks
separately). Shared: `duck_level`, `duck_exclude_apps`, `media_check_mpris`,
`exclude_conference=True`.

### Config defaults (shipped)

| Key | Default | Role |
|-----|---------|------|
| `duck_media_during_tts` | `true` | Volume duck while TTS plays |
| `pause_media_during_tts` | `false` | MPRIS Pause during TTS (+ duck rest) |
| `duck_media_during_stt` | `true` | Volume duck during answer / post-wake listen |
| `pause_media_during_stt` | `true` | MPRIS Pause during STT (dogfood) |
| `duck_level` | **0.15** | Fraction of prior per-stream volume (not 0.2) |
| `duck_exclude_apps` | `[]` | Extra app name / binary substrings to never duck |
| `media_check_mpris` | `true` | Secondary media signal via `playerctl` |

Env defaults when the TOML key is **absent** (explicit TOML wins):  
`HARK_DUCK_MEDIA_DURING_TTS`, `HARK_PAUSE_MEDIA_DURING_TTS`,
`HARK_DUCK_MEDIA_DURING_STT`, `HARK_PAUSE_MEDIA_DURING_STT`, `HARK_DUCK_LEVEL`,
`HARK_MEDIA_CHECK_MPRIS`.

Example (`hark config init` / `DEFAULT_CONFIG_TOML`):

```toml
[audio]
duck_media_during_tts = true
pause_media_during_tts = false
duck_media_during_stt = true
pause_media_during_stt = true   # dogfood default ON
duck_level = 0.15
# duck_exclude_apps = ["easyeffects"]
media_check_mpris = true
```

Design detail: [plans/I002-media-ducking.md](plans/I002-media-ducking.md).

# Audio, gating, and turn-taking

Adapted from prior `AUDIO_DESIGN.md` for Hark.

## Practical selectivity (not biometrics)

A noise gate cannot prove “only me.” Selectivity comes from:

- close-talk headset / lapel / directional mic (recommended);  
- **listen only after** Hark asks;  
- mute/discard during TTS;  
- adaptive noise floor + speech hysteresis;  
- optional wake phrase (later);  
- risk-based confirmation.

Headset also reduces TTS echo into the mic.

## Capture pipeline

```text
device
  → resample to 16 kHz mono PCM16
  → optional high-pass / light AGC
  → adaptive noise-floor (gate closed only)
  → energy (+ optional classic WebRTC VAD)
  → open confirmation frames + hangover
  → pre-roll ring buffer (~250 ms)
  → bounded utterance → cloud STT
```

No local neural ASR/TTS.

## Suggested defaults (calibrate)

| Parameter | Start |
|-----------|--------|
| Frame | 20 ms |
| Noise-floor time constant | 3–8 s |
| Open margin | 10–16 dB above floor |
| Open confirm | ~160 ms |
| Pre-roll | 250 ms |
| End silence (normal) | ~1.1 s |
| End silence (long_answer) | 2.0–2.5 s |
| Hangover | ~200 ms |
| Max utterance | 120 s |
| Min speech | ~300 ms |
| Post-TTS guard | 250–500 ms |
| Initial response timeout | ~20–45 s |

## Endpointing

Users pause mid-dictation (paths, numbers). Do not end on the first short silence. Prefer provider **Smart Turn** (xAI) when streaming. Support spoken “keep listening” as a meta-command in Mode A/B.

### End modes (`[listen]` in `~/.config/hark/config.toml`)

| `end_mode` | Behavior |
|------------|----------|
| **`silence`** (default) | End utterance on Smart Turn / end-silence hang. Good for short answers. |
| **`radio`** | **Keep listening through long thinking pauses.** Only finalize when the operator speaks an **end phrase** (radio-style “over” / “stop”). Cancel phrases abort without deliver. |

Radio mode is for long, thoughtful replies: you may pause for many seconds (or longer) while formulating; Hark must **not** cut you off on silence alone.

```toml
[listen]
end_mode = "radio"   # or "silence"
end_phrases = [
  "okay send it",
  "ok send it",
  "send it",
  "end prompt",
  "end of prompt",
  "end of message",
  "over",
]
cancel_phrases = [
  "cancel that",
  "never mind",
  "scratch that",
  "abort send",
]
strip_phrase = true      # remove the end phrase from delivered text
max_listen_s = 300       # hard safety cap (always)
# nudge_silence_s = 45   # optional TTS "still listening" after quiet (0 = off)
```

Env override: `HARK_LISTEN_END_MODE=radio`.  
CLI override (when implemented): `hark listen --end-mode radio`, `hark ask --end-mode radio`.

**Matching rules (normative for library):**

1. Case-insensitive; trailing punctuation ignored.  
2. Phrase must appear as a **terminal** segment (end of current transcript), at a word boundary — not mid-sentence.  
3. Longest matching phrase wins (`okay send it` over `send it`).  
4. On **end**: strip phrase (if `strip_phrase`) → that body is the answer transcript.  
5. On **cancel**: exit abort (code 7); do not deliver.  
6. In radio mode, Smart Turn / end-silence **MUST NOT** finalize; they MAY only segment interim STT.  
7. `max_listen_s` always applies (timeout → exit 6).  
8. Prefer multi-word defaults; bare `"stop"` is **not** a default (too many false ends: “please stop the server…”). Operators may add it.

Optional readiness cue when radio mode arms: short TTS or beep “listening — say okay send it when done.”

## Half-duplex sequence (MVP)

1. Speak question (TTS).  
2. Wait for device drain + post-TTS guard.  
3. Optional short readiness cue (beep / “ready”; radio mode may mention end phrase).  
4. Arm capture.  
5. On endpoint (silence/Smart Turn **or** radio end phrase) → STT finalize → (confirm if needed) → deliver.  

Barge-in: later, with AEC.

## False-trigger defenses

- No STT outside answer window (event-driven mode).  
- Min voiced duration + min non-filler tokens.  
- Discard empty / filler-only.  
- Discard high textual overlap with last TTS (echo).  
- Bound concurrent listen (one mic lease).  

## Devices

Store **stable device ids**, not only names. On device loss: cancel capture, keep pending event, never silently switch mics without policy.

## Privacy

- Ring buffer in memory only.  
- Delete raw audio after STT unless debug capture explicitly enabled (with warning + TTL).  

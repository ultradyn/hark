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

## Half-duplex sequence (MVP)

1. Speak question (TTS).  
2. Wait for device drain + post-TTS guard.  
3. Optional short readiness cue (beep / “ready”).  
4. Arm capture.  
5. On endpoint → STT → (confirm if needed) → deliver.  

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

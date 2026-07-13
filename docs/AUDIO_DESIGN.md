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
| End (radio) | `okay hark send`, `end prompt`, `hark over` | `send it`, bare `over`, `stop` |
| Cancel | `hark cancel`, `abort hark send` | `cancel that`, `never mind` |
| Activation | `hey hark`, `hey herald` | bare `hark` mid-sentence |

Operators may add casual phrases if they accept false triggers.

## End modes (`[listen]`)

| `end_mode` | Behavior |
|------------|----------|
| **`silence`** (default) | Smart Turn / end-silence |
| **`radio`** | Keep listening through long pauses until end phrase |

```toml
[listen]
end_mode = "radio"
end_phrases = ["okay hark send", "end prompt", "hark over"]
cancel_phrases = ["hark cancel", "cancel hark", "abort hark send"]
strip_phrase = true
max_listen_s = 300
# Optional informal closers — DEFAULT OFF (see soft end below)
soft_end_phrases_enabled = false
```

Env: `HARK_LISTEN_END_MODE=radio`.

### Soft end phrases (optional, default off)

Mode A agents can always finish a radio capture from partials with
`hark listen-end`. Optionally, Hark itself can auto-finish on a **small
conservative set** of informal closers without agent intervention.

| Config | Default | Meaning |
|--------|---------|---------|
| `soft_end_phrases_enabled` | `false` | Master switch (off by default) |
| `soft_end_phrases` | built-in safe list | Override/replace the default soft list |
| Env `HARK_SOFT_END_PHRASES_ENABLED` | unset | `1`/`true`/`yes`/`on` enables |

**Matching rules (must all hold):**

1. Radio mode only (evaluated after each segment; segment ends on
   `radio_end_silence_s` quiet — trailing silence required).
2. Phrase is **utterance-final**: whole transcript equals the phrase, or the
   phrase is a word-bounded suffix after normalize + trailing punct strip.
3. Cancel and product `end_phrases` always win over soft phrases.
4. Mid-clause text does **not** match — e.g. `"that's all I know about X"`
   never finishes on `"that's all"`.

**Default soft list (safe when terminal-only):**

| Phrase | Notes |
|--------|--------|
| `that's all` / `that is all` / `thats all` | Common closer; apostrophe variants for STT |
| `end of message` / `end message` | Explicit message terminator |
| `end of transmission` | Radio-style formal closer |
| `okay send it` / `ok send it` | Multi-word; not bare `send it` |
| `okay send` / `ok send` | Shorter multi-word send |
| `over and out` | Radio closer; not bare `over` |

**Not in the default list (unsafe / high false-finish risk):**

| Phrase | Why excluded |
|--------|----------------|
| `send it` | Matches `"please just send it"` |
| bare `over` | `"turn it over"`, `"hand over"` |
| `done` / `i'm done` | Mid-thought pauses after partial work |
| `that's it` | `"that's it for the migration"` after a pause |
| `finished` / `go` / `go ahead` | Too common mid-speech |
| `cancel that` | Cancel semantics — use product cancel phrases |

Prefer leaving soft end **off** and using product phrases (`hark send`) or
agent `listen-end` unless you accept residual false-finish risk when the
operator pauses right after an informal closer mid-thought.

## Ambient (`[ambient]`)

```toml
[ambient]
enabled = false
activation_phrases = ["hey hark", "hey herald", "okay hark"]
engine = "vosk"          # or text_probe for tests
# model_path = "/path/to/vosk-model-small-en-us"
snippet_s = 2.5
timeout_s = 300
```

CLI: `hark ambient` (forces a wake+listen cycle).

## Half-duplex sequence (answer)

1. Speak question (TTS).  
2. Post-TTS guard.  
3. Arm capture.  
4. Endpoint (silence or radio phrase) → STT → confirm if needed → deliver.  

## False-trigger defenses

- No cloud STT outside answer window or post-activation.  
- Product-scoped control lexicon by default.  
- Min speech duration; filler discard; TTS echo overlap reject.  
- One mic lease at a time.  

## Privacy

- Wake snippets processed locally; not uploaded.  
- Delete raw audio after STT unless debug capture enabled.  

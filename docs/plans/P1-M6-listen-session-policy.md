# P1.M6 — ListenSessionPolicy (stop ambient leaking into listen)

**Status:** design locked for E1 (implementation E2–E4)  
**Date:** 2026-07-15  
**Backlog:** `P1.M6`  
**Depends on:** M1 Answer Window (policy type already exists as `AnswerWindowPolicy`)  
**Related:** B098 streaming partials, B105 TTS quiet-gate, B108 silence HOLD under streaming, B112 radio idle clamp

## Goal

One **explicit policy object** for every listen / TTS-quiet-gate decision so
`[ambient].streaming` cannot leak into **bound-answer** radio/silence windows
unless the call seam chooses a profile that allows it.

## Naming (M1 alignment)

M1 introduced `AnswerWindowPolicy` + `policy_from_config(cfg, profile)`.
M6 product name is **ListenSessionPolicy**. **They are the same type:**

| Alias | Canonical |
|-------|-----------|
| `ListenSessionPolicy` | `AnswerWindowPolicy` |
| `listen_session_policy_from_config` / `from_config` | `policy_from_config` |
| profiles | `bound_answer`, `post_wake`, `confirm` |

E2 exports aliases; no duplicate dataclass. Session loops and `open_answer_window`
already take the frozen policy — M6 closes remaining **call-seam** ambient
getattr holes (`run_listen` legacy path, TTS quiet-gate, helpers).

## Profiles (defaults)

| Profile | Typical caller | `streaming` default | Arm / gate notes |
|---------|----------------|---------------------|------------------|
| **`bound_answer`** | CLI `hark listen` / `ask`, bound speak-then-listen | **False** (never inherits `[ambient].streaming`) | `arm_cue` from `[audio].answer_arm_cue`; gate from `[listen]` |
| **`post_wake`** | ambient post-wake capture | **`[ambient].streaming`** | softer `post_wake_*` gate/lead-in/arm/nudge from ambient |
| **`confirm`** | confirm short listen | **False** | short bound-like; streaming off |

**Invariant:** bound-answer radio with ambient TOML `streaming=true` still has
`policy.streaming is False`. Ambient path must pass `profile="post_wake"`.

## Field map: config → policy

### From `[listen]` / ListenConfig

| Policy field | Config source | Default |
|--------------|---------------|---------|
| `end_mode` | `listen.end_mode` | silence |
| `max_listen_s` | `listen.max_listen_s` | 120 |
| `abs_open_db` | `listen.abs_open_db` (post_wake may override) | −48 |
| `open_margin_db` | `listen.open_margin_db` | 8 |
| `initial_timeout_s` | `listen.initial_timeout_s` (post_wake may override) | 45 |
| `pre_roll_ms` | `listen.pre_roll_ms` | 300 |
| `no_open_retry` / `no_open_nudge` | `listen.*` | true |
| `empty_stt_retry` / `empty_stt_nudge` | `listen.*` | true |
| `endpoint_strategy_name` | `listen.endpoint_strategy` | energy |
| `smart_turn_*` | `listen.smart_turn_*` | None |
| `endpoint_probe_silence_s` / `endpoint_max_silence_s` | listen | 0.4 / 6.0 |
| `stream_partials` | `listen.stream_partials` | true |
| `radio_partial_silence_s` | listen | 0.6 |
| `radio_segment_overlap_ms` / `pad_ms` | listen | 300 / 250 |
| `radio_idle_end_silence_s` | listen | 0 → 3× end_silence |
| `end_silence_s` | listen | 2.1 |
| `end_phrases` / `cancel_phrases` / `soft_end_*` | listen | module defaults |
| `strip_phrase` | listen | true |

### From `[ambient]` / AmbientConfig (seam only)

| Policy field | When read | Notes |
|--------------|-----------|-------|
| `streaming` | **post_wake only** as default | bound_answer / confirm → always False unless override |
| `streaming_ack_min_quiet_s` | all profiles (numeric default) | used when streaming true |
| `post_wake_abs_open_db` | post_wake | optional override |
| `post_wake_timeout_s` | post_wake | → `initial_timeout_s` |
| `post_wake_lead_in_ms` | post_wake | → `lead_in_ms` |
| `post_wake_arm_cue` | post_wake | → `arm_cue` |
| `post_wake_no_open_nudge` / `_tts` | post_wake | recovery text |

Ambient is read **only inside `policy_from_config`**, never inside radio/silence
session loops.

### From `[audio]` / AudioConfig

| Policy field | Config source | Default |
|--------------|---------------|---------|
| `mute_edge_pad_ms` | `audio.mute_edge_pad_ms` | 300 |
| `duck_media_during_stt` / `pause_media_during_stt` | audio | true / false |
| `arm_cue` (bound/confirm) | `audio.answer_arm_cue` | true |

### Call-seam only (not TOML)

| Policy field | Source |
|--------------|--------|
| `last_tts`, `post_tts_guard_s`, `already_armed` | half-duplex handoff |
| `discard_leading_ms`, `stream_id`, `partial_kind` | radio / ambient |
| `stt_provider` | STT override |
| `suppress_stop_cue` | None → derive from `streaming` |

## Remaining ambient leaks (E3)

| Site | Today | Target |
|------|-------|--------|
| `speech.run_listen` with `profile=None` | forces `streaming` from ambient onto bound_answer | **bound defaults only** (streaming False); callers that need ambient streaming pass `profile="post_wake"` |
| `speech.run_tts` quiet-gate | `getattr(cfg.ambient, "streaming")` | build policy at seam **or** read `streaming` from active listen registration |
| `effective_radio_idle_end_s` | falls back to ambient if kwargs omitted | require explicit streaming kwargs or policy; document seam-only |

## Active-listen streaming flag (E3 TTS)

`register_active_listen(stream_id, mode=…, streaming=policy.streaming)` writes
`streaming` into `active.json`. TTS quiet-gate uses **active capture streaming**
when present (correct for bound HOLD vs ambient streaming radio); falls back to
`policy_from_config(cfg, "post_wake").streaming` only when no active listen
(should not quiet-gate). Prefer: **no active → streaming=False** (HOLD only if
capture active without flag).

## Acceptance (locked)

| ID | Criterion |
|----|-----------|
| AC1 | Field + profile tables in this doc |
| AC2 | `ListenSessionPolicy` alias + `from_config` / `policy_from_config` typed construction |
| AC3 | `bound_answer` streaming False even if ambient.streaming True |
| AC4 | `post_wake` streaming follows ambient when configured |
| AC5 | No `cfg.ambient` reads inside speech listen path after E3 (only policy_from_config seam) |
| AC6 | TTS quiet-gate does not treat bound radio as streaming solely because ambient TOML is on |
| AC7 | Tests AC3 + AC4 (E4) |

## Implementation order

1. **E2.T001** — export aliases; ensure listed fields have no getattr holes in builders  
2. **E2.T002** — profile defaults verified (bound off, post_wake inherits, confirm off)  
3. **E3.T001** — `run_listen` drops legacy ambient streaming override  
4. **E3.T002** — ambient/CLI already profile-based; TTS + active.json streaming  
5. **E4** — tests for bound vs post_wake under ambient.streaming=true  

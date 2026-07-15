# P1.M1 — Deepen the Answer Window

**Status:** design locked for E1 (implementation follows E2–E5)  
**Date:** 2026-07-15  
**Backlog:** `P1.M1` · architecture review candidate 1 (Strong)  
**ADRs in force:** 008 (event-driven answer windows), 009 (half-duplex), 014 / 014b (radio soft-end + prosign)  
**E1.T003:** acceptance criteria + non-goals below are **LOCKED** (2026-07-15) — implement against them; change only via backlog amend + plan edit

## Goal

Collapse `run_listen` and the radio-end shards into one **deep** Answer Window module:

- **Small external interface** — callers learn a profile + sparse overrides, not ~18 kwargs.
- **Large implementation** — radio/silence state machines, segment STT join, partial HEP, control IPC, idle clamps, empty/no-open recovery, echo reject.
- **One test seam** — radio/silence/soft-end/endpointing tests hit `open(policy) → result` (or pure helpers that remain pure).

## Problem (current)

`speech.run_listen` is shallow relative to what callers must learn. The public surface nearly matches every internal knob:

| Surface today | Internal complexity it exposes |
|---------------|--------------------------------|
| `end_mode`, `max_s`, `last_tts`, `post_tts_guard_s`, `already_armed` | Mode branch + half-duplex settle |
| `on_partial`, `stream_id`, `partial_kind` | Radio partial HEP + HOLD vs streaming strings |
| `discard_leading_ms`, `audio_ok_after` | TTS-tail / echo pre-arm |
| `abs_open_db`, `open_margin_db`, `initial_timeout_s`, `lead_in_ms`, `arm_cue` | Gate / post-wake soft open |
| `no_open_retry`, `no_open_nudge`, `no_open_nudge_text` | Silence recovery |
| `getattr(cfg.ambient, "streaming")` inside the loop | Bound listen polluted by ambient TOML |

Radio-end knowledge is also **sharded** across `speech.py`, `listen_end.py`, `listen_control.py`, `partial.py`, and ambient/CLI call sites. Bugs and policy changes fan out.

## Solution

One deep module at a clean seam. Callers and tests cross the same surface.

```text
                    ┌─────────────────────────────────────┐
  call sites  ──►   │  Answer Window                       │
  (thin facades)    │  open(policy) → ListenResult         │
                    ├─────────────────────────────────────┤
                    │  RadioSession | SilenceSession       │
                    │  partial HEP · control poll · cues   │
                    │  empty/no-open recovery · echo       │
                    │  uses pure: listen_end, endpointing  │
                    └─────────────────────────────────────┘
```

---

## External interface

### Primary entry

```python
# Proposed: src/hark/answer_window.py (name may be answer_window / listen_session;
# implementation epic owns final module path; speech.run_listen becomes facade.)

def open_answer_window(
    policy: AnswerWindowPolicy,
    *,
    deps: AnswerWindowDeps | None = None,
) -> ListenResult:
    """Run one answer-window capture under *policy*. Blocking until result or error."""
```

**Shape rule:** the external interface is **`open(policy) → result`**. There is no public radio vs silence API — profile/`end_mode` inside policy selects the session implementation.

Optional thin method form (same semantics):

```python
class AnswerWindow:
    def __init__(self, deps: AnswerWindowDeps | None = None) -> None: ...
    def open(self, policy: AnswerWindowPolicy) -> ListenResult: ...
```

Either is acceptable; prefer a module-level function if deps are usually defaulted, class if tests want long-lived fakes. **Do not** expose both as two public paths.

### Policy (inputs that used to be kwargs + ambient leaks)

Policy is a frozen (or dataclass) value object built **at the call seam**, never by re-reading `[ambient]` inside the session loop.

```python
@dataclass(frozen=True)
class AnswerWindowPolicy:
    """Everything the window needs that is not a runtime dependency."""

    # Identity / mode
    profile: Literal["bound_answer", "post_wake", "confirm"]
    end_mode: EndMode                    # radio | silence
    max_listen_s: float
    stream_id: str | None = None
    partial_kind: str = "ambient.partial"  # HEP kind for partials
    stt_provider: str | None = None        # None → config default

    # Half-duplex / echo
    last_tts: str | None = None
    post_tts_guard_s: float = 0.0
    already_armed: bool = False
    discard_leading_ms: int = 0
    # audio_ok_after stays a runtime hook (callable) on Deps, not policy

    # Energy gate (silence + radio open)
    abs_open_db: float = -48.0
    open_margin_db: float = 8.0
    initial_timeout_s: float = 45.0
    pre_roll_ms: int = 300
    lead_in_ms: int = 0
    arm_cue: bool = False

    # Silence recovery
    no_open_retry: bool = True
    no_open_nudge: bool = True
    no_open_nudge_text: str = "..."      # product default
    empty_stt_retry: bool = True         # from config when building policy
    empty_stt_nudge: bool = True

    # Endpointing (silence)
    endpoint_strategy_name: str = "energy"  # energy | smart_turn | ...
    smart_turn_model_path: str | None = None
    smart_turn_threshold: float | None = None

    # Radio product knobs (no ambient section)
    stream_partials: bool = True
    radio_partial_silence_s: float = 0.6
    radio_segment_overlap_ms: int = 300
    radio_segment_pad_ms: int = 250
    radio_idle_end_silence_s: float = 0.0  # 0 → derive 3× end_silence_s
    end_silence_s: float = 2.1             # silence finalize; also idle floor
    end_phrases: tuple[str, ...] = ()
    cancel_phrases: tuple[str, ...] = ()
    soft_end_phrases: tuple[str, ...] = ()
    soft_end_phrases_enabled: bool = True
    strip_phrase: bool = True

    # Streaming / idle clamp — **policy fields**, not getattr(cfg.ambient, ...)
    streaming: bool = False
    streaming_ack_min_quiet_s: float = 2.0
    suppress_stop_cue: bool | None = None  # None → True when streaming

    # Media duck during STT (explicit; B046)
    duck_media_during_stt: bool = True
    pause_media_during_stt: bool = False
```

**Builders (seam helpers, not session internals):**

```python
def policy_from_config(
    cfg: HarkConfig,
    profile: Literal["bound_answer", "post_wake", "confirm"],
    **overrides,
) -> AnswerWindowPolicy: ...
```

Profile defaults (product intent; refine under M6 if types land there first):

| Profile | Typical end_mode | streaming default | Gate / arm | Use |
|---------|------------------|-------------------|------------|-----|
| `bound_answer` | cfg.listen | **off** | answer_arm_cue; normal gate | Mode A ask/answer, CLI listen for bound |
| `post_wake` | cfg.listen | **cfg.ambient.streaming** | softer open, lead_in, post_wake arm | ambient `complete_after_wake` |
| `confirm` | often silence | off | short max, last_tts set | R2/R3 confirm turn |

**Cross-milestone note (M6):** Backlog marks M6 as “natural first slice of M1.” `AnswerWindowPolicy` and `ListenSessionPolicy` should be **one type** (or Answer Window imports M6’s type). If M6 is not landed when E2.T004 runs:

1. Prefer **landing M6.E1–E2 first** (policy + profiles), then M1 consumes it; or  
2. Define the type under Answer Window and have M6 re-export / rename later.

Do **not** soft-land E2.T004 with new `getattr(cfg.ambient, "streaming")` inside the session loop — that is the leak we are closing.

### Dependencies (runtime, injectable for tests)

```python
@dataclass
class AnswerWindowDeps:
    """Hardware/provider/control seams — fakeable without audio devices."""

    stt: Any | None = None                 # resolved STT client
    capture: Any | None = None             # mic / ContinuousMicStream factory
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    on_partial: Callable[[dict], None] | None = None
    audio_ok_after: Callable[[], float | None] | None = None
    play_record_start: Callable[[], None] | None = None
    play_record_stop: Callable[[], None] | None = None
    poll_listen_action: Callable[[str], str | None] | None = None
    consume_listen_action: Callable[[str], str | None] | None = None
    clear_active_listen: Callable[[str], None] | None = None
    register_active_listen: Callable[..., None] | None = None
    duck_media: Any | None = None
    run_tts_nudge: Callable[..., None] | None = None  # empty/no-open TTS
    syslog: Callable[..., None] | None = None
    usage_store: Any | None = None
    endpoint_strategy: EndpointStrategy | None = None  # prebuilt or from policy
```

Production path builds deps from config + real modules. Unit tests inject fakes and never open the mic.

### Result

Reuse existing `ListenResult` (or a thin rename-compatible type):

```python
@dataclass
class ListenResult:
    text: str
    provider: str
    duration_ms: int
    end_mode: str
    end_phrase: str | None = None
    cancelled: bool = False
    stream_id: str | None = None
    partials_emitted: int = 0
    meta_command: str | None = None
```

**Public behavior and Mode A CLI exit codes stay unchanged** (OK / ABORT / TIMEOUT / AUDIO / PROVIDER). Exceptions that today map to exit codes keep the same strings (`no speech detected`, max_listen, …).

---

## Internal structure (implementation only)

Not part of the external interface; listed so E2–E3 stay aligned.

### RadioSession

Owns radio-mode loop:

| Concern | Ownership |
|---------|-----------|
| Segment capture + pad/overlap | RadioSession |
| Per-segment STT + `join_radio_stt_segments` | RadioSession |
| Monotonic partial text + `on_partial` emit | RadioSession (HEP shape via `partial` helpers as **internal** import) |
| `evaluate_radio_transcript` (hard + soft end) | calls **pure** `listen_end` |
| Agent finish/cancel | polls `listen_control` as **internal** |
| Idle quiet finalize (B074) + streaming idle clamp (B112) | policy fields only |
| max_listen exit | RadioSession |
| Cues (start/stop; stop suppressed when streaming) | RadioSession |

**State sketch (informative, not a formal FSM library):**

```text
ARMED → (optional lead_in / arm_cue)
  → WAIT_OPEN (energy / audio_ok_after)
  → OPEN / SEGMENTING ⇄ PARTIAL_EMIT (segment silence → STT → join → partial)
  → FINALIZING  (end phrase | soft end | agent finish | idle | max_listen)
  → CANCELLED   (cancel phrase | agent cancel)
```

### SilenceSession

Owns silence-mode loop:

| Concern | Ownership |
|---------|-----------|
| Energy gate + injectable `EndpointStrategy` | SilenceSession |
| Smart Turn optional; fail-open to energy | build at open, same as today |
| Empty-STT retry/nudge | SilenceSession |
| No-open recovery retry/nudge | SilenceSession |
| Echo reject vs `last_tts` | SilenceSession (policy + last_tts) |
| Agent finish/cancel if wired | same control internals |
| Final STT once | SilenceSession |

### What stays pure / outside the module

| Module | Role after M1 |
|--------|----------------|
| `listen_end.py` | **Stays pure** — phrase evaluation only; no I/O. Called by RadioSession. CLI `hark listen-end` remains agent IPC surface via `listen_control`. |
| `listen_control.py` | IPC files stay; Answer Window **calls** poll/consume as internals. CLI `listen-end` unchanged. |
| `partial.py` | String/event builders may move behind package-private import; HEP field shapes **must not change**. |
| `endpointing.py` | Strategy types + builders; SilenceSession injects. |
| `answering.py` / `delivery.py` | **Out of scope** — bound delivery stays separate (M2). |
| TTS / `speak_and_listen` | Handoff stays in speech or later M4; Answer Window is listen-only. |

---

## Facades and call-site migration (E4)

| Call site | After |
|-----------|--------|
| `speech.run_listen(...)` | Thin facade: build policy from kwargs+cfg (compat) → `open_answer_window` |
| `ambient.complete_after_wake` | Build `post_wake` profile policy; no gate kwargs |
| `cli.cmd_listen` / ask listen | Build `bound_answer` (or confirm) profile |
| `speak_and_listen` / `run_ask` | Build policy at seam; optional last_tts |
| `dashboard.dictation` | Same facade or direct open with bound/dictation profile |

Migration order: facade first (behavior parity) → migrate call sites off kwargs soup → delete obsolete public kwargs when grep-clean.

Compat layer may accept old kwargs **only** on `run_listen` during the transition; new code and ambient/CLI should not grow kwargs.

---

## Invariants

1. **No cloud STT outside an open answer window / post-wake activation** (ADR-008).
2. **Half-duplex:** post-TTS guard / mute settle before energy open (ADR-009); `last_tts` available for echo reject.
3. **Radio soft-end / prosign rules** unchanged (ADR-014 / 014b) — still pure in `listen_end`.
4. **Partial HEP shapes** stable: `partial=true`, `stream_id`, `seq`, `text`, `fragment`, HOLD vs streaming warning/instructions, `stt_seq` when present.
5. **Agent control:** `hark listen-end` / finish|cancel still ends radio; soft end may finish without agent.
6. **Streaming clamp:** when `policy.streaming`, radio idle uses tighter quiet window; no read of `cfg.ambient` inside the loop.
7. **Bound profile default:** streaming **off** unless an override is explicit — bound windows must not inherit ambient dogfood streaming by accident (M6 intent).
8. **Library must not** apply LLM judgment to transcripts (ADR-002 spirit).

## Error modes

| Mode | Behavior (preserve) |
|------|---------------------|
| Cancel phrase / agent cancel | `ListenResult.cancelled=True`, end_phrase set; CLI → ABORT |
| max_listen exceeded (radio, no body) | `TimeoutError` / TIMEOUT |
| No speech / no open after recovery | raise / empty path as today; syslog `speech.no_open` |
| Empty STT after recovery | fail as today; syslog `speech.empty_stt` |
| Echo overlap | reject / re-listen per current echo policy |
| Provider STT failure | PROVIDER path unchanged |
| Audio device failure | AUDIO path unchanged |

---

## Tests that cross the seam

Primary seam = `open_answer_window(policy, deps=fakes)` or facade `run_listen` after it becomes thin.

| Area | Tests (representative) | Seam expectation |
|------|------------------------|------------------|
| Radio soft end / prosign | `test_listen_end.py`, radio soft-end cases | Pure `listen_end` unit + session integration |
| Radio idle + streaming clamp | `test_radio_idle_end.py`, `test_streaming_latency.py` | Policy fields drive idle; no ambient TOML in session |
| Partial HEP / HOLD vs streaming | `test_radio_partial_silence.py`, fixtures `radio_partial_*` | `on_partial` shapes via deps |
| Segment pad / join | `test_radio_segment_pad.py` | RadioSession internals via open |
| Agent finish/cancel | `test_listen_end.py` run_listen cases | Control fakes on deps |
| Stop cue suppress | `test_streaming_stop_cue.py` | `policy.streaming` / suppress_stop_cue |
| Empty STT | `test_empty_stt.py` | SilenceSession recovery |
| Echo | `test_echo_overlap.py` | last_tts on policy |
| Endpoint strategy | `test_endpointing.py` | injectable strategy |
| Cues / arm | `test_ambient_record_cues.py`, `test_answer_arm_cue.py` | arm_cue / open cues |
| Media duck | `test_media.py` run_listen duck | deps duck flags |
| Post-wake | `test_post_wake_listen.py`, ambient wake tests | post_wake profile builder |
| Silence + streaming finalize | `test_silence_streaming_finalize.py` | policy, not ambient getattr |

**E5 target:** tests do not require private `_loop` hooks; pure helpers remain unit-tested without opening a window.

---

## Non-goals (M1) — LOCKED

These are hard scope fences for the milestone (from backlog + design grill):

1. **No Mode B / harkd dialogue FSM** in the Answer Window library module.
2. **No LLM judgment** of transcript completeness, routing, or delivery targets.
3. **`listen_end` stays pure** — phrase evaluation only; no I/O, no session state.
4. **Answering / delivery / fingerprint revalidation stay separate** (M2 owns Answerability).
5. **Speak-then-listen handoff** is not fully relocated here (M4 may deepen later); `speak_and_listen` may remain a caller of Answer Window.
6. **No STT/TTS provider rewrite** and **no HEP schema version bump** for partials/finals.
7. **No wake/KWS redesign** — only the post_wake *listen* profile is in scope.

## Acceptance criteria (milestone) — LOCKED

A milestone is **done** only when all of the following hold:

| # | Criterion | How we know |
|---|-----------|-------------|
| AC1 | Deep module with small external interface `open(policy) → ListenResult` (+ policy builders) | Public surface documented; callers do not need radio/silence internals |
| AC2 | Radio and silence paths are session implementations behind that interface | RadioSession + SilenceSession (or equiv.) exist; not kwargs on the public surface |
| AC3 | `run_listen` is a thin facade (or removed after migration) | Implementation bulk is not in the facade body |
| AC4 | Ambient post-wake and CLI listen/ask build **profiles**, not gate-kwargs soup | Call sites use profile builders / policy |
| AC5 | Session loop does **not** read `[ambient]` (e.g. no `getattr(cfg.ambient, "streaming")`) | Grep-clean inside session; streaming/idle from policy fields |
| AC6 | Radio/silence/soft-end/endpointing/echo/empty-stt tests green (ported to deep seam where needed) | CI / targeted pytest |
| AC7 | ADR-014/014b behaviors intact; partial HEP shapes stable; Mode A CLI exit codes stable | Regression suite + fixtures |
| AC8 | `ARCHITECTURE.md` + `CHANGELOG` note Answer Window; domain terms in `CONTEXT.md` | Docs present |

**Exit gate for E1:** this plan section is locked; E2–E5 implement against AC1–AC8 and the non-goals list.

## Residual risks

| Risk | Mitigation |
|------|------------|
| M6 policy type vs M1 policy type drift | Single type ownership; E1 note + E2.T004 dep on M6.E2.T002 |
| 1kLOC move breaks subtle radio timing | Port tests first or keep facade parity tests; extract session behind facade before deleting kwargs |
| Partial HEP string drift | Golden fixtures + parity tests |
| Over-extraction (too many public types) | Only policy + result + open are public; sessions private |
| `speak_and_listen` half-duplex races | Keep post_tts_guard / already_armed on policy; M4 later |

## Epic validity (E2–E5)

| Epic | Still valid? | Notes |
|------|--------------|-------|
| E2 RadioSession | **Yes** | Types + segment/partial/control/idle as specified |
| E3 SilenceSession | **Yes** | Endpoint inject, empty/no-open, echo |
| E4 Facades | **Yes** | run_listen → open; ambient + CLI profiles |
| E5 Tests + docs | **Yes** | Port to deep interface; ARCHITECTURE + CHANGELOG |

No backlog epic rewrites required from this design. Optional later: rename task text if module path is not `answer_window.py`.

## Implementation order (for agents)

1. **E1** — this note + CONTEXT terms + lock AC (done when design accepted).
2. **M6.E1–E2** (recommended before E2.T004) — `ListenSessionPolicy` / profiles, or define type here and share.
3. **E2.T001** RadioSession types/state (unit-testable without hardware).
4. **E2.T002–T003** move loop body; **E3.*** parallel after E1.
5. **E2.T004** policy idle/streaming fields only.
6. **E4** facade + migrate + delete kwargs.
7. **E5** port tests + docs + regression.

## Related

- Architecture review: `~/served-html/architecture-review-hark-latest.html` (Candidate 1).
- Deep-module vocabulary: codebase-design skill (module, interface, depth, seam, adapter, leverage, locality).
- Audio product rules: `docs/AUDIO_DESIGN.md`, `docs/ENDPOINTING.md`.
- Phase backlog: `.backlog/01-architecture-revamp-1-grok/`.

## E5.T003 regression receipt (2026-07-15)

ADR-014 / 014b soft-end, listen-end IPC, partial HEP shapes, exit-code paths
verified green after deepen:

```text
uv run pytest tests/test_listen_end.py tests/test_listen_control.py \
  tests/test_radio_partial_silence.py tests/test_radio_idle_end.py \
  tests/test_answer_window_radio.py tests/test_answer_window_silence.py \
  tests/test_empty_stt.py tests/test_echo_overlap.py \
  tests/test_streaming_stop_cue.py tests/test_fixtures_parity.py -q
→ 261 passed
```

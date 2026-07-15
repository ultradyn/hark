# P1.M3 — Deepen Pane Understanding

**Status:** design in progress (E1.T001 interface draft)  
**Date:** 2026-07-15  
**Backlog:** `P1.M3` · architecture review candidate 3  
**Depends on:** M2 Bound Answerability (delivery side; emission is independent)  
**Related bugs:** B016 (false done), B096 (busy subagent), B111 (idle chrome vs menu)  
**E1.T001:** classify interface + state types documented below

## Goal

Collapse edge-detection, false-done menus, busy-subagent suppression, and
question-changed tracking into one **deep Pane Understanding** module:

- **Small external interface** — pure-ish `classify` / batch `process` that
  takes agent status + pane text (+ prior module state) and returns HEP facts
  (or ready-to-emit HEP drafts) + next state. No Herdr I/O inside the module.
- **Large implementation** — status edge machine, false-done / false-idle
  heuristics, Tasks/subagent chrome, fingerprint dedupe, pane_capture split.
- **Thin watch** — list agents, read pane, emit, register, lifecycle
  invalidation only. No false-done policy left in `watch.py`.
- **Thin HEP builders** — `make_agent_*` remain packing functions that take
  already-decided fields (kind, text, hit, capture).

## Problem (current)

`EdgeTracker` in `watch.py` (~440 LOC of policy) plus pure heuristics in
`events.py` (`looks_like_pending_question`, `detect_active_subagents`) own
the “what does this pane mean?” knowledge. Watch also owns I/O (list, read,
emit, register, lifecycle). That mix means:

| Concern | Where today | Pain |
|---------|-------------|------|
| Status edge + first-seen rules | `EdgeTracker.process` | hard to unit-test without watch |
| False-done / needs_input | `_maybe_false_done` | policy buried in I/O module |
| Busy subagent (B096) | `_emit_busy_subagent` + `_subagents_busy` | same |
| Question changed while blocked | `_same_status_events` | same |
| Menu / Tasks heuristics | `events.py` pure fns | good, but not co-located with classifier |
| HEP packing | `make_agent_*` | already mostly thin; some risk of policy creep |
| Pane lifecycle invalidate | `_handle_lifecycle_event` | correctly I/O-side |

M2 Answerability **consumes** `agent.needs_input` / fingerprints; M3 owns
**emitting** those HEPs correctly from pane observations.

---

## E1.T001 — Classify interface + state types

### Primary entry (external interface)

Module path (implementation owns final package layout; prefer package):

```text
src/hark/pane_understanding/
  __init__.py          # re-exports public API
  types.py             # state + observation + config types
  heuristics.py        # looks_like_pending_question, detect_active_subagents
  classify.py          # EdgeTracker → Classifier (stateful process)
```

**Shape rule:** callers cross one surface — **observe → facts**. Watch never
re-implements false-done policy.

```python
# Conceptual API (names may tighten in E2; semantics locked here)

@dataclass(frozen=True)
class PaneObservation:
    """One agent tick after I/O (no Herdr types required inside pure tests)."""
    session_id: str
    pane_id: str
    status: str                    # Herdr wire status, lower-case preferred
    pane_text: str | None = None   # full bounded body or None if unread
    # Optional passthrough for HEP builders (target/cwd/revision not used by
    # pure policy but needed when packing events):
    agent: str | None = None
    revision: int = 0
    # Opaque carrier so builders can still use AgentInfo if convenient:
    raw_agent: Any = None          # optional AgentInfo; watch may pass it

@dataclass(frozen=True)
class ClassifyPolicy:
    interest: frozenset[str]       # e.g. blocked, done, idle
    detect_false_done: bool = True
    pane_capture: bool = True
    pane_capture_lines: int = 40   # DEFAULT_PANE_CAPTURE_LINES
    pane_capture_max_chars: int = 12000

@dataclass
class PaneUnderstandingState:
    """Module-owned per-(session, pane) maps. No watch.py private state."""
    status: dict[tuple[str, str], str]
    dedupe: set[tuple[str, str, str, str]]
    last_fp: dict[tuple[str, str], str]
    false_done_scanned: dict[tuple[str, str], str]
    subagents_busy: dict[tuple[str, str], int]

    @staticmethod
    def empty() -> PaneUnderstandingState: ...


class PaneClassifier:
    """Stateful classifier. Equivalent to today's EdgeTracker, without I/O."""

    def __init__(self, policy: ClassifyPolicy) -> None: ...

    @property
    def state(self) -> PaneUnderstandingState: ...

    def process(
        self,
        observations: Sequence[PaneObservation],
    ) -> list[dict[str, Any]]:
        """Return HEP event dicts ready to emit (via thin make_* builders).

        Pure w.r.t. Herdr: pane_text must already be attached (or None).
        Mutates module-owned state only.
        """
```

**Batch vs single:** the public entry is **batch** `process(observations)`
matching today's `EdgeTracker.process(agents, …)`. A single-agent helper is
optional and non-public if it only reduces duplication.

**Functional form** (equivalent; either style is OK, not both public):

```python
def classify(
    observations: Sequence[PaneObservation],
    state: PaneUnderstandingState,
    policy: ClassifyPolicy,
) -> tuple[list[dict[str, Any]], PaneUnderstandingState]:
    """Pure-ish: returns events + next state (new or mutated copy)."""
```

Prefer **class + mutable state** for watch (matches EdgeTracker ergonomics and
existing tests). Prefer **pure function** only if a second pure path is needed;
do not dual-export.

### Inputs / outputs contract

| Input | Source | Notes |
|-------|--------|-------|
| `status` | Herdr `AgentInfo.status` | edge key with previous module state |
| `pane_text` | watch `read_pane` (or test fixture) | full viewport-ish body; classifier splits trail vs capture |
| `interest` | watch config statuses | gates which edges become HEP |
| `detect_false_done` | watch config | off → no needs_input / busy-subagent paths |
| `prev state` | module | first-seen = key missing from `state.status` |

| Output HEP kinds (facts) | When |
|--------------------------|------|
| `agent.blocked` | status → blocked (or first-seen blocked) |
| `agent.completed` / `agent.state_changed` | idle-like / other interest transitions |
| `agent.needs_input` | idle-like + pending menu (false done) |
| `agent.state_changed` + `busy_subagent` | idle-like + active Tasks strip |
| `agent.question_changed` | still blocked, fingerprint changed |

Events are full HEP dicts produced by thin `make_agent_*` so watch can
`emit(event)` unchanged. Classifier **decides** fields; builders **pack**.

### Classify decision sketch (normative for E2)

Per observation key `(session_id, pane_id)`:

1. **Same status** → `_same_status` path: question_changed if blocked + FP
   changed; if idle-like and (was busy-subagent or not yet false-done scanned)
   re-check Tasks → menu → maybe needs_input / completed.
2. **Status change** → update `state.status`; first-seen only emits blocked or
   false-done; later edges emit blocked / false-done / generic status event.
3. **False-done order** (idle-like): Tasks/subagent full-pane scan **before**
   menu heuristics (B096 beats B016 when both present).
4. **Dedupe** keys: `(session, pane, kind_tag, fingerprint_or_count)` as today.

### Non-goals (E1 interface)

- No change to HEP schema wire shapes.
- No Answerability changes (M2 already consumes needs_input).
- No moving lifecycle invalidation into the module (stays in watch — E3.T002).
- No Herdr client imports inside `pane_understanding` pure paths.
- Heuristic *behavior* parity with current EdgeTracker + B016/B096/B111 tests
  (not a redesign of menus/Tasks).

### Acceptance (E1.T001)

| ID | Criterion |
|----|-----------|
| AC-I1 | `classify` / `PaneClassifier.process` documented with observation + policy + state types |
| AC-I2 | Output kinds and false-done/busy order documented |
| AC-I3 | Explicit: no Herdr I/O inside module; pane_text is pre-read |
| AC-I4 | HEP builders remain pack-only; policy lives in classifier |

---

## Preview: state field map (E1.T002)

| EdgeTracker field | Module type field | Notes |
|-------------------|-------------------|-------|
| `_status` | `state.status` | last Herdr status per pane |
| `_dedupe` | `state.dedupe` | emit once per (pane, tag, fp) |
| `_last_fp` | `state.last_fp` | question_changed + blocked FP |
| `_false_done_scanned` | `state.false_done_scanned` | seal one scan per status epoch |
| `_subagents_busy` | `state.subagents_busy` | count while Tasks strip active |
| `pane_capture*` | `ClassifyPolicy` | not per-pane mutable state |

**Acceptance for E1.T002:** no EdgeTracker-private maps remain only in
`watch.py` after E2 extract; watch may hold a `PaneClassifier` instance only.

---

## Implementation order (E2–E4)

1. **E2.T001** — move pure heuristics next to classifier; re-export from
   `events` for back-compat if needed.
2. **E2.T002** — move EdgeTracker body into `PaneClassifier`; unit-testable
   without Herdr.
3. **E2.T003** — audit `make_agent_*`: builders take decided fields only.
4. **E3.T001** — watch: I/O → observations → `classifier.process` → emit/register.
5. **E3.T002** — lifecycle invalidation stays in watch; document boundary.
6. **E4.T001** — port false-done / busy-subagent / binding tests to module.
7. **E4.T002** — ARCHITECTURE.md EdgeTracker → Pane Understanding.

## Test seam

```python
clf = PaneClassifier(ClassifyPolicy(interest=frozenset({"blocked", "done"})))
events = clf.process([
    PaneObservation(session_id="s", pane_id="p1", status="done", pane_text=MENU),
])
assert any(e["kind"] == "agent.needs_input" for e in events)
```

No socket, no client, no `question_for` callback — text is on the observation.

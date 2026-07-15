# P1.M2 — Deepen Bound Answerability

**Status:** design locked for E1 (implementation follows E2–E4)  
**Date:** 2026-07-15  
**Backlog:** `P1.M2` · architecture review candidate 2  
**Depends on:** M1 Answer Window (listen-only); delivery store + fingerprint remain  
**Safety:** `docs/SAFETY.md` Routing — *compatible state* before send  
**E1.T002:** acceptance criteria + non-goals below are **LOCKED** (2026-07-15) — implement against them; change only via backlog amend + plan edit

## Goal

Collapse the scattered “is this bound event still safe to answer?” checks into one
**deep Answerability** module:

- **Small external interface** — pure `assess(...)` → deliver|refuse + reason;
  I/O helpers re-read live status + fingerprint behind injectable clients.
- **Large implementation** — status × HEP kind × pane heuristics, false-done
  menus on idle-like Herdr status, revision/fingerprint gates, fail-soft vs
  hard refuse policy for queue vs send paths.
- **One test seam** — unit tests without network; fakes for Herdr client.

## Problem (current)

`answer_bound_event` and `cli._queue_live_answerable` hard-code:

```text
live.status != "blocked"  →  refuse "not_blocked"
```

That is **too narrow**. Watch already emits `agent.needs_input` when Herdr
reports `done` / `idle` but the pane still shows a pending menu (false done /
false idle — B016). Those HEPs register for bound delivery with a fingerprint,
and Mode A is instructed to treat them like `agent.blocked`. Yet `hark answer`
and queue live-filter **refuse** them with `not_blocked` before fingerprint
re-check.

Also:

| Surface | Check today | Drift risk |
|---------|-------------|------------|
| `answering.answer_bound_event` | `status == blocked` only | misses needs_input |
| `cli._queue_live_answerable` | same `status == blocked` | duplicate logic |
| dashboard `POST /answer` | calls `answer_bound_event` | inherits bug |
| SAFETY “compatible state” | prose only | not codified |

---

## E1.T001 — Answerable status matrix

### Axes

1. **Herdr live.status** (from `AgentInfo.status`, normalized lower-case)  
   Known wire values in use: `blocked`, `working`, `idle`, `done`  
   Idle-like set (matches `is_idle_like_status`): `done`, `idle`, `completed`, `complete`
2. **HEP kind** (bound event `meta.kind` when registered from HEP)  
   Answer-relevant kinds: `agent.blocked`, `agent.needs_input`, `agent.question_changed`  
   Other kinds (e.g. `agent.completed`) are not bound for answer in normal flow.
3. **Pane heuristics** (live re-read after status gate)  
   - fingerprint match vs bound `question_fingerprint`  
   - optional: `looks_like_pending_question` still true (menu present)  
   - revision match when `pane_revision > 0`  
   - pane existence (`get_agent` non-null)

### Decision codes (stable reasons)

| Code | Meaning | Typical path |
|------|---------|--------------|
| `ok` | Deliverable (all gates pass) | answer + queue keep |
| `pane_gone` | No live agent for pane | refuse / expire |
| `not_compatible` | Live status not compatible for this HEP kind | refuse / expire |
| `not_blocked` | **Legacy alias** of `not_compatible` for `working`/wrong status when HEP is classic blocked-only (keep during migration if needed; prefer `not_compatible`) | refuse |
| `stale_revision` | Live revision ≠ bound revision | refuse |
| `fingerprint_mismatch` | Live excerpt fingerprint ≠ bound | refuse |
| `fingerprint_unavailable` | Pane read / Herdr error during FP check | answer: reject; queue soft-keep may differ |
| `missing_question_fingerprint` | Bound event has no FP | refuse |
| `already_delivered` / `not_pending` / `unknown_event` / `bad_request` | Store / request gates (outside pure status matrix) | refuse |

**Send path (answer):** hard refuse on mismatch; on `HerdrError` during *write*,
status stays `uncertain` (never blind-retry) — unchanged.

**Queue live filter / prune:** fail-soft on Herdr transport errors (keep event);
expire only when live evidence says unanswerable (`pane_gone`,
`not_compatible`, `stale_revision`). Do **not** expire solely because FP
re-read failed (transient).

### Matrix: live.status × HEP kind → gate (before pane FP)

Legend:

- **D** = may proceed to pane/fingerprint checks (potentially deliver)  
- **R** = refuse / not answerable (reason)  
- **—** = not a normal bindable answer HEP (if somehow registered: R `not_compatible`)

| live.status ↓ \ HEP kind → | `agent.blocked` | `agent.needs_input` | `agent.question_changed` | missing / other |
|----------------------------|-----------------|---------------------|--------------------------|-----------------|
| **blocked** | **D** | **D** | **D** | **D** if FP present (treat as blocked-class) |
| **idle** | **R** `not_compatible` | **D** (false-done path) | **R** `not_compatible` | **R** |
| **done** | **R** `not_compatible` | **D** (false-done path) | **R** `not_compatible` | **R** |
| **completed** / **complete** | **R** | **D** | **R** | **R** |
| **working** | **R** `not_compatible` | **R** `not_compatible` | **R** | **R** |
| **unknown / empty** | **R** `not_compatible` | **R** | **R** | **R** |
| **pane gone** (`live is None`) | **R** `pane_gone` | **R** `pane_gone` | **R** `pane_gone` | **R** `pane_gone` |

Notes:

1. **`agent.needs_input` + idle-like** is the false-done / false-idle delivery
   path. SAFETY “compatible state” means *still waiting for human input on the
   pane*, not solely `status==blocked`.
2. **`agent.blocked` + idle-like** refuses: the bound question was for a true
   block; if Herdr left blocked, the turn moved on (or status desynced). A new
   `needs_input` HEP would re-bind if a menu remains.
3. **`agent.question_changed`** only while still **blocked** (watch only emits
   on blocked). Same status gate as blocked.
4. **Missing kind** (older bound rows): if `live.status == blocked` → **D**;
   else **R**. Do not open idle-like delivery without `agent.needs_input`
   (or an explicit false_done marker if we add one later).

### Pane heuristic layer (after status gate = D)

| Check | Order | Fail reason | Notes |
|-------|-------|-------------|-------|
| Bound FP non-empty | 1 | `missing_question_fingerprint` | Required for any deliver |
| `pane_revision > 0` and live.revision known and ≠ | 2 | `stale_revision` | Same as today |
| `read_pane` + excerpt FP | 3 | `fingerprint_mismatch` or `fingerprint_unavailable` | Uses `extract_question_excerpt` + `question_fingerprint` |
| Optional: menu still present | 4 | (see below) | Only for idle-like needs_input |

**Optional menu heuristic (idle-like + needs_input only):**

| Pane looks like pending question? | Fingerprint | Decision |
|-----------------------------------|-------------|----------|
| yes | match | **deliver** |
| yes | mismatch | refuse `fingerprint_mismatch` |
| no (empty / idle chrome only) | match | refuse `not_compatible` (menu gone; false-done resolved or FP stale coincidence) |
| no | mismatch | refuse `fingerprint_mismatch` or `not_compatible` |

Rationale: a matching FP on empty Claude `❯` chrome is suspicious; prefer refuse.
If FP matches and menu still matches, deliver. E2 may implement menu check via
existing `looks_like_pending_question` on a short trailing read — pure core
receives a boolean `pane_still_pending` from the I/O helper so the core stays
Herdr-free.

**For `blocked` status:** do **not** require menu heuristic (agent may wait on
free-text without a multi-choice menu). Fingerprint (+ revision) suffice.

### SAFETY.md alignment (review)

| SAFETY Routing control | Matrix mapping |
|------------------------|----------------|
| Opaque targets | Unchanged — session/pane IDs only |
| Bound delivery: event_id + revision + fingerprint | Pre-send gates 1–3 |
| Re-check pane exists, **compatible state**, FP unchanged | `pane_gone` + status×kind matrix + FP match |
| On mismatch: refuse send | All **R** rows + FP fail |
| Idempotency / no blind resend | Store `already_delivered`; write `HerdrError` → `uncertain` (outside matrix) |

**Compatible state (normative for M2):**

A pane is **compatible** for bound answer when either:

1. Herdr status is **`blocked`**, or  
2. Bound HEP kind is **`agent.needs_input`** and Herdr status is **idle-like**
   and the live pane still presents the same question fingerprint (and, when
   implemented, still looks pending).

Not compatible: `working`, other statuses, pane gone, or idle-like without a
needs_input bind.

### Explicit non-goals for the matrix (E1)

- No LLM judgment of pane text (ADR-002 spirit).  
- No change to HEP schema or watch false-done emission (already correct).  
- No auto-answer / Mode B dialogue.  
- Queue age/supersede rules stay in `DeliveryStore`; Answerability only supplies
  the live answerable predicate.

---

## E1.T002 — Design: Answerability seam

### Solution shape

```text
                    ┌──────────────────────────────────────────┐
  answer_bound_event│  Answerability                            │
  queue live filter │  assess_snapshot(...) → AnswerabilityVerdict │
  dashboard /answer │  (pure)                                   │
                    ├──────────────────────────────────────────┤
                    │  read_live_snapshot(client, bound)        │
                    │  → LiveAnswerSnapshot (I/O, injectable)   │
                    │  uses: get_agent, read_pane, FP helpers   │
                    └──────────────────────────────────────────┘
```

**Shape rule:** pure core never imports Herdr clients. Call sites either:

1. Build a `LiveAnswerSnapshot` via helper + call pure `assess_snapshot`, or  
2. Use a thin orchestrator that does both (e.g. inside `answer_bound_event`).

### Module layout (implementation epic owns final path)

Preferred package (mirrors Answer Window depth without forcing it on day one):

```text
src/hark/answerability/
  __init__.py          # public: assess_snapshot, LiveAnswerSnapshot, Verdict, reasons
  core.py              # pure assess_snapshot + status×kind matrix
  live.py              # read_live_snapshot(client, bound) — injectable client
  reasons.py           # stable reason string constants (optional)
```

Acceptable first cut: single module `src/hark/answerability.py` if LOC stays small
(~150–250); split when helpers grow. **Do not** grow more status checks inside
`cli.py` or duplicate matrix rows in `answering.py`.

`answering.answer_bound_event` remains the **delivery** orchestrator (store
lookup, mark delivered/rejected/uncertain, send_text/keys). It **calls**
Answerability for the live-compatible + FP gate instead of inlining
`status != "blocked"`.

### Pure types

```python
@dataclass(frozen=True)
class LiveAnswerSnapshot:
    """Everything pure assess needs that came from live I/O (or fakes)."""

    pane_exists: bool
    live_status: str | None          # None if pane gone
    live_revision: int | None
    bound_revision: int
    bound_fingerprint: str           # stripped; empty → missing
    live_fingerprint: str | None     # None if read failed
    fingerprint_error: bool          # True when pane read/Herdr failed
    hep_kind: str | None             # from bound.meta.get("kind")
    pane_still_pending: bool | None  # None = not evaluated (blocked path)
    # True/False only required when status gate is needs_input + idle-like


@dataclass(frozen=True)
class AnswerabilityVerdict:
    ok: bool                 # True → may deliver (send path) / keep (queue)
    reason: str              # "ok" or stable refuse code
```

```python
def assess_snapshot(snap: LiveAnswerSnapshot) -> AnswerabilityVerdict:
    """Pure: matrix + revision + FP (+ optional menu). No I/O."""
```

### Live helper

```python
def read_live_snapshot(
    *,
    session_id: str,
    pane_id: str,
    bound_revision: int,
    bound_fingerprint: str | None,
    hep_kind: str | None,
    client: Any,  # duck-typed: get_agent, read_pane
    pane_lines: int = 40,
    require_pending_heuristic: bool | None = None,
) -> LiveAnswerSnapshot:
    """I/O: get_agent + optional read_pane + extract_question_excerpt + FP.

    When require_pending_heuristic is True (or auto for needs_input + idle-like),
    also set pane_still_pending via looks_like_pending_question.
    """
```

**No hard Herdr coupling in pure core:** tests construct `LiveAnswerSnapshot`
directly. Live helper tests use FakeClient (same pattern as `test_cli_answer.py`).

### Call sites (must converge)

| Call site | Today | After M2 |
|-----------|--------|----------|
| `answering.answer_bound_event` | inline `status != blocked` + FP | `read_live_snapshot` + `assess_snapshot`; refuse with verdict.reason; then send |
| `cli._queue_live_answerable` | duplicate `status != blocked` | same assess (queue mode: soft-keep on transport error **before** snapshot; on snapshot, expire only hard unanswerable reasons) |
| `cli.cmd_queue` prune/list | via `_queue_live_answerable` | no local status strings |
| `dashboard.api.answer_action` | `answer_bound_event` | unchanged entry; inherits Answerability |
| `cli.cmd_answer` | `answer_bound_event` | same |

**Grep exit gate for E3:** no remaining `live.status != "blocked"` (or
`== "blocked"`) gates for answer/queue outside Answerability module (tests may
assert reasons).

### Queue vs send policy

| Verdict reason | `hark answer` / dashboard | `hark queue` live filter/prune |
|----------------|---------------------------|--------------------------------|
| `ok` | send | keep fresh |
| `pane_gone`, `not_compatible`, `stale_revision` | reject | expire / stale |
| `fingerprint_mismatch` | reject | expire / stale (question moved) |
| `fingerprint_unavailable` | reject | **keep** (transient; fail-soft) |
| Herdr error before snapshot | n/a (caught as unavailable) | **keep** (`herdr_error:…`) |
| `missing_question_fingerprint` | reject (store mark) | expire if present in queue |

### Reason code migration

- Prefer **`not_compatible`** for status-matrix refusals.  
- Existing tests assert `not_blocked` — migration may map:
  - `working` / wrong status → emit `not_compatible` **or** keep emitting
    `not_blocked` as a synonym for one release if CLI/API consumers depend on it.
  - **Decision for implementers:** new pure core returns `not_compatible`;
    `answer_bound_event` may alias to `not_blocked` only if a grep of public
    docs/tests shows hard dependency. Prefer updating tests to `not_compatible`
    (clearer for needs_input world). Update `test_cli_answer` accordingly.

---

## Non-goals (M2) — LOCKED

1. **No LLM judgment** of pane text or auto-routing (ADR-002 spirit).  
2. **No HEP schema change** and **no watch false-done redesign** — emission of
   `agent.needs_input` already correct; this milestone **delivers** those events.  
3. **No Mode B / dialogue FSM** and no multi-target disambiguation changes.  
4. **Answer Window (M1)** stays listen-only; Answerability does not open mics.  
5. **DeliveryStore** ownership of age/supersede/idempotency stays in
   `delivery.py`; Answerability is the live-compatible predicate only.  
6. **No provider/STT/TTS** work.  
7. **Pane Understanding (M3)** may later deepen question extraction; M2 uses
   existing `extract_question_excerpt` + `question_fingerprint` +
   `looks_like_pending_question`.

---

## Acceptance criteria (milestone) — LOCKED

| # | Criterion | How we know |
|---|-----------|-------------|
| AC1 | Pure Answerability core implements status×kind matrix + FP/revision gates | Unit tests without network; table-driven status×kind cases |
| AC2 | Live re-read helpers are injectable (`client_for` / duck client); pure core has no Herdr import | Grep + unit tests with FakeClient |
| AC3 | `answer_bound_event` uses Answerability; **needs_input + idle-like + menu + FP match → deliver** | Green tests E4.T001 + existing answer tests |
| AC4 | `needs_input` + idle empty pane (no menu) → refuse | Green refuse path test |
| AC5 | `cmd_queue` live filter + prune use same module; no duplicated `status==blocked` answer gates | Grep-clean outside answerability |
| AC6 | Dashboard `/answer` inherits AC3 via `answer_bound_event` | No forked status check in dashboard |
| AC7 | Write path still `uncertain` on `HerdrError` (never blind-retry) | Existing / regression test |
| AC8 | `SAFETY.md` documents compatible state including false-done; skill notes match | Docs + skill grep |

### False-done delivery acceptance (explicit)

| Scenario | Herdr status | HEP kind | Pane | Expected |
|----------|--------------|----------|------|----------|
| F1 | `done` or `idle` | `agent.needs_input` | menu-like + FP match | **deliver** (`ok`) |
| F2 | `done` or `idle` | `agent.needs_input` | idle chrome only / empty | **refuse** `not_compatible` |
| F3 | `done` or `idle` | `agent.needs_input` | menu but FP mismatch | **refuse** `fingerprint_mismatch` |
| F4 | `blocked` | `agent.blocked` | FP match | **deliver** (classic path) |
| F5 | `working` | any | any | **refuse** `not_compatible` |
| F6 | `done` | `agent.blocked` | menu present | **refuse** (wrong HEP class; need needs_input bind) |
| F7 | pane gone | any | — | **refuse** `pane_gone` |

---

## Epic map

| Epic | Work |
|------|------|
| **E1** | Matrix + this design (done when locked) |
| **E2** | Pure core + live snapshot helpers + unit tests |
| **E3** | Wire answering + queue + dashboard; remove duplicate checks |
| **E4** | Fixture tests F1–F2 (+ peers); SAFETY.md + skill notes |

## Exit gate for E1

This plan (matrix + seam + AC1–AC8 + F1–F7 + non-goals) is **locked**.
E2–E4 implement against it.


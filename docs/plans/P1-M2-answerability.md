# P1.M2 — Deepen Bound Answerability

**Status:** E1.T001 status matrix specified (design expands in E1.T002)  
**Date:** 2026-07-15  
**Backlog:** `P1.M2` · architecture review candidate 2  
**Depends on:** M1 Answer Window (listen-only); delivery store + fingerprint remain  
**Safety:** `docs/SAFETY.md` Routing — *compatible state* before send  

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

## Next (E1.T002+)

- Design note: module shape `assess` / live helpers, call sites (answering,
  queue, dashboard), acceptance criteria for false-done delivery.
- E2: pure core + injectable re-read helpers.  
- E3: migrate `answer_bound_event`, `_queue_live_answerable`, dashboard.  
- E4: fixture tests + SAFETY.md / skill notes.

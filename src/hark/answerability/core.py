"""Pure Answerability core: live snapshot → deliver|refuse + reason.

No Herdr imports. See ``docs/plans/P1-M2-answerability.md`` for the status
matrix and false-done delivery rules (F1–F7).
"""

from __future__ import annotations

from dataclasses import dataclass

from hark.answerability import reasons as R

# Match ``hark.events.is_idle_like_status`` without importing events (keeps
# pure core free of AgentInfo / HEP builders).
_IDLE_LIKE = frozenset({"done", "idle", "completed", "complete"})

_KIND_NEEDS_INPUT = "agent.needs_input"


@dataclass(frozen=True)
class LiveAnswerSnapshot:
    """Everything pure assess needs from live I/O (or test fakes)."""

    pane_exists: bool
    live_status: str | None
    live_revision: int | None
    bound_revision: int
    bound_fingerprint: str
    live_fingerprint: str | None
    fingerprint_error: bool
    hep_kind: str | None
    pane_still_pending: bool | None = None
    """True/False when evaluated (needs_input + idle-like); None if not needed."""


@dataclass(frozen=True)
class AnswerabilityVerdict:
    ok: bool
    reason: str

    def as_tuple(self) -> tuple[bool, str]:
        return self.ok, self.reason


def is_idle_like(status: str | None) -> bool:
    return (status or "").strip().lower() in _IDLE_LIKE


def normalize_status(status: str | None) -> str:
    return (status or "").strip().lower()


def normalize_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    k = str(kind).strip()
    return k or None


def assess_snapshot(snap: LiveAnswerSnapshot) -> AnswerabilityVerdict:
    """Pure status×kind matrix + revision + fingerprint (+ optional menu)."""
    fp = (snap.bound_fingerprint or "").strip()
    if not fp:
        return AnswerabilityVerdict(False, R.MISSING_QUESTION_FINGERPRINT)

    if not snap.pane_exists or snap.live_status is None:
        return AnswerabilityVerdict(False, R.PANE_GONE)

    status = normalize_status(snap.live_status)
    kind = normalize_kind(snap.hep_kind)

    if not _status_kind_allows(status, kind):
        return AnswerabilityVerdict(False, R.NOT_COMPATIBLE)

    if (
        isinstance(snap.bound_revision, int)
        and snap.bound_revision > 0
        and snap.live_revision is not None
        and int(snap.live_revision) != int(snap.bound_revision)
    ):
        return AnswerabilityVerdict(False, R.STALE_REVISION)

    if snap.fingerprint_error or snap.live_fingerprint is None:
        return AnswerabilityVerdict(False, R.FINGERPRINT_UNAVAILABLE)

    live_fp = (snap.live_fingerprint or "").strip()
    if live_fp != fp:
        return AnswerabilityVerdict(False, R.FINGERPRINT_MISMATCH)

    # needs_input + idle-like: menu must still look pending when evaluated.
    if kind == _KIND_NEEDS_INPUT and is_idle_like(status):
        if snap.pane_still_pending is False:
            return AnswerabilityVerdict(False, R.NOT_COMPATIBLE)
        # None = caller skipped heuristic (treat as allow if FP matched) —
        # live helper should set True/False; pure core is lenient on None
        # only for unit tests that focus on FP alone.

    return AnswerabilityVerdict(True, R.OK)


def _status_kind_allows(status: str, kind: str | None) -> bool:
    """Matrix gate before pane FP (docs/plans/P1-M2-answerability.md)."""
    if status == "blocked":
        # Blocked row: D for blocked / needs_input / question_changed /
        # missing kind / other (FP already required by assess_snapshot).
        return True
    if is_idle_like(status):
        # Only agent.needs_input may deliver on false-done / false-idle.
        return kind == _KIND_NEEDS_INPUT
    # working, empty, unknown → refuse
    return False

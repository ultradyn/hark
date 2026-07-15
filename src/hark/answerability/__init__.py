"""Bound Answerability: pure assess + (later) live snapshot helpers.

Small external interface — ``assess_snapshot(snap) → AnswerabilityVerdict``.
See ``docs/plans/P1-M2-answerability.md``.
"""

from __future__ import annotations

from hark.answerability.core import (
    AnswerabilityVerdict,
    LiveAnswerSnapshot,
    assess_snapshot,
    is_idle_like,
    normalize_kind,
    normalize_status,
)
from hark.answerability import reasons

__all__ = [
    "AnswerabilityVerdict",
    "LiveAnswerSnapshot",
    "assess_snapshot",
    "is_idle_like",
    "normalize_kind",
    "normalize_status",
    "reasons",
]

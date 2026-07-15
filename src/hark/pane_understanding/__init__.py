"""Pane Understanding: pure heuristics + (later) stateful classify.

See ``docs/plans/P1-M3-pane-understanding.md``.
"""

from __future__ import annotations

from hark.pane_understanding.heuristics import (
    ActiveSubagentsHit,
    PendingQuestionHit,
    detect_active_subagents,
    looks_like_pending_question,
)

__all__ = [
    "ActiveSubagentsHit",
    "PendingQuestionHit",
    "detect_active_subagents",
    "looks_like_pending_question",
]

"""Pane Understanding: pure heuristics + stateful classify.

See ``docs/plans/P1-M3-pane-understanding.md``.

Import graph note: ``hark.events`` re-exports heuristics from
``pane_understanding.heuristics`` during its own import. This package
``__init__`` therefore must not import ``classify`` (which imports events).
Import classifier as::

    from hark.pane_understanding.classify import PaneClassifier, EdgeTracker
    # or (after events is fully loaded):
    from hark.pane_understanding import PaneClassifier  # via __getattr__
"""

from __future__ import annotations

from typing import Any

from hark.pane_understanding.heuristics import (
    ActiveSubagentsHit,
    PendingQuestionHit,
    detect_active_subagents,
    looks_like_pending_question,
)
from hark.pane_understanding.types import (
    ClassifyPolicy,
    PaneObservation,
    PaneUnderstandingState,
)

__all__ = [
    "ActiveSubagentsHit",
    "ClassifyPolicy",
    "EdgeTracker",
    "PaneClassifier",
    "PaneObservation",
    "PaneUnderstandingState",
    "PendingQuestionHit",
    "detect_active_subagents",
    "looks_like_pending_question",
]


def __getattr__(name: str) -> Any:
    if name in ("EdgeTracker", "PaneClassifier"):
        from hark.pane_understanding.classify import EdgeTracker, PaneClassifier

        if name == "EdgeTracker":
            return EdgeTracker
        return PaneClassifier
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""Pane Understanding state, policy, and observation types (P1.M3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# (session_id, pane_id)
PaneKey = tuple[str, str]


@dataclass(frozen=True)
class ClassifyPolicy:
    """Immutable knobs for one watch run (or test)."""

    interest: frozenset[str] = frozenset()
    detect_false_done: bool = True
    pane_capture: bool = True
    pane_capture_lines: int = 100
    pane_capture_max_chars: int = 12000


@dataclass
class PaneUnderstandingState:
    """Module-owned per-pane maps (was EdgeTracker private fields)."""

    status: dict[PaneKey, str] = field(default_factory=dict)
    dedupe: set[tuple[str, str, str, str]] = field(default_factory=set)
    last_fp: dict[PaneKey, str] = field(default_factory=dict)
    false_done_scanned: dict[PaneKey, str] = field(default_factory=dict)
    subagents_busy: dict[PaneKey, int] = field(default_factory=dict)

    @staticmethod
    def empty() -> PaneUnderstandingState:
        return PaneUnderstandingState()


@dataclass(frozen=True)
class PaneObservation:
    """One agent tick after I/O — pane_text already read (or None).

    ``raw_agent`` may carry AgentInfo so thin HEP builders keep working during
    the E2→E3 migration without re-fetching fields.
    """

    session_id: str
    pane_id: str
    status: str
    pane_text: str | None = None
    agent: str | None = None
    revision: int = 0
    raw_agent: Any = None

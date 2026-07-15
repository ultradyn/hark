"""Deep Answer Window module: open(policy) → result.

Public surface is intentionally small. Radio/silence sessions, partial HEP,
and listen-control polling are implementation details. See
``docs/plans/P1-M1-answer-window.md``.
"""

from __future__ import annotations

from hark.answer_window.deps import AnswerWindowDeps
from hark.answer_window.policy import (
    AnswerWindowPolicy,
    AnswerWindowProfile,
    effective_radio_idle_s,
    policy_from_config,
)
from hark.answer_window.radio import (
    RadioEvent,
    RadioSession,
    RadioState,
    radio_transition,
)
from hark.answer_window.result import ListenResult

__all__ = [
    "AnswerWindowDeps",
    "AnswerWindowPolicy",
    "AnswerWindowProfile",
    "ListenResult",
    "RadioEvent",
    "RadioSession",
    "RadioState",
    "effective_radio_idle_s",
    "policy_from_config",
    "radio_transition",
]

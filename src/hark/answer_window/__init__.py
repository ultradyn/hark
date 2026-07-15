"""Deep Answer Window module: open(policy) → result.

Public surface is intentionally small. Radio/silence sessions, partial HEP,
and listen-control polling are implementation details. See
``docs/plans/P1-M1-answer-window.md``.
"""

from __future__ import annotations

from hark.answer_window.deps import AnswerWindowDeps
from hark.answer_window.open_window import open_answer_window
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
from hark.answer_window.silence import (
    EMPTY_STT_NUDGE_TEXT,
    NO_OPEN_NUDGE_TEXT,
    SilenceEvent,
    SilenceRecoveryDecision,
    SilenceSession,
    SilenceState,
    echo_overlap,
    is_no_open_timeout,
    log_empty_stt,
    log_no_open,
    resolve_endpoint_strategy,
    silence_transition,
)
from hark.answer_window.text_join import (
    join_radio_stt_segments,
    monotonic_partial_text,
    prefer_complete_transcript,
)

__all__ = [
    "AnswerWindowDeps",
    "AnswerWindowPolicy",
    "AnswerWindowProfile",
    "EMPTY_STT_NUDGE_TEXT",
    "ListenResult",
    "NO_OPEN_NUDGE_TEXT",
    "RadioEvent",
    "RadioSession",
    "RadioState",
    "SilenceEvent",
    "SilenceRecoveryDecision",
    "SilenceSession",
    "SilenceState",
    "echo_overlap",
    "effective_radio_idle_s",
    "is_no_open_timeout",
    "join_radio_stt_segments",
    "log_empty_stt",
    "log_no_open",
    "monotonic_partial_text",
    "open_answer_window",
    "policy_from_config",
    "prefer_complete_transcript",
    "radio_transition",
    "resolve_endpoint_strategy",
    "silence_transition",
]

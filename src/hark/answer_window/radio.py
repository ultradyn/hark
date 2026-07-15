"""Radio Answer Window session: state machine + types (no STT provider details).

Unit-testable without audio hardware. Capture/STT wiring lands in later tasks
(E2.T002–T004); this module owns states, events, and pure transitions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from hark.answer_window.deps import AnswerWindowDeps
from hark.answer_window.policy import AnswerWindowPolicy, effective_radio_idle_s
from hark.answer_window.result import ListenResult
from hark.listen_end import EndMode


class RadioState(str, Enum):
    """Lifecycle of one radio-profile answer window."""

    ARMED = "armed"  # stream registered; optional lead-in / arm cue
    WAIT_OPEN = "wait_open"  # energy gate; no speech yet
    SEGMENTING = "segmenting"  # speech open; capturing toward segment boundary
    PARTIAL_EMIT = "partial_emit"  # segment STT done; emitting partial HEP
    FINALIZING = "finalizing"  # product/agent soft-or-hard end; assembling body
    CANCELLED = "cancelled"  # cancel phrase or agent cancel
    DONE = "done"  # terminal success (includes idle / max_listen with body)
    FAILED = "failed"  # terminal error (e.g. max_listen with no body)


class RadioEvent(str, Enum):
    """Inputs that advance the radio session (no provider payload on the event)."""

    START = "start"
    ARM_CUE_DONE = "arm_cue_done"
    SPEECH_OPENED = "speech_opened"
    SEGMENT_BOUNDARY = "segment_boundary"  # trailing silence cut for interim STT
    PARTIAL_EMITTED = "partial_emitted"
    END_PHRASE = "end_phrase"
    SOFT_END = "soft_end"
    CANCEL_PHRASE = "cancel_phrase"
    AGENT_FINISH = "agent_finish"
    AGENT_CANCEL = "agent_cancel"
    IDLE_TIMEOUT = "idle_timeout"  # post-open continuous quiet
    MAX_LISTEN = "max_listen"
    NO_OPEN_TIMEOUT = "no_open_timeout"  # never opened before initial_timeout


# Legal transitions: (state, event) → new state. Terminal states absorb nothing.
_RADIO_TRANSITIONS: dict[tuple[RadioState, RadioEvent], RadioState] = {
    (RadioState.ARMED, RadioEvent.START): RadioState.WAIT_OPEN,
    (RadioState.ARMED, RadioEvent.ARM_CUE_DONE): RadioState.WAIT_OPEN,
    (RadioState.WAIT_OPEN, RadioEvent.SPEECH_OPENED): RadioState.SEGMENTING,
    (RadioState.WAIT_OPEN, RadioEvent.AGENT_CANCEL): RadioState.CANCELLED,
    (RadioState.WAIT_OPEN, RadioEvent.AGENT_FINISH): RadioState.FINALIZING,
    (RadioState.WAIT_OPEN, RadioEvent.NO_OPEN_TIMEOUT): RadioState.FAILED,
    (RadioState.WAIT_OPEN, RadioEvent.MAX_LISTEN): RadioState.FAILED,
    (RadioState.SEGMENTING, RadioEvent.SEGMENT_BOUNDARY): RadioState.PARTIAL_EMIT,
    (RadioState.SEGMENTING, RadioEvent.END_PHRASE): RadioState.FINALIZING,
    (RadioState.SEGMENTING, RadioEvent.SOFT_END): RadioState.FINALIZING,
    (RadioState.SEGMENTING, RadioEvent.CANCEL_PHRASE): RadioState.CANCELLED,
    (RadioState.SEGMENTING, RadioEvent.AGENT_FINISH): RadioState.FINALIZING,
    (RadioState.SEGMENTING, RadioEvent.AGENT_CANCEL): RadioState.CANCELLED,
    (RadioState.SEGMENTING, RadioEvent.IDLE_TIMEOUT): RadioState.FINALIZING,
    (RadioState.SEGMENTING, RadioEvent.MAX_LISTEN): RadioState.FINALIZING,
    (RadioState.PARTIAL_EMIT, RadioEvent.PARTIAL_EMITTED): RadioState.SEGMENTING,
    (RadioState.PARTIAL_EMIT, RadioEvent.END_PHRASE): RadioState.FINALIZING,
    (RadioState.PARTIAL_EMIT, RadioEvent.SOFT_END): RadioState.FINALIZING,
    (RadioState.PARTIAL_EMIT, RadioEvent.CANCEL_PHRASE): RadioState.CANCELLED,
    (RadioState.PARTIAL_EMIT, RadioEvent.AGENT_FINISH): RadioState.FINALIZING,
    (RadioState.PARTIAL_EMIT, RadioEvent.AGENT_CANCEL): RadioState.CANCELLED,
    (RadioState.PARTIAL_EMIT, RadioEvent.IDLE_TIMEOUT): RadioState.FINALIZING,
    (RadioState.PARTIAL_EMIT, RadioEvent.MAX_LISTEN): RadioState.FINALIZING,
    (RadioState.FINALIZING, RadioEvent.END_PHRASE): RadioState.DONE,
    (RadioState.FINALIZING, RadioEvent.SOFT_END): RadioState.DONE,
    (RadioState.FINALIZING, RadioEvent.AGENT_FINISH): RadioState.DONE,
    (RadioState.FINALIZING, RadioEvent.IDLE_TIMEOUT): RadioState.DONE,
    (RadioState.FINALIZING, RadioEvent.MAX_LISTEN): RadioState.DONE,
    (RadioState.FINALIZING, RadioEvent.CANCEL_PHRASE): RadioState.CANCELLED,
    (RadioState.FINALIZING, RadioEvent.AGENT_CANCEL): RadioState.CANCELLED,
}


def radio_transition(state: RadioState, event: RadioEvent) -> RadioState:
    """Pure state transition. Raises ``ValueError`` on illegal pairs."""
    if state in (RadioState.DONE, RadioState.CANCELLED, RadioState.FAILED):
        raise ValueError(f"radio session already terminal ({state.value})")
    key = (state, event)
    if key not in _RADIO_TRANSITIONS:
        raise ValueError(f"illegal radio transition: {state.value} + {event.value}")
    return _RADIO_TRANSITIONS[key]


@dataclass
class RadioSession:
    """Radio-profile session: owns state; no STT provider types on the interface.

    Later tasks move segment join / partial emit / control poll into methods on
    this type. For E2.T001, :meth:`apply` exercises the state machine only.
    """

    policy: AnswerWindowPolicy
    deps: AnswerWindowDeps = field(default_factory=AnswerWindowDeps)
    state: RadioState = RadioState.ARMED
    stream_id: str | None = None
    speech_opened: bool = False
    partial_seq: int = 0
    last_partial_text: str = ""
    text_segments: list[str] = field(default_factory=list)
    end_phrase: str | None = None
    cancelled: bool = False
    _history: list[tuple[RadioState, RadioEvent, RadioState]] = field(
        default_factory=list, repr=False
    )

    def __post_init__(self) -> None:
        if self.policy.end_mode is not EndMode.RADIO:
            # Session may still be constructed for tests; open() will route by mode.
            pass
        if self.stream_id is None:
            self.stream_id = self.policy.stream_id

    @property
    def radio_idle_s(self) -> float:
        """Idle finalize threshold from policy fields only."""
        return effective_radio_idle_s(self.policy)

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            RadioState.DONE,
            RadioState.CANCELLED,
            RadioState.FAILED,
        )

    def apply(self, event: RadioEvent, *, end_phrase: str | None = None) -> RadioState:
        """Apply a pure event; record history for tests."""
        prev = self.state
        nxt = radio_transition(prev, event)
        self._history.append((prev, event, nxt))
        self.state = nxt
        if event is RadioEvent.SPEECH_OPENED:
            self.speech_opened = True
        if event in (
            RadioEvent.END_PHRASE,
            RadioEvent.SOFT_END,
            RadioEvent.CANCEL_PHRASE,
            RadioEvent.AGENT_FINISH,
            RadioEvent.AGENT_CANCEL,
            RadioEvent.IDLE_TIMEOUT,
            RadioEvent.MAX_LISTEN,
        ):
            if end_phrase is not None:
                self.end_phrase = end_phrase
            elif event is RadioEvent.AGENT_FINISH:
                self.end_phrase = "agent:finish"
            elif event is RadioEvent.AGENT_CANCEL:
                self.end_phrase = "agent:cancel"
            elif event is RadioEvent.IDLE_TIMEOUT:
                self.end_phrase = "radio_idle"
            elif event is RadioEvent.MAX_LISTEN:
                self.end_phrase = "max_listen"
        if event in (RadioEvent.CANCEL_PHRASE, RadioEvent.AGENT_CANCEL) or nxt is RadioState.CANCELLED:
            self.cancelled = True
        return nxt

    def note_segment_text(self, text: str) -> None:
        """Record a segment transcript (join lands in E2.T002)."""
        body = (text or "").strip()
        if body:
            self.text_segments.append(body)

    def note_partial_emitted(self, text: str) -> None:
        self.partial_seq += 1
        self.last_partial_text = text or ""

    def history(self) -> list[tuple[RadioState, RadioEvent, RadioState]]:
        return list(self._history)

    def result_stub(
        self,
        *,
        text: str = "",
        provider: str = "test",
        duration_ms: int = 0,
    ) -> ListenResult:
        """Build a ListenResult from current session flags (no I/O)."""
        return ListenResult(
            text=text or self.last_partial_text,
            provider=provider,
            duration_ms=duration_ms,
            end_mode=EndMode.RADIO.value,
            end_phrase=self.end_phrase,
            cancelled=self.cancelled,
            stream_id=self.stream_id,
            partials_emitted=self.partial_seq,
        )

    def as_debug_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "stream_id": self.stream_id,
            "speech_opened": self.speech_opened,
            "partial_seq": self.partial_seq,
            "radio_idle_s": self.radio_idle_s,
            "streaming": self.policy.streaming,
            "end_phrase": self.end_phrase,
            "cancelled": self.cancelled,
            "terminal": self.is_terminal,
        }

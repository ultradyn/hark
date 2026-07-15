"""Silence Answer Window session: types + endpoint strategy injection.

Empty-STT / no-open recovery and echo reject move in later tasks (E3.T002–T003).
This module owns states, events, pure transitions, and strategy resolution
(energy default; Smart Turn optional; fail-open to energy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from hark.answer_window.deps import AnswerWindowDeps
from hark.answer_window.policy import AnswerWindowPolicy
from hark.answer_window.result import ListenResult
from hark.endpointing import EndpointStrategy, build_endpoint_strategy
from hark.listen_end import EndMode


class SilenceState(str, Enum):
    """Lifecycle of one silence-profile answer window (including recovery attempts)."""

    ARMED = "armed"
    WAIT_OPEN = "wait_open"
    CAPTURING = "capturing"  # speech open until energy/strategy ends
    TRANSCRIBING = "transcribing"
    RECOVER_EMPTY = "recover_empty"  # empty STT retry/nudge path
    RECOVER_NO_OPEN = "recover_no_open"  # no speech detected path
    FINALIZING = "finalizing"
    CANCELLED = "cancelled"
    DONE = "done"
    FAILED = "failed"


class SilenceEvent(str, Enum):
    START = "start"
    ARM_CUE_DONE = "arm_cue_done"
    SPEECH_OPENED = "speech_opened"
    CAPTURE_ENDED = "capture_ended"  # energy / strategy finalized utterance
    TRANSCRIPT_OK = "transcript_ok"
    TRANSCRIPT_EMPTY = "transcript_empty"
    NO_OPEN_TIMEOUT = "no_open_timeout"
    ECHO_REJECT = "echo_reject"
    RETRY = "retry"  # automatic re-listen after empty/no-open/echo
    NUDGE = "nudge"  # TTS nudge then re-listen
    AGENT_FINISH = "agent_finish"
    AGENT_CANCEL = "agent_cancel"
    MAX_LISTEN = "max_listen"
    GIVE_UP = "give_up"  # recovery exhausted


_SILENCE_TRANSITIONS: dict[tuple[SilenceState, SilenceEvent], SilenceState] = {
    (SilenceState.ARMED, SilenceEvent.START): SilenceState.WAIT_OPEN,
    (SilenceState.ARMED, SilenceEvent.ARM_CUE_DONE): SilenceState.WAIT_OPEN,
    (SilenceState.WAIT_OPEN, SilenceEvent.SPEECH_OPENED): SilenceState.CAPTURING,
    (SilenceState.WAIT_OPEN, SilenceEvent.NO_OPEN_TIMEOUT): SilenceState.RECOVER_NO_OPEN,
    (SilenceState.WAIT_OPEN, SilenceEvent.AGENT_CANCEL): SilenceState.CANCELLED,
    (SilenceState.WAIT_OPEN, SilenceEvent.AGENT_FINISH): SilenceState.FINALIZING,
    (SilenceState.WAIT_OPEN, SilenceEvent.MAX_LISTEN): SilenceState.FAILED,
    (SilenceState.CAPTURING, SilenceEvent.CAPTURE_ENDED): SilenceState.TRANSCRIBING,
    (SilenceState.CAPTURING, SilenceEvent.AGENT_CANCEL): SilenceState.CANCELLED,
    (SilenceState.CAPTURING, SilenceEvent.AGENT_FINISH): SilenceState.FINALIZING,
    (SilenceState.CAPTURING, SilenceEvent.MAX_LISTEN): SilenceState.TRANSCRIBING,
    (SilenceState.TRANSCRIBING, SilenceEvent.TRANSCRIPT_OK): SilenceState.FINALIZING,
    (SilenceState.TRANSCRIBING, SilenceEvent.TRANSCRIPT_EMPTY): SilenceState.RECOVER_EMPTY,
    (SilenceState.TRANSCRIBING, SilenceEvent.ECHO_REJECT): SilenceState.RECOVER_EMPTY,
    (SilenceState.RECOVER_EMPTY, SilenceEvent.RETRY): SilenceState.WAIT_OPEN,
    (SilenceState.RECOVER_EMPTY, SilenceEvent.NUDGE): SilenceState.WAIT_OPEN,
    (SilenceState.RECOVER_EMPTY, SilenceEvent.GIVE_UP): SilenceState.FAILED,
    (SilenceState.RECOVER_NO_OPEN, SilenceEvent.RETRY): SilenceState.WAIT_OPEN,
    (SilenceState.RECOVER_NO_OPEN, SilenceEvent.NUDGE): SilenceState.WAIT_OPEN,
    (SilenceState.RECOVER_NO_OPEN, SilenceEvent.GIVE_UP): SilenceState.FAILED,
    (SilenceState.FINALIZING, SilenceEvent.TRANSCRIPT_OK): SilenceState.DONE,
    (SilenceState.FINALIZING, SilenceEvent.AGENT_FINISH): SilenceState.DONE,
    (SilenceState.FINALIZING, SilenceEvent.AGENT_CANCEL): SilenceState.CANCELLED,
    (SilenceState.FINALIZING, SilenceEvent.ECHO_REJECT): SilenceState.RECOVER_EMPTY,
}


def silence_transition(state: SilenceState, event: SilenceEvent) -> SilenceState:
    """Pure state transition. Raises ``ValueError`` on illegal pairs."""
    if state in (SilenceState.DONE, SilenceState.CANCELLED, SilenceState.FAILED):
        raise ValueError(f"silence session already terminal ({state.value})")
    key = (state, event)
    if key not in _SILENCE_TRANSITIONS:
        raise ValueError(f"illegal silence transition: {state.value} + {event.value}")
    return _SILENCE_TRANSITIONS[key]


def resolve_endpoint_strategy(
    policy: AnswerWindowPolicy,
    *,
    prebuilt: EndpointStrategy | None = None,
    on_warn: Callable[[str], None] | None = None,
    predict_fn: Any | None = None,
) -> EndpointStrategy | None:
    """Resolve endpoint strategy for silence mode; ``None`` means energy gate.

    Fail-open: any smart-turn build failure returns ``None`` (energy).
    """
    if prebuilt is not None:
        return prebuilt
    name = (policy.endpoint_strategy_name or "energy").strip().lower()
    if name in ("", "energy", "energy_gate", "gate", "off", "none"):
        return None
    threshold = policy.smart_turn_threshold
    if threshold is None:
        threshold = 0.5
    return build_endpoint_strategy(
        strategy_name=policy.endpoint_strategy_name,
        smart_turn_model_path=policy.smart_turn_model_path,
        smart_turn_threshold=float(threshold),
        predict_fn=predict_fn,
        on_warn=on_warn,
    )


@dataclass
class SilenceSession:
    """Silence-profile session with injectable endpoint strategy.

    Strategy is resolved at construction (or via :meth:`bind_endpoint_strategy`).
    ``None`` strategy = energy default — same contract as ``audio.capture``.
    """

    policy: AnswerWindowPolicy
    deps: AnswerWindowDeps = field(default_factory=AnswerWindowDeps)
    state: SilenceState = SilenceState.ARMED
    stream_id: str | None = None
    speech_opened: bool = False
    attempt: int = 0
    did_empty_retry: bool = False
    did_empty_nudge: bool = False
    did_no_open_retry: bool = False
    did_no_open_nudge: bool = False
    end_phrase: str | None = None
    cancelled: bool = False
    last_text: str = ""
    endpoint_strategy: EndpointStrategy | None = field(default=None, repr=False)
    _history: list[tuple[SilenceState, SilenceEvent, SilenceState]] = field(
        default_factory=list, repr=False
    )
    _strategy_bound: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.stream_id is None:
            self.stream_id = self.policy.stream_id
        if not self._strategy_bound:
            self.bind_endpoint_strategy()

    def bind_endpoint_strategy(
        self,
        *,
        prebuilt: EndpointStrategy | None = None,
        on_warn: Callable[[str], None] | None = None,
        predict_fn: Any | None = None,
    ) -> EndpointStrategy | None:
        """Inject or resolve strategy; fail-open to energy (``None``)."""
        if prebuilt is None and self.deps.endpoint_strategy is not None:
            prebuilt = self.deps.endpoint_strategy
        warn = on_warn
        if warn is None and self.deps.syslog is not None:
            def _warn(msg: str) -> None:
                self.deps.syslog(  # type: ignore[misc]
                    "listen.endpoint_fallback",
                    component="stt",
                    level="warn",
                    message=msg,
                )

            warn = _warn
        self.endpoint_strategy = resolve_endpoint_strategy(
            self.policy,
            prebuilt=prebuilt,
            on_warn=warn,
            predict_fn=predict_fn,
        )
        self._strategy_bound = True
        return self.endpoint_strategy

    @property
    def uses_energy_gate(self) -> bool:
        return self.endpoint_strategy is None

    @property
    def is_terminal(self) -> bool:
        return self.state in (
            SilenceState.DONE,
            SilenceState.CANCELLED,
            SilenceState.FAILED,
        )

    def apply(self, event: SilenceEvent, *, end_phrase: str | None = None) -> SilenceState:
        prev = self.state
        nxt = silence_transition(prev, event)
        self._history.append((prev, event, nxt))
        self.state = nxt
        if event is SilenceEvent.SPEECH_OPENED:
            self.speech_opened = True
        if event is SilenceEvent.RETRY:
            self.attempt = max(self.attempt, 1)
            if prev is SilenceState.RECOVER_EMPTY:
                self.did_empty_retry = True
            if prev is SilenceState.RECOVER_NO_OPEN:
                self.did_no_open_retry = True
            self.speech_opened = False
        if event is SilenceEvent.NUDGE:
            self.attempt = 2
            if prev is SilenceState.RECOVER_EMPTY:
                self.did_empty_nudge = True
            if prev is SilenceState.RECOVER_NO_OPEN:
                self.did_no_open_nudge = True
            self.speech_opened = False
        if event is SilenceEvent.AGENT_FINISH:
            self.end_phrase = end_phrase or "agent:finish"
        elif event is SilenceEvent.AGENT_CANCEL:
            self.end_phrase = end_phrase or "agent:cancel"
            self.cancelled = True
        elif end_phrase is not None:
            self.end_phrase = end_phrase
        if nxt is SilenceState.CANCELLED:
            self.cancelled = True
        return nxt

    def plan_empty_stt_recovery(self) -> SilenceEvent:
        """Which recovery event to fire after empty STT (policy-driven)."""
        if self.policy.empty_stt_retry and not self.did_empty_retry:
            return SilenceEvent.RETRY
        if self.policy.empty_stt_nudge and not self.did_empty_nudge:
            return SilenceEvent.NUDGE
        return SilenceEvent.GIVE_UP

    def plan_no_open_recovery(self) -> SilenceEvent:
        if self.policy.no_open_retry and not self.did_no_open_retry:
            return SilenceEvent.RETRY
        if self.policy.no_open_nudge and not self.did_no_open_nudge:
            return SilenceEvent.NUDGE
        return SilenceEvent.GIVE_UP

    def history(self) -> list[tuple[SilenceState, SilenceEvent, SilenceState]]:
        return list(self._history)

    def result_stub(
        self,
        *,
        text: str = "",
        provider: str = "test",
        duration_ms: int = 0,
    ) -> ListenResult:
        return ListenResult(
            text=text or self.last_text,
            provider=provider,
            duration_ms=duration_ms,
            end_mode=EndMode.SILENCE.value,
            end_phrase=self.end_phrase or "silence",
            cancelled=self.cancelled,
            stream_id=self.stream_id,
        )

    def as_debug_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "stream_id": self.stream_id,
            "speech_opened": self.speech_opened,
            "attempt": self.attempt,
            "endpoint": getattr(self.endpoint_strategy, "name", "energy"),
            "uses_energy_gate": self.uses_energy_gate,
            "did_empty_retry": self.did_empty_retry,
            "did_no_open_retry": self.did_no_open_retry,
            "terminal": self.is_terminal,
        }

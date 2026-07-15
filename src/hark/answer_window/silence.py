"""Silence Answer Window session: endpoint strategy + empty/no-open recovery.

Owns states, events, pure transitions, strategy resolution (energy default;
Smart Turn optional; fail-open to energy), and empty-STT / no-open recovery
decision + attempt bookkeeping (E3.T001–T002). Echo reject lands in E3.T003.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from hark.answer_window.deps import AnswerWindowDeps
from hark.answer_window.policy import AnswerWindowPolicy
from hark.answer_window.result import ListenResult
from hark.endpointing import EndpointStrategy, build_endpoint_strategy
from hark.listen_end import EndMode

# Product nudge lines (B012 / B031). Callers may override no-open via policy.
EMPTY_STT_NUDGE_TEXT = "Sorry, I didn't catch that."
NO_OPEN_NUDGE_TEXT = "I didn't hear anything. Please speak after the beep."


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


@dataclass(frozen=True)
class SilenceRecoveryDecision:
    """Result of :meth:`SilenceSession.on_empty_stt` / :meth:`on_no_open`.

    ``action`` is RETRY (re-listen), NUDGE (TTS then re-listen), or GIVE_UP.
    ``phase`` describes the failed attempt that triggered recovery
    (``initial`` / ``retry`` / ``nudge``) for structured logs.
    ``nudge_text`` is set when ``action`` is NUDGE (caller or deps runs TTS).
    """

    action: SilenceEvent
    phase: str
    nudge_text: str | None = None


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


def is_no_open_timeout(exc: BaseException) -> bool:
    """True when energy gate never opened (vs empty STT after open)."""
    msg = str(exc).lower()
    return "no speech detected" in msg or "no speech captured" in msg


def _default_syslog(event: str, **data: Any) -> None:
    from hark.syslog import log as syslog

    syslog(event, **data)


def log_no_open(
    *,
    peak_rms: float | None = None,
    peak_db: float | None = None,
    open_thresh: float | None = None,
    after_tts: bool,
    attempt: int,
    stream_id: str | None,
    phase: str,
    error: str,
    abs_open_db: float | None = None,
    syslog_fn: Callable[..., None] | None = None,
) -> None:
    """Structured metric when capture times out before speech opens."""
    log = syslog_fn or _default_syslog
    # Best-effort parse peak/open from TimeoutError message
    if peak_db is None:
        m = re.search(r"peak_db=(-?[\d.]+)", error)
        if m:
            try:
                peak_db = float(m.group(1))
            except ValueError:
                pass
    if peak_rms is None:
        m = re.search(r"peak_rms=(-?[\d.]+)", error)
        if m:
            try:
                peak_rms = float(m.group(1))
            except ValueError:
                pass
    if open_thresh is None:
        m = re.search(r"open_thresh≈(-?[\d.]+)", error)
        if m:
            try:
                open_thresh = float(m.group(1))
            except ValueError:
                pass
    log(
        "speech.no_open",
        component="stt",
        level="warn",
        message="energy gate never opened",
        peak_db=round(peak_db, 2) if peak_db is not None else None,
        rms=round(peak_rms, 6) if peak_rms is not None else None,
        open_thresh=round(open_thresh, 2) if open_thresh is not None else None,
        abs_open_db=abs_open_db,
        after_tts=after_tts,
        attempt=attempt,
        stream_id=stream_id,
        phase=phase,
        error=error[:240],
    )


def log_empty_stt(
    *,
    duration_ms: int,
    peak_rms: float | None,
    peak_db: float | None,
    wait_speech_ms: int,
    after_tts: bool,
    attempt: int,
    provider: str | None,
    stream_id: str | None,
    phase: str,
    syslog_fn: Callable[..., None] | None = None,
) -> None:
    """Structured metric for empty STT rate / residual-TTS diagnosis."""
    log = syslog_fn or _default_syslog
    log(
        "speech.empty_stt",
        component="stt",
        level="warn",
        message="STT returned empty transcript",
        duration_ms=duration_ms,
        audio_ms=duration_ms,
        rms=round(peak_rms, 6) if peak_rms is not None else None,
        peak_db=round(peak_db, 2) if peak_db is not None else None,
        wait_speech_ms=wait_speech_ms,
        after_tts=after_tts,
        attempt=attempt,
        provider=provider,
        stream_id=stream_id,
        phase=phase,
    )


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
    """Silence-profile session: endpoint strategy + empty/no-open recovery.

    Strategy is resolved at construction (or via :meth:`bind_endpoint_strategy`).
    ``None`` strategy = energy default — same contract as ``audio.capture``.

    Recovery: call :meth:`on_empty_stt` / :meth:`on_no_open` after STT or
    gate failure; the session logs, updates attempt flags, and returns the
    next action (retry / nudge / give_up). TTS nudge stays with the caller
    (or ``deps.run_tts_nudge`` if wired).
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

    @property
    def empty_stt_phase(self) -> str:
        """Phase label for the *current* empty-STT event (before next recovery)."""
        if self.did_empty_nudge:
            return "nudge"
        if self.did_empty_retry:
            return "retry"
        return "initial"

    @property
    def no_open_phase(self) -> str:
        """Phase label for the *current* no-open event (before next recovery)."""
        if self.did_no_open_nudge:
            return "nudge"
        if self.did_no_open_retry:
            return "retry"
        return "initial"

    def _syslog(self, event: str, **data: Any) -> None:
        log = self.deps.syslog or _default_syslog
        log(event, **data)

    def _snap_state(self, target: SilenceState, via: SilenceEvent) -> None:
        """Record a transition into *target* without requiring a legal prior pair.

        Used when the facade has not fully driven the capture FSM yet (E3.T002
        recovery integration) but recovery still needs RECOVER_* bookkeeping.
        """
        if self.state is target:
            return
        prev = self.state
        self._history.append((prev, via, target))
        self.state = target

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

    def on_empty_stt(
        self,
        *,
        duration_ms: int,
        peak_rms: float | None = None,
        peak_db: float | None = None,
        wait_speech_ms: int = 0,
        after_tts: bool = False,
        provider: str | None = None,
    ) -> SilenceRecoveryDecision:
        """Log empty STT, decide retry/nudge/give_up, update attempt bookkeeping.

        Returns a decision for the facade: RETRY → continue capture loop;
        NUDGE → speak ``nudge_text`` then continue; GIVE_UP → raise.
        """
        phase = self.empty_stt_phase
        log_empty_stt(
            duration_ms=duration_ms,
            peak_rms=peak_rms,
            peak_db=peak_db,
            wait_speech_ms=wait_speech_ms,
            after_tts=after_tts,
            attempt=self.attempt,
            provider=provider,
            stream_id=self.stream_id,
            phase=phase,
            syslog_fn=self.deps.syslog,
        )
        # Drive into RECOVER_EMPTY via legal path when possible
        if self.state is SilenceState.CAPTURING:
            self.apply(SilenceEvent.CAPTURE_ENDED)
        if self.state is SilenceState.TRANSCRIBING:
            self.apply(SilenceEvent.TRANSCRIPT_EMPTY)
        elif self.state is not SilenceState.RECOVER_EMPTY:
            self._snap_state(SilenceState.RECOVER_EMPTY, SilenceEvent.TRANSCRIPT_EMPTY)

        action = self.plan_empty_stt_recovery()
        self.apply(action)

        nudge_text: str | None = None
        if action is SilenceEvent.RETRY:
            self._syslog(
                "speech.empty_stt_retry",
                component="stt",
                level="info",
                after_tts=after_tts,
                stream_id=self.stream_id,
                duration_ms=duration_ms,
            )
        elif action is SilenceEvent.NUDGE:
            nudge_text = EMPTY_STT_NUDGE_TEXT
            self._syslog(
                "speech.empty_stt_nudge",
                component="stt",
                level="info",
                after_tts=after_tts,
                stream_id=self.stream_id,
                text=nudge_text,
            )
            if self.deps.run_tts_nudge is not None:
                try:
                    self.deps.run_tts_nudge(nudge_text, kind="empty_stt")
                except Exception as nudge_exc:
                    self._syslog(
                        "speech.empty_stt_nudge_failed",
                        component="stt",
                        level="warn",
                        error=str(nudge_exc)[:200],
                        stream_id=self.stream_id,
                    )
        return SilenceRecoveryDecision(
            action=action, phase=phase, nudge_text=nudge_text
        )

    def on_no_open(
        self,
        *,
        after_tts: bool = False,
        error: str = "",
        abs_open_db: float | None = None,
        peak_rms: float | None = None,
        peak_db: float | None = None,
        open_thresh: float | None = None,
    ) -> SilenceRecoveryDecision:
        """Log no-open, decide retry/nudge/give_up, update attempt bookkeeping."""
        phase = self.no_open_phase
        log_no_open(
            peak_rms=peak_rms,
            peak_db=peak_db,
            open_thresh=open_thresh,
            after_tts=after_tts,
            attempt=self.attempt,
            stream_id=self.stream_id,
            phase=phase,
            error=error,
            abs_open_db=abs_open_db,
            syslog_fn=self.deps.syslog,
        )
        if self.state is SilenceState.WAIT_OPEN:
            self.apply(SilenceEvent.NO_OPEN_TIMEOUT)
        elif self.state is SilenceState.ARMED:
            self.apply(SilenceEvent.START)
            self.apply(SilenceEvent.NO_OPEN_TIMEOUT)
        elif self.state is not SilenceState.RECOVER_NO_OPEN:
            self._snap_state(SilenceState.RECOVER_NO_OPEN, SilenceEvent.NO_OPEN_TIMEOUT)

        action = self.plan_no_open_recovery()
        self.apply(action)

        nudge_text: str | None = None
        if action is SilenceEvent.RETRY:
            self._syslog(
                "speech.no_open_retry",
                component="stt",
                level="info",
                after_tts=after_tts,
                stream_id=self.stream_id,
                abs_open_db=abs_open_db,
            )
        elif action is SilenceEvent.NUDGE:
            nudge_text = (
                self.policy.no_open_nudge_text
                if self.policy.no_open_nudge_text
                else NO_OPEN_NUDGE_TEXT
            )
            self._syslog(
                "speech.no_open_nudge",
                component="stt",
                level="info",
                after_tts=after_tts,
                stream_id=self.stream_id,
                text=nudge_text,
            )
            if self.deps.run_tts_nudge is not None:
                try:
                    self.deps.run_tts_nudge(nudge_text, kind="no_open")
                except Exception as nudge_exc:
                    self._syslog(
                        "speech.no_open_nudge_failed",
                        component="stt",
                        level="warn",
                        error=str(nudge_exc)[:200],
                        stream_id=self.stream_id,
                    )
        return SilenceRecoveryDecision(
            action=action, phase=phase, nudge_text=nudge_text
        )

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
            "did_empty_nudge": self.did_empty_nudge,
            "did_no_open_retry": self.did_no_open_retry,
            "did_no_open_nudge": self.did_no_open_nudge,
            "terminal": self.is_terminal,
        }

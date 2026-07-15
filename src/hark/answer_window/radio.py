"""Radio Answer Window session: state machine + segment text / partial HEP.

Unit-testable without audio hardware. Capture loop wiring lands in later tasks;
this module owns states, events, pure transitions, segment STT join, partial
emit (HEP shapes via :mod:`hark.partial`), and listen_end / listen_control
as session internals (E2.T003).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from hark.answer_window.deps import AnswerWindowDeps
from hark.answer_window.policy import AnswerWindowPolicy, effective_radio_idle_s
from hark.answer_window.result import ListenResult
from hark.answer_window.text_join import (
    join_radio_stt_segments,
    monotonic_partial_text,
    prefer_complete_transcript,
)
from hark.listen_end import EndMode, PhraseHit, evaluate_radio_transcript
from hark.partial import make_partial_event, partial_fragment


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
    """Radio-profile session: owns state, segment join, partial HEP, end/control.

    No STT provider *types* on the public interface — only optional string
    provider names and callables for partials. Phrase evaluation stays pure
    via :func:`hark.listen_end.evaluate_radio_transcript`; agent finish/cancel
    uses injectable deps (or :mod:`hark.listen_control` defaults). Capture
    loop still lives in ``speech.run_listen`` until later E2 tasks move it
    behind ``open()``.
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
    last_partial_event: dict[str, Any] | None = field(default=None, repr=False)
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
        """Record a segment transcript without join/monotonic (legacy helper)."""
        body = (text or "").strip()
        if body:
            self.text_segments.append(body)

    def joined_body(self) -> str:
        """Assemble cumulative text from per-segment STT (overlap-trimmed)."""
        return join_radio_stt_segments(self.text_segments)

    def ingest_segment_transcript(
        self, text: str, *, provider: str | None = None
    ) -> str:
        """Append non-empty segment STT, join, return monotonic cumulative body.

        ``provider`` is accepted for call-site symmetry but not stored as a
        provider type — only the optional name string is used by callers.
        """
        del provider  # interface only; last provider stays on the capture loop
        body = (text or "").strip()
        if body:
            self.text_segments.append(body)
        joined = self.joined_body()
        # Fallback to raw segment when join is empty but STT returned something
        candidate = joined or body
        return monotonic_partial_text(self.last_partial_text, candidate)

    def finalize_joined_body(
        self, full_stt_text: str | None = None
    ) -> str:
        """Joined + monotonic body; optionally merge a full-audio re-STT candidate.

        Full re-STT may only *extend* completeness — never replace a longer
        joined partial body with a shorter rewrite.
        """
        body = monotonic_partial_text(self.last_partial_text, self.joined_body())
        if full_stt_text is not None:
            body = prefer_complete_transcript(
                body, (full_stt_text or "").strip()
            )
        return body

    def note_partial_emitted(self, text: str) -> None:
        self.partial_seq += 1
        self.last_partial_text = text or ""

    def emit_partial_if_needed(
        self,
        body: str,
        *,
        provider: str | None = None,
        stt_seq: int | None = None,
        on_partial: Callable[[dict[str, Any]], None] | None = None,
        streaming: bool | None = None,
        streaming_ack_min_quiet_s: float | None = None,
        partial_kind: str | None = None,
    ) -> bool:
        """Build and emit a partial HEP if policy allows and body advanced.

        Returns True when a partial was emitted. Uses policy/deps defaults when
        optional kwargs are omitted. Preserves existing HEP field shapes
        (HOLD vs streaming strings via :func:`hark.partial.make_partial_event`).
        """
        body_so_far = (body or "").strip()
        if not self.policy.stream_partials:
            return False
        if not body_so_far or body_so_far == self.last_partial_text:
            return False
        cb = on_partial if on_partial is not None else self.deps.on_partial
        if cb is None:
            return False

        stream = self.stream_id
        if not stream:
            return False

        is_streaming = (
            bool(self.policy.streaming) if streaming is None else bool(streaming)
        )
        kind = partial_kind if partial_kind is not None else self.policy.partial_kind
        ack = (
            streaming_ack_min_quiet_s
            if streaming_ack_min_quiet_s is not None
            else self.policy.streaming_ack_min_quiet_s
        )

        prev_body = self.last_partial_text
        frag = partial_fragment(prev_body, body_so_far)
        self.partial_seq += 1
        self.last_partial_text = body_so_far
        ev = make_partial_event(
            stream_id=stream,
            seq=self.partial_seq,
            text=body_so_far,
            kind=kind,
            provider=provider,
            fragment=frag,
            prev_text=prev_body,
            streaming=is_streaming,
            ack_min_quiet_s=(ack if is_streaming else None),
        )
        if stt_seq is not None:
            ev["stt_seq"] = stt_seq
        self.last_partial_event = ev
        try:
            cb(ev)
        except Exception:
            pass
        return True

    def history(self) -> list[tuple[RadioState, RadioEvent, RadioState]]:
        return list(self._history)

    # --- listen_end (pure) + listen_control (IPC via deps) — E2.T003 ---

    def evaluate_transcript(self, text: str) -> PhraseHit | None:
        """Soft/hard end + cancel from policy phrase lists (pure; no I/O).

        Wraps :func:`hark.listen_end.evaluate_radio_transcript` with this
        session's policy. Priority remains cancel → product end → soft end.
        """
        return evaluate_radio_transcript(
            text,
            end_phrases=self.policy.end_phrases,
            cancel_phrases=self.policy.cancel_phrases,
            soft_end_phrases=self.policy.soft_end_phrases,
            soft_end_phrases_enabled=bool(self.policy.soft_end_phrases_enabled),
        )

    def poll_agent_action(self) -> str | None:
        """Non-destructive peek at agent finish/cancel (listen_control IPC)."""
        sid = self.stream_id
        if not sid:
            return None
        poll = self.deps.poll_listen_action
        if poll is not None:
            return poll(sid)
        from hark.listen_control import poll_listen_action

        return poll_listen_action(sid)

    def consume_agent_action(self) -> str | None:
        """Read and clear pending agent finish/cancel."""
        sid = self.stream_id
        if not sid:
            return None
        consume = self.deps.consume_listen_action
        if consume is not None:
            return consume(sid)
        from hark.listen_control import consume_listen_action

        return consume_listen_action(sid)

    def agent_wants_stop(self) -> bool:
        """True when the agent has requested finish or cancel (capture gate)."""
        return self.poll_agent_action() is not None

    def result_for_phrase_hit(
        self,
        hit: PhraseHit,
        *,
        text: str,
        provider: str,
        duration_ms: int,
    ) -> ListenResult:
        """Map a pure phrase hit to a terminal :class:`ListenResult`."""
        self.end_phrase = hit.phrase
        if hit.kind == "cancel":
            self.cancelled = True
            return ListenResult(
                text=hit.body,
                provider=provider,
                duration_ms=duration_ms,
                end_mode=EndMode.RADIO.value,
                end_phrase=hit.phrase,
                cancelled=True,
                stream_id=self.stream_id,
                partials_emitted=self.partial_seq,
            )
        body = hit.body if self.policy.strip_phrase else text
        return ListenResult(
            text=body,
            provider=provider,
            duration_ms=duration_ms,
            end_mode=EndMode.RADIO.value,
            end_phrase=hit.phrase,
            stream_id=self.stream_id,
            partials_emitted=self.partial_seq,
        )

    def result_for_agent_action(
        self,
        action: str,
        *,
        text: str = "",
        provider: str = "unknown",
        duration_ms: int = 0,
    ) -> ListenResult:
        """Map agent finish/cancel to a terminal :class:`ListenResult`."""
        body = (text or "").strip() or self.last_partial_text
        if action == "cancel":
            self.end_phrase = "agent:cancel"
            self.cancelled = True
            return ListenResult(
                text=body,
                provider=provider,
                duration_ms=duration_ms,
                end_mode=EndMode.RADIO.value,
                end_phrase="agent:cancel",
                cancelled=True,
                stream_id=self.stream_id,
                partials_emitted=self.partial_seq,
            )
        # finish (and any other non-cancel action treated as finish)
        self.end_phrase = "agent:finish"
        return ListenResult(
            text=body,
            provider=provider,
            duration_ms=duration_ms,
            end_mode=EndMode.RADIO.value,
            end_phrase="agent:finish",
            stream_id=self.stream_id,
            partials_emitted=self.partial_seq,
        )

    def handle_agent_or_phrase(
        self,
        text: str,
        *,
        provider: str,
        duration_ms: int,
        consume_agent: bool = True,
    ) -> ListenResult | None:
        """If agent action or end/cancel phrase ends the window, return result.

        Agent finish/cancel takes priority over phrase evaluation (same order
        as the radio capture loop). Returns ``None`` when listening continues.
        """
        act = (
            self.consume_agent_action()
            if consume_agent
            else self.poll_agent_action()
        )
        if act == "cancel":
            return self.result_for_agent_action(
                "cancel",
                text=text,
                provider=provider,
                duration_ms=duration_ms,
            )
        if act == "finish":
            return self.result_for_agent_action(
                "finish",
                text=text,
                provider=provider,
                duration_ms=duration_ms,
            )
        hit = self.evaluate_transcript(text)
        if hit is None:
            return None
        return self.result_for_phrase_hit(
            hit,
            text=text,
            provider=provider,
            duration_ms=duration_ms,
        )

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
            "segments": len(self.text_segments),
            "joined_len": len(self.joined_body()),
        }

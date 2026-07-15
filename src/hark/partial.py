"""Partial / turn transcript events for radio HOLD and streaming conversation."""

from __future__ import annotations

import secrets
import time
from typing import Any

from hark.events import new_event_id, utc_now_iso

HOLD_WARNING = (
    "PARTIAL TRANSCRIPT — not complete. More speech may still be captured. "
    "Do NOT send a final answer, do NOT speak to the user yet, and do NOT "
    "deliver to a Herdr pane based on this alone. You MAY begin private "
    "thinking/planning. Wait for the matching final event "
    "(same stream_id, partial=false / final=true)."
)

HOLD_INSTRUCTIONS = (
    "HOLD RESPONSE. This is an interim radio-mode partial. "
    "Do NOT TTS a full answer or deliver to a pane yet. "
    "If the cumulative text (or fragment) clearly ends with a done/stop signal "
    "and capture is still active, you MUST run agent_control.end_recording "
    "(finish) for this stream_id — e.g. ends with 'over', 'okay over', "
    "'okay hark send', 'that's all', 'send it', 'stop recording', 'message done'. "
    "Prefer finish when the thought is complete; use cancel_recording only to abort. "
    "Do NOT end on mid-clause false positives ('over the weekend', "
    "'send it to staging', 'that's all I know about X'). "
    "Otherwise HOLD and wait for the next partial or ambient.prompt "
    "(same stream_id, final=true)."
)

# B098 + B121: ambient.streaming = true — conversation mode (not radio-with-acks).
# Quiet-gated turns allow full interim TTS replies; pane delivery still waits for
# an explicit finalize / non-conversation final. B105 quiet gate still applies.
STREAMING_WARNING = (
    "CONVERSATION TURN / PARTIAL — operator is mid open conversation after wake. "
    "Streaming/conversation mode is ON: you MAY (and usually SHOULD) reply with "
    "full TTS answers or clarifying questions as understanding firms up. Hark "
    "holds TTS play until the operator has been quiet ~2s "
    "(streaming_ack_min_quiet_s) or the turn listen ends — continuous speech is "
    "not stepped on. Do NOT deliver to a Herdr pane based on this alone unless "
    "they clearly ask you to. Radio end-phrase structure is optional; do not wait "
    "for a special 'finished' final before speaking a real answer."
)

STREAMING_INSTRUCTIONS = (
    "CONVERSATION / STREAMING — full TTS reply allowed (pause-gated). "
    "This is an open post-wake conversation turn (or mid-turn partial). "
    "You MAY TTS a full answer, status, or clarifying question — not only short "
    "acks. Prefer one clear reply after a real pause; do not stack many acks "
    "while the operator is mid-sentence. "
    "Hark defers play until operator quiet ≥ ack_min_quiet_s (~2s) or listen ends "
    "(B105); mute-during-TTS still applies in that quiet window. "
    "Do NOT deliver to a Herdr pane unless they clearly ask. "
    "Soft/end phrases ('over', 'okay hark send', …) are optional for explicit "
    "session finalize — not required for a full answer. "
    "If capture is still active and text clearly ends with a done/stop signal, "
    "you MAY run agent_control.end_recording (finish) for this stream_id, or "
    "cancel_recording to abort / bleed. "
    "Do NOT end on mid-clause false positives ('over the weekend', "
    "'send it to staging', 'that's all I know about X'). "
    "After you reply: stop this turn; wait for the next Monitor event "
    "(next ambient.turn / partial / ambient.prompt / cancelled). "
    "Operator stays in conversation without re-saying the wake name."
)

# Compact monitor-feed strings (shorter than full HEP instructions).
HOLD_COMPACT_INSTRUCTIONS = (
    "RADIO PARTIAL — HOLD. Do not TTS a full answer. "
    "Use fragment for the new slice; text is cumulative. "
    "MUST: if text clearly ends with a done signal (over, okay hark send, "
    "that's all, send it, stop recording, message done, …) and stream "
    "still active → hark listen-end --stream-id <id> (finish, not cancel). "
    "No mid-clause false finishes. Then STOP; wait for next partial or final."
)

STREAMING_COMPACT_INSTRUCTIONS = (
    "CONVERSATION/STREAMING — full TTS reply ok after ~2s operator quiet. "
    "Not radio-with-acks-only: answer the turn; pane delivery still needs a clear ask. "
    "Use fragment for the new slice when present; text may be the full turn. "
    "Soft/end phrases optional for session end, not required for a full answer. "
    "Cancel with listen-end --cancel on bleed. Then STOP; wait for next turn/final."
)

TURN_INSTRUCTIONS = (
    "CONVERSATION TURN — operator quiet ended this turn; session stays open. "
    "Reply with hark tts (full answer OK). Do not require re-wake. "
    "Not bound to a pane unless they ask. Then idle for the next Monitor event "
    "(next ambient.turn, ambient.prompt finalize, or ambient.cancelled). "
    "Optional: product end phrases (okay hark send, hark over, end prompt) "
    "finalize the conversation session."
)

TURN_COMPACT_INSTRUCTIONS = (
    "CONVERSATION TURN — full TTS reply. Session stays open (no re-wake). "
    "Then idle for next turn/final/cancelled."
)


def partial_warning(*, streaming: bool = False) -> str:
    return STREAMING_WARNING if streaming else HOLD_WARNING


def partial_instructions(*, streaming: bool = False) -> str:
    return STREAMING_INSTRUCTIONS if streaming else HOLD_INSTRUCTIONS


def partial_compact_instructions(*, streaming: bool = False) -> str:
    return (
        STREAMING_COMPACT_INSTRUCTIONS if streaming else HOLD_COMPACT_INSTRUCTIONS
    )


def new_stream_id() -> str:
    return f"s{int(time.time() * 1000):x}{secrets.token_hex(3)}"


def partial_fragment(prev_text: str | None, full_text: str) -> str:
    """Delta since last partial; full text if STT replaced the body."""
    full = (full_text or "").strip()
    prev = (prev_text or "").strip()
    if not full:
        return ""
    if not prev:
        return full
    if full.startswith(prev):
        return full[len(prev) :].lstrip()
    # STT rewrote earlier words — surface full body as the fragment
    return full


def make_partial_event(
    *,
    stream_id: str,
    seq: int,
    text: str,
    kind: str = "ambient.partial",
    provider: str | None = None,
    phrase: str | None = None,
    event_id: str | None = None,
    fragment: str | None = None,
    prev_text: str | None = None,
    streaming: bool = False,
    ack_min_quiet_s: float | None = None,
) -> dict[str, Any]:
    from hark.listen_control import agent_control_block

    frag = fragment
    if frag is None:
        frag = partial_fragment(prev_text, text)

    streaming = bool(streaming)
    # B105: surface quiet gate duration on streaming partials (default 2s).
    if streaming and ack_min_quiet_s is None:
        ack_min_quiet_s = 2.0
    ev: dict[str, Any] = {
        "schema": "hark.event.v1",
        "kind": kind,
        "event_id": event_id or new_event_id(),
        "observed_at": utc_now_iso(),
        "partial": True,
        "final": False,
        "stream_id": stream_id,
        "seq": seq,
        "text": text,
        "fragment": frag,
        "text_len": len(text or ""),
        "phrase": phrase,
        "provider": provider,
        "streaming": streaming,
        "warning": partial_warning(streaming=streaming),
        "instructions": partial_instructions(streaming=streaming),
        "agent_control": agent_control_block(stream_id),
    }
    if streaming and ack_min_quiet_s is not None:
        ev["ack_min_quiet_s"] = float(ack_min_quiet_s)
    return ev


def make_turn_event(
    *,
    stream_id: str,
    text: str,
    conversation_id: str,
    turn: int,
    provider: str | None = None,
    phrase: str | None = None,
    event_id: str | None = None,
    ack_min_quiet_s: float | None = 2.0,
    listen: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Quiet-gated conversation turn (B121/B122) — full TTS reply OK; session open."""
    ack = 2.0 if ack_min_quiet_s is None else float(ack_min_quiet_s)
    return {
        "schema": "hark.event.v1",
        "kind": "ambient.turn",
        "event_id": event_id or new_event_id(),
        "observed_at": utc_now_iso(),
        "partial": False,
        "final": False,
        "streaming": True,
        "conversation": True,
        "conversation_id": conversation_id,
        "turn": int(turn),
        "stream_id": stream_id,
        "text": text,
        "text_len": len(text or ""),
        "phrase": phrase,
        "provider": provider,
        "ack_min_quiet_s": ack,
        "listen": listen,
        "warning": None,
        "instructions": TURN_INSTRUCTIONS,
    }


def make_final_event(
    *,
    stream_id: str,
    text: str,
    kind: str = "ambient.prompt",
    provider: str | None = None,
    phrase: str | None = None,
    event_id: str | None = None,
    listen: dict[str, Any] | None = None,
    cancelled: bool = False,
    end_phrase: str | None = None,
    partials_emitted: int = 0,
) -> dict[str, Any]:
    return {
        "schema": "hark.event.v1",
        "kind": kind if not cancelled else "ambient.cancelled",
        "event_id": event_id or new_event_id(),
        "observed_at": utc_now_iso(),
        "partial": False,
        "final": True,
        "stream_id": stream_id,
        "text": text,
        "phrase": phrase,
        "provider": provider,
        "end_phrase": end_phrase,
        "listen": listen,
        "partials_emitted": partials_emitted,
        "warning": None,
        "instructions": (
            "FINAL transcript for this stream_id. Supersedes all prior partials. "
            "You may now respond / act."
            if not cancelled
            else "Cancelled — do not act on partials for this stream_id."
        ),
    }

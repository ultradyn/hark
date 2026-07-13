"""Partial transcript events for radio-mode streaming to Mode A agents."""

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
) -> dict[str, Any]:
    from hark.listen_control import agent_control_block

    frag = fragment
    if frag is None:
        frag = partial_fragment(prev_text, text)

    return {
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
        "warning": HOLD_WARNING,
        "instructions": HOLD_INSTRUCTIONS,
        "agent_control": agent_control_block(stream_id),
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

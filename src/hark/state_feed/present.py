"""Single Mode-A / harness Monitor presentation profile (read edge)."""

from __future__ import annotations

from typing import Any

from hark.events import monitor_profile

_CONVERSATION_END_DIAGNOSTIC_LIMITS = {
    "listen_error": 240,
    "error_type": 80,
    "failure_stream_id": 160,
    "last_event_id": 160,
    "last_stream_id": 160,
    "last_text": 240,
    "last_provider": 120,
}


def _bounded_diagnostic(value: Any, max_chars: int) -> str | None:
    return value[:max_chars] if isinstance(value, str) and value else None


def present_for_monitor(event: dict[str, Any]) -> dict[str, Any]:
    """Single HEP presentation profile for harness Monitors.

    Unifies agent/watch ``monitor_profile`` and ambient/tts compact branches.
    Applied once at the presentation edge (monitor emit), not twice.
    """
    kind = str(event.get("kind") or "")
    if kind.startswith("agent.") or kind in (
        "watch.armed",
        "watch.error",
        "target.invalidated",
    ):
        return monitor_profile(event)

    compact: dict[str, Any] = {
        "schema": event.get("schema") or "hark.event.v1",
        "kind": kind,
        "event_id": event.get("event_id"),
        "observed_at": event.get("observed_at"),
    }

    if kind == "ambient.prompt":
        text = event.get("text")
        if isinstance(text, str) and len(text) > 400:
            text = text[:397] + "…"
        compact.update(
            {
                "phrase": event.get("phrase"),
                "text": text,
                "stream_id": event.get("stream_id"),
                "conversation_id": event.get("conversation_id"),
                "turn": event.get("turn"),
                "final": True,
                "partial": False,
                "instructions": (
                    "FINAL operator voice prompt. Reply with hark tts. "
                    "Not bound to a pane unless they ask. Then idle for next Monitor event."
                ),
            }
        )
    elif kind == "ambient.turn":
        from hark.partial import TURN_COMPACT_INSTRUCTIONS

        text = event.get("text")
        if isinstance(text, str) and len(text) > 400:
            text = text[:397] + "…"
        compact.update(
            {
                "phrase": event.get("phrase"),
                "text": text,
                "stream_id": event.get("stream_id"),
                "conversation_id": event.get("conversation_id"),
                "turn": event.get("turn"),
                "partial": False,
                "final": False,
                "streaming": True,
                "conversation": True,
                "instructions": event.get("instructions") or TURN_COMPACT_INSTRUCTIONS,
            }
        )
        if event.get("ack_min_quiet_s") is not None:
            try:
                compact["ack_min_quiet_s"] = float(event["ack_min_quiet_s"])
            except (TypeError, ValueError):
                compact["ack_min_quiet_s"] = 2.0
    elif kind == "ambient.conversation_end":
        compact.update(
            {
                "phrase": event.get("phrase"),
                "conversation_id": event.get("conversation_id"),
                "turns": event.get("turns"),
                "reason": event.get("reason"),
                "final": True,
                "partial": False,
                "streaming": True,
                "instructions": event.get("instructions")
                or ("Conversation session ended. Wake re-armed; no new prompt."),
            }
        )
        compact.update(
            {
                key: _bounded_diagnostic(event.get(key), max_chars)
                for key, max_chars in _CONVERSATION_END_DIAGNOSTIC_LIMITS.items()
            }
        )
        if isinstance(event.get("last_turn"), int) and not isinstance(
            event.get("last_turn"), bool
        ):
            compact["last_turn"] = event["last_turn"]
    elif kind == "ambient.partial":
        from hark.partial import partial_compact_instructions

        text = event.get("text")
        full_len = len(text) if isinstance(text, str) else 0
        if isinstance(text, str) and len(text) > 400:
            text = text[:397] + "…"
        frag = event.get("fragment")
        if isinstance(frag, str) and len(frag) > 240:
            frag = frag[:237] + "…"
        streaming = bool(event.get("streaming"))
        compact.update(
            {
                "stream_id": event.get("stream_id"),
                "seq": event.get("seq"),
                # Prefer delta for the orchestrator / logs; keep full body truncated as text
                "fragment": frag if frag is not None else text,
                "text": text,
                "text_len": full_len or None,
                "partial": True,
                "final": False,
                "streaming": streaming,
                "instructions": partial_compact_instructions(streaming=streaming),
            }
        )
        # B105: surface quiet gate when streaming
        if streaming and event.get("ack_min_quiet_s") is not None:
            try:
                compact["ack_min_quiet_s"] = float(event["ack_min_quiet_s"])
            except (TypeError, ValueError):
                compact["ack_min_quiet_s"] = 2.0
    elif kind == "ambient.wake_near_miss":
        attempts = event.get("attempts") or []
        texts = []
        if isinstance(attempts, list):
            for a in attempts[:5]:
                if isinstance(a, dict) and a.get("text"):
                    texts.append(str(a["text"]))
                elif isinstance(a, str):
                    texts.append(a)
        compact.update(
            {
                "count": event.get("count"),
                "total_near_misses": event.get("total_near_misses"),
                "group_index": event.get("group_index"),
                "attempts": texts or attempts,
                "priority": event.get("priority", 35),
                "instructions": (
                    "Failed wake attempt(s) — not a prompt. Review attempts; "
                    "learning may auto-expand aliases (wake_learned). "
                    "Optional: adjust ambient names / extra_trigger_phrases. See docs/CUSTOM_WAKE.md."
                ),
            }
        )
    elif kind == "ambient.wake_learned":
        compact.update(
            {
                "learn_kind": event.get("learn_kind"),
                "value": event.get("value"),
                "canonical": event.get("canonical"),
                "wake_mode": event.get("wake_mode"),
                "instructions": (
                    "Learned a new wake alternate (no restart). "
                    "Optional: pin in config (names / trigger_phrases)."
                ),
            }
        )
    elif kind == "ambient.error":
        compact.update(
            {
                "error": event.get("error") or event.get("message"),
                "reason": event.get("reason"),
                "phrase": event.get("phrase"),
                "stream_id": event.get("stream_id"),
                "instructions": "Ambient/listen error — speak briefly if useful; fix or retry.",
            }
        )
    elif kind in ("ambient.cancelled", "ambient.reloaded", "ambient.armed"):
        compact.update(
            {
                "phrase": event.get("phrase"),
                "wake_mode": event.get("wake_mode"),
                "names": event.get("names"),
                "phrases": event.get("phrases"),
                "instructions": event.get("instructions")
                or f"{kind}: informational; continue idle with monitors armed.",
            }
        )
    elif kind == "tts.truncated":
        compact.update(
            {
                "original_chars": event.get("original_chars"),
                "kept_chars": event.get("kept_chars"),
                "max_chars": event.get("max_chars"),
                "text_preview": event.get("text_preview"),
                "instructions": event.get("instructions")
                or (
                    "TTS text was truncated to tts.max_chars. Full agent text was NOT spoken. "
                    "Raise [tts].max_chars (0=unlimited) or shorten the reply."
                ),
            }
        )
    elif kind == "tts.chunked":
        compact.update(
            {
                "chars": event.get("chars"),
                "n_chunks": event.get("n_chunks"),
                "chunk_chars": event.get("chunk_chars"),
                "instructions": event.get("instructions")
                or "Long TTS multi-chunk play (informational).",
            }
        )
    else:
        # Pass through compact non-null subset
        for key in (
            "text",
            "phrase",
            "error",
            "stream_id",
            "instructions",
            "priority",
        ):
            if event.get(key) is not None:
                compact[key] = event[key]

    return {k: v for k, v in compact.items() if v is not None}

"""Hark Event Protocol (HEP) v1 builders and monitor profile."""

from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Any

from hark import __schema__
from hark.fingerprint import question_fingerprint
from hark.herdr.client import AgentInfo
from hark.risk import classify_question


def new_event_id() -> str:
    # ULID-ish: time prefix + random (not full ULID; stable enough for v1)
    ms = int(time.time() * 1000)
    return f"{ms:011x}{secrets.token_hex(8)}"


def utc_now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def make_watch_armed(
    sessions: list[str],
    *,
    transport: str,
    statuses: list[str],
) -> dict[str, Any]:
    return {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": "watch.armed",
        "priority": 10,
        "sessions": sessions,
        "transport": transport,
        "statuses": statuses,
        "disposition": "info",
        "instructions": (
            "Use the hark skill; do not invent answers. "
            "On agent.blocked: hark context <session>/<pane>, then ask/answer."
        ),
    }


def make_watch_heartbeat(sessions: list[str]) -> dict[str, Any]:
    return {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": "watch.heartbeat",
        "priority": 0,
        "sessions": sessions,
        "disposition": "info",
    }


def make_watch_error(session_id: str, message: str) -> dict[str, Any]:
    return {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": "watch.error",
        "priority": 90,
        "session_id": session_id,
        "error": message,
        "disposition": "error",
    }


def make_target_invalidated(
    session_id: str,
    pane_id: str,
    *,
    reason: str,
    event_ids: list[str],
) -> dict[str, Any]:
    return {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": "target.invalidated",
        "priority": 90,
        "session_id": session_id,
        "target": {"server_instance": session_id, "pane_id": pane_id},
        "reason": reason,
        "invalidated_event_ids": event_ids,
        "disposition": "invalidated",
    }


def agent_to_target(agent: AgentInfo) -> dict[str, Any]:
    return {
        "server_instance": agent.session_id,
        "workspace_id": agent.workspace_id or "",
        "tab_id": agent.tab_id or "",
        "pane_id": agent.pane_id,
        "pane_revision": agent.revision,
        "terminal_id": agent.terminal_id,
        "agent": agent.agent,
        "agent_session": None,
        "friendly_name": None,
    }


def extract_question_excerpt(text: str, max_chars: int = 500) -> str:
    """Strip ANSI lightly and take trailing ask block."""
    # crude ANSI strip
    import re

    cleaned = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text or "")
    lines = [ln.rstrip() for ln in cleaned.splitlines()]
    # drop empty trailing
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    # take last ~25 non-empty-ish lines
    block = lines[-40:]
    excerpt = "\n".join(block).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[-max_chars:]
    return excerpt


def make_agent_status_event(
    agent: AgentInfo,
    *,
    from_status: str | None,
    to_status: str,
    question_text: str | None = None,
    choices: list[str] | None = None,
) -> dict[str, Any]:
    kind = "agent.state_changed"
    priority = 40
    if to_status == "blocked":
        kind = "agent.blocked"
        priority = 80
    elif to_status == "done":
        kind = "agent.completed"
        priority = 50

    q_text = question_text or ""
    risk = classify_question(q_text, choices)
    fp = question_fingerprint(q_text, choices) if q_text else None

    event: dict[str, Any] = {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": kind,
        "priority": priority,
        "session_id": agent.session_id,
        "target": agent_to_target(agent),
        "state": {
            "from": from_status,
            "to": to_status,
            "blocked_epoch": None,
        },
        "disposition": "pending" if kind == "agent.blocked" else "info",
    }
    if q_text or kind == "agent.blocked":
        event["question"] = {
            "kind": risk.kind,
            "text": q_text[:2000] if q_text else None,
            "choices": choices,
            "fingerprint": fp,
            "confidence": risk.confidence,
            "risk": risk.risk,
        }
    return event


def monitor_profile(event: dict[str, Any]) -> dict[str, Any]:
    """Compact --for-monitor line: no secrets, no huge transcripts."""
    target = event.get("target") or {}
    question = event.get("question") or {}
    q_text = question.get("text")
    if isinstance(q_text, str) and len(q_text) > 240:
        q_text = q_text[:237] + "…"

    session_id = event.get("session_id") or target.get("server_instance")
    pane_id = target.get("pane_id")
    compact: dict[str, Any] = {
        "schema": __schema__,
        "kind": event.get("kind"),
        "event_id": event.get("event_id"),
        "observed_at": event.get("observed_at"),
        "session_id": session_id,
        "agent": target.get("agent"),
        "name": target.get("friendly_name"),
        "pane_id": pane_id,
        "status_to": (event.get("state") or {}).get("to"),
    }
    if q_text:
        compact["question"] = q_text
    if question.get("risk"):
        compact["risk"] = question["risk"]
    if question.get("fingerprint"):
        compact["fingerprint"] = question["fingerprint"]

    if event.get("kind") == "agent.blocked" and session_id and pane_id:
        compact["instructions"] = (
            "Use the hark skill; do not invent an answer. "
            f"hark context {session_id}/{pane_id}"
        )
    elif event.get("kind") == "agent.completed":
        compact["instructions"] = (
            "Done event: judge if finished; do not auto-announce. "
            f"Optional: hark context {session_id}/{pane_id}"
        )
    elif event.get("kind") == "watch.armed":
        compact["sessions"] = event.get("sessions")
        compact["instructions"] = event.get("instructions")
    elif event.get("kind") == "watch.error":
        compact["error"] = event.get("error")
        compact["session_id"] = event.get("session_id")

    # Drop nulls for compactness
    return {k: v for k, v in compact.items() if v is not None}

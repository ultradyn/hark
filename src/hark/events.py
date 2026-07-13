"""Hark Event Protocol (HEP) v1 builders and monitor profile."""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from hark import __schema__
from hark.fingerprint import question_fingerprint
from hark.herdr.client import AgentInfo
from hark.risk import classify_question

# Statuses that Herdr may report while a human menu is still on screen (false done).
_IDLE_LIKE = frozenset({"done", "idle", "completed", "complete"})

# Numbered / lettered choice lines (menus).
_MENU_LINE = re.compile(r"^\s*(?:\d+|[A-Da-d])[\).\]]\s+\S", re.M)
# Explicit choice / reply prompts.
_CHOICE_PHRASE = re.compile(
    r"(?i)\b("
    r"which\s+option|which\s+one|choose\s+one|select\s+(an?\s+)?option|"
    r"pick\s+one|reply\s+with|respond\s+with|enter\s+(a\s+)?number|"
    r"type\s+(a\s+)?number|press\s+\d|select\s+\d|"
    r"option\s*[1-9]|choices?\s*:|menu\s*:"
    r")\b"
)
# Yes/no style still awaiting input.
_YN_PROMPT = re.compile(
    r"(?i)(\[?\s*y\s*/\s*n\s*\]?|\byes\s*/\s*no\b|\b\(y/n\)\b|"
    r"\ballow\b.+\?|\bdo\s+you\s+want\b.+\?|\bproceed\b.+\?)"
)
# Trailing question that looks like it needs an answer (not rhetorical dump).
_TRAILING_QUESTION = re.compile(r"\?\s*$")


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


@dataclass(frozen=True)
class PendingQuestionHit:
    """Result of trailing-pane pending-question heuristics (false-done path)."""

    matched: bool
    reasons: tuple[str, ...]
    choices: tuple[str, ...] = ()
    confidence: float = 0.0

    def __bool__(self) -> bool:
        return self.matched


def looks_like_pending_question(text: str | None) -> PendingQuestionHit:
    """Heuristic: does trailing pane text still look like it needs human input?

    Used when Herdr reports done/idle but the bottom of the pane still shows a
    multi-option menu or explicit ask (false done / false idle).
    """
    raw = (text or "").strip()
    if not raw:
        return PendingQuestionHit(matched=False, reasons=())

    reasons: list[str] = []
    choices: list[str] = []
    confidence = 0.0

    menu_lines = [m.group(0).strip() for m in _MENU_LINE.finditer(raw)]
    if len(menu_lines) >= 2:
        reasons.append("numbered_menu")
        choices = menu_lines[:12]
        confidence = max(confidence, 0.9)
    elif len(menu_lines) == 1:
        reasons.append("single_menu_line")
        choices = menu_lines
        confidence = max(confidence, 0.55)

    if _CHOICE_PHRASE.search(raw):
        reasons.append("choice_phrase")
        confidence = max(confidence, 0.85)

    if _YN_PROMPT.search(raw):
        reasons.append("yes_no_prompt")
        confidence = max(confidence, 0.8)

    # Question mark in trailing chunk + menu/choice already strong; alone weaker
    # unless there is also a short last line ending in ?
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    trailing = "\n".join(lines[-8:]) if lines else raw
    if _TRAILING_QUESTION.search(trailing) and (
        "numbered_menu" in reasons
        or "choice_phrase" in reasons
        or "yes_no_prompt" in reasons
        or re.search(r"(?i)\b(which|what|should\s+i|do\s+you|allow|confirm)\b", trailing)
    ):
        if "trailing_question" not in reasons:
            reasons.append("trailing_question")
        confidence = max(confidence, 0.75 if "numbered_menu" in reasons else 0.65)

    # Require a solid signal: menu (≥2 lines), or choice/yn phrase, or
    # single menu line + question-ish trail.
    matched = False
    if "numbered_menu" in reasons:
        matched = True
    elif "choice_phrase" in reasons and (
        "trailing_question" in reasons
        or "single_menu_line" in reasons
        or "yes_no_prompt" in reasons
        or len(raw) < 800
    ):
        matched = True
        confidence = max(confidence, 0.8)
    elif "yes_no_prompt" in reasons:
        matched = True
    elif "single_menu_line" in reasons and "trailing_question" in reasons:
        matched = True
        confidence = max(confidence, 0.7)

    if not matched:
        return PendingQuestionHit(matched=False, reasons=tuple(reasons), confidence=confidence)

    return PendingQuestionHit(
        matched=True,
        reasons=tuple(reasons),
        choices=tuple(choices),
        confidence=min(1.0, confidence or 0.7),
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
            "On agent.blocked or agent.needs_input: hark context <session>/<pane>, "
            "then ask/answer. needs_input may fire when status is done/idle but the "
            "pane still shows a menu (false done)."
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


def make_agent_needs_input(
    agent: AgentInfo,
    *,
    from_status: str | None,
    to_status: str,
    question_text: str,
    hit: PendingQuestionHit | None = None,
    choices: list[str] | None = None,
) -> dict[str, Any]:
    """Synthetic re-block: status is done/idle but pane still needs a human.

    Mode A should treat this like agent.blocked (speak + answer). Bound for
    ``hark answer`` when a fingerprint is present.
    """
    choice_list = list(choices) if choices is not None else list(hit.choices if hit else ())
    q_text = question_text or ""
    risk = classify_question(q_text, choice_list or None)
    fp = question_fingerprint(q_text, choice_list or None) if q_text else None
    reasons = list(hit.reasons) if hit else ["false_done"]
    conf = hit.confidence if hit else 0.7

    return {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": "agent.needs_input",
        "priority": 80,
        "session_id": agent.session_id,
        "target": agent_to_target(agent),
        "state": {
            "from": from_status,
            "to": to_status,
            "blocked_epoch": None,
        },
        "disposition": "pending",
        "false_done": True,
        "pending_reasons": reasons,
        "question": {
            "kind": risk.kind,
            "text": q_text[:2000] if q_text else None,
            "choices": choice_list or None,
            "fingerprint": fp,
            "confidence": max(risk.confidence, conf),
            "risk": risk.risk,
        },
        "instructions": (
            "False done / idle with pending human question on pane. "
            "Treat like agent.blocked: do not invent an answer. "
            f"hark context {agent.session_id}/{agent.pane_id}"
        ),
    }


def make_agent_question_changed(
    agent: AgentInfo,
    *,
    to_status: str,
    question_text: str,
    choices: list[str] | None = None,
) -> dict[str, Any]:
    """Still blocked (or needs input); the ask fingerprint changed."""
    q_text = question_text or ""
    risk = classify_question(q_text, choices)
    fp = question_fingerprint(q_text, choices) if q_text else None
    return {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": "agent.question_changed",
        "priority": 75,
        "session_id": agent.session_id,
        "target": agent_to_target(agent),
        "state": {
            "from": to_status,
            "to": to_status,
            "blocked_epoch": None,
        },
        "disposition": "pending",
        "question": {
            "kind": risk.kind,
            "text": q_text[:2000] if q_text else None,
            "choices": choices,
            "fingerprint": fp,
            "confidence": risk.confidence,
            "risk": risk.risk,
        },
    }


def is_idle_like_status(status: str | None) -> bool:
    return (status or "").strip().lower() in _IDLE_LIKE


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
    if event.get("false_done"):
        compact["false_done"] = True
    if event.get("pending_reasons"):
        compact["pending_reasons"] = event["pending_reasons"]

    kind = event.get("kind")
    if kind in ("agent.blocked", "agent.needs_input") and session_id and pane_id:
        if kind == "agent.needs_input":
            compact["instructions"] = (
                "False done: pane still needs input. Treat like blocked. "
                f"hark context {session_id}/{pane_id}"
            )
        else:
            compact["instructions"] = (
                "Use the hark skill; do not invent an answer. "
                f"hark context {session_id}/{pane_id}"
            )
    elif kind == "agent.question_changed" and session_id and pane_id:
        compact["instructions"] = (
            "Question changed while still awaiting input. "
            f"hark context {session_id}/{pane_id}"
        )
    elif kind == "agent.completed":
        compact["instructions"] = (
            "Done event: judge if finished; do not auto-announce. "
            "If pane still shows a menu, treat as needs-input. "
            f"Optional: hark context {session_id}/{pane_id}"
        )
    elif kind == "watch.armed":
        compact["sessions"] = event.get("sessions")
        compact["instructions"] = event.get("instructions")
    elif kind == "watch.error":
        compact["error"] = event.get("error")
        compact["session_id"] = event.get("session_id")

    # Drop nulls for compactness
    return {k: v for k, v in compact.items() if v is not None}

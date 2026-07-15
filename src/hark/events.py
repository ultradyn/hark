"""Hark Event Protocol (HEP) v1 builders and monitor profile."""

from __future__ import annotations

import re
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from hark import __schema__
from hark.fingerprint import question_fingerprint
from hark.herdr.client import AgentInfo
from hark.pane_understanding.heuristics import (
    ActiveSubagentsHit as ActiveSubagentsHit,
    PendingQuestionHit as PendingQuestionHit,
    detect_active_subagents as detect_active_subagents,
    looks_like_pending_question as looks_like_pending_question,
)
from hark.risk import classify_question

# Statuses that Herdr may report while a human menu is still on screen (false done).
_IDLE_LIKE = frozenset({"done", "idle", "completed", "complete"})

# Pending-question / busy-subagent heuristics live in
# hark.pane_understanding.heuristics (re-exported above for back-compat).


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
    self_target: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
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
            "On agent.blocked / agent.needs_input / agent.question_changed: prefer "
            "embedded pane_capture.text when present; optional live re-read via "
            "hark context <session>/<pane>. needs_input may fire when status is "
            "done/idle but the pane still shows a menu (false done)."
        ),
    }
    if self_target:
        # Own pane (hark runs inside herdr): excluded from watch to avoid loops.
        event["self_target"] = self_target
    return event


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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Defaults for full pane capture on agent wake HEP (overridable via config).
DEFAULT_PANE_CAPTURE_LINES = 100
DEFAULT_PANE_CAPTURE_MAX_CHARS = 12000
# Compact monitor may still pass a large body so Mode A can decide without
# a second fetch; slightly below full default so logs stay bounded.
MONITOR_PANE_CAPTURE_MAX_CHARS = 12000


def strip_ansi(text: str | None) -> str:
    return _ANSI_RE.sub("", text or "")


def extract_question_excerpt(text: str, max_chars: int = 500) -> str:
    """Strip ANSI lightly and take trailing ask block."""
    cleaned = strip_ansi(text)
    lines = [ln.rstrip() for ln in cleaned.splitlines()]
    # drop empty trailing
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return ""
    # take last ~40 lines (viewport-ish ask block)
    block = lines[-40:]
    excerpt = "\n".join(block).strip()
    if len(excerpt) > max_chars:
        excerpt = excerpt[-max_chars:]
    return excerpt


def prepare_pane_capture(
    text: str | None,
    *,
    max_lines: int = DEFAULT_PANE_CAPTURE_LINES,
    max_chars: int = DEFAULT_PANE_CAPTURE_MAX_CHARS,
    source: str = "recent-unwrapped",
) -> dict[str, Any] | None:
    """Bound recent pane text for HEP attachment (Mode A wake events).

    Prefer the same recent-unwrapped body Herdr exposes via ``agent read`` /
    ``hark context``. Returns None when empty after cleaning.
    """
    cleaned = strip_ansi(text)
    lines = [ln.rstrip() for ln in cleaned.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return None

    max_lines = max(1, int(max_lines))
    max_chars = max(64, int(max_chars))
    truncated = False
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    body = "\n".join(lines).strip()
    if not body:
        return None
    if len(body) > max_chars:
        body = body[-max_chars:]
        truncated = True
        # Avoid mid-line junk when char-capping from the end.
        nl = body.find("\n")
        if 0 <= nl < 200:
            body = body[nl + 1 :]
    return {
        "text": body,
        "line_count": body.count("\n") + 1 if body else 0,
        "char_count": len(body),
        "truncated": truncated,
        "source": source,
    }


def _attach_pane_capture(
    event: dict[str, Any],
    pane_capture: dict[str, Any] | None,
) -> dict[str, Any]:
    if not pane_capture or not isinstance(pane_capture.get("text"), str):
        return event
    if not pane_capture["text"].strip():
        return event
    event["pane_capture"] = pane_capture
    return event


def _wake_instructions(
    kind: str,
    session_id: str,
    pane_id: str,
    *,
    has_capture: bool,
) -> str:
    target = f"{session_id}/{pane_id}"
    if kind == "agent.needs_input":
        base = (
            "False done / idle with pending human question on pane. "
            "Treat like agent.blocked: do not invent an answer."
        )
    elif kind == "agent.question_changed":
        base = "Question changed while still awaiting input. Do not invent an answer."
    else:
        base = "Use the hark skill; do not invent an answer."
    if has_capture:
        return (
            f"{base} Pane capture attached (pane_capture.text) — decide from it when "
            f"sufficient. Optional live re-read: hark context {target}"
        )
    return f"{base} hark context {target}"


def make_agent_status_event(
    agent: AgentInfo,
    *,
    from_status: str | None,
    to_status: str,
    question_text: str | None = None,
    choices: list[str] | None = None,
    pane_capture: dict[str, Any] | None = None,
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
    if kind in ("agent.blocked", "agent.completed") and (
        pane_capture or kind == "agent.blocked"
    ):
        has_cap = bool(pane_capture and pane_capture.get("text"))
        if kind == "agent.blocked":
            event["instructions"] = _wake_instructions(
                kind, agent.session_id, agent.pane_id, has_capture=has_cap
            )
    _attach_pane_capture(event, pane_capture)
    return event


def make_agent_needs_input(
    agent: AgentInfo,
    *,
    from_status: str | None,
    to_status: str,
    question_text: str,
    hit: PendingQuestionHit | None = None,
    choices: list[str] | None = None,
    pane_capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Synthetic re-block: status is done/idle but pane still needs a human.

    The orchestrator should treat this like agent.blocked (speak + answer). Bound for
    ``hark answer`` when a fingerprint is present.
    """
    choice_list = list(choices) if choices is not None else list(hit.choices if hit else ())
    q_text = question_text or ""
    risk = classify_question(q_text, choice_list or None)
    fp = question_fingerprint(q_text, choice_list or None) if q_text else None
    reasons = list(hit.reasons) if hit else ["false_done"]
    conf = hit.confidence if hit else 0.7
    has_cap = bool(pane_capture and pane_capture.get("text"))

    event: dict[str, Any] = {
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
        "instructions": _wake_instructions(
            "agent.needs_input",
            agent.session_id,
            agent.pane_id,
            has_capture=has_cap,
        ),
    }
    _attach_pane_capture(event, pane_capture)
    return event


def make_agent_busy_subagent(
    agent: AgentInfo,
    *,
    from_status: str | None,
    herdr_status: str,
    hit: ActiveSubagentsHit,
    pane_capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Herdr reports done/idle but the pane still shows active Tasks/subagents.

    Reclassifies to ``working`` so Mode A does not treat the turn as finished.
    ``state.herdr`` retains the wire status from Herdr for diagnostics.
    """
    has_cap = bool(pane_capture and pane_capture.get("text"))
    n = max(1, int(hit.count or 1))
    reasons = list(hit.reasons) if hit.reasons else ["active_subagents"]
    target = f"{agent.session_id}/{agent.pane_id}"
    event: dict[str, Any] = {
        "schema": __schema__,
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "kind": "agent.state_changed",
        "priority": 35,
        "session_id": agent.session_id,
        "target": agent_to_target(agent),
        "state": {
            "from": from_status,
            "to": "working",
            "blocked_epoch": None,
            "herdr": herdr_status,
        },
        "disposition": "info",
        "false_done": True,
        "busy_subagent": True,
        "subagents_running": n,
        "pending_reasons": reasons,
        "instructions": (
            "False done: pane still has active subagent/tasks "
            f"({n} running). Treat as working until tasks settle; "
            "do not announce completion or act as if the turn finished."
            + (
                " Pane capture attached (pane_capture.text)."
                if has_cap
                else f" Optional live re-read: hark context {target}"
            )
        ),
    }
    if hit.labels:
        event["subagent_labels"] = list(hit.labels)
    _attach_pane_capture(event, pane_capture)
    return event


def make_agent_question_changed(
    agent: AgentInfo,
    *,
    to_status: str,
    question_text: str,
    choices: list[str] | None = None,
    pane_capture: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Still blocked (or needs input); the ask fingerprint changed."""
    q_text = question_text or ""
    risk = classify_question(q_text, choices)
    fp = question_fingerprint(q_text, choices) if q_text else None
    has_cap = bool(pane_capture and pane_capture.get("text"))
    event: dict[str, Any] = {
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
        "instructions": _wake_instructions(
            "agent.question_changed",
            agent.session_id,
            agent.pane_id,
            has_capture=has_cap,
        ),
    }
    _attach_pane_capture(event, pane_capture)
    return event


def is_idle_like_status(status: str | None) -> bool:
    return (status or "").strip().lower() in _IDLE_LIKE


def monitor_profile(event: dict[str, Any]) -> dict[str, Any]:
    """Compact --for-monitor line: no secrets, no huge transcripts.

    Tolerates legacy/malformed wire shapes: ``question`` or ``target`` may be a
    plain string (older watch lines / partial normalizers) instead of objects.
    """
    raw_target = event.get("target")
    if isinstance(raw_target, dict):
        target: dict[str, Any] = raw_target
    elif isinstance(raw_target, str) and raw_target.strip():
        # "session/pane" or bare pane
        parts = raw_target.split("/", 1)
        if len(parts) == 2:
            target = {"server_instance": parts[0].strip(), "pane_id": parts[1].strip()}
        else:
            target = {"pane_id": raw_target.strip()}
    else:
        target = {}

    raw_q = event.get("question")
    q_meta: dict[str, Any] = {}
    if isinstance(raw_q, dict):
        q_meta = raw_q
        q_text = raw_q.get("text")
    elif isinstance(raw_q, str):
        q_text = raw_q
    else:
        q_text = None
    if isinstance(q_text, str) and len(q_text) > 240:
        q_text = q_text[:237] + "…"

    state = event.get("state") if isinstance(event.get("state"), dict) else {}

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
        "status_to": state.get("to"),
    }
    if q_text:
        compact["question"] = q_text
    if q_meta.get("risk"):
        compact["risk"] = q_meta["risk"]
    if q_meta.get("fingerprint"):
        compact["fingerprint"] = q_meta["fingerprint"]
    if event.get("false_done"):
        compact["false_done"] = True
    if event.get("pending_reasons"):
        compact["pending_reasons"] = event["pending_reasons"]
    if event.get("busy_subagent"):
        compact["busy_subagent"] = True
    if event.get("subagents_running") is not None:
        compact["subagents_running"] = event["subagents_running"]
    if event.get("subagent_labels"):
        compact["subagent_labels"] = event["subagent_labels"]

    # Full pane text capture (B094) — prefer event body over a second context fetch.
    raw_cap = event.get("pane_capture")
    cap_text: str | None = None
    cap_truncated = False
    if isinstance(raw_cap, dict) and isinstance(raw_cap.get("text"), str):
        cap_text = raw_cap["text"]
        cap_truncated = bool(raw_cap.get("truncated"))
    elif isinstance(raw_cap, str) and raw_cap.strip():
        cap_text = raw_cap
    if cap_text is not None:
        body = cap_text
        if len(body) > MONITOR_PANE_CAPTURE_MAX_CHARS:
            body = body[-MONITOR_PANE_CAPTURE_MAX_CHARS:]
            cap_truncated = True
            nl = body.find("\n")
            if 0 <= nl < 200:
                body = body[nl + 1 :]
        compact["pane_capture"] = {
            "text": body,
            "char_count": len(body),
            "truncated": cap_truncated
            or (isinstance(raw_cap, dict) and bool(raw_cap.get("truncated"))),
            "source": (
                raw_cap.get("source")
                if isinstance(raw_cap, dict)
                else "recent-unwrapped"
            ),
        }
        if isinstance(raw_cap, dict) and raw_cap.get("line_count") is not None:
            compact["pane_capture"]["line_count"] = raw_cap.get("line_count")

    kind = event.get("kind")
    has_capture = bool(compact.get("pane_capture"))
    if kind in ("agent.blocked", "agent.needs_input") and session_id and pane_id:
        # Prefer event-level instructions (already capture-aware) when present.
        if isinstance(event.get("instructions"), str) and event["instructions"].strip():
            compact["instructions"] = event["instructions"]
        elif kind == "agent.needs_input":
            compact["instructions"] = _wake_instructions(
                "agent.needs_input", str(session_id), str(pane_id), has_capture=has_capture
            )
        else:
            compact["instructions"] = _wake_instructions(
                "agent.blocked", str(session_id), str(pane_id), has_capture=has_capture
            )
    elif kind == "agent.question_changed" and session_id and pane_id:
        if isinstance(event.get("instructions"), str) and event["instructions"].strip():
            compact["instructions"] = event["instructions"]
        else:
            compact["instructions"] = _wake_instructions(
                "agent.question_changed",
                str(session_id),
                str(pane_id),
                has_capture=has_capture,
            )
    elif kind == "agent.state_changed" and event.get("busy_subagent"):
        if isinstance(event.get("instructions"), str) and event["instructions"].strip():
            compact["instructions"] = event["instructions"]
        else:
            n = event.get("subagents_running") or 1
            compact["instructions"] = (
                f"Busy subagent: {n} task(s) still running; treat as working, not done. "
                f"Optional: hark context {session_id}/{pane_id}"
            )
    elif kind == "agent.completed":
        if has_capture:
            compact["instructions"] = (
                "Done event: judge if finished; do not auto-announce. "
                "Pane capture attached — if it still shows a menu, treat as needs-input; "
                "if it still shows active Tasks/subagents, treat as working. "
                f"Optional live re-read: hark context {session_id}/{pane_id}"
            )
        else:
            compact["instructions"] = (
                "Done event: judge if finished; do not auto-announce. "
                "If pane still shows a menu, treat as needs-input; "
                "if active Tasks/subagents remain, treat as working. "
                f"Optional: hark context {session_id}/{pane_id}"
            )
    elif kind == "watch.armed":
        compact["sessions"] = event.get("sessions")
        compact["instructions"] = event.get("instructions")
        if event.get("self_target"):
            compact["self_target"] = event["self_target"]
    elif kind == "watch.error":
        compact["error"] = event.get("error")
        compact["session_id"] = event.get("session_id")

    # Drop nulls for compactness
    return {k: v for k, v in compact.items() if v is not None}

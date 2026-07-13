"""Bound event store for hark answer (fingerprint + revision checks)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hark.paths import state_dir


@dataclass
class BoundEvent:
    event_id: str
    session_id: str
    pane_id: str
    pane_revision: int
    question_fingerprint: str | None
    question_text: str | None = None
    risk: str | None = None
    status: str = "pending"  # pending | delivered | rejected | skipped
    created_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


class DeliveryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (state_dir() / "events.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._deliveries = self.path.parent / "deliveries.jsonl"

    def save_event(self, event: BoundEvent) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(event), separators=(",", ":")) + "\n")

    def get(self, event_id: str) -> BoundEvent | None:
        if not self.path.is_file():
            return None
        found: BoundEvent | None = None
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("event_id") == event_id:
                    found = BoundEvent(**{
                        k: data[k]
                        for k in BoundEvent.__dataclass_fields__
                        if k in data
                    })
        if found is not None:
            status = self._latest_statuses().get(event_id)
            if status:
                found.status = status
        return found

    def register_from_hep(self, hep: dict[str, Any]) -> BoundEvent:
        target = hep.get("target") or {}
        question = hep.get("question") or {}
        ev = BoundEvent(
            event_id=str(hep.get("event_id") or uuid.uuid4().hex),
            session_id=str(hep.get("session_id") or target.get("server_instance") or "local"),
            pane_id=str(target.get("pane_id") or ""),
            pane_revision=int(target.get("pane_revision") or 0),
            question_fingerprint=question.get("fingerprint"),
            question_text=question.get("text"),
            risk=question.get("risk"),
            meta={"kind": hep.get("kind")},
        )
        self.save_event(ev)
        return ev

    def mark(self, event_id: str, status: str, **extra: Any) -> None:
        rec = {
            "event_id": event_id,
            "status": status,
            "ts": time.time(),
            **extra,
        }
        with self._deliveries.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def _latest_statuses(self) -> dict[str, str]:
        statuses: dict[str, str] = {}
        if not self._deliveries.is_file():
            return statuses
        with self._deliveries.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = data.get("event_id")
                status = data.get("status")
                if isinstance(event_id, str) and isinstance(status, str):
                    statuses[event_id] = status
        return statuses

    def invalidate_target(
        self, session_id: str, pane_id: str, *, reason: str
    ) -> list[BoundEvent]:
        """Mark still-pending bound events for a removed/moved target invalid."""
        if not self.path.is_file():
            return []
        statuses = self._latest_statuses()
        invalidated: list[BoundEvent] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = data.get("event_id")
                if (
                    isinstance(event_id, str)
                    and data.get("session_id") == session_id
                    and data.get("pane_id") == pane_id
                    and statuses.get(event_id, "pending") == "pending"
                ):
                    self.mark(event_id, "invalidated", reason=reason)
                    invalidated.append(
                        BoundEvent(
                            **{
                                key: data[key]
                                for key in BoundEvent.__dataclass_fields__
                                if key in data
                            }
                        )
                    )
        return invalidated

    def already_delivered(self, event_id: str) -> bool:
        if not self._deliveries.is_file():
            return False
        with self._deliveries.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("event_id") == event_id and data.get("status") == "delivered":
                    return True
        return False

    def pending_events(self) -> list[dict[str, Any]]:
        """Return the current pending bound events (the multi-session queue).

        Uses the authoritative latest status from ``deliveries.jsonl`` so events
        that have been delivered/skipped/rejected/invalidated are excluded — not
        just delivered ones. Deduplicated by ``event_id`` keeping the latest
        registration.
        """
        if not self.path.is_file():
            return []
        seen: dict[str, dict[str, Any]] = {}
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = data.get("event_id")
                if isinstance(eid, str) and eid:
                    seen[eid] = data
        statuses = self._latest_statuses()
        out: list[dict[str, Any]] = []
        for eid, data in seen.items():
            status = statuses.get(eid, data.get("status", "pending"))
            if status == "pending":
                out.append(data)
        return out


def summarize_pending(pending: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize pending queue events for TTS announcement.

    Counts **distinct targets** (``session_id``/``pane_id``) so multiple records
    for one pane never inflate the count and answers/counts never merge across
    panes. Returns ``count``, the distinct ``targets``, and a spoken
    ``announcement`` phrase.
    """
    targets: list[str] = []
    seen: set[tuple[str, str]] = set()
    for p in pending:
        session_id = str(p.get("session_id") or "").strip()
        pane_id = str(p.get("pane_id") or "").strip()
        if not pane_id:
            # Malformed / unroutable event — never fold into a shared "/" target
            # (would undercount). A real blocked agent always has a pane.
            continue
        key = (session_id, pane_id)
        if key in seen:
            continue
        seen.add(key)
        targets.append(f"{session_id}/{pane_id}")
    count = len(targets)
    return {
        "count": count,
        "targets": targets,
        "announcement": queue_announcement(count),
    }


def queue_announcement(count: int) -> str:
    """Build a natural spoken phrase for ``count`` waiting agents."""
    if count <= 0:
        return "No agents are waiting for input."
    if count == 1:
        return "One agent is waiting for input."
    return f"{count} agents are waiting for input."

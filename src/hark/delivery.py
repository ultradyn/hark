"""Bound event store for hark answer (fingerprint + revision checks)."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from hark.paths import state_dir

# Undelivered bound events older than this are treated as stale for queue
# listing/announce (B101). Override with env HARK_QUEUE_MAX_AGE_S (seconds).
DEFAULT_QUEUE_MAX_AGE_S = 4 * 3600


def queue_max_age_s(override: float | None = None) -> float:
    """Resolve queue max-age: explicit override, env, or default."""
    if override is not None:
        return max(0.0, float(override))
    raw = (os.environ.get("HARK_QUEUE_MAX_AGE_S") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return float(DEFAULT_QUEUE_MAX_AGE_S)


def event_age_s(data: dict[str, Any], *, now: float | None = None) -> float:
    """Age of a bound event in seconds. Missing/invalid created_at → infinite."""
    ts = now if now is not None else time.time()
    created = data.get("created_at")
    try:
        return max(0.0, float(ts) - float(created))
    except (TypeError, ValueError):
        return float("inf")


@dataclass
class BoundEvent:
    event_id: str
    session_id: str
    pane_id: str
    pane_revision: int
    question_fingerprint: str | None
    question_text: str | None = None
    risk: str | None = None
    status: str = "pending"  # pending | delivered | rejected | skipped | expired | invalidated
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

    def _all_pending_raw(self) -> list[dict[str, Any]]:
        """All status=pending events (latest registration per event_id)."""
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

    def classify_pending(
        self,
        *,
        max_age_s: float | None = None,
        now: float | None = None,
        dedupe_targets: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """Split pending events into fresh vs stale for a trustworthy queue.

        Fresh: within ``max_age_s`` (default :func:`queue_max_age_s`) and, when
        ``dedupe_targets``, the newest pending record per (session_id, pane_id).

        Stale reasons (stored under ``_stale_reason`` on each stale record):
        - ``max_age`` — older than TTL
        - ``superseded`` — older pending for same target (newer event exists)
        - ``no_pane`` — missing pane_id (unroutable)
        """
        age_limit = queue_max_age_s(max_age_s)
        ts = now if now is not None else time.time()
        raw = self._all_pending_raw()

        # Annotate age; separate unroutable / aged-out first.
        candidates: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        for data in raw:
            item = dict(data)
            age = event_age_s(item, now=ts)
            item["_age_s"] = age
            pane_id = str(item.get("pane_id") or "").strip()
            if not pane_id:
                item["_stale_reason"] = "no_pane"
                stale.append(item)
                continue
            if age_limit > 0 and age > age_limit:
                item["_stale_reason"] = "max_age"
                stale.append(item)
                continue
            candidates.append(item)

        if not dedupe_targets:
            return {"fresh": candidates, "stale": stale}

        # Keep newest per target; older same-target entries are superseded.
        by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in candidates:
            key = (
                str(item.get("session_id") or "").strip(),
                str(item.get("pane_id") or "").strip(),
            )
            by_target.setdefault(key, []).append(item)

        fresh: list[dict[str, Any]] = []
        for group in by_target.values():
            group.sort(
                key=lambda d: (
                    float(d.get("created_at") or 0.0),
                    str(d.get("event_id") or ""),
                )
            )
            *older, newest = group
            for o in older:
                o["_stale_reason"] = "superseded"
                stale.append(o)
            fresh.append(newest)

        # Stable-ish order: oldest fresh first (answer queue FIFO by target).
        fresh.sort(
            key=lambda d: (
                float(d.get("created_at") or 0.0),
                str(d.get("event_id") or ""),
            )
        )
        return {"fresh": fresh, "stale": stale}

    def pending_events(
        self,
        *,
        max_age_s: float | None = None,
        now: float | None = None,
        include_stale: bool = False,
        dedupe_targets: bool = True,
    ) -> list[dict[str, Any]]:
        """Return the current pending bound events (the multi-session queue).

        Uses the authoritative latest status from ``deliveries.jsonl`` so events
        that have been delivered/skipped/rejected/invalidated/expired are
        excluded. Deduplicated by ``event_id`` keeping the latest registration.

        By default (B101) only **fresh** events are returned: within the queue
        max age and at most one (newest) per target. Pass ``include_stale=True``
        for the raw pending set (still excluding non-pending statuses).
        """
        if include_stale:
            # Raw pending; still strip internal-only keys if any.
            return self._all_pending_raw()
        classified = self.classify_pending(
            max_age_s=max_age_s, now=now, dedupe_targets=dedupe_targets
        )
        # Drop internal annotation keys from public pending list.
        out: list[dict[str, Any]] = []
        for item in classified["fresh"]:
            out.append({k: v for k, v in item.items() if not str(k).startswith("_")})
        return out

    def prune(
        self,
        *,
        max_age_s: float | None = None,
        now: float | None = None,
        dedupe_targets: bool = True,
        is_answerable: Callable[[dict[str, Any]], tuple[bool, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Expire stale pending events so the queue stays trustworthy.

        Marks each stale (age / superseded / no_pane) event as ``expired`` in
        ``deliveries.jsonl``. When ``is_answerable`` is provided, also expires
        still-fresh events that fail the live check (pane gone / not blocked).

        Returns the list of expired event dicts (with ``_stale_reason``).
        """
        classified = self.classify_pending(
            max_age_s=max_age_s, now=now, dedupe_targets=dedupe_targets
        )
        expired: list[dict[str, Any]] = []

        for item in classified["stale"]:
            eid = item.get("event_id")
            if not isinstance(eid, str) or not eid:
                continue
            reason = str(item.get("_stale_reason") or "stale")
            self.mark(eid, "expired", reason=reason)
            expired.append(item)

        if is_answerable is not None:
            for item in classified["fresh"]:
                eid = item.get("event_id")
                if not isinstance(eid, str) or not eid:
                    continue
                try:
                    ok, reason = is_answerable(item)
                except Exception as exc:  # noqa: BLE001 — prune must be fail-soft
                    ok, reason = True, f"check_error:{exc}"
                if ok:
                    continue
                tagged = dict(item)
                tagged["_stale_reason"] = reason or "not_answerable"
                self.mark(eid, "expired", reason=tagged["_stale_reason"])
                expired.append(tagged)

        return expired


def summarize_pending(
    pending: list[dict[str, Any]],
    *,
    stale: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize pending queue events for TTS announcement.

    Counts **distinct targets** (``session_id``/``pane_id``) so multiple records
    for one pane never inflate the count and answers/counts never merge across
    panes. Returns ``count``, the distinct ``targets``, optional stale stats,
    and a spoken ``announcement`` phrase (fresh count only).
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

    stale_list = stale or []
    stale_targets: list[str] = []
    stale_seen: set[tuple[str, str]] = set()
    for p in stale_list:
        session_id = str(p.get("session_id") or "").strip()
        pane_id = str(p.get("pane_id") or "").strip()
        if not pane_id:
            continue
        key = (session_id, pane_id)
        if key in stale_seen or key in seen:
            continue
        stale_seen.add(key)
        stale_targets.append(f"{session_id}/{pane_id}")

    return {
        "count": count,
        "targets": targets,
        "stale_count": len(stale_list),
        "stale_targets": stale_targets,
        "announcement": queue_announcement(count, stale_count=len(stale_list)),
    }


def queue_announcement(count: int, *, stale_count: int = 0) -> str:
    """Build a natural spoken phrase for ``count`` waiting agents.

    ``count`` is the fresh/answerable target count. When stale items were
    filtered out, append a short aside so operators know the queue was cleaned
    for noise (B101) without overstating urgency.
    """
    if count <= 0:
        base = "No agents are waiting for input."
    elif count == 1:
        base = "One agent is waiting for input."
    else:
        base = f"{count} agents are waiting for input."
    if stale_count > 0 and count > 0:
        return f"{base} {stale_count} stale queue items ignored."
    if stale_count > 0 and count <= 0:
        return f"{base} {stale_count} stale queue items ignored."
    return base

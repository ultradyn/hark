"""Bound event store for hark answer (fingerprint + revision checks)."""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from hark.paths import state_dir

# Undelivered bound events older than this are treated as stale for queue
# listing/announce (B101). Override with env HARK_QUEUE_MAX_AGE_S (seconds).
DEFAULT_QUEUE_MAX_AGE_S = 4 * 3600

# An owner that has not advanced its durable state in this long may be
# superseded before it reaches the irreversible send boundary.  Once an owner
# has entered ``sending``, expiry becomes ``uncertain`` instead of retryable.
DEFAULT_DELIVERY_OWNER_STALE_S = 30.0
_ACTIVE_DELIVERY_STATES = frozenset({"acquired", "validating", "sending"})
_DELIVERY_TRANSITIONS = {
    "acquired": frozenset({"validating", "rejected"}),
    "validating": frozenset({"sending", "rejected"}),
    "sending": frozenset({"delivered", "uncertain"}),
}


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
    # pending | delivered | rejected | skipped | expired | invalidated
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryClaim:
    """Result of atomically trying to own one bound-event delivery."""

    owned: bool
    status: str
    token: str | None = None
    reason: str | None = None


class DeliveryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (state_dir() / "events.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._deliveries = self.path.parent / "deliveries.jsonl"
        self._delivery_lock = self.path.parent / "deliveries.lock"

    @contextmanager
    def _locked_delivery_state(self) -> Iterator[None]:
        """Serialize delivery state changes across threads and processes."""
        fd = os.open(self._delivery_lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _append_delivery_unlocked(self, record: dict[str, Any]) -> None:
        """Append and fsync one transition while ``_delivery_lock`` is held."""
        created = not self._deliveries.exists()
        with self._deliveries.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if created:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            parent_fd = os.open(self._deliveries.parent, flags)
            try:
                # File fsync makes the contents durable; directory fsync makes
                # the first deliveries.jsonl directory entry durable.
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    def _latest_delivery_unlocked(self, event_id: str) -> dict[str, Any] | None:
        latest: dict[str, Any] | None = None
        if not self._deliveries.is_file():
            return None
        with self._deliveries.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict) and data.get("event_id") == event_id:
                    latest = data
        return latest

    @staticmethod
    def _owner_is_stale(
        record: dict[str, Any], *, now: float, stale_after_s: float
    ) -> bool:
        try:
            age = max(0.0, now - float(record.get("ts")))
        except (TypeError, ValueError):
            return True
        if age >= stale_after_s:
            return True

        try:
            pid = int(record.get("owner_pid"))
        except (TypeError, ValueError):
            return True
        if pid == os.getpid():
            try:
                owner_thread = int(record.get("owner_thread"))
            except (TypeError, ValueError):
                return False
            return not any(
                thread.ident == owner_thread for thread in threading.enumerate()
            )
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return True
        return False

    def acquire_delivery(
        self,
        event_id: str,
        *,
        now: float | None = None,
        stale_after_s: float = DEFAULT_DELIVERY_OWNER_STALE_S,
        owner_token: str | None = None,
        owner_pid: int | None = None,
        owner_thread: int | None = None,
        event_status: str = "pending",
    ) -> DeliveryClaim:
        """Atomically acquire durable ownership before validation and send.

        ``acquired`` and ``validating`` owners may be superseded after their
        lease expires (or their process/thread dies).  An abandoned ``sending``
        record is instead made terminally ``uncertain`` because the write may
        already have landed.
        """
        ts = time.time() if now is None else float(now)
        token = owner_token or uuid.uuid4().hex
        pid = os.getpid() if owner_pid is None else int(owner_pid)
        thread_id = (
            threading.get_ident()
            if owner_thread is None and pid == os.getpid()
            else owner_thread
        )
        stale_after = max(0.0, float(stale_after_s))

        with self._locked_delivery_state():
            latest = self._latest_delivery_unlocked(event_id)
            if latest is None and event_status != "pending":
                if event_status == "delivered":
                    return DeliveryClaim(False, "delivered", reason="already_delivered")
                if event_status == "uncertain":
                    return DeliveryClaim(
                        False, "uncertain", reason="delivery_uncertain"
                    )
                return DeliveryClaim(
                    False,
                    "rejected",
                    reason=f"not_pending:{event_status}",
                )
            if latest is not None:
                status = str(latest.get("status") or "")
                reason = latest.get("reason")
                reason_s = str(reason) if reason is not None else None
                if status == "delivered":
                    return DeliveryClaim(False, status, reason="already_delivered")
                if status == "uncertain":
                    return DeliveryClaim(False, status, reason=reason_s)
                if status not in _ACTIVE_DELIVERY_STATES:
                    return DeliveryClaim(
                        False, "rejected", reason=reason_s or f"not_pending:{status}"
                    )
                if not self._owner_is_stale(latest, now=ts, stale_after_s=stale_after):
                    return DeliveryClaim(
                        False, "in_progress", reason="delivery_in_progress"
                    )
                if status == "sending":
                    reason_s = "owner_lost_after_send_started"
                    self._append_delivery_unlocked(
                        {
                            "event_id": event_id,
                            "status": "uncertain",
                            "ts": ts,
                            "reason": reason_s,
                            "owner_token": latest.get("owner_token"),
                            "owner_pid": latest.get("owner_pid"),
                            "owner_thread": latest.get("owner_thread"),
                        }
                    )
                    return DeliveryClaim(False, "uncertain", reason=reason_s)

            record: dict[str, Any] = {
                "event_id": event_id,
                "status": "acquired",
                "ts": ts,
                "owner_token": token,
                "owner_pid": pid,
            }
            if thread_id is not None:
                record["owner_thread"] = int(thread_id)
            if latest is not None:
                record["recovered_from"] = latest.get("status")
                record["previous_owner_token"] = latest.get("owner_token")
            self._append_delivery_unlocked(record)
            return DeliveryClaim(True, "acquired", token=token)

    def advance_delivery(
        self,
        event_id: str,
        owner_token: str,
        status: str,
        *,
        now: float | None = None,
        **extra: Any,
    ) -> bool:
        """Durably compare-and-set an owned delivery transition."""
        ts = time.time() if now is None else float(now)
        with self._locked_delivery_state():
            latest = self._latest_delivery_unlocked(event_id)
            if latest is None or latest.get("owner_token") != owner_token:
                return False
            previous = str(latest.get("status") or "")
            if status not in _DELIVERY_TRANSITIONS.get(previous, frozenset()):
                return False
            record = {
                "event_id": event_id,
                "status": status,
                "ts": ts,
                "owner_token": owner_token,
                "owner_pid": latest.get("owner_pid"),
                "owner_thread": latest.get("owner_thread"),
                **extra,
            }
            self._append_delivery_unlocked(record)
            return True

    def current_delivery(
        self, event_id: str, *, event_status: str = "pending"
    ) -> DeliveryClaim:
        """Read the stable delivery outcome without acquiring or recovering."""
        with self._locked_delivery_state():
            latest = self._latest_delivery_unlocked(event_id)
            if latest is None:
                if event_status == "delivered":
                    return DeliveryClaim(False, "delivered", reason="already_delivered")
                if event_status == "uncertain":
                    return DeliveryClaim(
                        False, "uncertain", reason="delivery_uncertain"
                    )
                return DeliveryClaim(
                    False, "rejected", reason=f"not_pending:{event_status}"
                )
            status = str(latest.get("status") or "")
            reason = latest.get("reason")
            reason_s = str(reason) if reason is not None else None
            if status == "delivered":
                return DeliveryClaim(False, status, reason="already_delivered")
            if status == "uncertain":
                return DeliveryClaim(False, status, reason=reason_s)
            if status in _ACTIVE_DELIVERY_STATES:
                return DeliveryClaim(
                    False, "in_progress", reason="delivery_in_progress"
                )
            return DeliveryClaim(
                False, "rejected", reason=reason_s or f"not_pending:{status}"
            )

    def ensure_uncertain_after_send(
        self, event_id: str, owner_token: str, *, reason: str
    ) -> str:
        """Make a failed post-send CAS durably safe.

        This is intentionally stronger than an external terminal write: once
        the send boundary was crossed, any conflicting non-delivered state is
        unsafe to retry and must become ``uncertain``.
        """
        with self._locked_delivery_state():
            latest = self._latest_delivery_unlocked(event_id)
            current = str((latest or {}).get("status") or "")
            if current in ("delivered", "uncertain"):
                return current
            self._append_delivery_unlocked(
                {
                    "event_id": event_id,
                    "status": "uncertain",
                    "ts": time.time(),
                    "reason": reason,
                    "owner_token": owner_token,
                    "owner_pid": (latest or {}).get("owner_pid"),
                    "owner_thread": (latest or {}).get("owner_thread"),
                    "superseded_status": current or None,
                }
            )
            return "uncertain"

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
                    found = BoundEvent(
                        **{
                            k: data[k]
                            for k in BoundEvent.__dataclass_fields__
                            if k in data
                        }
                    )
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
            session_id=str(
                hep.get("session_id") or target.get("server_instance") or "local"
            ),
            pane_id=str(target.get("pane_id") or ""),
            pane_revision=int(target.get("pane_revision") or 0),
            question_fingerprint=question.get("fingerprint"),
            question_text=question.get("text"),
            risk=question.get("risk"),
            meta={"kind": hep.get("kind")},
        )
        self.save_event(ev)
        return ev

    def mark(self, event_id: str, status: str, **extra: Any) -> bool:
        """Apply an external terminal transition if it cannot race a send.

        External actions may fence an owner before the irreversible boundary,
        but they never replace ``sending`` or any already-terminal outcome.
        Returns whether the requested transition was appended.
        """
        with self._locked_delivery_state():
            latest = self._latest_delivery_unlocked(event_id)
            previous = str((latest or {}).get("status") or "pending")
            if previous == "sending" or (
                latest is not None
                and previous not in ("pending", "acquired", "validating")
            ):
                return False
            rec = {
                "event_id": event_id,
                "status": status,
                "ts": time.time(),
                **extra,
            }
            if previous in ("acquired", "validating"):
                rec["superseded_owner_token"] = (latest or {}).get("owner_token")
                rec["superseded_status"] = previous
            self._append_delivery_unlocked(rec)
            return True

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
                    if not self.mark(event_id, "invalidated", reason=reason):
                        continue
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
                if (
                    data.get("event_id") == event_id
                    and data.get("status") == "delivered"
                ):
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
            if self.mark(eid, "expired", reason=reason):
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
                if self.mark(eid, "expired", reason=tagged["_stale_reason"]):
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

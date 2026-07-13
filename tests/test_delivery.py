import time

from hark.delivery import (
    BoundEvent,
    DeliveryStore,
    event_age_s,
    queue_announcement,
    queue_max_age_s,
    summarize_pending,
)


def test_delivery_store_roundtrip(tmp_path):
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    ev = BoundEvent(
        event_id="evt1",
        session_id="local",
        pane_id="w1:p1",
        pane_revision=3,
        question_fingerprint="blake2b:abc",
        question_text="Allow?",
        risk="R2",
    )
    store.save_event(ev)
    got = store.get("evt1")
    assert got is not None
    assert got.pane_id == "w1:p1"
    assert store.already_delivered("evt1") is False
    store.mark("evt1", "delivered", text="no")
    assert store.already_delivered("evt1") is True


def test_register_from_hep(tmp_path):
    store = DeliveryStore(tmp_path / "e.jsonl")
    hep = {
        "event_id": "abc12345",
        "session_id": "local",
        "kind": "agent.blocked",
        "target": {"pane_id": "w1:p6", "pane_revision": 1, "server_instance": "local"},
        "question": {"text": "Allow?", "fingerprint": "blake2b:x", "risk": "R2"},
    }
    bound = store.register_from_hep(hep)
    assert bound.event_id == "abc12345"
    assert store.get("abc12345") is not None


def test_invalidate_target_marks_only_pending_events(tmp_path):
    store = DeliveryStore(tmp_path / "events.jsonl")
    pending = BoundEvent(
        event_id="pending",
        session_id="local",
        pane_id="w1:p1",
        pane_revision=3,
        question_fingerprint="blake2b:pending",
    )
    delivered = BoundEvent(
        event_id="delivered",
        session_id="local",
        pane_id="w1:p1",
        pane_revision=3,
        question_fingerprint="blake2b:delivered",
    )
    other = BoundEvent(
        event_id="other",
        session_id="work",
        pane_id="w1:p1",
        pane_revision=3,
        question_fingerprint="blake2b:other",
    )
    for event in (pending, delivered, other):
        store.save_event(event)
    store.mark("delivered", "delivered")

    invalidated = store.invalidate_target("local", "w1:p1", reason="pane.closed")

    assert [event.event_id for event in invalidated] == ["pending"]
    assert store.get("pending").status == "invalidated"
    assert store.get("delivered").status == "delivered"
    assert store.get("other").status == "pending"
    assert store.invalidate_target("local", "w1:p1", reason="pane.closed") == []


def _seed(store: DeliveryStore, event_id: str, pane_id: str, *, created_at: float, session_id="local"):
    store.save_event(
        BoundEvent(
            event_id=event_id,
            session_id=session_id,
            pane_id=pane_id,
            pane_revision=1,
            question_fingerprint=f"blake2b:{event_id}",
            created_at=created_at,
        )
    )


def test_pending_events_filters_max_age(tmp_path):
    """B101: aged-out pending events must not inflate the queue."""
    store = DeliveryStore(tmp_path / "events.jsonl")
    now = 1_700_000_000.0
    _seed(store, "old", "w1:p1", created_at=now - 10_000)
    _seed(store, "fresh", "w1:p2", created_at=now - 10)

    assert {e["event_id"] for e in store.pending_events(now=now, max_age_s=3600)} == {
        "fresh"
    }
    assert {
        e["event_id"] for e in store.pending_events(include_stale=True)
    } == {"old", "fresh"}


def test_pending_events_dedupes_superseded_per_target(tmp_path):
    """Multiple pending rows for one pane → only newest is fresh."""
    store = DeliveryStore(tmp_path / "events.jsonl")
    now = 1_700_000_000.0
    _seed(store, "old", "w1:p6", created_at=now - 100)
    _seed(store, "mid", "w1:p6", created_at=now - 50)
    _seed(store, "new", "w1:p6", created_at=now - 1)
    _seed(store, "other", "w1:p7", created_at=now - 1)

    pending = store.pending_events(now=now, max_age_s=3600)
    assert {e["event_id"] for e in pending} == {"new", "other"}

    classified = store.classify_pending(now=now, max_age_s=3600)
    reasons = {
        s["event_id"]: s["_stale_reason"] for s in classified["stale"]
    }
    assert reasons == {"old": "superseded", "mid": "superseded"}


def test_prune_expires_stale_and_live_unanswerable(tmp_path):
    store = DeliveryStore(tmp_path / "events.jsonl")
    now = 1_700_000_000.0
    _seed(store, "old", "w1:p1", created_at=now - 99_999)
    _seed(store, "superseded", "w1:p2", created_at=now - 20)
    _seed(store, "keep", "w1:p2", created_at=now - 5)
    _seed(store, "idle", "w1:p3", created_at=now - 5)

    def is_answerable(ev):
        if ev.get("event_id") == "idle":
            return False, "not_blocked"
        return True, "ok"

    expired = store.prune(
        now=now, max_age_s=3600, is_answerable=is_answerable
    )
    expired_ids = {e["event_id"] for e in expired}
    assert expired_ids == {"old", "superseded", "idle"}
    assert store.get("old").status == "expired"
    assert store.get("superseded").status == "expired"
    assert store.get("idle").status == "expired"
    assert store.get("keep").status == "pending"
    assert {e["event_id"] for e in store.pending_events(now=now, max_age_s=3600)} == {
        "keep"
    }


def test_summarize_and_announce_report_stale(tmp_path):
    fresh = [{"event_id": "a", "session_id": "local", "pane_id": "w1:p1"}]
    stale = [
        {"event_id": "b", "session_id": "local", "pane_id": "w1:p2"},
        {"event_id": "c", "session_id": "local", "pane_id": "w1:p2"},
    ]
    summary = summarize_pending(fresh, stale=stale)
    assert summary["count"] == 1
    assert summary["stale_count"] == 2
    assert "One agent" in summary["announcement"]
    assert "stale" in summary["announcement"]
    assert "No agents" in queue_announcement(0, stale_count=3)
    assert "3 stale" in queue_announcement(0, stale_count=3)


def test_event_age_and_max_age_env(monkeypatch):
    assert event_age_s({"created_at": 100.0}, now=150.0) == 50.0
    assert event_age_s({}, now=1.0) == float("inf")
    monkeypatch.setenv("HARK_QUEUE_MAX_AGE_S", "12")
    assert queue_max_age_s() == 12.0
    assert queue_max_age_s(99) == 99.0

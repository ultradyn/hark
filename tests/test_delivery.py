from hark.delivery import BoundEvent, DeliveryStore


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

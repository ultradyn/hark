from hark.config import SessionConfig
from hark.delivery import BoundEvent, DeliveryStore
from hark.watch import _handle_lifecycle_event


class _Client:
    session = SessionConfig(id="local")


def test_socket_lifecycle_invalidates_pending_target(tmp_path):
    store = DeliveryStore(tmp_path / "events.jsonl")
    store.save_event(
        BoundEvent(
            event_id="event-1",
            session_id="local",
            pane_id="w1:p1",
            pane_revision=1,
            question_fingerprint="blake2b:question",
        )
    )
    emitted = []

    handled = _handle_lifecycle_event(
        {"params": {"event": {"type": "pane.closed", "pane_id": "w1:p1"}}},
        client=_Client(),
        store=store,
        emit=emitted.append,
    )

    assert handled is True
    assert store._latest_statuses() == {"event-1": "invalidated"}
    assert emitted[0]["kind"] == "target.invalidated"
    assert emitted[0]["invalidated_event_ids"] == ["event-1"]

    _handle_lifecycle_event(
        {"type": "pane.closed", "pane_id": "w1:p1"},
        client=_Client(),
        store=store,
        emit=emitted.append,
    )
    assert emitted[1]["invalidated_event_ids"] == []

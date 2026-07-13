import io
import json

import hark.cli as cli
import hark.watch as watch
from hark.config import HarkConfig, SessionConfig
from hark.delivery import BoundEvent, DeliveryStore
from hark.herdr.client import AgentInfo, HerdrError


class FakeWatchStore:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def register_from_hep(self, event: dict[str, object]) -> None:
        self.events.append(event)


class FakeWatchClient:
    def __init__(self, session: SessionConfig, *, read_error: Exception | None = None) -> None:
        self.session = session
        self.read_error = read_error
        self.read_pane_calls: list[tuple[str, int]] = []

    def socket_exists(self) -> bool:
        return False

    def list_agents(self) -> list[AgentInfo]:
        return [
            AgentInfo(
                session_id=self.session.id,
                pane_id="w1:p1",
                agent="codex",
                status="blocked",
                revision=3,
            )
        ]

    def read_pane(self, pane_id: str, lines: int = 60) -> str:
        self.read_pane_calls.append((pane_id, lines))
        if self.read_error:
            raise self.read_error
        return "Allow this action?"


def _run_once(monkeypatch, *, read_error: Exception | None = None):
    client: FakeWatchClient | None = None

    def make_client(session: SessionConfig) -> FakeWatchClient:
        nonlocal client
        client = FakeWatchClient(session, read_error=read_error)
        return client

    store = FakeWatchStore()
    monkeypatch.setattr(watch, "HerdrClient", make_client)
    monkeypatch.setattr(watch, "DeliveryStore", lambda: store)
    out = io.StringIO()
    cfg = HarkConfig(sessions=[SessionConfig(id="local")])
    assert watch.run_watch(cfg, transport="poll", once=True, out=out) == 0
    assert client is not None
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    return client, store, events


def test_default_watch_reads_and_registers_fingerprinted_blocked_event(monkeypatch):
    client, store, events = _run_once(monkeypatch)

    blocked = next(event for event in events if event["kind"] == "agent.blocked")
    assert client.read_pane_calls == [("w1:p1", 40)]
    assert blocked["question"]["fingerprint"]
    assert store.events == [blocked]


def test_watch_emits_but_does_not_register_unbound_blocked_event(monkeypatch):
    client, store, events = _run_once(monkeypatch, read_error=HerdrError("unavailable"))

    blocked = next(event for event in events if event["kind"] == "agent.blocked")
    assert client.read_pane_calls == [("w1:p1", 40)]
    assert blocked["question"]["fingerprint"] is None
    assert store.events == []


def test_watch_cli_reads_questions_by_default_and_keeps_legacy_flag(monkeypatch):
    args = cli.build_parser().parse_args(["watch", "--once"])
    legacy_args = cli.build_parser().parse_args(["watch", "--once", "--read-questions"])
    calls: list[dict[str, object]] = []

    def fake_run_watch(cfg, **kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_watch", fake_run_watch)
    assert cli.dispatch(args, HarkConfig()) == 0
    assert cli.dispatch(legacy_args, HarkConfig()) == 0
    assert [call["read_questions"] for call in calls] == [True, True]


def test_socket_lifecycle_event_invalidates_bound_target(monkeypatch, tmp_path):
    from hark.herdr import socket_client

    store = DeliveryStore(tmp_path / "events.jsonl")
    store.save_event(
        BoundEvent(
            event_id="evt-pending",
            session_id="local",
            pane_id="w1:p1",
            pane_revision=3,
            question_fingerprint="blake2b:abc",
        )
    )
    client = FakeWatchClient(SessionConfig(id="local"))
    client.socket_path = tmp_path / "herdr.sock"
    emitted: list[dict[str, object]] = []
    raw = {
        "method": "events.notify",
        "params": {
            "event": {
                "type": "pane.closed",
                "data": {"pane": {"id": "w1:p1", "revision": 4}},
            }
        },
    }

    def fake_subscribe(_socket_path, on_event):
        on_event(raw)

    monkeypatch.setattr(socket_client, "run_subscribe_loop", fake_subscribe)

    assert watch._watch_socket(
        client,
        tracker=watch.EdgeTracker(),
        interest={"blocked"},
        emit=emitted.append,
        heartbeat_s=60,
        sessions=["local"],
        question_for=None,
        store=store,
    ) == 0

    invalidated = [event for event in emitted if event["kind"] == "target.invalidated"]
    assert len(invalidated) == 1
    assert invalidated[0]["session_id"] == "local"
    assert invalidated[0]["target"]["pane_id"] == "w1:p1"
    assert invalidated[0]["disposition"] == "invalidated"
    assert invalidated[0]["invalidated_event_ids"] == ["evt-pending"]
    assert store.get("evt-pending").status == "invalidated"

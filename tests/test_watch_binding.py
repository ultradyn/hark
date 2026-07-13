import io
import json

import hark.cli as cli
import hark.watch as watch
from hark.config import HarkConfig, SessionConfig
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

"""Watch excludes hark's own herdr pane (B029)."""

from __future__ import annotations

import io
import json

import hark.watch as watch
from hark.config import HarkConfig, SessionConfig
from hark.herdr.client import AgentInfo
from hark.self_detect import SelfIdentity


class FakeMultiClient:
    """Returns a self pane + a peer pane; records which panes get read."""

    def __init__(self, session: SessionConfig, socket_path: str = "/run/herdr.sock") -> None:
        self.session = session
        self.socket_path = socket_path
        self.read_pane_calls: list[str] = []

    def socket_exists(self) -> bool:
        return False

    def list_agents(self) -> list[AgentInfo]:
        return [
            AgentInfo(
                session_id=self.session.id,
                pane_id="wG:p3",  # hark's own pane
                agent="claude",
                status="blocked",
                revision=1,
            ),
            AgentInfo(
                session_id=self.session.id,
                pane_id="w1:p1",  # a monitored peer agent
                agent="codex",
                status="blocked",
                revision=2,
            ),
        ]

    def read_pane(self, pane_id: str, lines: int = 60) -> str:
        self.read_pane_calls.append(pane_id)
        return "Allow this action?"


def _run_once(monkeypatch, self_ident: SelfIdentity | None):
    client: FakeMultiClient | None = None

    def make_client(session: SessionConfig) -> FakeMultiClient:
        nonlocal client
        client = FakeMultiClient(session)
        return client

    monkeypatch.setattr(watch, "HerdrClient", make_client)
    monkeypatch.setattr(watch, "detect_self", lambda: self_ident)
    out = io.StringIO()
    cfg = HarkConfig(sessions=[SessionConfig(id="local")])
    assert watch.run_watch(cfg, transport="poll", once=True, out=out, register_events=False) == 0
    assert client is not None
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    return client, events


def test_self_pane_is_not_forwarded_or_read(monkeypatch):
    ident = SelfIdentity(pane_id="wG:p3", socket_path="/run/herdr.sock", session="local")
    client, events = _run_once(monkeypatch, ident)

    blocked = [e for e in events if e["kind"] == "agent.blocked"]
    panes = {e["session_id"] + "/" + e["target"]["pane_id"] for e in blocked}
    # Peer emitted, self never emitted.
    assert "local/w1:p1" in panes
    assert "local/wG:p3" not in panes
    # Self pane was never read (no reaction), peer was.
    assert "wG:p3" not in client.read_pane_calls
    assert "w1:p1" in client.read_pane_calls

    armed = next(e for e in events if e["kind"] == "watch.armed")
    assert armed["self_target"] == "local/wG:p3"


def test_without_self_all_panes_forwarded(monkeypatch):
    client, events = _run_once(monkeypatch, None)
    blocked = [e for e in events if e["kind"] == "agent.blocked"]
    panes = {e["target"]["pane_id"] for e in blocked}
    assert panes == {"wG:p3", "w1:p1"}
    armed = next(e for e in events if e["kind"] == "watch.armed")
    assert "self_target" not in armed


def test_self_on_different_socket_not_excluded(monkeypatch):
    # Self detected but on a different herdr socket -> the panes here are NOT self.
    ident = SelfIdentity(pane_id="wG:p3", socket_path="/some/other.sock", session="local")
    client, events = _run_once(monkeypatch, ident)
    blocked = [e for e in events if e["kind"] == "agent.blocked"]
    panes = {e["target"]["pane_id"] for e in blocked}
    assert panes == {"wG:p3", "w1:p1"}


def test_missing_self_socket_does_not_exclude_same_pane_on_another_server(monkeypatch):
    # Pane ids are scoped to a server; with no self socket, a local session
    # bearing the same pane id must remain visible rather than being hidden.
    ident = SelfIdentity(pane_id="wG:p3", socket_path=None, session="local")
    client, events = _run_once(monkeypatch, ident)
    blocked = [e for e in events if e["kind"] == "agent.blocked"]
    panes = {e["target"]["pane_id"] for e in blocked}
    assert panes == {"wG:p3", "w1:p1"}
    assert "wG:p3" in client.read_pane_calls


def test_multiple_sessions_exclude_only_the_matching_server(monkeypatch):
    clients: dict[str, FakeMultiClient] = {}

    def make_client(session: SessionConfig) -> FakeMultiClient:
        socket = "/run/herdr.sock" if session.id == "local" else "/run/other.sock"
        client = FakeMultiClient(session, socket_path=socket)
        clients[session.id] = client
        return client

    monkeypatch.setattr(watch, "HerdrClient", make_client)
    monkeypatch.setattr(
        watch,
        "detect_self",
        lambda: SelfIdentity(pane_id="wG:p3", socket_path="/run/herdr.sock"),
    )
    out = io.StringIO()
    cfg = HarkConfig(
        sessions=[
            SessionConfig(id="local"),
            SessionConfig(id="other", socket="/run/other.sock"),
        ]
    )

    assert watch.run_watch(cfg, transport="poll", once=True, out=out, register_events=False) == 0
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    blocked = [event for event in events if event["kind"] == "agent.blocked"]
    targets = {(event["session_id"], event["target"]["pane_id"]) for event in blocked}

    assert targets == {
        ("local", "w1:p1"),
        ("other", "wG:p3"),
        ("other", "w1:p1"),
    }
    assert clients["local"].read_pane_calls == ["w1:p1"]
    assert set(clients["other"].read_pane_calls) == {"wG:p3", "w1:p1"}


def test_socket_reconcile_and_wire_never_read_or_emit_self(monkeypatch):
    from hark.herdr import socket_client

    client = FakeMultiClient(SessionConfig(id="local"))
    emitted: list[dict[str, object]] = []

    def fake_subscribe(_socket_path, on_event):
        # This non-lifecycle notification exercises the on_wire refresh after
        # the initial reconcile has already run.
        on_event({"type": "pane.agent_status_changed"})

    monkeypatch.setattr(socket_client, "run_subscribe_loop", fake_subscribe)

    assert watch._watch_socket(
        client,
        classifier=watch.EdgeTracker(),
        interest={"blocked"},
        emit=emitted.append,
        heartbeat_s=60,
        sessions=["local"],
        read_pane=lambda agent: client.read_pane(agent.pane_id),
        store=None,
        self_ident=SelfIdentity(pane_id="wG:p3", socket_path="/run/herdr.sock"),
    ) == 0

    blocked = [event for event in emitted if event["kind"] == "agent.blocked"]
    assert [event["target"]["pane_id"] for event in blocked] == ["w1:p1"]
    assert client.read_pane_calls
    assert set(client.read_pane_calls) == {"w1:p1"}

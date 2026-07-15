import argparse

import hark.cli as cli
from hark.delivery import BoundEvent
from hark.exitcodes import ABORT
from hark.herdr.client import AgentInfo, HerdrError


class FakeStore:
    def __init__(self, bound: BoundEvent) -> None:
        self.bound = bound
        self.marks: list[tuple[str, str, dict[str, object]]] = []

    def get(self, event_id: str) -> BoundEvent:
        assert event_id == self.bound.event_id
        return self.bound

    def already_delivered(self, event_id: str) -> bool:
        assert event_id == self.bound.event_id
        return False

    def mark(self, event_id: str, status: str, **extra: object) -> None:
        self.marks.append((event_id, status, extra))


class FakeClient:
    def __init__(self, live: AgentInfo | None, *, read_error: Exception | None = None) -> None:
        self.live = live
        self.read_error = read_error
        self.get_agent_calls: list[str] = []
        self.read_pane_calls: list[tuple[str, int]] = []
        self.sent_text: list[tuple[str, str]] = []
        self.sent_keys: list[tuple[str, list[str]]] = []

    def get_agent(self, pane_id: str) -> AgentInfo | None:
        self.get_agent_calls.append(pane_id)
        return self.live

    def read_pane(self, pane_id: str, lines: int = 60) -> str:
        self.read_pane_calls.append((pane_id, lines))
        if self.read_error:
            raise self.read_error
        return "Allow this action?"

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent_text.append((pane_id, text))

    def send_keys(self, pane_id: str, keys: list[str]) -> None:
        self.sent_keys.append((pane_id, keys))


def _bound_event(*, fingerprint: str | None = "blake2b:bound", revision: int = 3) -> BoundEvent:
    return BoundEvent(
        event_id="evt1",
        session_id="local",
        pane_id="w1:p1",
        pane_revision=revision,
        question_fingerprint=fingerprint,
    )


def _live_agent(*, status: str = "blocked", revision: int = 3) -> AgentInfo:
    return AgentInfo(
        session_id="local",
        pane_id="w1:p1",
        agent="codex",
        status=status,
        revision=revision,
    )


def _answer_args() -> argparse.Namespace:
    return argparse.Namespace(event_id="evt1", text="yes", keys=None)


def _patch_answer_dependencies(monkeypatch, store: FakeStore, client: FakeClient) -> None:
    monkeypatch.setattr(cli, "DeliveryStore", lambda: store)
    monkeypatch.setattr(cli, "_client_for", lambda cfg, session_id: client)


def test_cmd_answer_rejects_when_fingerprint_read_fails(monkeypatch):
    store = FakeStore(_bound_event())
    client = FakeClient(_live_agent(), read_error=HerdrError("unavailable"))
    _patch_answer_dependencies(monkeypatch, store, client)

    assert cli.cmd_answer(_answer_args(), cfg=None) == ABORT
    assert store.marks == [("evt1", "rejected", {"reason": "fingerprint_unavailable"})]
    assert client.sent_text == []
    assert client.sent_keys == []


def test_cmd_answer_rejects_when_agent_is_no_longer_blocked(monkeypatch):
    store = FakeStore(_bound_event())
    client = FakeClient(_live_agent(status="working"))
    _patch_answer_dependencies(monkeypatch, store, client)

    assert cli.cmd_answer(_answer_args(), cfg=None) == ABORT
    assert store.marks == [("evt1", "rejected", {"reason": "not_compatible"})]
    # Live helper may read_pane before assess refuses on status; send must not run.
    assert client.sent_text == []
    assert client.sent_keys == []


def test_cmd_answer_requires_fingerprint_even_with_revision(monkeypatch):
    store = FakeStore(_bound_event(fingerprint=" ", revision=3))
    client = FakeClient(_live_agent())
    _patch_answer_dependencies(monkeypatch, store, client)

    assert cli.cmd_answer(_answer_args(), cfg=None) == ABORT
    assert store.marks == [
        ("evt1", "rejected", {"reason": "missing_question_fingerprint"})
    ]
    assert client.get_agent_calls == []
    assert client.sent_text == []
    assert client.sent_keys == []


def test_cmd_answer_rejects_unknown_live_revision(monkeypatch):
    store = FakeStore(_bound_event(revision=3))
    client = FakeClient(_live_agent(revision=0))
    _patch_answer_dependencies(monkeypatch, store, client)

    assert cli.cmd_answer(_answer_args(), cfg=None) == ABORT
    assert store.marks == [("evt1", "rejected", {"reason": "stale_revision"})]
    assert client.sent_text == []
    assert client.sent_keys == []


def test_cmd_answer_delivers_needs_input_when_menu_still_present(monkeypatch):
    """E3.T001 AC: false-done needs_input + idle-like + menu → deliver."""
    from hark.events import extract_question_excerpt
    from hark.fingerprint import question_fingerprint

    menu = "Continue?\n1. Yes\n2. No\n"
    fp = question_fingerprint(extract_question_excerpt(menu))
    bound = BoundEvent(
        event_id="evt1",
        session_id="local",
        pane_id="w1:p1",
        pane_revision=3,
        question_fingerprint=fp,
        meta={"kind": "agent.needs_input"},
    )
    store = FakeStore(bound)
    client = FakeClient(_live_agent(status="done", revision=3))

    def _read(pane_id: str, lines: int = 60) -> str:
        client.read_pane_calls.append((pane_id, lines))
        return menu

    client.read_pane = _read  # type: ignore[method-assign]
    _patch_answer_dependencies(monkeypatch, store, client)

    assert cli.cmd_answer(_answer_args(), cfg=None) == 0
    assert store.marks == [("evt1", "delivered", {"text": "yes"})]
    assert client.sent_text == [("w1:p1", "yes")]

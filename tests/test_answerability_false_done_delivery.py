"""E4.T001: needs_input deliverable when menu still present; idle empty refuses.

Uses false_done.jsonl fixtures where available + answer_bound_event path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hark.answering import answer_bound_event
from hark.delivery import BoundEvent, DeliveryStore
from hark.events import extract_question_excerpt, looks_like_pending_question
from hark.fingerprint import question_fingerprint
from hark.herdr.client import AgentInfo

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "text" / "false_done.jsonl"


def _load_cases() -> list[dict]:
    rows: list[dict] = []
    with FIX.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


class _Store:
    def __init__(self, bound: BoundEvent) -> None:
        self.bound = bound
        self.marks: list[tuple[str, str, dict]] = []

    def get(self, event_id: str) -> BoundEvent:
        assert event_id == self.bound.event_id
        return self.bound

    def already_delivered(self, event_id: str) -> bool:
        return False

    def mark(self, event_id: str, status: str, **extra: object) -> None:
        self.marks.append((event_id, status, dict(extra)))


class _Client:
    def __init__(self, status: str, pane_text: str, *, revision: int = 1) -> None:
        self.status = status
        self.pane_text = pane_text
        self.revision = revision
        self.sent_text: list[tuple[str, str]] = []

    def get_agent(self, pane_id: str) -> AgentInfo:
        return AgentInfo(
            session_id="local",
            pane_id=pane_id,
            agent="codex",
            status=self.status,
            revision=self.revision,
        )

    def read_pane(self, pane_id: str, lines: int = 60) -> str:
        return self.pane_text

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent_text.append((pane_id, text))

    def send_keys(self, pane_id: str, keys: list[str]) -> None:
        raise AssertionError("keys not used in these tests")


def _bound_for(pane_text: str, *, kind: str = "agent.needs_input") -> BoundEvent:
    fp = question_fingerprint(extract_question_excerpt(pane_text))
    return BoundEvent(
        event_id="fd1",
        session_id="local",
        pane_id="w1:p1",
        pane_revision=1,
        question_fingerprint=fp,
        meta={"kind": kind},
    )


@pytest.mark.parametrize("status", ["done", "idle"])
def test_needs_input_menu_delivers(status: str) -> None:
    """F1: needs_input + idle-like + menu fixture → deliver."""
    cases = [c for c in _load_cases() if c.get("expect_pending")]
    assert cases, "need at least one pending menu fixture"
    case = next(c for c in cases if c["id"] == "nomad-menu")
    text = case["text"]
    assert looks_like_pending_question(text).matched

    store = _Store(_bound_for(text))
    client = _Client(status, text)
    result = answer_bound_event(
        "fd1",
        text="1",
        store=store,  # type: ignore[arg-type]
        client_for=lambda _sid: client,
    )
    assert result.ok is True
    assert result.status == "delivered"
    assert client.sent_text == [("w1:p1", "1")]
    assert store.marks[-1][1] == "delivered"


@pytest.mark.parametrize(
    "case_id",
    [
        "claude-idle-empty-prompt",
        "clean-done",
        "box-drawing-only",
    ],
)
def test_needs_input_idle_empty_refuses(case_id: str) -> None:
    """F2: needs_input + idle-like + non-pending pane → refuse."""
    case = next(c for c in _load_cases() if c["id"] == case_id)
    text = case["text"]
    assert looks_like_pending_question(text).matched is False

    # Bound fingerprint from an earlier menu (operator still has old bind).
    menu = next(c for c in _load_cases() if c["id"] == "nomad-menu")["text"]
    store = _Store(_bound_for(menu))
    client = _Client("idle", text)
    result = answer_bound_event(
        "fd1",
        text="1",
        store=store,  # type: ignore[arg-type]
        client_for=lambda _sid: client,
    )
    assert result.ok is False
    assert result.status == "rejected"
    assert result.reason in ("not_compatible", "fingerprint_mismatch")
    assert client.sent_text == []


def test_blocked_kind_on_done_refuses_even_with_menu() -> None:
    """F6: agent.blocked bind does not open idle-like delivery."""
    menu = next(c for c in _load_cases() if c["id"] == "nomad-menu")["text"]
    store = _Store(_bound_for(menu, kind="agent.blocked"))
    client = _Client("done", menu)
    result = answer_bound_event(
        "fd1",
        text="1",
        store=store,  # type: ignore[arg-type]
        client_for=lambda _sid: client,
    )
    assert result.ok is False
    assert result.reason == "not_compatible"
    assert client.sent_text == []


def test_working_refuses() -> None:
    """F5."""
    menu = next(c for c in _load_cases() if c["id"] == "nomad-menu")["text"]
    store = _Store(_bound_for(menu))
    client = _Client("working", menu)
    result = answer_bound_event(
        "fd1",
        text="1",
        store=store,  # type: ignore[arg-type]
        client_for=lambda _sid: client,
    )
    assert result.ok is False
    assert result.reason == "not_compatible"


def test_classic_blocked_still_delivers() -> None:
    """F4: classic blocked path unchanged."""
    menu = next(c for c in _load_cases() if c["id"] == "nomad-menu")["text"]
    store = _Store(_bound_for(menu, kind="agent.blocked"))
    client = _Client("blocked", menu)
    result = answer_bound_event(
        "fd1",
        text="2",
        store=store,  # type: ignore[arg-type]
        client_for=lambda _sid: client,
    )
    assert result.status == "delivered"
    assert client.sent_text == [("w1:p1", "2")]


def test_uncertain_on_send_herdr_error() -> None:
    """AC7: write HerdrError → uncertain, never blind-retry."""
    from hark.herdr.client import HerdrError

    menu = next(c for c in _load_cases() if c["id"] == "nomad-menu")["text"]
    store = _Store(_bound_for(menu, kind="agent.blocked"))
    client = _Client("blocked", menu)

    def boom(pane_id: str, text: str) -> None:
        raise HerdrError("socket closed")

    client.send_text = boom  # type: ignore[method-assign]
    result = answer_bound_event(
        "fd1",
        text="1",
        store=store,  # type: ignore[arg-type]
        client_for=lambda _sid: client,
    )
    assert result.status == "uncertain"
    assert result.ok is True
    assert store.marks[-1][1] == "uncertain"

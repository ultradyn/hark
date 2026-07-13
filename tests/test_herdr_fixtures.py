"""Contract tests for redacted Herdr wire fixtures (B005)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hark.herdr.client import HerdrError, parse_agent_list
from hark.watch import _handle_lifecycle_event

ROOT = Path(__file__).resolve().parents[1]
HERDR_FIX = ROOT / "fixtures" / "herdr"


def _load_json(name: str) -> dict:
    path = HERDR_FIX / name
    assert path.is_file(), f"missing fixture {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_fixture_files_exist():
    for name in (
        "agent-list-empty.json",
        "agent-list-blocked.json",
        "agent-list-working.json",
        "agent-list-mixed.json",
        "watch-stream-blocked.jsonl",
        "watch-stream-wire.jsonl",
    ):
        assert (HERDR_FIX / name).is_file(), name


def test_agent_list_empty_parses():
    agents = parse_agent_list(_load_json("agent-list-empty.json"), session_id="default")
    assert agents == []


def test_agent_list_blocked_has_blocked_status():
    agents = parse_agent_list(_load_json("agent-list-blocked.json"), session_id="default")
    assert agents
    assert any(a.status == "blocked" for a in agents)
    for a in agents:
        assert a.pane_id
        assert a.session_id == "default"
        # redaction: no operator home paths
        if a.cwd:
            assert "/home/xertrov" not in a.cwd
            assert a.cwd.startswith("/home/operator") or not a.cwd.startswith("/home/")


def test_agent_list_working_has_working_status():
    agents = parse_agent_list(_load_json("agent-list-working.json"), session_id="default")
    assert agents
    assert any(a.status == "working" for a in agents)
    for a in agents:
        assert a.pane_id
        assert a.agent  # real captures always set agent type


def test_agent_list_mixed_statuses_and_fields():
    agents = parse_agent_list(_load_json("agent-list-mixed.json"), session_id="s1")
    assert len(agents) >= 2
    statuses = {a.status for a in agents}
    # live capture at fixture generation had blocked+working+idle/done
    assert statuses & {"blocked", "working", "idle", "done"}
    for a in agents:
        assert a.session_id == "s1"
        assert a.pane_id.startswith("w")
        assert a.raw.get("agent_status") or a.raw.get("status")
        if a.cwd:
            assert "xertrov" not in a.cwd


def test_agent_list_rejects_garbage():
    with pytest.raises(HerdrError, match="agents"):
        parse_agent_list({"result": {"type": "agent_list"}})
    with pytest.raises(HerdrError):
        parse_agent_list({"not": "valid"})  # type: ignore[arg-type]


def test_watch_stream_hep_parses_jsonl():
    path = HERDR_FIX / "watch-stream-blocked.jsonl"
    kinds: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        assert isinstance(obj, dict)
        kinds.add(str(obj.get("kind") or obj.get("type") or ""))
        text = json.dumps(obj)
        assert "/home/xertrov" not in text
    # real captures + meta
    assert "agent.blocked" in kinds or "agent.state_changed" in kinds
    assert "fixture.meta" in kinds or "agent.completed" in kinds


def test_watch_stream_wire_lifecycle_shapes():
    path = HERDR_FIX / "watch-stream-wire.jsonl"
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert lines
    # at least one nested event envelope and one flat type
    types: set[str] = set()
    for raw in lines:
        if "params" in raw and isinstance(raw["params"], dict):
            ev = raw["params"].get("event") or {}
            if isinstance(ev, dict) and ev.get("type"):
                types.add(str(ev["type"]))
        if raw.get("type"):
            types.add(str(raw["type"]))
    assert "pane.agent_status_changed" in types
    assert "pane.closed" in types


def test_wire_pane_closed_fixture_invalidates_via_watch_helper(tmp_path):
    """Contract: fixture wire shape is accepted by lifecycle handler."""
    from hark.config import SessionConfig
    from hark.delivery import BoundEvent, DeliveryStore

    class _Client:
        session = SessionConfig(id="default")

    store = DeliveryStore(tmp_path / "events.jsonl")
    store.save_event(
        BoundEvent(
            event_id="e1",
            session_id="default",
            pane_id="w9:p1",
            pane_revision=1,
            question_fingerprint="blake2b:q",
        )
    )
    wire_lines = (HERDR_FIX / "watch-stream-wire.jsonl").read_text().splitlines()
    closed = None
    for line in wire_lines:
        obj = json.loads(line)
        if "pane.closed" in line:
            closed = obj
            break
    assert closed is not None
    emitted: list[dict] = []
    assert _handle_lifecycle_event(
        closed, client=_Client(), store=store, emit=emitted.append
    )
    assert any(e.get("kind") == "target.invalidated" for e in emitted)

from hark.events import (
    looks_like_pending_question,
    make_agent_needs_input,
    make_agent_status_event,
    make_watch_armed,
    monitor_profile,
)
from hark.herdr.client import AgentInfo


def test_watch_armed_monitor():
    e = make_watch_armed(["local"], transport="poll", statuses=["blocked", "done"])
    m = monitor_profile(e)
    assert m["kind"] == "watch.armed"
    assert m["event_id"]
    assert "sessions" in m
    assert "needs_input" in (m.get("instructions") or "")


def test_blocked_monitor_compact():
    agent = AgentInfo(
        session_id="local",
        pane_id="w1:p6",
        agent="claude",
        status="blocked",
        revision=3,
        workspace_id="w1",
        tab_id="w1:t1",
    )
    e = make_agent_status_event(
        agent,
        from_status="working",
        to_status="blocked",
        question_text="Allow running something?",
    )
    m = monitor_profile(e)
    assert m["kind"] == "agent.blocked"
    assert m["pane_id"] == "w1:p6"
    assert "event_id" in m
    assert "instructions" in m
    assert "invent" in m["instructions"].lower()


def test_needs_input_monitor_compact():
    agent = AgentInfo(
        session_id="local",
        pane_id="w7:p1",
        agent="claude",
        status="done",
        revision=1,
    )
    text = "Which option?\n1. A\n2. B\nReply with a number."
    e = make_agent_needs_input(
        agent,
        from_status="working",
        to_status="done",
        question_text=text,
        hit=looks_like_pending_question(text),
    )
    m = monitor_profile(e)
    assert m["kind"] == "agent.needs_input"
    assert m["false_done"] is True
    assert "context" in m["instructions"].lower()

"""B094: full pane text capture on agent wake HEP events.

PaneClassifier (P1.M3) owns capture split; coverage retained.
"""

from __future__ import annotations

from hark.config import load_config
from hark.events import (
    make_agent_needs_input,
    make_agent_status_event,
    monitor_profile,
    prepare_pane_capture,
)
from hark.herdr.client import AgentInfo
from hark.monitor_feed import compact_mode_a_event
from hark.pane_understanding import looks_like_pending_question
from hark.pane_understanding.classify import PaneClassifier as EdgeTracker

# Multi-option menu as would appear on a blocked / false-done pane.
BLOCKED_MENU_PANE = """\
Exploring deploy options for the edge node.

Some earlier log noise that should still be available in pane_capture:
  built package
  ran tests
  waiting for operator

Which option should I use for the Nomad worker path?

1. Use the default path
2. Custom path under /opt
3. Skip worker setup

Reply with a number or option.
"""


def _agent(status: str = "blocked") -> AgentInfo:
    return AgentInfo(
        session_id="local",
        pane_id="w7:p1",
        agent="claude",
        status=status,
        revision=2,
        workspace_id="w7",
        tab_id="w7:t1",
    )


def test_prepare_pane_capture_bounds_lines_and_chars() -> None:
    lines = [f"line-{i}" for i in range(200)]
    body = "\n".join(lines)
    cap = prepare_pane_capture(body, max_lines=50, max_chars=500)
    assert cap is not None
    assert cap["truncated"] is True
    assert cap["line_count"] <= 50
    assert cap["char_count"] <= 500
    assert "line-199" in cap["text"]
    assert cap["source"] == "recent-unwrapped"


def test_prepare_pane_capture_strips_ansi() -> None:
    raw = "\x1b[31mred\x1b[0m menu\n1. Yes\n2. No"
    cap = prepare_pane_capture(raw)
    assert cap is not None
    assert "\x1b" not in cap["text"]
    assert "1. Yes" in cap["text"]


def test_blocked_event_includes_menu_choices_in_capture() -> None:
    tracker = EdgeTracker()
    events = tracker.process(
        [_agent("blocked")],
        interest={"blocked", "done"},
        question_for=lambda _a: BLOCKED_MENU_PANE,
    )
    blocked = next(e for e in events if e["kind"] == "agent.blocked")
    cap = blocked.get("pane_capture")
    assert isinstance(cap, dict)
    text = cap["text"]
    assert "1. Use the default path" in text
    assert "2. Custom path under /opt" in text
    assert "3. Skip worker setup" in text
    assert "Nomad worker" in text
    # Earlier scrollback retained when under caps
    assert "ran tests" in text
    assert blocked["question"]["fingerprint"]
    assert "pane_capture" in (blocked.get("instructions") or "")


def test_needs_input_event_includes_menu_choices_in_capture() -> None:
    tracker = EdgeTracker()
    tracker.process(
        [_agent("working")],
        interest={"blocked", "done"},
        question_for=lambda _a: None,
    )
    events = tracker.process(
        [_agent("done")],
        interest={"blocked", "done"},
        question_for=lambda _a: BLOCKED_MENU_PANE,
        detect_false_done=True,
    )
    needs = next(e for e in events if e["kind"] == "agent.needs_input")
    text = needs["pane_capture"]["text"]
    assert "1. Use the default path" in text
    assert "3. Skip worker setup" in text
    assert needs["false_done"] is True


def test_question_changed_carries_capture() -> None:
    tracker = EdgeTracker()
    q1 = "Allow network?\n1. Yes\n2. No"
    q2 = "Allow disk write?\n1. Yes\n2. No\n3. Skip"
    tracker.process(
        [_agent("blocked")],
        interest={"blocked", "done"},
        question_for=lambda _a: q1,
    )
    events = tracker.process(
        [_agent("blocked")],
        interest={"blocked", "done"},
        question_for=lambda _a: q2,
    )
    changed = next(e for e in events if e["kind"] == "agent.question_changed")
    assert "3. Skip" in changed["pane_capture"]["text"]


def test_pane_capture_can_be_disabled() -> None:
    tracker = EdgeTracker(pane_capture=False)
    events = tracker.process(
        [_agent("blocked")],
        interest={"blocked", "done"},
        question_for=lambda _a: BLOCKED_MENU_PANE,
    )
    blocked = next(e for e in events if e["kind"] == "agent.blocked")
    assert "pane_capture" not in blocked
    assert blocked["question"]["text"]
    # Instructions still point at hark context
    assert "hark context" in (blocked.get("instructions") or "")


def test_monitor_profile_surfaces_pane_capture() -> None:
    agent = _agent("blocked")
    cap = prepare_pane_capture(BLOCKED_MENU_PANE)
    event = make_agent_status_event(
        agent,
        from_status="working",
        to_status="blocked",
        question_text="Which option?\n1. A\n2. B",
        pane_capture=cap,
    )
    compact = monitor_profile(event)
    assert compact["kind"] == "agent.blocked"
    assert "1. Use the default path" in compact["pane_capture"]["text"]
    assert "2. Custom path under /opt" in compact["pane_capture"]["text"]
    assert "pane_capture" in compact["instructions"].lower() or "capture" in compact[
        "instructions"
    ].lower()

    # Unified Mode A compact path uses the same monitor_profile for agent.*
    mode_a = compact_mode_a_event(event)
    assert "1. Use the default path" in mode_a["pane_capture"]["text"]


def test_monitor_profile_needs_input_capture() -> None:
    agent = _agent("done")
    hit = looks_like_pending_question(BLOCKED_MENU_PANE)
    event = make_agent_needs_input(
        agent,
        from_status="working",
        to_status="done",
        question_text=BLOCKED_MENU_PANE,
        hit=hit,
        pane_capture=prepare_pane_capture(BLOCKED_MENU_PANE),
    )
    m = monitor_profile(event)
    assert m["kind"] == "agent.needs_input"
    assert "Skip worker setup" in m["pane_capture"]["text"]
    assert m["false_done"] is True


def test_config_pane_capture_keys(tmp_path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'version = 1\n[[herdr.sessions]]\nid = "local"\n',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.watch.pane_capture is True
    assert cfg.watch.pane_capture_lines == 100
    assert cfg.watch.pane_capture_max_chars == 12000

    path.write_text(
        'version = 1\n[[herdr.sessions]]\nid = "local"\n'
        "[watch]\n"
        "pane_capture = false\n"
        "pane_capture_lines = 80\n"
        "pane_capture_max_chars = 8000\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.watch.pane_capture is False
    assert cfg.watch.pane_capture_lines == 80
    assert cfg.watch.pane_capture_max_chars == 8000

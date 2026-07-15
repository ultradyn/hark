"""B016: false-done detection — done/idle with menu-like pane → needs_input.

Coverage retained under Pane Understanding (P1.M3): heuristics + PaneClassifier.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hark.events import make_agent_needs_input, monitor_profile
from hark.herdr.client import AgentInfo
from hark.pane_understanding import looks_like_pending_question
from hark.pane_understanding.classify import PaneClassifier as EdgeTracker

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "text" / "false_done.jsonl"

NOMAD_PANE = """\
Finished exploring Nomad worker layout.
Which option should I use for the Nomad worker path?

1. Use the default path
2. Custom path under /opt
3. Skip worker setup

Reply with a number or option.
"""


def _load_cases() -> list[dict]:
    rows: list[dict] = []
    for line in FIX.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_fixture_false_done_heuristics(case: dict) -> None:
    hit = looks_like_pending_question(case["text"])
    assert bool(hit) is case["expect_pending"]
    if case["expect_pending"]:
        any_reasons = set(case.get("expect_reasons_any") or [])
        if any_reasons:
            assert any_reasons & set(hit.reasons), (
                f"expected one of {any_reasons}, got {hit.reasons}"
            )


def test_nomad_menu_is_pending() -> None:
    hit = looks_like_pending_question(NOMAD_PANE)
    assert hit
    assert "numbered_menu" in hit.reasons
    assert len(hit.choices) >= 3


def test_clean_completion_not_pending() -> None:
    hit = looks_like_pending_question(
        "All done.\nCommitted and pushed.\nNothing else needed."
    )
    assert not hit


# ---------------------------------------------------------------------------
# B111: idle Claude Code empty ❯ must not look like numbered_menu
# ---------------------------------------------------------------------------

CLAUDE_IDLE_PANE = """\
I finished the review of the agent-session-forwarder.

Want me to walk through any specific part?

╭──────────────────────────────────────────────────────────────╮
│ ❯                                                            │
╰──────────────────────────────────────────────────────────────╯
  ~/s/agent-session-forwarder  main   Opus 4.8 (1M context)
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""

CLAUDE_IDLE_WITH_PROSE_LIST = """\
I made three changes:

1. Updated the watcher to ignore empty prompts
2. Aligned with hark queue stale treatment
3. Added fixtures for Claude Code chrome

Want me to walk through any specific part?

❯
────────────────────────────────────
   ~/s/agent-session-forwarder  main   Opus 4.8 (1M context)
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""

CLAUDE_PERMISSION_MENU = """\
Claude wants to edit package.json

Do you want to proceed?
❯ 1. Yes
  2. Yes, and don't ask again for this session
  3. No, and tell Claude what to do differently
"""


def test_claude_idle_empty_prompt_not_pending() -> None:
    """Empty ❯ + box-drawing / status chrome alone is conversational turn-end."""
    hit = looks_like_pending_question(CLAUDE_IDLE_PANE)
    assert not hit
    assert "numbered_menu" not in hit.reasons


def test_claude_idle_prose_numbered_list_not_pending() -> None:
    """Assistant enumeration in scrollback above idle ❯ is not a menu (B111)."""
    hit = looks_like_pending_question(CLAUDE_IDLE_WITH_PROSE_LIST)
    assert not hit
    assert "numbered_menu" not in hit.reasons


def test_claude_permission_menu_still_pending() -> None:
    """Real Claude selection UI (❯ 1. Yes / 2. / 3.) remains false-done."""
    hit = looks_like_pending_question(CLAUDE_PERMISSION_MENU)
    assert hit
    assert "numbered_menu" in hit.reasons
    assert len(hit.choices) >= 2


def test_menu_immediately_above_empty_prompt_still_pending() -> None:
    """Typed-reply menus: contiguous options then bare ❯ still count (B016)."""
    text = (
        "Which path should I use?\n\n"
        "1. Use the default path\n"
        "2. Custom path under /opt\n"
        "3. Skip worker setup\n\n"
        "❯"
    )
    hit = looks_like_pending_question(text)
    assert hit
    assert "numbered_menu" in hit.reasons


def test_edge_tracker_idle_claude_no_needs_input() -> None:
    """Watch must not emit agent.needs_input for idle empty Claude prompts."""
    tracker = EdgeTracker()
    interest = {"blocked", "done"}
    tracker.process(
        [_agent("working")],
        interest=interest,
        question_for=lambda _a: None,
    )
    events = tracker.process(
        [_agent("done")],
        interest=interest,
        question_for=lambda _a: CLAUDE_IDLE_WITH_PROSE_LIST,
        detect_false_done=True,
    )
    kinds = [e["kind"] for e in events]
    assert "agent.needs_input" not in kinds
    assert "agent.completed" in kinds
    completed = next(e for e in events if e["kind"] == "agent.completed")
    assert completed.get("false_done") is not True


def test_ansi_wrapped_empty_prompt_not_pending() -> None:
    """Colorized idle ❯ must still suppress scrollback numbered lists."""
    text = (
        "I made three changes:\n\n"
        "1. Updated the watcher\n"
        "2. Aligned with queue\n"
        "3. Added fixtures\n\n"
        "Want me to walk through any specific part?\n\n"
        "\x1b[38;5;245m❯\x1b[0m\n"
        "────────────────────────────────────\n"
        "  ~/src/foo  main\n"
    )
    hit = looks_like_pending_question(text)
    assert not hit
    assert "numbered_menu" not in hit.reasons


def _agent(status: str = "done") -> AgentInfo:
    return AgentInfo(
        session_id="local",
        pane_id="w7:p1",
        agent="claude",
        status=status,
        revision=2,
        workspace_id="w7",
        tab_id="w7:t1",
    )


def test_edge_tracker_emits_needs_input_on_false_done() -> None:
    tracker = EdgeTracker()
    interest = {"blocked", "done"}
    # seed working
    assert (
        tracker.process(
            [_agent("working")],
            interest=interest,
            question_for=lambda _a: None,
        )
        == []
    )

    events = tracker.process(
        [_agent("done")],
        interest=interest,
        question_for=lambda _a: NOMAD_PANE,
        detect_false_done=True,
    )
    kinds = [e["kind"] for e in events]
    assert "agent.needs_input" in kinds
    needs = next(e for e in events if e["kind"] == "agent.needs_input")
    assert needs["priority"] == 80
    assert needs["disposition"] == "pending"
    assert needs["false_done"] is True
    assert needs["question"]["fingerprint"]
    assert "Nomad" in (needs["question"]["text"] or "")
    # completed may also be present (lower priority)
    if "agent.completed" in kinds:
        completed = next(e for e in events if e["kind"] == "agent.completed")
        assert completed.get("false_done") is True
        assert completed["priority"] <= needs["priority"]


def test_edge_tracker_real_done_without_menu() -> None:
    tracker = EdgeTracker()
    interest = {"blocked", "done"}
    tracker.process(
        [_agent("working")],
        interest=interest,
        question_for=lambda _a: None,
    )
    events = tracker.process(
        [_agent("done")],
        interest=interest,
        question_for=lambda _a: "All tasks completed successfully.\nTests passed.",
        detect_false_done=True,
    )
    kinds = [e["kind"] for e in events]
    assert "agent.needs_input" not in kinds
    assert "agent.completed" in kinds


def test_detect_false_done_flag_off() -> None:
    tracker = EdgeTracker()
    interest = {"blocked", "done"}
    tracker.process(
        [_agent("working")],
        interest=interest,
        question_for=lambda _a: None,
    )
    events = tracker.process(
        [_agent("done")],
        interest=interest,
        question_for=lambda _a: NOMAD_PANE,
        detect_false_done=False,
    )
    kinds = [e["kind"] for e in events]
    assert "agent.needs_input" not in kinds
    assert "agent.completed" in kinds


def test_first_sight_done_with_menu() -> None:
    """Agent already done when watch arms — still surface needs_input."""
    tracker = EdgeTracker()
    events = tracker.process(
        [_agent("done")],
        interest={"blocked", "done"},
        question_for=lambda _a: NOMAD_PANE,
        detect_false_done=True,
    )
    assert any(e["kind"] == "agent.needs_input" for e in events)


def test_monitor_profile_tolerates_string_question() -> None:
    """Legacy watch lines may store question as a bare string — must not crash monitor."""
    from hark.events import monitor_profile

    compact = monitor_profile(
        {
            "kind": "agent.blocked",
            "event_id": "e1",
            "session_id": "default",
            "target": {"pane_id": "w1:p1", "agent": "claude"},
            "question": "Do you want to proceed?\n> 1. Yes",
            "state": {"to": "blocked"},
        }
    )
    assert compact["kind"] == "agent.blocked"
    assert "proceed" in (compact.get("question") or "")
    assert compact["pane_id"] == "w1:p1"

    # target as "session/pane" string
    compact2 = monitor_profile(
        {
            "kind": "agent.completed",
            "event_id": "e2",
            "target": "work/w2:p3",
            "question": None,
        }
    )
    assert compact2["session_id"] == "work"
    assert compact2["pane_id"] == "w2:p3"
    assert "instructions" in compact2


def test_question_changed_while_blocked() -> None:
    tracker = EdgeTracker()
    interest = {"blocked", "done"}
    q1 = "Allow network access?\n1. Yes\n2. No"
    q2 = "Allow writing to disk?\n1. Yes\n2. No"

    # enter blocked
    events = tracker.process(
        [_agent("blocked")],
        interest=interest,
        question_for=lambda _a: q1,
    )
    assert any(e["kind"] == "agent.blocked" for e in events)

    # same status, new question
    events = tracker.process(
        [_agent("blocked")],
        interest=interest,
        question_for=lambda _a: q2,
    )
    assert any(e["kind"] == "agent.question_changed" for e in events)

    # same question again — no re-fire
    events = tracker.process(
        [_agent("blocked")],
        interest=interest,
        question_for=lambda _a: q2,
    )
    assert events == []


def test_needs_input_deduped_same_fingerprint() -> None:
    tracker = EdgeTracker()
    interest = {"blocked", "done"}
    tracker.process(
        [_agent("working")],
        interest=interest,
        question_for=lambda _a: None,
    )
    first = tracker.process(
        [_agent("done")],
        interest=interest,
        question_for=lambda _a: NOMAD_PANE,
    )
    assert any(e["kind"] == "agent.needs_input" for e in first)

    # oscillate working → done again with same menu
    tracker.process(
        [_agent("working")],
        interest=interest,
        question_for=lambda _a: None,
    )
    second = tracker.process(
        [_agent("done")],
        interest=interest,
        question_for=lambda _a: NOMAD_PANE,
    )
    # fingerprint dedupe: needs_input not re-emitted for same ask
    assert not any(e["kind"] == "agent.needs_input" for e in second)


def test_monitor_profile_needs_input() -> None:
    agent = _agent("done")
    hit = looks_like_pending_question(NOMAD_PANE)
    event = make_agent_needs_input(
        agent,
        from_status="working",
        to_status="done",
        question_text=NOMAD_PANE,
        hit=hit,
    )
    m = monitor_profile(event)
    assert m["kind"] == "agent.needs_input"
    assert m["false_done"] is True
    assert "blocked" in m["instructions"].lower() or "needs input" in m["instructions"].lower()
    assert "question" in m
    assert m.get("question")


def test_config_detect_false_done_default(tmp_path) -> None:
    from hark.config import load_config

    path = tmp_path / "config.toml"
    path.write_text('version = 1\n[[herdr.sessions]]\nid = "local"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.watch.detect_false_done is True

    path.write_text(
        'version = 1\n[[herdr.sessions]]\nid = "local"\n'
        "[watch]\ndetect_false_done = false\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.watch.detect_false_done is False

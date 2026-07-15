"""B096: false agent.completed while Herdr/Grok subagent Tasks still running.

Coverage retained under Pane Understanding (P1.M3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hark.events import make_agent_busy_subagent, monitor_profile
from hark.herdr.client import AgentInfo
from hark.pane_understanding import detect_active_subagents
from hark.pane_understanding.classify import PaneClassifier as EdgeTracker

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "text" / "subagent_tasks.jsonl"

# Live-shaped Grok Build task strip (near top of pane).
GROK_TASKS_PANE = """\
  master ~/src/amaroo-nomad                                                                                       ⸬ 2 │ 180K / 500K │ 6/6 ✓

     ▾ Tasks 1
     ⸬ Task Install rclone on Windows; configure nas-s3; smoke test                                                     (21+) 33m14s [↗][✗]
     ▾ Watchers 1
     ⸬ Monitor cache keepalive heartbeat                                                                                   (3) 2m58s [↗][✗]


     ❯ keep going on the backup

     Local steps finished. Background install still running.
"""

GROK_TASKS_CLEARED = """\
  master ~/src/amaroo-nomad

     All background tasks finished.
     Committed summary to notes.md.
     Ready for review.
"""

WATCHERS_ONLY = """\
  master ~/src/proj

     ▾ Watchers 1
     ⸬ Monitor cache keepalive heartbeat                                                                                   (3) 2m58s [↗][✗]

     All done. Nothing else pending.
"""


def _load_cases() -> list[dict]:
    rows: list[dict] = []
    for line in FIX.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _agent(status: str = "done") -> AgentInfo:
    return AgentInfo(
        session_id="local",
        pane_id="wJ:p1",
        agent="grok",
        status=status,
        revision=2,
        workspace_id="wJ",
        tab_id="wJ:t1",
    )


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_fixture_subagent_heuristics(case: dict) -> None:
    hit = detect_active_subagents(case["text"])
    assert bool(hit) is case["expect_active"]
    if case["expect_active"]:
        assert hit.count >= int(case.get("expect_count_min") or 1)
        any_reasons = set(case.get("expect_reasons_any") or [])
        if any_reasons:
            assert any_reasons & set(hit.reasons), (
                f"expected one of {any_reasons}, got {hit.reasons}"
            )


def test_grok_tasks_strip_is_active() -> None:
    hit = detect_active_subagents(GROK_TASKS_PANE)
    assert hit
    assert hit.count >= 1
    assert "tasks_header" in hit.reasons
    assert any("rclone" in lab.lower() for lab in hit.labels)


def test_watchers_only_not_active() -> None:
    assert not detect_active_subagents(WATCHERS_ONLY)


def test_edge_tracker_suppresses_completed_while_tasks_run() -> None:
    tracker = EdgeTracker()
    interest = {"blocked", "done"}
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
        question_for=lambda _a: GROK_TASKS_PANE,
        detect_false_done=True,
    )
    kinds = [e["kind"] for e in events]
    assert "agent.completed" not in kinds
    assert any(e.get("busy_subagent") for e in events)
    busy = next(e for e in events if e.get("busy_subagent"))
    assert busy["kind"] == "agent.state_changed"
    assert busy["state"]["to"] == "working"
    assert busy["state"].get("herdr") == "done"
    assert busy["false_done"] is True
    assert int(busy["subagents_running"]) >= 1
    assert busy["priority"] < 50
    assert "Tasks" in (busy.get("pane_capture") or {}).get("text", "") or busy.get(
        "subagents_running"
    )


def test_edge_tracker_dedupes_busy_while_still_running() -> None:
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
        question_for=lambda _a: GROK_TASKS_PANE,
    )
    assert any(e.get("busy_subagent") for e in first)

    # Same status, tasks still present — no re-fire / no completed.
    second = tracker.process(
        [_agent("done")],
        interest=interest,
        question_for=lambda _a: GROK_TASKS_PANE,
    )
    assert second == []
    assert "agent.completed" not in [e["kind"] for e in second]


def test_busy_dedupe_across_working_oscillation_no_completed() -> None:
    """working→done→working→done while Tasks stay up must never complete."""
    tracker = EdgeTracker()
    interest = {"blocked", "done"}
    q = lambda _a: GROK_TASKS_PANE  # noqa: E731
    tracker.process([_agent("working")], interest=interest, question_for=q)
    e1 = tracker.process([_agent("done")], interest=interest, question_for=q)
    assert any(e.get("busy_subagent") for e in e1)
    tracker.process([_agent("working")], interest=interest, question_for=q)
    e2 = tracker.process([_agent("done")], interest=interest, question_for=q)
    # Second done edge: busy may be deduped to [] but must not emit completed.
    assert not any(e["kind"] == "agent.completed" for e in e2)


def test_edge_tracker_emits_completed_after_tasks_clear() -> None:
    tracker = EdgeTracker()
    interest = {"blocked", "done"}
    tracker.process(
        [_agent("working")],
        interest=interest,
        question_for=lambda _a: None,
    )
    busy_events = tracker.process(
        [_agent("done")],
        interest=interest,
        question_for=lambda _a: GROK_TASKS_PANE,
    )
    assert any(e.get("busy_subagent") for e in busy_events)
    assert not any(e["kind"] == "agent.completed" for e in busy_events)

    # Tasks cleared, Herdr still done → now real completion.
    done_events = tracker.process(
        [_agent("done")],
        interest=interest,
        question_for=lambda _a: GROK_TASKS_CLEARED,
    )
    kinds = [e["kind"] for e in done_events]
    assert "agent.completed" in kinds
    assert not any(e.get("busy_subagent") for e in done_events)


def test_real_done_without_tasks_still_completes() -> None:
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
    assert "agent.completed" in kinds
    assert not any(e.get("busy_subagent") for e in events)


def test_watchers_only_still_allows_completed() -> None:
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
        question_for=lambda _a: WATCHERS_ONLY,
        detect_false_done=True,
    )
    assert any(e["kind"] == "agent.completed" for e in events)
    assert not any(e.get("busy_subagent") for e in events)


def test_detect_false_done_flag_off_skips_subagent_guard() -> None:
    """Same config gate as menu false-done (detect_false_done)."""
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
        question_for=lambda _a: GROK_TASKS_PANE,
        detect_false_done=False,
    )
    kinds = [e["kind"] for e in events]
    assert "agent.completed" in kinds
    assert not any(e.get("busy_subagent") for e in events)


def test_first_sight_done_with_tasks() -> None:
    tracker = EdgeTracker()
    events = tracker.process(
        [_agent("done")],
        interest={"blocked", "done"},
        question_for=lambda _a: GROK_TASKS_PANE,
        detect_false_done=True,
    )
    assert any(e.get("busy_subagent") for e in events)
    assert not any(e["kind"] == "agent.completed" for e in events)


def test_make_and_monitor_busy_subagent() -> None:
    agent = _agent("done")
    hit = detect_active_subagents(GROK_TASKS_PANE)
    event = make_agent_busy_subagent(
        agent,
        from_status="working",
        herdr_status="done",
        hit=hit,
        pane_capture={"text": GROK_TASKS_PANE, "truncated": False, "source": "test"},
    )
    assert event["busy_subagent"] is True
    assert event["subagents_running"] >= 1
    assert event["state"]["to"] == "working"
    m = monitor_profile(event)
    assert m["busy_subagent"] is True
    assert int(m["subagents_running"]) >= 1
    assert "working" in (m.get("instructions") or "").lower() or "subagent" in (
        m.get("instructions") or ""
    ).lower()

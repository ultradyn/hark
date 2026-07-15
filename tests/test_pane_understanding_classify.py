"""P1.M3: PaneClassifier process_observations (no Herdr I/O)."""

from __future__ import annotations

from hark.pane_understanding import (
    ClassifyPolicy,
    PaneClassifier,
    PaneObservation,
)

NOMAD_PANE = """\
Finished exploring Nomad worker layout.
Which option should I use for the Nomad worker path?

1. Use the default path
2. Custom path under /opt
3. Skip worker setup

Reply with a number or option.
"""


def test_process_observations_emits_needs_input_on_false_done() -> None:
    clf = PaneClassifier(
        ClassifyPolicy(interest=frozenset({"blocked", "done"}), detect_false_done=True)
    )
    # First tick: working
    clf.process_observations(
        [
            PaneObservation(
                session_id="s1", pane_id="p1", status="working", pane_text=None
            )
        ],
        interest={"blocked", "done"},
    )
    events = clf.process_observations(
        [
            PaneObservation(
                session_id="s1",
                pane_id="p1",
                status="done",
                pane_text=NOMAD_PANE,
            )
        ],
        interest={"blocked", "done"},
    )
    kinds = [e["kind"] for e in events]
    assert "agent.needs_input" in kinds
    assert ("s1", "p1") in clf.state.status
    assert clf.state.status[("s1", "p1")] == "done"


def test_process_observations_no_herdr_types_required() -> None:
    """raw_agent optional — classifier builds minimal AgentInfo."""
    clf = PaneClassifier()
    events = clf.process_observations(
        [PaneObservation(session_id="a", pane_id="b", status="blocked", pane_text="?")],
        interest={"blocked"},
    )
    assert any(e["kind"] == "agent.blocked" for e in events)

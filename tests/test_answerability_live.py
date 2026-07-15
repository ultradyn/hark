"""Live snapshot helpers — FakeClient only (no real Herdr)."""

from __future__ import annotations

from hark.answerability import (
    assess_live,
    hep_kind_from_bound,
    read_live_snapshot,
    reasons as R,
)
from hark.fingerprint import question_fingerprint
from hark.herdr.client import AgentInfo, HerdrError


class FakeClient:
    def __init__(
        self,
        live: AgentInfo | None,
        pane_text: str = "",
        *,
        read_error: Exception | None = None,
        get_error: Exception | None = None,
    ) -> None:
        self.live = live
        self.pane_text = pane_text
        self.read_error = read_error
        self.get_error = get_error
        self.get_calls: list[str] = []
        self.read_calls: list[tuple[str, int]] = []

    def get_agent(self, pane_id: str) -> AgentInfo | None:
        self.get_calls.append(pane_id)
        if self.get_error:
            raise self.get_error
        return self.live

    def read_pane(self, pane_id: str, lines: int = 60) -> str:
        self.read_calls.append((pane_id, lines))
        if self.read_error:
            raise self.read_error
        return self.pane_text


def _agent(*, status: str = "blocked", revision: int = 3) -> AgentInfo:
    return AgentInfo(
        session_id="local",
        pane_id="w1:p1",
        agent="codex",
        status=status,
        revision=revision,
    )


# Menu-like pane used by false-done fixtures (looks_like_pending_question).
MENU_PANE = """Some assistant text.

Do you want to continue?
1. Yes
2. No
"""

IDLE_CHROME = """
╭──────────────────────────────────────────╮
│ project                                   │
╰──────────────────────────────────────────╯
❯ 
"""


def test_read_snapshot_blocked_fp_match():
    excerpt_src = "Allow this action?"
    # answering uses extract_question_excerpt then fingerprint — match that
    from hark.events import extract_question_excerpt

    fp = question_fingerprint(extract_question_excerpt(excerpt_src))
    client = FakeClient(_agent(), pane_text=excerpt_src)
    snap = read_live_snapshot(
        pane_id="w1:p1",
        bound_revision=3,
        bound_fingerprint=fp,
        hep_kind="agent.blocked",
        client=client,
    )
    assert snap.pane_exists is True
    assert snap.live_status == "blocked"
    assert snap.live_fingerprint == fp
    assert snap.fingerprint_error is False
    assert snap.pane_still_pending is None  # not needed on blocked
    v = assess_live(
        pane_id="w1:p1",
        bound_revision=3,
        bound_fingerprint=fp,
        hep_kind="agent.blocked",
        client=client,
    )
    assert v.ok is True


def test_pane_gone():
    client = FakeClient(None)
    snap = read_live_snapshot(
        pane_id="w1:p1",
        bound_revision=1,
        bound_fingerprint="blake2b:x",
        hep_kind="agent.blocked",
        client=client,
    )
    assert snap.pane_exists is False
    v = assess_live(
        pane_id="w1:p1",
        bound_revision=1,
        bound_fingerprint="blake2b:x",
        hep_kind="agent.blocked",
        client=client,
    )
    assert v.reason == R.PANE_GONE


def test_needs_input_done_with_menu():
    from hark.events import extract_question_excerpt

    fp = question_fingerprint(extract_question_excerpt(MENU_PANE))
    client = FakeClient(_agent(status="done"), pane_text=MENU_PANE)
    snap = read_live_snapshot(
        pane_id="w1:p1",
        bound_revision=3,
        bound_fingerprint=fp,
        hep_kind="agent.needs_input",
        client=client,
    )
    assert snap.pane_still_pending is True
    v = assess_live(
        pane_id="w1:p1",
        bound_revision=3,
        bound_fingerprint=fp,
        hep_kind="agent.needs_input",
        client=client,
    )
    assert v.ok is True


def test_needs_input_idle_empty_chrome_refuses():
    from hark.events import extract_question_excerpt

    # Bound FP from an earlier menu; live pane is empty idle chrome.
    menu_fp = question_fingerprint(extract_question_excerpt(MENU_PANE))
    client = FakeClient(_agent(status="idle"), pane_text=IDLE_CHROME)
    snap = read_live_snapshot(
        pane_id="w1:p1",
        bound_revision=3,
        bound_fingerprint=menu_fp,
        hep_kind="agent.needs_input",
        client=client,
    )
    assert snap.pane_still_pending is False
    v = assess_live(
        pane_id="w1:p1",
        bound_revision=3,
        bound_fingerprint=menu_fp,
        hep_kind="agent.needs_input",
        client=client,
    )
    # Either not_compatible (menu gone) or fingerprint_mismatch (excerpt changed)
    assert v.ok is False
    assert v.reason in (R.NOT_COMPATIBLE, R.FINGERPRINT_MISMATCH)


def test_fingerprint_herdr_error():
    client = FakeClient(_agent(), read_error=HerdrError("down"))
    snap = read_live_snapshot(
        pane_id="w1:p1",
        bound_revision=3,
        bound_fingerprint="blake2b:x",
        hep_kind="agent.blocked",
        client=client,
    )
    assert snap.fingerprint_error is True
    v = assess_live(
        pane_id="w1:p1",
        bound_revision=3,
        bound_fingerprint="blake2b:x",
        hep_kind="agent.blocked",
        client=client,
    )
    assert v.reason == R.FINGERPRINT_UNAVAILABLE


def test_hep_kind_from_bound_meta():
    class B:
        meta = {"kind": "agent.needs_input"}

    assert hep_kind_from_bound(B()) == "agent.needs_input"
    assert hep_kind_from_bound({"meta": {"kind": "agent.blocked"}}) == "agent.blocked"
    assert hep_kind_from_bound({}) is None


def test_pure_core_still_herdr_free():
    """E2.T002 AC: pure core must not import Herdr."""
    import hark.answerability.core as core

    src = open(core.__file__, encoding="utf-8").read()
    assert "from hark.herdr" not in src
    assert "import hark.herdr" not in src
    # No third-party / herdr client imports — only stdlib + reasons.
    import_lines = [
        ln for ln in src.splitlines() if ln.startswith(("import ", "from "))
    ]
    assert import_lines == [
        "from __future__ import annotations",
        "from dataclasses import dataclass",
        "from hark.answerability import reasons as R",
    ]

"""Unit tests for pure Answerability core (no network / Herdr)."""

from __future__ import annotations

import pytest

from hark.answerability import (
    AnswerabilityVerdict,
    LiveAnswerSnapshot,
    assess_snapshot,
    is_idle_like,
    reasons as R,
)


def _snap(**kwargs) -> LiveAnswerSnapshot:
    base = dict(
        pane_exists=True,
        live_status="blocked",
        live_revision=3,
        bound_revision=3,
        bound_fingerprint="blake2b:q1",
        live_fingerprint="blake2b:q1",
        fingerprint_error=False,
        hep_kind="agent.blocked",
        pane_still_pending=None,
    )
    base.update(kwargs)
    return LiveAnswerSnapshot(**base)


def test_classic_blocked_deliver():
    v = assess_snapshot(_snap())
    assert v == AnswerabilityVerdict(True, R.OK)
    assert v.as_tuple() == (True, R.OK)


def test_missing_fingerprint():
    v = assess_snapshot(_snap(bound_fingerprint="  "))
    assert v == AnswerabilityVerdict(False, R.MISSING_QUESTION_FINGERPRINT)


def test_pane_gone():
    v = assess_snapshot(_snap(pane_exists=False, live_status=None))
    assert v == AnswerabilityVerdict(False, R.PANE_GONE)


@pytest.mark.parametrize(
    "status",
    ["working", "unknown", "", "running"],
)
def test_incompatible_status_refuses(status: str):
    v = assess_snapshot(_snap(live_status=status))
    assert v == AnswerabilityVerdict(False, R.NOT_COMPATIBLE)


def test_stale_revision():
    v = assess_snapshot(_snap(live_revision=9, bound_revision=3))
    assert v == AnswerabilityVerdict(False, R.STALE_REVISION)


def test_revision_zero_skips_check():
    # bound_revision 0 → do not enforce (same as answering today)
    v = assess_snapshot(_snap(bound_revision=0, live_revision=99))
    assert v.ok is True


def test_fingerprint_mismatch():
    v = assess_snapshot(_snap(live_fingerprint="blake2b:other"))
    assert v == AnswerabilityVerdict(False, R.FINGERPRINT_MISMATCH)


def test_fingerprint_unavailable_on_error():
    v = assess_snapshot(_snap(fingerprint_error=True, live_fingerprint=None))
    assert v == AnswerabilityVerdict(False, R.FINGERPRINT_UNAVAILABLE)


def test_fingerprint_unavailable_when_none():
    v = assess_snapshot(_snap(fingerprint_error=False, live_fingerprint=None))
    assert v == AnswerabilityVerdict(False, R.FINGERPRINT_UNAVAILABLE)


# --- needs_input / false-done matrix (F1–F6 pure) ---


@pytest.mark.parametrize("status", ["done", "idle", "completed", "complete"])
def test_needs_input_idle_like_with_menu_delivers(status: str):
    """F1: needs_input + idle-like + menu + FP match → deliver."""
    v = assess_snapshot(
        _snap(
            live_status=status,
            hep_kind="agent.needs_input",
            pane_still_pending=True,
        )
    )
    assert v == AnswerabilityVerdict(True, R.OK)


@pytest.mark.parametrize("status", ["done", "idle"])
def test_needs_input_idle_empty_refuses(status: str):
    """F2: needs_input + idle-like + no menu → refuse not_compatible."""
    v = assess_snapshot(
        _snap(
            live_status=status,
            hep_kind="agent.needs_input",
            pane_still_pending=False,
        )
    )
    assert v == AnswerabilityVerdict(False, R.NOT_COMPATIBLE)


def test_needs_input_fp_mismatch():
    """F3: menu present but FP mismatch."""
    v = assess_snapshot(
        _snap(
            live_status="done",
            hep_kind="agent.needs_input",
            live_fingerprint="blake2b:other",
            pane_still_pending=True,
        )
    )
    assert v == AnswerabilityVerdict(False, R.FINGERPRINT_MISMATCH)


def test_working_any_kind_refuses():
    """F5."""
    v = assess_snapshot(
        _snap(live_status="working", hep_kind="agent.needs_input", pane_still_pending=True)
    )
    assert v == AnswerabilityVerdict(False, R.NOT_COMPATIBLE)


def test_done_with_blocked_kind_refuses_even_if_menu():
    """F6: agent.blocked bind does not open idle-like delivery."""
    v = assess_snapshot(
        _snap(
            live_status="done",
            hep_kind="agent.blocked",
            pane_still_pending=True,
        )
    )
    assert v == AnswerabilityVerdict(False, R.NOT_COMPATIBLE)


def test_missing_kind_on_blocked_delivers():
    """Legacy bound rows without meta.kind: blocked still ok."""
    v = assess_snapshot(_snap(hep_kind=None))
    assert v.ok is True


def test_missing_kind_on_idle_refuses():
    v = assess_snapshot(_snap(live_status="idle", hep_kind=None, pane_still_pending=True))
    assert v == AnswerabilityVerdict(False, R.NOT_COMPATIBLE)


def test_question_changed_on_blocked_ok():
    v = assess_snapshot(_snap(hep_kind="agent.question_changed"))
    assert v.ok is True


def test_question_changed_on_idle_refuses():
    v = assess_snapshot(
        _snap(live_status="idle", hep_kind="agent.question_changed", pane_still_pending=True)
    )
    assert v == AnswerabilityVerdict(False, R.NOT_COMPATIBLE)


def test_needs_input_on_blocked_delivers():
    """needs_input HEP while still truly blocked (edge) still ok."""
    v = assess_snapshot(
        _snap(live_status="blocked", hep_kind="agent.needs_input", pane_still_pending=None)
    )
    assert v.ok is True


def test_is_idle_like_matches_events_set():
    assert is_idle_like("done")
    assert is_idle_like("IDLE")
    assert is_idle_like("completed")
    assert not is_idle_like("blocked")
    assert not is_idle_like("working")
    assert not is_idle_like(None)

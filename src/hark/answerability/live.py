"""Live re-read helpers for Answerability (injectable Herdr-like client).

Pure core stays free of Herdr. This module performs get_agent / read_pane and
builds ``LiveAnswerSnapshot`` for ``assess_snapshot``.
"""

from __future__ import annotations

from typing import Any, Protocol

from hark.answerability.core import (
    AnswerabilityVerdict,
    LiveAnswerSnapshot,
    assess_snapshot,
    is_idle_like,
    normalize_kind,
    normalize_status,
)
from hark.events import extract_question_excerpt, looks_like_pending_question
from hark.fingerprint import question_fingerprint
from hark.herdr.client import HerdrError


class SupportsAnswerLive(Protocol):
    """Duck type used by read_live_snapshot (FakeClient or HerdrClient)."""

    def get_agent(self, pane_id: str) -> Any: ...

    def read_pane(self, pane_id: str, lines: int = 60) -> str: ...


def read_live_snapshot(
    *,
    pane_id: str,
    bound_revision: int,
    bound_fingerprint: str | None,
    hep_kind: str | None,
    client: SupportsAnswerLive,
    pane_lines: int = 40,
    require_pending_heuristic: bool | None = None,
) -> LiveAnswerSnapshot:
    """I/O: live status + optional pane FP + menu heuristic.

    When ``require_pending_heuristic`` is None, auto-enable for
    ``agent.needs_input`` + idle-like live status (false-done path).
    """
    fp = (bound_fingerprint or "").strip()
    kind = normalize_kind(hep_kind)

    live = client.get_agent(pane_id)
    if live is None:
        return LiveAnswerSnapshot(
            pane_exists=False,
            live_status=None,
            live_revision=None,
            bound_revision=int(bound_revision or 0),
            bound_fingerprint=fp,
            live_fingerprint=None,
            fingerprint_error=False,
            hep_kind=kind,
            pane_still_pending=None,
        )

    status_raw = getattr(live, "status", None)
    status = normalize_status(str(status_raw) if status_raw is not None else "")
    rev = getattr(live, "revision", None)
    live_revision: int | None
    try:
        live_revision = int(rev) if rev is not None else None
    except (TypeError, ValueError):
        live_revision = None

    # Fingerprint re-read (always for answerability when we have a bound FP;
    # assess will fail missing FP earlier if empty).
    live_fp: str | None = None
    fingerprint_error = False
    pane_text: str | None = None
    try:
        pane_text = client.read_pane(pane_id, lines=pane_lines)
        excerpt = extract_question_excerpt(pane_text or "")
        live_fp = question_fingerprint(excerpt)
    except HerdrError:
        fingerprint_error = True
        live_fp = None

    # Menu heuristic for false-done path
    need_pending = require_pending_heuristic
    if need_pending is None:
        need_pending = kind == "agent.needs_input" and is_idle_like(status)

    pane_still_pending: bool | None = None
    if need_pending:
        if fingerprint_error or pane_text is None:
            # Cannot evaluate menu; leave None — pure core allows FP-only when
            # None, but fingerprint_error already forces refuse.
            pane_still_pending = None
        else:
            hit = looks_like_pending_question(pane_text)
            pane_still_pending = bool(hit.matched)

    return LiveAnswerSnapshot(
        pane_exists=True,
        live_status=status or str(status_raw or ""),
        live_revision=live_revision,
        bound_revision=int(bound_revision or 0),
        bound_fingerprint=fp,
        live_fingerprint=live_fp,
        fingerprint_error=fingerprint_error,
        hep_kind=kind,
        pane_still_pending=pane_still_pending,
    )


def assess_live(
    *,
    pane_id: str,
    bound_revision: int,
    bound_fingerprint: str | None,
    hep_kind: str | None,
    client: SupportsAnswerLive,
    pane_lines: int = 40,
    require_pending_heuristic: bool | None = None,
) -> AnswerabilityVerdict:
    """Convenience: read_live_snapshot + assess_snapshot."""
    snap = read_live_snapshot(
        pane_id=pane_id,
        bound_revision=bound_revision,
        bound_fingerprint=bound_fingerprint,
        hep_kind=hep_kind,
        client=client,
        pane_lines=pane_lines,
        require_pending_heuristic=require_pending_heuristic,
    )
    return assess_snapshot(snap)


def hep_kind_from_bound(bound: Any) -> str | None:
    """Extract HEP kind from BoundEvent.meta or a plain dict."""
    meta = getattr(bound, "meta", None)
    if meta is None and isinstance(bound, dict):
        meta = bound.get("meta")
    if isinstance(meta, dict):
        kind = meta.get("kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip()
    # Queue items may store kind at top level after register
    if isinstance(bound, dict):
        kind = bound.get("kind") or (bound.get("meta") or {}).get("kind")
        if isinstance(kind, str) and kind.strip():
            return kind.strip()
    return None

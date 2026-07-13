"""Bound-answer core shared by `hark answer` (CLI) and `hark serve` (/answer).

Single implementation of the safe-delivery checks (fingerprint, pane revision,
live status, idempotency) so no surface can drift from the safety invariants
in docs/SAFETY.md / docs/ARCHITECTURE.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from hark.delivery import DeliveryStore
from hark.fingerprint import question_fingerprint
from hark.herdr.client import HerdrError


@dataclass
class AnswerResult:
    ok: bool
    event_id: str
    status: str  # delivered | rejected | uncertain
    reason: str | None = None  # rejection reason code
    target: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "event_id": self.event_id,
            "status": self.status,
            "detail": self.reason,
        }


def answer_bound_event(
    event_id: str,
    *,
    text: str | None = None,
    keys: list[str] | None = None,
    store: DeliveryStore | None = None,
    client_for: Callable[[str], Any],
    register_fallback: Callable[[str], dict[str, Any] | None] | None = None,
) -> AnswerResult:
    """Deliver ``text`` or ``keys`` to the target bound to ``event_id``.

    ``client_for(session_id)`` returns a Herdr client. ``register_fallback``
    (dashboard register-on-demand) maps an unknown ``event_id`` to its HEP
    record so events observed by the tailer — but never registered by a live
    ``hark watch --register-events`` — remain answerable.
    """
    if bool(text) == bool(keys):
        return AnswerResult(False, event_id, "rejected", "bad_request")

    store = store or DeliveryStore()
    bound = store.get(event_id)
    if bound is None and register_fallback is not None:
        hep = register_fallback(event_id)
        if hep is not None:
            bound = store.register_from_hep(hep)
    if bound is None:
        return AnswerResult(False, event_id, "rejected", "unknown_event")
    if store.already_delivered(event_id):
        return AnswerResult(False, event_id, "rejected", "already_delivered")
    if bound.status != "pending":
        return AnswerResult(False, event_id, "rejected", f"not_pending:{bound.status}")

    fingerprint = (
        bound.question_fingerprint.strip()
        if isinstance(bound.question_fingerprint, str)
        else ""
    )
    if not fingerprint:
        store.mark(event_id, "rejected", reason="missing_question_fingerprint")
        return AnswerResult(False, event_id, "rejected", "missing_question_fingerprint")

    has_revision = isinstance(bound.pane_revision, int) and bound.pane_revision > 0
    target = f"{bound.session_id}/{bound.pane_id}"

    client = client_for(bound.session_id)
    live = client.get_agent(bound.pane_id)
    if live is None:
        store.mark(event_id, "rejected", reason="pane_gone")
        return AnswerResult(False, event_id, "rejected", "pane_gone", target)
    if live.status != "blocked":
        store.mark(event_id, "rejected", reason="not_blocked")
        return AnswerResult(False, event_id, "rejected", "not_blocked", target)
    if has_revision and live.revision != bound.pane_revision:
        store.mark(event_id, "rejected", reason="stale_revision")
        return AnswerResult(False, event_id, "rejected", "stale_revision", target)

    try:
        pane_text = client.read_pane(bound.pane_id, lines=40)
        from hark.events import extract_question_excerpt

        excerpt = extract_question_excerpt(pane_text)
        live_fp = question_fingerprint(excerpt)
        if live_fp != fingerprint:
            store.mark(event_id, "rejected", reason="fingerprint_mismatch")
            return AnswerResult(False, event_id, "rejected", "fingerprint_mismatch", target)
    except HerdrError:
        store.mark(event_id, "rejected", reason="fingerprint_unavailable")
        return AnswerResult(False, event_id, "rejected", "fingerprint_unavailable", target)

    try:
        if keys:
            client.send_keys(bound.pane_id, list(keys))
            store.mark(event_id, "delivered", keys=list(keys))
        else:
            client.send_text(bound.pane_id, text)
            store.mark(event_id, "delivered", text=text)
    except HerdrError as exc:
        # The write may or may not have landed — never blind-retry.
        store.mark(event_id, "uncertain", reason=str(exc))
        return AnswerResult(True, event_id, "uncertain", str(exc), target)

    return AnswerResult(True, event_id, "delivered", None, target)

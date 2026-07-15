"""Bound-answer core shared by `hark answer` (CLI) and `hark serve` (/answer).

Single implementation of the safe-delivery checks (fingerprint, pane revision,
live status / false-done compatibility, idempotency) so no surface can drift
from the safety invariants in docs/SAFETY.md / docs/plans/P1-M2-answerability.md.

Live-compatible gates live in ``hark.answerability``; this module owns store
lookup, send, and mark delivered/rejected/uncertain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from hark.answerability import assess_live, hep_kind_from_bound
from hark.delivery import DeliveryStore


@dataclass
class AnswerResult:
    ok: bool
    event_id: str
    status: str  # delivered | in_progress | rejected | uncertain
    reason: str | None = None  # rejection reason code
    target: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "event_id": self.event_id,
            "status": self.status,
            "detail": self.reason,
        }


def _result_for_claim(event_id: str, target: str, claim: Any) -> AnswerResult:
    """Translate a non-owned durable claim into the public answer result."""
    if claim.status == "delivered":
        return AnswerResult(False, event_id, "rejected", "already_delivered", target)
    if claim.status == "in_progress":
        return AnswerResult(
            False,
            event_id,
            "in_progress",
            claim.reason or "delivery_in_progress",
            target,
        )
    if claim.status == "uncertain":
        return AnswerResult(True, event_id, "uncertain", claim.reason, target)
    return AnswerResult(
        False, event_id, "rejected", claim.reason or "not_pending", target
    )


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

    target = f"{bound.session_id}/{bound.pane_id}"
    acquire = getattr(store, "acquire_delivery", None)
    advance = getattr(store, "advance_delivery", None)
    current_delivery = getattr(store, "current_delivery", None)
    ensure_uncertain = getattr(store, "ensure_uncertain_after_send", None)
    owner_token: str | None = None
    if callable(acquire) and callable(advance):
        claim = acquire(event_id, event_status=bound.status)
        if not claim.owned:
            return _result_for_claim(event_id, target, claim)
        owner_token = claim.token
    else:
        # Compatibility for small in-memory test/adaptor stores.  Durable
        # DeliveryStore instances always take the atomic path above.
        if store.already_delivered(event_id):
            return AnswerResult(False, event_id, "rejected", "already_delivered")
        if bound.status != "pending":
            return AnswerResult(
                False, event_id, "rejected", f"not_pending:{bound.status}"
            )

    fingerprint = (
        bound.question_fingerprint.strip()
        if isinstance(bound.question_fingerprint, str)
        else ""
    )
    if not fingerprint:
        if owner_token is not None:
            rejected = advance(
                event_id,
                owner_token,
                "rejected",
                reason="missing_question_fingerprint",
            )
            if not rejected and callable(current_delivery):
                return _result_for_claim(
                    event_id,
                    target,
                    current_delivery(event_id, event_status=bound.status),
                )
        else:
            store.mark(event_id, "rejected", reason="missing_question_fingerprint")
        return AnswerResult(False, event_id, "rejected", "missing_question_fingerprint")

    if owner_token is not None and not advance(event_id, owner_token, "validating"):
        if callable(current_delivery):
            return _result_for_claim(
                event_id,
                target,
                current_delivery(event_id, event_status=bound.status),
            )
        return AnswerResult(
            False, event_id, "in_progress", "delivery_ownership_lost", target
        )

    client = client_for(bound.session_id)

    verdict = assess_live(
        pane_id=bound.pane_id,
        bound_revision=int(bound.pane_revision or 0),
        bound_fingerprint=fingerprint,
        hep_kind=hep_kind_from_bound(bound),
        client=client,
    )
    if not verdict.ok:
        if owner_token is not None:
            rejected = advance(event_id, owner_token, "rejected", reason=verdict.reason)
            if not rejected and callable(current_delivery):
                return _result_for_claim(
                    event_id,
                    target,
                    current_delivery(event_id, event_status=bound.status),
                )
        else:
            store.mark(event_id, "rejected", reason=verdict.reason)
        return AnswerResult(False, event_id, "rejected", verdict.reason, target)

    if owner_token is not None and not advance(event_id, owner_token, "sending"):
        if callable(current_delivery):
            return _result_for_claim(
                event_id,
                target,
                current_delivery(event_id, event_status=bound.status),
            )
        return AnswerResult(
            False, event_id, "in_progress", "delivery_ownership_lost", target
        )

    try:
        if keys:
            client.send_keys(bound.pane_id, list(keys))
            delivered = (
                advance(event_id, owner_token, "delivered", keys=list(keys))
                if owner_token is not None
                else None
            )
            if owner_token is None:
                store.mark(event_id, "delivered", keys=list(keys))
        else:
            client.send_text(bound.pane_id, text)
            delivered = (
                advance(event_id, owner_token, "delivered", text=text)
                if owner_token is not None
                else None
            )
            if owner_token is None:
                store.mark(event_id, "delivered", text=text)
        if owner_token is not None and not delivered:
            # The send returned but ownership changed while it was in flight;
            # actively repair legacy/conflicting state to durable uncertainty.
            durable = (
                ensure_uncertain(
                    event_id,
                    owner_token,
                    reason="delivery_state_changed_after_send",
                )
                if callable(ensure_uncertain)
                else "uncertain"
            )
            if durable == "delivered":
                return AnswerResult(True, event_id, "delivered", None, target)
            return AnswerResult(
                True,
                event_id,
                "uncertain",
                "delivery_state_changed_after_send",
                target,
            )
    except Exception as exc:  # noqa: BLE001 - any post-boundary error is ambiguous
        # The write may or may not have landed — never blind-retry, regardless
        # of which transport/runtime exception escaped the client.
        if owner_token is not None:
            persisted = advance(event_id, owner_token, "uncertain", reason=str(exc))
            if not persisted and callable(ensure_uncertain):
                ensure_uncertain(event_id, owner_token, reason=str(exc))
        else:
            store.mark(event_id, "uncertain", reason=str(exc))
        return AnswerResult(True, event_id, "uncertain", str(exc), target)

    return AnswerResult(True, event_id, "delivered", None, target)

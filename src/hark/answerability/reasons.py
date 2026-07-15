"""Stable Answerability reason codes (public contract for answer + queue)."""

from __future__ import annotations

# Success
OK = "ok"

# Live / matrix refusals
PANE_GONE = "pane_gone"
NOT_COMPATIBLE = "not_compatible"
# Legacy synonym used by older answer tests / callers; prefer NOT_COMPATIBLE.
NOT_BLOCKED = "not_blocked"
STALE_REVISION = "stale_revision"
FINGERPRINT_MISMATCH = "fingerprint_mismatch"
FINGERPRINT_UNAVAILABLE = "fingerprint_unavailable"
MISSING_QUESTION_FINGERPRINT = "missing_question_fingerprint"

# Store / request gates (orchestrator; pure core does not emit these)
ALREADY_DELIVERED = "already_delivered"
UNKNOWN_EVENT = "unknown_event"
BAD_REQUEST = "bad_request"

# Queue fail-soft prefix when Herdr transport fails before a snapshot
HERDR_ERROR_PREFIX = "herdr_error:"

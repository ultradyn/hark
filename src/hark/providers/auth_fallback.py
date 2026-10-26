"""Authentication failures eligible for an observable auto-provider handover."""

from __future__ import annotations

import re

from hark.providers.base import (
    ProviderError,
    ProviderUnsupported,
    safe_exception_detail,
)

_STATUS = re.compile(r"^(?:[\w -]+?\s+)?(?:HTTP\s+)?(\d{3})\b", re.IGNORECASE)
_MISSING = ("auth missing", "api key missing", "credentials missing")


def is_auth_failure(exc: BaseException) -> bool:
    """Recognize provider auth rejection, never generic errors or quota failures.

    Providers preserve HTTP status in their messages across the isolated TTS
    worker boundary; xAI/OpenAI's special 401 messages omit the word HTTP.
    """
    if not isinstance(exc, ProviderError) or isinstance(exc, ProviderUnsupported):
        return False
    detail = safe_exception_detail(exc, fallback="", limit=4000).lower()
    status = _STATUS.search(detail)
    if status is not None:
        return status.group(1) in ("401", "403")
    return any(marker in detail for marker in _MISSING)

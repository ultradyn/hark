"""One complete JSONL record from a followed state file."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FeedRecord:
    """A single completed JSONL object with cursor coordinates.

    ``source`` is the logical envelope source (watch, ambient, delivery, …).
    ``cursor_key`` may be finer-grained (e.g. ``bound`` vs ``delivery`` for
    delivery envelopes) and is what appears in the composite cursor token.
    ``seq`` is 1-based line index in the current file incarnation.
    ``incarnation`` identifies that backing-file incarnation when known.
    """

    source: str
    cursor_key: str
    seq: int
    payload: dict[str, Any]
    incarnation: str | None = None

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
    The remaining fields are opaque checkpoint metadata used for safe,
    byte-offset resume; adapters should preserve them in emitted cursors.
    """

    source: str
    cursor_key: str
    seq: int
    payload: dict[str, Any]
    incarnation: str | None = None
    checkpoint: str | None = None
    byte_offset: int | None = None

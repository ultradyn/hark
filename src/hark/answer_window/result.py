"""Answer Window result type (shared with speech facade)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ListenResult:
    """Outcome of one answer-window capture."""

    text: str
    provider: str
    duration_ms: int
    end_mode: str
    end_phrase: str | None = None
    cancelled: bool = False
    stream_id: str | None = None
    partials_emitted: int = 0
    meta_command: str | None = None

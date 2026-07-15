"""Multi-path StateFeedFollower with composite cursor resume."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from hark.state_feed.cursor import (
    InvalidCursorPosition,
    format_cursor,
    parse_cursor_positions,
)
from hark.state_feed.record import FeedRecord
from hark.state_feed.source import SourceFollower


def line_count(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(
                chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 20), b"")
            )
    except OSError:
        return 0


class StateFeedFollower:
    """Follow many JSONL sources with one composite cursor token.

    Adapters (monitor, dashboard) supply the source list and interpret payloads;
    this core owns buffer/rotation/seq/cursor only.
    """

    def __init__(self, sources: list[SourceFollower]) -> None:
        self.sources = list(sources)

    @property
    def tailers(self) -> list[SourceFollower]:
        """Alias for dashboard adapter compatibility (MultiTailer.tailers)."""
        return self.sources

    def composite_cursor(self) -> str:
        return format_cursor((s.cursor_key, s.cursor_position) for s in self.sources)

    def start_live(self) -> None:
        for s in self.sources:
            s.start_at_end()

    def start_from(self, cursor: str | None, *, default_tail: int = 0) -> None:
        """Resume from a composite cursor; unknown keys fall back to a recent
        tail of ``default_tail`` records (0 = from end)."""
        positions = parse_cursor_positions(cursor)
        for s in self.sources:
            if s.cursor_key in positions:
                position = positions[s.cursor_key]
                if isinstance(position, InvalidCursorPosition):
                    s.seek_to(0, conservative_legacy=True)
                    continue
                s.seek_to(
                    position.seq,
                    incarnation=position.incarnation,
                    checkpoint=position.checkpoint,
                    conservative_legacy=(
                        position.incarnation is None or position.checkpoint is None
                    ),
                )
            elif default_tail > 0:
                s.seek_to(max(0, line_count(s.path) - default_tail))
            else:
                s.start_at_end()

    def poll(self) -> Iterator[FeedRecord]:
        for s in self.sources:
            yield from s.poll()

    def close(self) -> None:
        for s in self.sources:
            s.close()

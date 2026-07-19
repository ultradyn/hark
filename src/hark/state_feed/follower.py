"""Multi-path StateFeedFollower with composite cursor resume."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from hark.state_feed.cursor import (
    CursorPosition,
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
        return format_cursor(
            (source.cursor_key, source.cursor_position) for source in self.sources
        )

    def start_live(self) -> None:
        for s in self.sources:
            s.start_at_end()

    def start_live_with_snapshot(self) -> list[FeedRecord]:
        """Establish live cursors and return records before each boundary.

        All source boundaries are subscribed before this method returns, so
        callers can emit a replay from the snapshot without opening a
        replay-to-live gap for appends that land during that emission.
        """
        records: list[FeedRecord] = []
        for source in self.sources:
            records.extend(source.snapshot_at_end())
        return records

    def start_from(self, cursor: str | None, *, default_tail: int = 0) -> None:
        """Resume from a composite cursor; unknown keys fall back to a recent
        tail of ``default_tail`` records (0 = from end)."""
        positions = parse_cursor_positions(cursor)
        for s in self.sources:
            if s.cursor_key in positions:
                position = positions[s.cursor_key]
                if isinstance(position, InvalidCursorPosition):
                    # Invalid known keys must not skip; replay from zero.
                    s.seek_to(CursorPosition(seq=0), conservative_legacy=True)
                    continue
                s.seek_to(
                    position,
                    conservative_legacy=(
                        position.incarnation is None or position.checkpoint is None
                    ),
                )
            elif default_tail > 0:
                s.seek_to(
                    CursorPosition(seq=max(0, line_count(s.path) - default_tail))
                )
            else:
                s.start_at_end()

    def poll(self) -> Iterator[FeedRecord]:
        for s in self.sources:
            yield from s.poll()

    def close(self) -> None:
        for s in self.sources:
            s.close()

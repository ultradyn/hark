"""Deep State Feed Follower: multi-path JSONL follow + presentation.

Producers append full events to state JSONL. Adapters (``hark monitor``,
dashboard MultiTailer) share :class:`StateFeedFollower` for hardened follow
(partial buffer, inode rotation, composite cursor). See
``docs/plans/P1-M5-state-feed-follower.md``.
"""

from __future__ import annotations

from hark.state_feed.cursor import (
    CursorPosition,
    InvalidCursorPosition,
    format_cursor,
    parse_cursor,
    parse_cursor_positions,
)
from hark.state_feed.follower import StateFeedFollower, line_count
from hark.state_feed.present import present_for_monitor
from hark.state_feed.record import FeedRecord
from hark.state_feed.source import SourceFollower

__all__ = [
    "FeedRecord",
    "CursorPosition",
    "InvalidCursorPosition",
    "SourceFollower",
    "StateFeedFollower",
    "format_cursor",
    "line_count",
    "parse_cursor",
    "parse_cursor_positions",
    "present_for_monitor",
]

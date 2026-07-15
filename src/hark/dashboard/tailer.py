"""JSONL source tailers with per-source cursors (hark.dashboard.v1).

Thin adapter over :mod:`hark.state_feed` (P1.M5). Hardened follow lives in
the deep core; this module owns dashboard source map, envelope transforms,
and ``read_page`` sorting/limits.

Cursor format (composite): ``key:seq@incarnation~checkpoint,…`` — see
DASHBOARD.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from hark.state_feed import (
    FeedRecord,
    SourceFollower,
    StateFeedFollower,
    parse_cursor,
)
# Public aliases — tests and server import these names.
Record = FeedRecord
SourceTailer = SourceFollower


def _delivery_bound(obj: dict[str, Any]) -> dict[str, Any]:
    return {"type": "bound", **obj}


def _delivery_outcome(obj: dict[str, Any]) -> dict[str, Any]:
    return {"type": "outcome", **obj}


def default_tailers(state: Path) -> list[SourceFollower]:
    return [
        SourceFollower(state / "watch.jsonl", source="watch"),
        SourceFollower(state / "ambient.jsonl", source="ambient"),
        SourceFollower(state / "system.jsonl", source="system"),
        SourceFollower(state / "usage.jsonl", source="usage"),
        SourceFollower(
            state / "events.jsonl",
            source="delivery",
            cursor_key="bound",
            transform=_delivery_bound,
        ),
        SourceFollower(
            state / "deliveries.jsonl",
            source="delivery",
            cursor_key="delivery",
            transform=_delivery_outcome,
        ),
    ]


class MultiTailer:
    """Dashboard adapter: all sources with one composite cursor over StateFeedFollower."""

    def __init__(
        self, state: Path, tailers: list[SourceFollower] | None = None
    ) -> None:
        self.state = state
        sources = tailers if tailers is not None else default_tailers(state)
        self._follower = StateFeedFollower(sources)
        # Expose list for tests / composite_cursor parity
        self.tailers = self._follower.sources

    def composite_cursor(self) -> str:
        return self._follower.composite_cursor()

    def start_live(self) -> None:
        self._follower.start_live()

    def start_from(self, cursor: str | None, *, default_tail: int = 0) -> None:
        self._follower.start_from(cursor, default_tail=default_tail)

    def poll(self) -> Iterator[FeedRecord]:
        yield from self._follower.poll()

    def close(self) -> None:
        self._follower.close()


def read_page(
    state: Path,
    *,
    since: str | None,
    sources: set[str] | None = None,
    limit: int = 500,
    history_limit: int = 2000,
) -> tuple[list[FeedRecord], str, bool]:
    """One-shot backfill page for GET /api/v1/events."""
    tailers = [
        t
        for t in default_tailers(state)
        if sources is None or t.source in sources
    ]
    mt = MultiTailer(state, tailers)
    mt.start_from(since, default_tail=history_limit if since is None else 0)
    records = list(mt.poll())
    mt.close()
    # interleave stably: JSONL files are append-ordered; cross-source order is
    # best-effort by per-record timestamp when present
    records.sort(key=_record_ts)
    complete = len(records) <= limit
    if not complete:
        records = records[-limit:]
    return records, mt.composite_cursor(), complete


def _record_ts(rec: FeedRecord) -> float:
    p = rec.payload
    for key in ("ts", "created_at"):
        v = p.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    v = p.get("observed_at")
    if isinstance(v, str) and v:
        return _iso_proxy(v)
    return 0.0


def _iso_proxy(iso: str) -> float:
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


# Re-export for any external imports of the helper
__all__ = [
    "MultiTailer",
    "Record",
    "SourceTailer",
    "default_tailers",
    "parse_cursor",
    "read_page",
]

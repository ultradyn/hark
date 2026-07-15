"""JSONL source tailers with per-source cursors (hark.dashboard.v1).

Thin adapter over :mod:`hark.state_feed` (P1.M5). Hardened follow lives in
the deep core; this module owns dashboard source map, envelope transforms,
and ``read_page`` sorting/limits.

Cursor format (composite): ``key:seq,key:seq,…`` — see DASHBOARD.md.
"""

from __future__ import annotations

import heapq
from pathlib import Path
from typing import Any, Iterator

from hark.state_feed import (
    FeedRecord,
    SourceFollower,
    StateFeedFollower,
    format_cursor,
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
    limit: int | None = 500,
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
    records = _replay_order(records)
    complete = limit is None or len(records) <= limit
    if limit is not None and not complete:
        # A fresh page is explicitly a recent-tail snapshot.  A resumed page
        # is forward pagination: retain the earliest unseen records so its
        # cursor cannot jump over an omitted record.
        records = records[-limit:] if since is None and limit else records[:limit]
    cursor = mt.composite_cursor()
    if since is not None and not complete:
        cursor = _cursor_after(records, since)
    return records, cursor, complete


def records_with_cursors(
    records: list[FeedRecord], since: str | None
) -> Iterator[tuple[FeedRecord, str]]:
    """Pair records with a composite cursor at that exact replay boundary.

    ``read_page`` sorts records across sources after polling them all, so its
    page cursor is necessarily the high-water mark *after the whole page*.
    Reusing that cursor on each envelope lets an SSE disconnect skip the rest
    of the page.  Advance a copy of the client's starting positions one record
    at a time instead; positions for sources not yet replayed stay unchanged.
    """
    positions = parse_cursor(since)
    for record in records:
        positions[record.cursor_key] = record.seq
        yield record, format_cursor(positions)


def _cursor_after(records: list[FeedRecord], since: str) -> str:
    cursor = since
    for _, cursor in records_with_cursors(records, since):
        pass
    return cursor


def _replay_order(records: list[FeedRecord]) -> list[FeedRecord]:
    """Timestamp-best-effort interleave without reordering a cursor stream.

    Payload clocks are not guaranteed monotonic.  A global timestamp sort can
    therefore emit ``watch:2`` before ``watch:1`` and make a mid-page resume
    skip the unseen first record.  Treat each cursor key as an ordered chain
    and merge only the current head of each chain by timestamp.
    """
    chains: dict[str, list[tuple[int, FeedRecord]]] = {}
    for original_index, record in enumerate(records):
        chains.setdefault(record.cursor_key, []).append((original_index, record))

    heap: list[tuple[float, int, str, int]] = []
    for cursor_key, chain in chains.items():
        original_index, record = chain[0]
        heapq.heappush(
            heap, (_record_ts(record), original_index, cursor_key, 0)
        )

    ordered: list[FeedRecord] = []
    while heap:
        _, _, cursor_key, chain_index = heapq.heappop(heap)
        chain = chains[cursor_key]
        _, record = chain[chain_index]
        ordered.append(record)
        next_index = chain_index + 1
        if next_index < len(chain):
            original_index, next_record = chain[next_index]
            heapq.heappush(
                heap,
                (_record_ts(next_record), original_index, cursor_key, next_index),
            )
    return ordered


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
    "records_with_cursors",
]

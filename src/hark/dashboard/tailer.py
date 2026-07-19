"""JSONL source tailers with per-source cursors (hark.dashboard.v1).

Thin adapter over :mod:`hark.state_feed` (P1.M5). Hardened follow lives in
the deep core; this module owns dashboard source map, envelope transforms,
and ``read_page`` sorting/limits.

Cursor format is a composite of per-source sequence/checkpoint positions; see
``DASHBOARD.md``. Legacy ``key:seq`` inputs remain accepted.
"""

from __future__ import annotations

import heapq
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Iterator

from hark.state_feed import (
    CursorPosition,
    FeedRecord,
    SourceFollower,
    StateFeedFollower,
    format_cursor,
    parse_cursor,
    parse_cursor_positions,
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
        t for t in default_tailers(state) if sources is None or t.source in sources
    ]
    mt = MultiTailer(state, tailers)
    mt.start_from(since, default_tail=history_limit if since is None else 0)
    try:
        ordered = _iter_replay_order(mt.tailers)
        if since is None:
            # A fresh page is a bounded recent-tail snapshot.  Materializing
            # only the requested tail avoids retaining every record from the
            # per-source ``history_limit`` windows.
            page_limit = max(limit, 0)
            recent: deque[FeedRecord] = deque(maxlen=page_limit)
            count = 0
            for record in ordered:
                count += 1
                recent.append(record)
            return list(recent), mt.composite_cursor(), count <= page_limit

        # Forward paging must not scan/materialize the whole unseen suffix.
        # Pull one bounded lookahead record solely to determine completeness;
        # the response cursor advances only through records actually returned.
        page_limit = max(limit, 0)
        materialized: list[FeedRecord] = []
        for _ in range(page_limit + 1):
            try:
                materialized.append(next(ordered))
            except StopIteration:
                break
        complete = len(materialized) <= page_limit
        records = materialized if complete else materialized[:page_limit]
        cursor = (
            _cursor_from_tailers(mt.tailers, since)
            if complete
            else _cursor_after(records, since)
        )
        return records, cursor, complete
    finally:
        mt.close()


def iter_replay_records(
    state: Path,
    *,
    since: str,
    sources: set[str] | None = None,
) -> Iterator[FeedRecord]:
    """Stream a forward replay in one bounded-memory source merge."""
    tailers = [
        tailer
        for tailer in default_tailers(state)
        if sources is None or tailer.source in sources
    ]
    mt = MultiTailer(state, tailers)
    mt.start_from(since, default_tail=0)
    try:
        yield from _iter_replay_order(mt.tailers)
    finally:
        mt.close()


def records_with_cursors(
    records: Iterable[FeedRecord], since: str | None
) -> Iterator[tuple[FeedRecord, str]]:
    """Pair records with a composite cursor at that exact replay boundary.

    ``read_page`` sorts records across sources after polling them all, so its
    page cursor is necessarily the high-water mark *after the whole page*.
    Reusing that cursor on each envelope lets an SSE disconnect skip the rest
    of the page.  Advance a copy of the client's starting positions one record
    at a time instead; positions for sources not yet replayed stay unchanged.
    """
    positions = parse_cursor_positions(since)
    for record in records:
        positions[record.cursor_key] = _record_position(record)
        yield record, format_cursor(positions)


def _record_position(record: FeedRecord) -> CursorPosition:
    return CursorPosition(
        seq=record.seq,
        incarnation=record.incarnation,
        checkpoint=record.checkpoint,
        byte_offset=record.byte_offset,
    )


def _cursor_from_tailers(tailers: list[SourceFollower], since: str | None) -> str:
    """Return tailer frontiers while preserving unselected cursor keys."""
    positions = parse_cursor_positions(since)
    for tailer in tailers:
        positions[tailer.cursor_key] = tailer.cursor_position
    return format_cursor(positions)


def _cursor_after(records: list[FeedRecord], since: str) -> str:
    cursor = since
    for _, cursor in records_with_cursors(records, since):
        pass
    return cursor


def _iter_replay_order(
    tailers: list[SourceFollower],
) -> Iterator[FeedRecord]:
    """Lazily interleave source heads without reordering a cursor stream.

    Payload clocks are not guaranteed monotonic.  A global timestamp sort can
    therefore emit ``watch:2`` before ``watch:1`` and make a mid-page resume
    skip the unseen first record.  Each source iterator is an ordered chain;
    keep only its current head in the heap.  Memory and parsing are therefore
    bounded by the requested page plus one lookahead, not the unseen suffix.
    """
    heap: list[tuple[float, int, FeedRecord, Iterator[FeedRecord]]] = []
    for source_index, tailer in enumerate(tailers):
        iterator = iter(tailer.poll())
        try:
            record = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(heap, (_record_ts(record), source_index, record, iterator))

    while heap:
        _, source_index, record, iterator = heapq.heappop(heap)
        yield record
        try:
            next_record = next(iterator)
        except StopIteration:
            continue
        heapq.heappush(
            heap, (_record_ts(next_record), source_index, next_record, iterator)
        )


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
    "iter_replay_records",
    "parse_cursor",
    "read_page",
    "records_with_cursors",
]

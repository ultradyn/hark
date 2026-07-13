"""JSONL source tailers with per-source cursors (hark.dashboard.v1).

Hardened beyond ``monitor_feed.follow_state_files``:

- partial trailing lines are buffered, never dropped (a line is consumed only
  once its ``\\n`` arrives);
- rotation is detected by inode/device change as well as truncation;
- every record gets a per-source ``seq`` (1-based line index in the current
  file incarnation) forming the composite cursor documented in DASHBOARD.md.

Cursor keys are per backing stream and may be finer-grained than envelope
sources: ``events.jsonl`` tracks as ``bound`` while ``deliveries.jsonl``
tracks as ``delivery`` — both surface as envelope source ``delivery``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


@dataclass
class Record:
    source: str  # envelope source
    cursor_key: str  # cursor key (usually == source)
    seq: int
    payload: dict[str, Any]


class SourceTailer:
    """Incremental reader for one JSONL file with seq tracking."""

    def __init__(
        self,
        path: Path,
        *,
        source: str,
        cursor_key: str | None = None,
        transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.path = path
        self.source = source
        self.cursor_key = cursor_key or source
        self.transform = transform
        self._fh = None
        self._ident: tuple[int, int] | None = None  # (st_dev, st_ino)
        self._buf = ""
        self._size_seen = 0
        self.seq = 0  # last emitted seq (line number in current incarnation)

    def _stat_ident(self) -> tuple[int, int] | None:
        try:
            st = self.path.stat()
            return (st.st_dev, st.st_ino)
        except OSError:
            return None

    def _reopen(self, *, from_start: bool) -> None:
        self.close()
        try:
            self._fh = self.path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            self._fh = None
            return
        self._ident = self._stat_ident()
        self._buf = ""
        self.seq = 0
        try:
            self._size_seen = self.path.stat().st_size
        except OSError:
            self._size_seen = 0
        if not from_start:
            self._skip_lines(None)

    def _skip_lines(self, target: int | None) -> None:
        """Advance past ``target`` complete lines (all when None), keeping any
        trailing partial line in the buffer so no record is ever split."""
        assert self._fh is not None
        while True:
            chunk = self._fh.read(65536)
            if not chunk:
                return
            self._buf += chunk
            while True:
                if target is not None and self.seq >= target:
                    return
                nl = self._buf.find("\n")
                if nl < 0:
                    break
                self._buf = self._buf[nl + 1 :]
                self.seq += 1

    def seek_to(self, seq: int) -> None:
        """Position so the next emitted record is ``seq + 1`` (best effort).

        Unknown/rotated positions fall back to the file start (gap beats a
        dead stream — DASHBOARD.md cursor semantics).
        """
        self._reopen(from_start=True)
        if self._fh is None:
            return
        self._skip_lines(seq)

    def start_at_end(self) -> None:
        self._reopen(from_start=False)

    def poll(self) -> Iterator[Record]:
        """Yield complete new records since the last poll."""
        ident = self._stat_ident()
        if self._fh is None:
            if ident is None:
                return
            self._reopen(from_start=True)
            if self._fh is None:
                return
        elif ident is not None and ident != self._ident:
            # rotated/replaced: drain nothing further from the old handle,
            # start the new incarnation from the top
            self._reopen(from_start=True)
            if self._fh is None:
                return
        else:
            try:
                size = self.path.stat().st_size
            except OSError:
                return
            if size < self._size_seen:
                # truncated in place
                self._reopen(from_start=True)
                if self._fh is None:
                    return
            self._size_seen = size

        while True:
            # drain complete lines already buffered (e.g. after seek_to)
            while True:
                nl = self._buf.find("\n")
                if nl < 0:
                    break
                line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
                self.seq += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if self.transform is not None:
                    obj = self.transform(obj)
                yield Record(self.source, self.cursor_key, self.seq, obj)
            chunk = self._fh.read(65536)
            if not chunk:
                return
            self._buf += chunk

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None


def _delivery_bound(obj: dict[str, Any]) -> dict[str, Any]:
    return {"type": "bound", **obj}


def _delivery_outcome(obj: dict[str, Any]) -> dict[str, Any]:
    return {"type": "outcome", **obj}


def default_tailers(state: Path) -> list[SourceTailer]:
    return [
        SourceTailer(state / "watch.jsonl", source="watch"),
        SourceTailer(state / "ambient.jsonl", source="ambient"),
        SourceTailer(state / "system.jsonl", source="system"),
        SourceTailer(state / "usage.jsonl", source="usage"),
        SourceTailer(
            state / "events.jsonl",
            source="delivery",
            cursor_key="bound",
            transform=_delivery_bound,
        ),
        SourceTailer(
            state / "deliveries.jsonl",
            source="delivery",
            cursor_key="delivery",
            transform=_delivery_outcome,
        ),
    ]


def parse_cursor(cursor: str | None) -> dict[str, int]:
    """``"watch:12,system:9"`` -> ``{"watch": 12, "system": 9}`` (lenient)."""
    out: dict[str, int] = {}
    if not cursor:
        return out
    for part in cursor.split(","):
        key, _, num = part.strip().partition(":")
        if key and num.isdigit():
            out[key] = int(num)
    return out


class MultiTailer:
    """All dashboard sources with one composite cursor."""

    def __init__(self, state: Path, tailers: list[SourceTailer] | None = None) -> None:
        self.state = state
        self.tailers = tailers if tailers is not None else default_tailers(state)

    def composite_cursor(self) -> str:
        return ",".join(f"{t.cursor_key}:{t.seq}" for t in self.tailers)

    def start_live(self) -> None:
        for t in self.tailers:
            t.start_at_end()

    def start_from(self, cursor: str | None, *, default_tail: int = 0) -> None:
        """Resume from a composite cursor; unknown keys fall back to a recent
        tail of ``default_tail`` records (0 = from end)."""
        positions = parse_cursor(cursor)
        for t in self.tailers:
            if t.cursor_key in positions:
                t.seek_to(positions[t.cursor_key])
            elif default_tail > 0:
                t.seek_to(max(0, _line_count(t.path) - default_tail))
            else:
                t.start_at_end()

    def poll(self) -> Iterator[Record]:
        for t in self.tailers:
            yield from t.poll()

    def close(self) -> None:
        for t in self.tailers:
            t.close()


def _line_count(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(1 << 20), b""))
    except OSError:
        return 0


def read_page(
    state: Path,
    *,
    since: str | None,
    sources: set[str] | None = None,
    limit: int = 500,
    history_limit: int = 2000,
) -> tuple[list[Record], str, bool]:
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


def _record_ts(rec: Record) -> float:
    p = rec.payload
    for key in ("ts", "created_at"):
        v = p.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    v = p.get("observed_at")
    if isinstance(v, str) and v:
        # ISO-8601 Zulu sorts lexically; cheap stable proxy without parsing
        return _iso_proxy(v)
    return 0.0


def _iso_proxy(iso: str) -> float:
    import datetime as _dt

    try:
        return _dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0

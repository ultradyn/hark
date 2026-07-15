"""Hardened single-file JSONL follower (partial buffer, inode, truncation)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator

from hark.state_feed.record import FeedRecord


class SourceFollower:
    """Incremental reader for one JSONL file with seq tracking.

    - Partial trailing lines are buffered, never dropped (a line is consumed
      only once its ``\\n`` arrives).
    - Rotation is detected by inode/device change as well as truncation.
    - Every record gets a per-source ``seq`` (1-based line index in the current
      file incarnation).
    """

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

    def poll(self) -> Iterator[FeedRecord]:
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
                yield FeedRecord(self.source, self.cursor_key, self.seq, obj)
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

"""Hardened single-file JSONL follower (partial buffer, inode, truncation)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterator

from hark.state_feed.cursor import CursorPosition
from hark.state_feed.record import FeedRecord


_PREFIX_IDENTITY_BYTES = 4096
_CHECKPOINT_SEED = hashlib.blake2s(
    b"hark-state-feed-checkpoint-v1", digest_size=16
).digest()


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
        self._prefix_identity: str | None = None
        self._checkpoint = _CHECKPOINT_SEED
        self._buf = b""
        self._size_seen = 0
        self.seq = 0  # last emitted seq (line number in current incarnation)

    @staticmethod
    def _incarnation(
        ident: tuple[int, int] | None, prefix_identity: str | None
    ) -> str | None:
        """Hash internal filesystem identity into an opaque client token."""
        if ident is None or prefix_identity is None:
            return None
        device, inode = ident
        internal_identity = f"{device}\0{inode}\0{prefix_identity}".encode()
        return hashlib.blake2s(
            b"hark-state-feed-incarnation-v1\0" + internal_identity,
            digest_size=16,
        ).hexdigest()

    @staticmethod
    def _next_checkpoint(checkpoint: bytes, line: bytes) -> bytes:
        """Extend the complete-line prefix proof by one raw JSONL line."""
        return hashlib.blake2s(
            b"hark-state-feed-line-v1\0" + checkpoint + line + b"\n",
            digest_size=16,
        ).digest()

    def _consume_line(self, line: bytes) -> None:
        self._checkpoint = self._next_checkpoint(self._checkpoint, line)
        self.seq += 1

    @staticmethod
    def _prefix_identity_from_fd(fd: int) -> str:
        """Return an append-stable bounded identity for a file's beginning.

        The first complete JSONL line is immutable for an append-only file, so
        ordinary appends do not change this value. Before that line completes,
        all short partial prefixes share a pending marker; completion changes
        the incarnation and causes the whole first record to be replayed. Huge
        first lines are bounded at ``_PREFIX_IDENTITY_BYTES``.
        """
        prefix = os.pread(fd, _PREFIX_IDENTITY_BYTES + 1, 0)
        newline = prefix.find(b"\n")
        if newline >= 0:
            identity_input = b"line\0" + prefix[: newline + 1]
        elif len(prefix) > _PREFIX_IDENTITY_BYTES:
            identity_input = b"bounded\0" + prefix[:_PREFIX_IDENTITY_BYTES]
        else:
            identity_input = b"pending"
        return hashlib.blake2s(identity_input, digest_size=16).hexdigest()

    @property
    def cursor_position(self) -> CursorPosition:
        incarnation = self._incarnation(self._ident, self._prefix_identity)
        return CursorPosition(
            self.seq,
            incarnation,
            self._checkpoint.hex() if incarnation is not None else None,
        )

    def _path_snapshot(self) -> tuple[tuple[int, int], int, str] | None:
        try:
            with self.path.open("rb") as handle:
                stat = os.fstat(handle.fileno())
                return (
                    (stat.st_dev, stat.st_ino),
                    stat.st_size,
                    self._prefix_identity_from_fd(handle.fileno()),
                )
        except OSError:
            return None

    def _reopen(self, *, from_start: bool) -> None:
        self.close()
        self._ident = None
        self._prefix_identity = None
        self._checkpoint = _CHECKPOINT_SEED
        self._buf = b""
        self._size_seen = 0
        self.seq = 0
        try:
            self._fh = self.path.open("rb")
        except OSError:
            self._fh = None
            return
        try:
            stat = os.fstat(self._fh.fileno())
            self._ident = (stat.st_dev, stat.st_ino)
            self._size_seen = stat.st_size
            self._prefix_identity = self._prefix_identity_from_fd(self._fh.fileno())
        except OSError:
            self.close()
            return
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
                nl = self._buf.find(b"\n")
                if nl < 0:
                    break
                line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
                self._consume_line(line)

    def seek_to(
        self,
        seq: int,
        *,
        incarnation: str | None = None,
        checkpoint: str | None = None,
        conservative_legacy: bool = False,
    ) -> None:
        """Position so the next emitted record is ``seq + 1`` (best effort).

        ``incarnation`` identifies the file without exposing filesystem values;
        ``checkpoint`` proves its complete-line prefix through ``seq``. A
        mismatch or incomplete legacy proof replays from the first complete
        line when ``conservative_legacy`` is true (duplicates beat silent loss).
        """
        self._reopen(from_start=True)
        if self._fh is None:
            return
        if conservative_legacy or (incarnation is None) != (checkpoint is None):
            return
        if incarnation is None:
            self._skip_lines(seq)
            return
        self._skip_lines(seq)
        position = self.cursor_position
        if (
            self.seq != seq
            or incarnation != position.incarnation
            or checkpoint != position.checkpoint
        ):
            self._reopen(from_start=True)

    def start_at_end(self) -> None:
        self._reopen(from_start=False)

    def poll(self) -> Iterator[FeedRecord]:
        """Yield complete new records since the last poll."""
        snapshot = self._path_snapshot()
        if self._fh is None:
            if snapshot is None:
                return
            self._reopen(from_start=True)
            if self._fh is None:
                return
        elif snapshot is not None and (
            snapshot[0] != self._ident or snapshot[2] != self._prefix_identity
        ):
            # Rotated/replaced, or rewritten in place with a changed bounded
            # prefix: drain nothing further from the old handle and replay.
            self._reopen(from_start=True)
            if self._fh is None:
                return
        else:
            if snapshot is None:
                return
            size = snapshot[1]
            if size < self._size_seen:
                # A truncation that preserves the first line still needs the
                # live follower to restart, even though its bounded identity is
                # necessarily unchanged.
                self._reopen(from_start=True)
                if self._fh is None:
                    return
            self._size_seen = size

        while True:
            # drain complete lines already buffered (e.g. after seek_to)
            while True:
                nl = self._buf.find(b"\n")
                if nl < 0:
                    break
                raw_line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
                self._consume_line(raw_line)
                line = raw_line.decode("utf-8", errors="replace").strip()
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
                yield FeedRecord(
                    self.source,
                    self.cursor_key,
                    self.seq,
                    obj,
                    incarnation=self.cursor_position.incarnation,
                )
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

"""Hardened single-file JSONL follower (partial buffer, rotation, checkpoints)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterator

from hark.state_feed.cursor import CursorPosition
from hark.state_feed.record import FeedRecord


_PREFIX_IDENTITY_BYTES = 4096
_CHECKPOINT_SEED = hashlib.blake2s(
    b"hark-state-feed-checkpoint-v1", digest_size=16
).digest()
_TRUSTED_CHECKPOINT_LIMIT = 8192
_TrustedKey = tuple[str, str, int, str, int]
_TrustedStat = tuple[int, int, int]
_trusted_checkpoints: OrderedDict[_TrustedKey, _TrustedStat] = OrderedDict()
_trusted_lock = threading.Lock()


class SourceFollower:
    """Incremental reader for one append-oriented JSONL source."""

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
        self._ident: tuple[int, int] | None = None
        self._prefix_identity: str | None = None
        self._checkpoint = _CHECKPOINT_SEED
        self._buf = b""
        self._size_seen = 0
        self._mtime_ns = 0
        self._ctime_ns = 0
        self._byte_offset = 0
        self.seq = 0

    @staticmethod
    def _incarnation(
        ident: tuple[int, int] | None, prefix_identity: str | None
    ) -> str | None:
        """Hash internal filesystem identity into an opaque client token."""
        if ident is None or prefix_identity is None:
            return None
        device, inode = ident
        internal = f"{device}\0{inode}\0{prefix_identity}".encode()
        return hashlib.blake2s(
            b"hark-state-feed-incarnation-v1\0" + internal,
            digest_size=16,
        ).hexdigest()

    @staticmethod
    def _next_checkpoint(checkpoint: bytes, line: bytes) -> bytes:
        """Extend the raw complete-line prefix proof by one line."""
        return hashlib.blake2s(
            b"hark-state-feed-line-v1\0" + checkpoint + line + b"\n",
            digest_size=16,
        ).digest()

    @staticmethod
    def _prefix_identity_from_fd(fd: int) -> str:
        chunks: list[bytes] = []
        offset = 0
        while offset <= _PREFIX_IDENTITY_BYTES:
            chunk = os.pread(
                fd,
                min(64, _PREFIX_IDENTITY_BYTES + 1 - offset),
                offset,
            )
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
            if b"\n" in chunk:
                break
        prefix = b"".join(chunks)
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
        position = CursorPosition(
            seq=self.seq,
            incarnation=incarnation,
            checkpoint=self._checkpoint.hex() if incarnation is not None else None,
            byte_offset=self._byte_offset if incarnation is not None else None,
        )
        self._remember(position)
        return position

    def _trusted_key(self, position: CursorPosition) -> _TrustedKey | None:
        if (
            position.incarnation is None
            or position.checkpoint is None
            or position.byte_offset is None
        ):
            return None
        return (
            str(self.path.resolve()),
            position.incarnation,
            position.seq,
            position.checkpoint,
            position.byte_offset,
        )

    def _remember(self, position: CursorPosition) -> None:
        key = self._trusted_key(position)
        if key is None:
            return
        stat = (max(self._size_seen, self._byte_offset), self._mtime_ns, self._ctime_ns)
        with _trusted_lock:
            _trusted_checkpoints[key] = stat
            _trusted_checkpoints.move_to_end(key)
            while len(_trusted_checkpoints) > _TRUSTED_CHECKPOINT_LIMIT:
                _trusted_checkpoints.popitem(last=False)

    @classmethod
    def _checkpoint_from_fd(
        cls, fd: int, end: int
    ) -> tuple[int, bytes] | None:
        """Rebuild the complete-line proof ending at byte offset ``end``."""
        checkpoint = _CHECKPOINT_SEED
        pending = b""
        offset = 0
        seq = 0
        while offset < end:
            chunk = os.pread(fd, min(65536, end - offset), offset)
            if not chunk:
                return None
            offset += len(chunk)
            pending += chunk
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                checkpoint = cls._next_checkpoint(checkpoint, pending[:newline])
                seq += 1
                pending = pending[newline + 1 :]
        if pending:
            # A consumed position must always be a complete-line boundary.
            return None
        return seq, checkpoint

    def _consumed_prefix_matches(
        self,
        fd: int,
        size: int,
        *,
        byte_offset: int | None = None,
        buffer: bytes | None = None,
    ) -> bool:
        """Authenticate this follower's consumed prefix and staged buffer."""
        if self._fh is None:
            return True
        consumed_end = self._byte_offset if byte_offset is None else byte_offset
        staged = self._buf if buffer is None else buffer
        if consumed_end < 0 or size < consumed_end + len(staged):
            return False
        proof = self._checkpoint_from_fd(fd, consumed_end)
        if proof != (self.seq, self._checkpoint):
            return False
        if not staged:
            return True
        return os.pread(fd, len(staged), consumed_end) == staged

    def _path_snapshot(
        self,
        *,
        byte_offset: int | None = None,
        buffer: bytes | None = None,
    ) -> tuple[tuple[int, int], int, int, int, str, bool] | None:
        """Observe path identity and whether our consumed prefix is still current.

        Returns ``None`` when the path is missing or the observation raced a
        concurrent rewrite (caller should retry on a later poll).
        """
        try:
            with self.path.open("rb") as handle:
                before = os.fstat(handle.fileno())
                ident = (before.st_dev, before.st_ino)
                prefix_identity = self._prefix_identity_from_fd(handle.fileno())
                consumed_current = self._consumed_prefix_matches(
                    handle.fileno(),
                    before.st_size,
                    byte_offset=byte_offset,
                    buffer=buffer,
                )
                after = os.fstat(handle.fileno())
                path_stat = self.path.stat()
                if (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                ) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    # Defer an unstable observation until a later poll instead
                    # of replaying an ordinary append that raced this audit.
                    return None
                path_ident = (path_stat.st_dev, path_stat.st_ino)
                if path_ident != ident:
                    return (
                        path_ident,
                        path_stat.st_size,
                        path_stat.st_mtime_ns,
                        path_stat.st_ctime_ns,
                        "",
                        False,
                    )
                if (
                    path_stat.st_size,
                    path_stat.st_mtime_ns,
                    path_stat.st_ctime_ns,
                ) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    return None
                return (
                    ident,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                    prefix_identity,
                    consumed_current,
                )
        except OSError:
            return None

    def _reset(self) -> None:
        self._ident = None
        self._prefix_identity = None
        self._checkpoint = _CHECKPOINT_SEED
        self._buf = b""
        self._size_seen = 0
        self._mtime_ns = 0
        self._ctime_ns = 0
        self._byte_offset = 0
        self.seq = 0

    def _reopen(self, *, from_start: bool) -> None:
        self.close()
        self._reset()
        try:
            self._fh = self.path.open("rb")
            stat = os.fstat(self._fh.fileno())
            self._ident = (stat.st_dev, stat.st_ino)
            self._size_seen = stat.st_size
            self._mtime_ns = stat.st_mtime_ns
            self._ctime_ns = stat.st_ctime_ns
            self._prefix_identity = self._prefix_identity_from_fd(self._fh.fileno())
        except OSError:
            self.close()
            return
        if not from_start:
            self._skip_lines(None)

    def _consume_line(self, line: bytes) -> None:
        self._checkpoint = self._next_checkpoint(self._checkpoint, line)
        self._byte_offset += len(line) + 1
        self.seq += 1

    def _skip_lines(self, target: int | None) -> None:
        """Advance through complete raw lines, retaining a trailing partial."""
        assert self._fh is not None
        if target is not None and self.seq >= target:
            return
        while True:
            line = self._read_complete_line()
            if line is None:
                return
            self._consume_line(line)
            if target is not None and self.seq >= target:
                return

    def _read_complete_line(self) -> bytes | None:
        """Read at most one raw line, retaining an incomplete EOF suffix."""
        assert self._fh is not None
        chunk = self._fh.readline()
        if chunk:
            self._buf += chunk
        if not self._buf.endswith(b"\n"):
            return None
        line = self._buf[:-1]
        self._buf = b""
        return line

    def _seek_trusted(self, position: CursorPosition) -> bool:
        key = self._trusted_key(position)
        if key is None or self._fh is None:
            return False
        with _trusted_lock:
            trusted = _trusted_checkpoints.get(key)
        current_incarnation = self._incarnation(self._ident, self._prefix_identity)
        if trusted is None or current_incarnation != position.incarnation:
            return False
        size, mtime_ns, ctime_ns = trusted
        if (
            self._size_seen < (position.byte_offset or 0)
            or self._size_seen != size
            or self._mtime_ns != mtime_ns
            or self._ctime_ns != ctime_ns
        ):
            return False
        try:
            if (
                position.byte_offset
                and os.pread(self._fh.fileno(), 1, position.byte_offset - 1) != b"\n"
            ):
                return False
            self._fh.seek(position.byte_offset or 0)
            self._checkpoint = bytes.fromhex(position.checkpoint or "")
        except (OSError, ValueError):
            return False
        self._buf = b""
        self._byte_offset = position.byte_offset or 0
        self.seq = position.seq
        return True

    def seek_to(
        self,
        position: CursorPosition | int,
        *,
        incarnation: str | None = None,
        checkpoint: str | None = None,
        conservative_legacy: bool = False,
    ) -> None:
        """Resume from a proved position, replaying safely on any mismatch.

        Callers may pass a :class:`CursorPosition` or a bare sequence with
        optional ``incarnation``/``checkpoint`` keywords (B131-compatible).

        Incomplete proofs and ``conservative_legacy`` reopen at the first
        complete line — duplicates beat silent loss when file identity cannot
        be verified (rotated shorter files, in-place rewrites).
        """
        if isinstance(position, CursorPosition):
            if incarnation is not None or checkpoint is not None:
                position = CursorPosition(
                    seq=position.seq,
                    incarnation=(
                        incarnation
                        if incarnation is not None
                        else position.incarnation
                    ),
                    checkpoint=(
                        checkpoint if checkpoint is not None else position.checkpoint
                    ),
                    byte_offset=position.byte_offset,
                )
        else:
            position = CursorPosition(
                seq=int(position),
                incarnation=incarnation,
                checkpoint=checkpoint,
            )
        self._reopen(from_start=True)
        if self._fh is None:
            return
        has_proof = position.incarnation is not None and position.checkpoint is not None
        # Partial proofs (exactly one of incarnation/checkpoint) and explicit
        # legacy resume cannot safely skip: a shorter replacement would be lost.
        partial_proof = (position.incarnation is None) != (position.checkpoint is None)
        if conservative_legacy or partial_proof:
            return
        if not has_proof:
            # Bare sequence without proof — used for recent-tail windows.
            # External client cursors must pass conservative_legacy via the
            # follower so they never take this path (B131).
            self._skip_lines(position.seq)
            return
        if position.byte_offset is not None and self._seek_trusted(position):
            return
        self._skip_lines(position.seq)
        actual = CursorPosition(
            seq=self.seq,
            incarnation=self._incarnation(self._ident, self._prefix_identity),
            checkpoint=self._checkpoint.hex(),
            byte_offset=self._byte_offset,
        )
        if (
            actual.seq != position.seq
            or actual.incarnation != position.incarnation
            or actual.checkpoint != position.checkpoint
            or (
                position.byte_offset is not None
                and actual.byte_offset != position.byte_offset
            )
        ):
            self._reopen(from_start=True)
            return
        self._remember(actual)

    def start_at_end(self) -> None:
        self._reopen(from_start=False)

    def snapshot_at_end(self) -> list[FeedRecord]:
        """Capture complete records and stay subscribed at that boundary.

        The snapshot boundary is the size of the opened file descriptor at
        subscription time, not a later path lookup. Bytes appended after that
        boundary remain unread on the same descriptor and are returned by
        :meth:`poll`. If the path is rotated, ``poll`` drains any unread
        bytes on the old descriptor before opening the new incarnation.
        """
        self.close()
        self._reset()
        try:
            self._fh = self.path.open("rb")
        except OSError:
            return []
        try:
            stat = os.fstat(self._fh.fileno())
            self._ident = (stat.st_dev, stat.st_ino)
            boundary = stat.st_size
            self._size_seen = boundary
            self._mtime_ns = stat.st_mtime_ns
            self._ctime_ns = stat.st_ctime_ns
            self._prefix_identity = self._prefix_identity_from_fd(self._fh.fileno())
        except OSError:
            self.close()
            return []

        remaining = boundary
        data = bytearray()
        while remaining > 0:
            chunk = self._fh.read(remaining)
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        self._buf = bytes(data)
        return list(self._emit_complete_lines())

    def _record_from_raw(self, raw_line: bytes) -> FeedRecord | None:
        self._consume_line(raw_line)
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        if self.transform is not None:
            obj = self.transform(obj)
        position = self.cursor_position
        return FeedRecord(
            self.source,
            self.cursor_key,
            self.seq,
            obj,
            incarnation=position.incarnation,
            checkpoint=position.checkpoint,
            byte_offset=position.byte_offset,
        )

    def _emit_complete_lines(self) -> Iterator[FeedRecord]:
        """Yield records for complete lines currently in ``_buf``."""
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                return
            raw_line, self._buf = self._buf[:nl], self._buf[nl + 1 :]
            record = self._record_from_raw(raw_line)
            if record is not None:
                yield record

    def _drain_handle(self, *, authenticate: bool = False) -> Iterator[FeedRecord]:
        """Read remaining bytes from the open descriptor and emit complete lines.

        When ``authenticate`` is true, each staged read is checked against the
        path so an in-place rewrite between the entry snapshot and the read
        cannot leak a suffix from a replacement. Generator return value is
        ``True`` when the caller must reopen-from-start and retry.
        """
        if self._fh is None:
            return False
        while True:
            yield from self._emit_complete_lines()
            try:
                raw_line = self._read_complete_line()
                fd_stat = os.fstat(self._fh.fileno())
            except (OSError, ValueError):
                self.close()
                return False

            if authenticate:
                staged = (
                    raw_line + b"\n" if raw_line is not None else self._buf
                )
                staged_snapshot = self._path_snapshot(
                    byte_offset=self._byte_offset,
                    buffer=staged,
                )
                if staged_snapshot is None:
                    # Observation raced a rewrite. A complete line already left
                    # the buffer — prefer replay. A partial/EOF can retry later.
                    if raw_line is not None:
                        return True
                    return False

                fd_ident = (fd_stat.st_dev, fd_stat.st_ino)
                if (
                    fd_ident != self._ident
                    or staged_snapshot[0] != fd_ident
                    or staged_snapshot[4] != self._prefix_identity
                    or not staged_snapshot[5]
                ):
                    return True

                self._size_seen = staged_snapshot[1]
                self._mtime_ns = staged_snapshot[2]
                self._ctime_ns = staged_snapshot[3]

            if raw_line is None:
                return False
            record = self._record_from_raw(raw_line)
            if record is not None:
                yield record

    def poll(self) -> Iterator[FeedRecord]:
        """Yield complete new records since the last poll."""
        while True:
            snapshot = self._path_snapshot()
            if self._fh is None:
                if snapshot is None:
                    return
                self._reopen(from_start=True)
                if self._fh is None:
                    return
                authenticate = True
            elif snapshot is not None and snapshot[0] != self._ident:
                # True rotation: drain unread bytes on the subscribed descriptor
                # so a pre-rotation append is not lost (B144), then reopen.
                yield from self._drain_handle(authenticate=False)
                self._reopen(from_start=True)
                if self._fh is None:
                    return
                authenticate = True
            elif snapshot is not None and (
                snapshot[4] != self._prefix_identity or not snapshot[5]
            ):
                # Same inode rewrite: first-record identity changed, or the
                # already-consumed prefix no longer matches (equal/larger
                # same-prefix replacement). Prefer replay over silent loss.
                self._reopen(from_start=True)
                if self._fh is None:
                    return
                authenticate = True
            else:
                if snapshot is None:
                    # Path disappeared; open descriptor still durable for prior writes.
                    yield from self._drain_handle(authenticate=False)
                    return
                size = snapshot[1]
                if size < self._size_seen:
                    self._reopen(from_start=True)
                    if self._fh is None:
                        return
                    authenticate = True
                else:
                    self._size_seen = size
                    self._mtime_ns = snapshot[2]
                    self._ctime_ns = snapshot[3]
                    authenticate = True

            restart = yield from self._drain_handle(authenticate=authenticate)
            if restart:
                self._reopen(from_start=True)
                if self._fh is None:
                    return
                continue
            return


    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None

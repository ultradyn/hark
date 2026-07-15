"""Composite cursor tokens for multi-source resume (dashboard SSE compatible).

Format: ``key:seq@incarnation~checkpoint,…``. ``incarnation`` is an opaque
file identity and ``checkpoint`` proves the complete-line prefix through
``seq``. Both are optional only for legacy tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping


_CURSOR_KEY = re.compile(r"[a-z][a-z0-9_-]*")
_LEGACY_INCARNATION = re.compile(r"[A-Za-z0-9._-]+")
_OPAQUE_PROOF = re.compile(r"[a-f0-9]{32}")
_ASCII_SEQUENCE = re.compile(r"[0-9]{1,19}")
_MAX_SEQUENCE = 10**19 - 1


@dataclass(frozen=True)
class CursorPosition:
    """One source's resumable position.

    ``incarnation`` is an opaque identity for the opened backing file.
    ``checkpoint`` authenticates the complete-line prefix through ``seq``.
    Legacy cursors omit one or both values and must be resumed conservatively.
    """

    seq: int
    incarnation: str | None = None
    checkpoint: str | None = None


@dataclass(frozen=True)
class InvalidCursorPosition:
    """A known cursor key whose sequence token is invalid and must replay."""

    reason: str = "invalid_sequence"


ParsedCursorPosition = CursorPosition | InvalidCursorPosition


def parse_cursor_positions(cursor: str | None) -> dict[str, ParsedCursorPosition]:
    """Parse positions while retaining invalid known keys for safe replay."""
    out: dict[str, ParsedCursorPosition] = {}
    if not cursor:
        return out
    for part in cursor.split(","):
        key, separator, raw_position = part.strip().partition(":")
        if not key or not separator:
            continue
        raw_seq, has_incarnation, raw_proof = raw_position.partition("@")
        if _ASCII_SEQUENCE.fullmatch(raw_seq) is None:
            out[key] = InvalidCursorPosition()
            continue
        incarnation, has_checkpoint, checkpoint = raw_proof.partition("~")
        parsed_incarnation = incarnation if has_incarnation else None
        parsed_checkpoint = checkpoint if has_checkpoint else None
        if parsed_checkpoint is not None:
            valid_proof = (
                parsed_incarnation is not None
                and _OPAQUE_PROOF.fullmatch(parsed_incarnation) is not None
                and _OPAQUE_PROOF.fullmatch(parsed_checkpoint) is not None
            )
            if not valid_proof:
                parsed_incarnation = None
                parsed_checkpoint = None
        elif parsed_incarnation and not _LEGACY_INCARNATION.fullmatch(
            parsed_incarnation
        ):
            # Retain the sequence as an untrusted legacy position so resume
            # chooses conservative replay instead of silently skipping data.
            parsed_incarnation = None
        out[key] = CursorPosition(
            seq=int(raw_seq),
            incarnation=parsed_incarnation,
            checkpoint=parsed_checkpoint,
        )
    return out


def parse_cursor(cursor: str | None) -> dict[str, int]:
    """Return only valid sequence positions for lenient compatibility."""
    return {
        key: position.seq
        for key, position in parse_cursor_positions(cursor).items()
        if isinstance(position, CursorPosition)
    }


def format_cursor(
    positions: Mapping[str, int | CursorPosition]
    | Iterable[tuple[str, int | CursorPosition]],
) -> str:
    """Build a composite cursor; preserves iteration order of *positions*."""
    if isinstance(positions, Mapping):
        items = positions.items()
    else:
        items = positions
    return ",".join(_format_part(key, position) for key, position in items)


def _format_part(key: str, position: int | CursorPosition) -> str:
    if not isinstance(key, str) or _CURSOR_KEY.fullmatch(key) is None:
        raise ValueError("cursor key must match [a-z][a-z0-9_-]*")
    if not isinstance(position, CursorPosition):
        if not isinstance(position, int) or isinstance(position, bool):
            raise TypeError("cursor sequence must be an integer")
        position = CursorPosition(seq=position)
    if not isinstance(position.seq, int) or isinstance(position.seq, bool):
        raise TypeError("cursor sequence must be an integer")
    if position.seq < 0 or position.seq > _MAX_SEQUENCE:
        raise ValueError("cursor sequence must be between 0 and 9999999999999999999")

    incarnation = position.incarnation
    checkpoint = position.checkpoint
    if incarnation is None:
        if checkpoint is not None:
            raise ValueError("cursor checkpoint requires an incarnation")
        suffix = ""
    elif checkpoint is None:
        if (
            not isinstance(incarnation, str)
            or _LEGACY_INCARNATION.fullmatch(incarnation) is None
        ):
            raise ValueError("legacy cursor incarnation contains invalid characters")
        suffix = f"@{incarnation}"
    else:
        if (
            not isinstance(incarnation, str)
            or not isinstance(checkpoint, str)
            or _OPAQUE_PROOF.fullmatch(incarnation) is None
            or _OPAQUE_PROOF.fullmatch(checkpoint) is None
        ):
            raise ValueError(
                "cursor proof must be two 32-character lowercase hex values"
            )
        suffix = f"@{incarnation}~{checkpoint}"
    return f"{key}:{int(position.seq)}{suffix}"

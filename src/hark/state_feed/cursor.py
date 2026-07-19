"""Composite cursor tokens for multi-source resume (dashboard SSE compatible).

Canonical proved positions use
``key:seq@incarnation~checkpoint[~byte_offset]``.
``incarnation`` is an opaque file identity and ``checkpoint`` proves the
complete-line prefix through ``seq``. Optional ``byte_offset`` lets a trusted
in-process follower jump without re-scanning. Legacy sequence-only or
incarnation-only tokens remain accepted and must be resumed conservatively.
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
    ``byte_offset`` is an optional trusted in-process shortcut.
    Legacy cursors omit one or more proofs and must be resumed conservatively.
    """

    seq: int
    incarnation: str | None = None
    checkpoint: str | None = None
    byte_offset: int | None = None


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
        incarnation: str | None = None
        checkpoint: str | None = None
        byte_offset: int | None = None
        if has_incarnation:
            # Proved form: incarnation~checkpoint[~byte_offset]
            # Legacy form: incarnation only (no '~').
            if "~" in raw_proof:
                incarnation, _, rest = raw_proof.partition("~")
                if "~" in rest:
                    checkpoint, _, raw_offset = rest.partition("~")
                    if _ASCII_SEQUENCE.fullmatch(raw_offset) is None:
                        incarnation = None
                        checkpoint = None
                    else:
                        byte_offset = int(raw_offset)
                else:
                    checkpoint = rest
                valid_proof = (
                    incarnation is not None
                    and checkpoint is not None
                    and _OPAQUE_PROOF.fullmatch(incarnation) is not None
                    and _OPAQUE_PROOF.fullmatch(checkpoint) is not None
                )
                if not valid_proof:
                    incarnation = None
                    checkpoint = None
                    byte_offset = None
            else:
                incarnation = raw_proof if raw_proof else None
                if incarnation and not _LEGACY_INCARNATION.fullmatch(incarnation):
                    # Retain the sequence as an untrusted legacy position so
                    # resume chooses conservative replay instead of skipping.
                    incarnation = None
        out[key] = CursorPosition(
            seq=int(raw_seq),
            incarnation=incarnation,
            checkpoint=checkpoint,
            byte_offset=byte_offset,
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
    if position.byte_offset is not None:
        if (
            not isinstance(position.byte_offset, int)
            or isinstance(position.byte_offset, bool)
            or position.byte_offset < 0
            or position.byte_offset > _MAX_SEQUENCE
        ):
            raise ValueError("cursor byte offset outside supported range")

    incarnation = position.incarnation
    checkpoint = position.checkpoint
    if incarnation is None:
        if checkpoint is not None:
            raise ValueError("cursor checkpoint requires an incarnation")
        if position.byte_offset is not None:
            raise ValueError("cursor byte offset requires proof")
        suffix = ""
    elif checkpoint is None:
        if position.byte_offset is not None:
            raise ValueError("cursor byte offset requires proof")
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
        if position.byte_offset is not None:
            suffix += f"~{position.byte_offset}"
    return f"{key}:{int(position.seq)}{suffix}"


def canonicalize_cursor(cursor: str) -> str:
    """Strictly validate external cursor text before reflecting it into SSE.

    Accepts the same grammar as a well-formed composite token: one or more
    ``key:seq[@proof…]`` parts with unique keys. Invalid sequences and
    non-canonical keys are rejected rather than reflected into SSE ``id:``.
    """
    if not cursor:
        raise ValueError("cursor must not be empty")
    positions: list[tuple[str, CursorPosition]] = []
    seen: set[str] = set()
    for part in cursor.split(","):
        if not part:
            raise ValueError("invalid cursor grammar")
        key, separator, raw_position = part.partition(":")
        if not separator or _CURSOR_KEY.fullmatch(key) is None:
            raise ValueError("invalid cursor grammar")
        if key in seen:
            raise ValueError("duplicate cursor key")
        seen.add(key)
        raw_seq, has_incarnation, raw_proof = raw_position.partition("@")
        if _ASCII_SEQUENCE.fullmatch(raw_seq) is None:
            raise ValueError("invalid cursor sequence")
        incarnation: str | None = None
        checkpoint: str | None = None
        byte_offset: int | None = None
        if has_incarnation:
            if "~" in raw_proof:
                incarnation, _, rest = raw_proof.partition("~")
                if "~" in rest:
                    checkpoint, _, raw_offset = rest.partition("~")
                    if _ASCII_SEQUENCE.fullmatch(raw_offset) is None:
                        raise ValueError("invalid cursor byte offset")
                    byte_offset = int(raw_offset)
                else:
                    checkpoint = rest
                if (
                    _OPAQUE_PROOF.fullmatch(incarnation or "") is None
                    or _OPAQUE_PROOF.fullmatch(checkpoint or "") is None
                ):
                    raise ValueError("invalid cursor proof")
            else:
                if not raw_proof or _LEGACY_INCARNATION.fullmatch(raw_proof) is None:
                    raise ValueError("invalid cursor incarnation")
                incarnation = raw_proof
        positions.append(
            (
                key,
                CursorPosition(
                    seq=int(raw_seq),
                    incarnation=incarnation,
                    checkpoint=checkpoint,
                    byte_offset=byte_offset,
                ),
            )
        )
    return format_cursor(positions)

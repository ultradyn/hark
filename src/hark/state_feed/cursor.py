"""Composite cursor tokens for multi-source resume (dashboard SSE compatible).

Format: ``key:seq,key:seq,…`` e.g. ``watch:184,ambient:42,bound:12``.
"""

from __future__ import annotations

from typing import Iterable, Mapping


def parse_cursor(cursor: str | None) -> dict[str, int]:
    """``"watch:12,system:9"`` → ``{"watch": 12, "system": 9}`` (lenient)."""
    out: dict[str, int] = {}
    if not cursor:
        return out
    for part in cursor.split(","):
        key, _, num = part.strip().partition(":")
        if key and num.isdigit():
            out[key] = int(num)
    return out


def format_cursor(
    positions: Mapping[str, int] | Iterable[tuple[str, int]],
) -> str:
    """Build a composite cursor; preserves iteration order of *positions*."""
    if isinstance(positions, Mapping):
        items = positions.items()
    else:
        items = positions
    return ",".join(f"{key}:{int(seq)}" for key, seq in items)

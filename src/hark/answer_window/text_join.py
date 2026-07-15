"""Pure radio STT text assembly helpers (no I/O, no provider types).

Used by :class:`~hark.answer_window.radio.RadioSession` and re-exported from
``hark.speech`` for back-compat with existing tests/callers.
"""

from __future__ import annotations


def join_radio_stt_segments(segments: list[str]) -> str:
    """Join per-segment STT without cumulative re-STT (avoids long-audio word loss).

    Each radio segment is transcribed alone; we assemble text with light overlap
    trim so repeated phrase tails do not double. Empty segments are skipped so a
    failed mid-slice STT does not erase prior text.
    """
    out: list[str] = []
    for raw in segments:
        part = " ".join((raw or "").split()).strip()
        if not part:
            continue
        if not out:
            out.append(part)
            continue
        prev = out[-1]
        # If new starts with end of previous (common STT re-prefix), drop overlap
        prev_toks = prev.split()
        part_toks = part.split()
        max_olap = min(len(prev_toks), len(part_toks), 8)
        olap = 0
        for n in range(max_olap, 0, -1):
            if prev_toks[-n:] == part_toks[:n]:
                olap = n
                break
        if olap:
            part_toks = part_toks[olap:]
        if part_toks:
            out.append(" ".join(part_toks))
    return " ".join(out).strip()


def prefer_complete_transcript(a: str, b: str) -> str:
    """Pick the more complete of two transcripts without inventing words.

    Used so a full-audio re-STT cannot *replace* a longer joined partial body
    with a shorter rewrite (the original word-loss symptom).
    """
    aa = " ".join((a or "").split()).strip()
    bb = " ".join((b or "").split()).strip()
    if not aa:
        return bb
    if not bb:
        return aa
    if aa == bb:
        return aa
    # One properly extends the other
    if bb.startswith(aa) or aa in bb:
        return bb
    if aa.startswith(bb) or bb in aa:
        return aa
    # Prefer more tokens (conservative: do not merge incompatible rewrites)
    if len(bb.split()) > len(aa.split()):
        return bb
    return aa


def monotonic_partial_text(prev: str, candidate: str) -> str:
    """Never shrink the published partial body across radio slices."""
    p = " ".join((prev or "").split()).strip()
    c = " ".join((candidate or "").split()).strip()
    if not p:
        return c
    if not c:
        return p
    return prefer_complete_transcript(p, c)

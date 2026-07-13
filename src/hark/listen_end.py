"""Listen endpoint modes: silence/Smart Turn vs radio-style end phrases.

Control phrases MUST be product-scoped by default so ordinary technical speech
does not abort or finalize a capture. Operators may add casual phrases if they
accept the false-trigger risk.

Optional **soft end phrases** (default OFF) add a conservative local heuristic
for informal closers ("that's all", "okay send it", …) without requiring the
Mode A agent to call ``hark listen-end``. Soft matches are **utterance-final
only** (phrase must end the transcript after normalize) and only evaluated after
a radio segment boundary (trailing silence). See docs/AUDIO_DESIGN.md.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class EndMode(str, Enum):
    SILENCE = "silence"  # end on silence / Smart Turn
    RADIO = "radio"  # keep listening until end phrase


# Product-scoped only — no "cancel that" / "send it" / bare "over".
DEFAULT_END_PHRASES: tuple[str, ...] = (
    "okay hark send",
    "ok hark send",
    "hark send it",
    "hark send",
    "end prompt",
    "end of prompt",
    "hark over",
)

DEFAULT_CANCEL_PHRASES: tuple[str, ...] = (
    "hark cancel",
    "cancel hark",
    "abort hark send",
    "hark abort",
)

# ---------------------------------------------------------------------------
# Soft end phrases (optional; default disabled)
#
# SAFE (included): multi-word informal closers that are unlikely mid-clause
# endings in technical dictation when required to be **transcript-terminal**.
#
# UNSAFE (not included — do not add without strong justification):
#   - "send it"           → matches "please just send it"
#   - bare "over"         → "turn it over", "hand over"
#   - "done" / "i'm done" → mid-thought pauses ("I'm done with the first part")
#   - "that's it"         → "that's it for the migration" after a pause
#   - "finished" / "go"   → too common mid-speech
#   - "cancel that"       → cancel semantics, not end
#
# Matching rules when soft_end_phrases_enabled:
#   1. Same terminal word-boundary match as product end phrases
#   2. Only runs after radio segment silence (caller evaluates post-capture)
#   3. Cancel and product end phrases always take priority
# ---------------------------------------------------------------------------
DEFAULT_SOFT_END_PHRASES: tuple[str, ...] = (
    "that's all",
    "that is all",
    "thats all",  # STT often drops the apostrophe
    "end of message",
    "end message",
    "end of transmission",
    "okay send it",
    "ok send it",
    "okay send",
    "ok send",
    "over and out",
)


_PUNCT_TRAIL = re.compile(r"[\s\.\!\?\,\;\:…]+$", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u2019", "'").replace("`", "'")
    text = text.lower().strip()
    text = _WS.sub(" ", text)
    return text


def _strip_trail_punct(text: str) -> str:
    return _PUNCT_TRAIL.sub("", text).strip()


@dataclass(frozen=True)
class PhraseHit:
    kind: str  # "end" | "cancel"
    phrase: str
    body: str
    raw: str


def _ends_with_phrase(normalized: str, phrase: str) -> bool:
    """True if phrase is the whole text or a word-bounded suffix (utterance-final)."""
    p = normalize_for_match(phrase)
    if not p or not normalized:
        return False
    if normalized == p:
        return True
    if not normalized.endswith(p):
        return False
    before = len(normalized) - len(p)
    if before == 0:
        return True
    # Word boundary: char immediately before phrase must be whitespace
    # (after trail-punct strip, mid-clause "that's all I know" does not match).
    return normalized[before - 1].isspace()


def find_terminal_phrase(
    text: str,
    phrases: list[str] | tuple[str, ...],
    *,
    kind: str,
) -> PhraseHit | None:
    """Match a phrase only when it is utterance-final (suffix / whole text).

    Mid-thought speech such as "that's all I know about X" does **not** match
    soft or hard end phrases — the phrase must terminate the normalized text.
    """
    raw = text or ""
    norm = normalize_for_match(raw)
    norm = _strip_trail_punct(norm)
    if not norm:
        return None

    ordered = sorted(
        (normalize_for_match(p) for p in phrases if p and str(p).strip()),
        key=len,
        reverse=True,
    )
    seen: set[str] = set()
    for p in ordered:
        if not p or p in seen:
            continue
        seen.add(p)
        if _ends_with_phrase(norm, p):
            body_norm = norm[: len(norm) - len(p)].rstrip()
            body_norm = _strip_trail_punct(body_norm)
            return PhraseHit(kind=kind, phrase=p, body=body_norm, raw=raw)
    return None


def evaluate_radio_transcript(
    text: str,
    *,
    end_phrases: list[str] | tuple[str, ...] = DEFAULT_END_PHRASES,
    cancel_phrases: list[str] | tuple[str, ...] = DEFAULT_CANCEL_PHRASES,
    soft_end_phrases: list[str] | tuple[str, ...] = DEFAULT_SOFT_END_PHRASES,
    soft_end_phrases_enabled: bool = False,
) -> PhraseHit | None:
    """Evaluate radio-mode transcript for cancel / end / optional soft end.

    Priority: cancel → product end phrases → soft end (if enabled).
    Soft end is default OFF; when on, only **terminal** soft phrases match.
    """
    cancel = find_terminal_phrase(text, cancel_phrases, kind="cancel")
    if cancel is not None:
        return cancel
    end = find_terminal_phrase(text, end_phrases, kind="end")
    if end is not None:
        return end
    if soft_end_phrases_enabled:
        soft = find_terminal_phrase(text, soft_end_phrases, kind="end")
        if soft is not None:
            return soft
    return None


def parse_end_mode(value: str | None, default: EndMode = EndMode.SILENCE) -> EndMode:
    if value is None or str(value).strip() == "":
        return default
    v = str(value).strip().lower()
    if v in ("silence", "smart_turn", "smart-turn", "vad"):
        return EndMode.SILENCE
    if v == "auto":
        return default
    if v in ("radio", "prosign", "phrase", "end_phrase", "end-phrase"):
        return EndMode.RADIO
    raise ValueError(
        f"unknown listen end_mode {value!r}; use 'silence' or 'radio'"
    )


def should_keep_listening(
    end_mode: EndMode | str,
    text: str,
    *,
    end_phrases: list[str] | tuple[str, ...] = DEFAULT_END_PHRASES,
    cancel_phrases: list[str] | tuple[str, ...] = DEFAULT_CANCEL_PHRASES,
    soft_end_phrases: list[str] | tuple[str, ...] = DEFAULT_SOFT_END_PHRASES,
    soft_end_phrases_enabled: bool = False,
    silence_would_end: bool = False,
) -> tuple[bool, PhraseHit | None]:
    mode = end_mode if isinstance(end_mode, EndMode) else parse_end_mode(str(end_mode))
    if mode is EndMode.SILENCE:
        if silence_would_end:
            return False, None
        return True, None

    hit = evaluate_radio_transcript(
        text,
        end_phrases=end_phrases,
        cancel_phrases=cancel_phrases,
        soft_end_phrases=soft_end_phrases,
        soft_end_phrases_enabled=soft_end_phrases_enabled,
    )
    if hit is None:
        return True, None
    return False, hit

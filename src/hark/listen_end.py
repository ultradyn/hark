"""Listen endpoint modes: silence/Smart Turn vs radio-style end phrases.

Radio mode (like saying "over" on the air): keep the mic open through long
thinking pauses until the operator speaks an explicit end phrase such as
"okay send it" or "end prompt". The phrase is stripped before delivery.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class EndMode(str, Enum):
    SILENCE = "silence"  # end on silence / Smart Turn
    RADIO = "radio"  # keep listening until end phrase


DEFAULT_END_PHRASES: tuple[str, ...] = (
    "okay send it",
    "ok send it",
    "okay, send it",
    "send it",
    "end prompt",
    "end of prompt",
    "end of message",
    "over",
)

DEFAULT_CANCEL_PHRASES: tuple[str, ...] = (
    "cancel that",
    "never mind",
    "nevermind",
    "scratch that",
    "abort send",
)


_PUNCT_TRAIL = re.compile(r"[\s\.\!\?\,\;\:…]+$", re.UNICODE)
_WS = re.compile(r"\s+")
# strip common trailing fillers before phrase match
_FILLER_TRAIL = re.compile(
    r"[\s]*(?:uh|um|erm|please)?[\s\.\!\?\,\;\:…]*$", re.I | re.UNICODE
)


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
    # transcript with the terminal phrase (and trailing punct) removed
    body: str
    # full original input (for logging; never contains secrets by itself)
    raw: str


def _ends_with_phrase(normalized: str, phrase: str) -> bool:
    """True if normalized text ends with phrase at a word boundary."""
    p = normalize_for_match(phrase)
    if not p or not normalized:
        return False
    # allow optional trailing punctuation already stripped on both
    if normalized == p:
        return True
    if not normalized.endswith(p):
        return False
    # word boundary: char before phrase must be whitespace or start
    before = len(normalized) - len(p)
    if before == 0:
        return True
    return normalized[before - 1].isspace()


def find_terminal_phrase(
    text: str,
    phrases: list[str] | tuple[str, ...],
    *,
    kind: str,
) -> PhraseHit | None:
    """If text ends with any phrase (longest first), return a hit with body stripped."""
    raw = text or ""
    norm = normalize_for_match(raw)
    norm = _strip_trail_punct(norm)
    if not norm:
        return None

    # Prefer longest phrase to avoid "send it" beating "okay send it"
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
            # Map body back approximately: use normalized body for delivery
            # (STT is already approximate; operators prefer clean text)
            return PhraseHit(kind=kind, phrase=p, body=body_norm, raw=raw)
    return None


def evaluate_radio_transcript(
    text: str,
    *,
    end_phrases: list[str] | tuple[str, ...] = DEFAULT_END_PHRASES,
    cancel_phrases: list[str] | tuple[str, ...] = DEFAULT_CANCEL_PHRASES,
) -> PhraseHit | None:
    """Check cancel first, then end. None = keep listening."""
    cancel = find_terminal_phrase(text, cancel_phrases, kind="cancel")
    if cancel is not None:
        return cancel
    return find_terminal_phrase(text, end_phrases, kind="end")


def parse_end_mode(value: str | None, default: EndMode = EndMode.SILENCE) -> EndMode:
    if value is None or str(value).strip() == "":
        return default
    v = str(value).strip().lower()
    if v in ("silence", "smart_turn", "smart-turn", "vad", "auto"):
        # auto maps to silence for endpoint policy (not provider auto)
        if v == "auto":
            return default
        return EndMode.SILENCE
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
    silence_would_end: bool = False,
) -> tuple[bool, PhraseHit | None]:
    """Return (keep_listening, hit).

    In silence mode, keep_listening is False when silence_would_end (caller
    supplies Smart Turn / end-silence decision).

    In radio mode, ignore silence_would_end; only end/cancel phrases finish.
    """
    mode = end_mode if isinstance(end_mode, EndMode) else parse_end_mode(str(end_mode))
    if mode is EndMode.SILENCE:
        if silence_would_end:
            return False, None
        return True, None

    # radio
    hit = evaluate_radio_transcript(
        text, end_phrases=end_phrases, cancel_phrases=cancel_phrases
    )
    if hit is None:
        return True, None
    return False, hit

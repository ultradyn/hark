"""Listen endpoint modes: silence/Smart Turn vs radio-style end phrases.

Control phrases MUST be product-scoped by default so ordinary technical speech
does not abort or finalize a capture. Operators may add casual phrases if they
accept the false-trigger risk.

Optional **soft end phrases** (default **ON** for radio dogfood) add a
conservative local heuristic for informal closers ("send it", "that's all",
utterance-final "over", …) without requiring the Mode A agent to call
``hark listen-end``. Soft matches are **utterance-final only** (phrase must end
the transcript after normalize) and only evaluated after a radio segment
boundary (trailing silence). Bare ``over`` additionally requires a
**sentence-final** boundary (sole utterance or after ``.`` / ``!`` / ``?`` /
``,``) so mid-clause "turn it over" / "over the weekend" never finishes.
Multi-word ``okay over`` / ``ok over`` cover STT that drops the comma in
"okay, over". Mode A **must** also call ``hark listen-end`` on partials when
the operator clearly finished. See docs/AUDIO_DESIGN.md.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class EndMode(str, Enum):
    SILENCE = "silence"  # end on silence / Smart Turn
    RADIO = "radio"  # keep listening until end phrase


# Product-scoped only — no "cancel that" / bare mid-clause "over".
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
# Soft end phrases (optional master switch; default ON for radio dogfood)
#
# SAFE (included): informal closers that are unlikely mid-clause endings in
# technical dictation when required to be **transcript-terminal**.
#
# Bare "over" is special: it is listed here but matched only with an extra
# **sentence-final** rule (see SENTENCE_FINAL_SOFT_PHRASES) so "turn it over"
# and "over the weekend" never finalize.
#
# UNSAFE (not included — do not add without strong justification):
#   - "done" / "i'm done" → mid-thought pauses ("I'm done with the first part")
#   - "that's it"         → "that's it for the migration" after a pause
#   - "finished" / "go"   → too common mid-speech
#   - "cancel that"       → cancel semantics, not end
#
# Matching rules when soft_end_phrases_enabled:
#   1. Same terminal word-boundary match as product end phrases
#   2. Phrases in SENTENCE_FINAL_SOFT_PHRASES also need a sentence boundary
#      (or sole-utterance) — see _is_sentence_final_suffix
#   3. Only runs after radio segment silence (caller evaluates post-capture)
#   4. Cancel and product end phrases always take priority
# ---------------------------------------------------------------------------
DEFAULT_SOFT_END_PHRASES: tuple[str, ...] = (
    "that's all",
    "that is all",
    "thats all",  # STT often drops the apostrophe
    "end of message",
    "end message",
    "end of transmission",
    "message done",  # informal closer (Mode A backup also catches this)
    "okay send it",
    "ok send it",
    "okay send",
    "ok send",
    "send it",
    "send that",
    "over and out",
    # STT often drops the comma in "okay, over" → bare "okay over" / "ok over"
    "okay over",
    "ok over",
    "over",  # sentence-final only (see SENTENCE_FINAL_SOFT_PHRASES)
)

# Soft phrases that must not fire on mere word-final use (e.g. "turn it over").
# Require sole utterance or sentence-ending punctuation immediately before the
# phrase (after normalize + trail-punct strip keeps internal ". " boundaries).
SENTENCE_FINAL_SOFT_PHRASES: frozenset[str] = frozenset({"over"})

# Soft end master switch default (B039 dogfood: bare "send it" / ". over." work).
DEFAULT_SOFT_END_PHRASES_ENABLED: bool = True


_PUNCT_TRAIL = re.compile(r"[\s\.\!\?\,\;\:…]+$", re.UNICODE)
_WS = re.compile(r"\s+")
# Sentence-ending punct at end of prefix before a sentence-final soft phrase.
# Comma counts as a soft sentence boundary so "okay, over" / "ready, over"
# finalize while mid-clause "turn it over" / "this is over" still do not.
_SENTENCE_END = re.compile(r"[.!?…;:,]+$")


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


def _is_sentence_final_suffix(normalized: str, phrase: str) -> bool:
    """Utterance-final phrase that is also sentence-final (or sole utterance).

    Used for bare ``over`` so technical "turn it over" / "hand it over" does
    not finalize, while "... implement. over." does.
    """
    p = normalize_for_match(phrase)
    if not p or not _ends_with_phrase(normalized, p):
        return False
    if normalized == p:
        return True
    before = len(normalized) - len(p)
    prefix = normalized[:before].rstrip()
    if not prefix:
        return True
    return _SENTENCE_END.search(prefix) is not None


def find_terminal_phrase(
    text: str,
    phrases: list[str] | tuple[str, ...],
    *,
    kind: str,
    sentence_final_phrases: frozenset[str] | set[str] | None = None,
) -> PhraseHit | None:
    """Match a phrase only when it is utterance-final (suffix / whole text).

    Mid-thought speech such as "that's all I know about X" does **not** match
    soft or hard end phrases — the phrase must terminate the normalized text.

    Phrases listed in *sentence_final_phrases* (normalized form) additionally
    require a sentence boundary before the phrase (or sole-utterance match).
    """
    raw = text or ""
    norm = normalize_for_match(raw)
    norm = _strip_trail_punct(norm)
    if not norm:
        return None

    sf = {
        normalize_for_match(p)
        for p in (sentence_final_phrases or ())
        if p and str(p).strip()
    }

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
        if p in sf:
            if not _is_sentence_final_suffix(norm, p):
                continue
        elif not _ends_with_phrase(norm, p):
            continue
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
    soft_end_phrases_enabled: bool = DEFAULT_SOFT_END_PHRASES_ENABLED,
    sentence_final_soft_phrases: frozenset[str]
    | set[str]
    | None = SENTENCE_FINAL_SOFT_PHRASES,
) -> PhraseHit | None:
    """Evaluate radio-mode transcript for cancel / end / optional soft end.

    Priority: cancel → product end phrases → soft end (if enabled).
    Soft end default ON (B039); when on, only **terminal** soft phrases match.
    Bare ``over`` (when present in soft list) is **sentence-final** only.
    """
    cancel = find_terminal_phrase(text, cancel_phrases, kind="cancel")
    if cancel is not None:
        return cancel
    end = find_terminal_phrase(text, end_phrases, kind="end")
    if end is not None:
        return end
    if soft_end_phrases_enabled:
        soft = find_terminal_phrase(
            text,
            soft_end_phrases,
            kind="end",
            sentence_final_phrases=sentence_final_soft_phrases,
        )
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
    soft_end_phrases_enabled: bool = DEFAULT_SOFT_END_PHRASES_ENABLED,
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

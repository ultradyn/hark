"""Spoken confirmation / cancel lexicon for ask --confirm."""

from __future__ import annotations

import re

from hark.listen_end import normalize_for_match

_PUNCTUATION = re.compile(r"[^\w\s']+", re.UNICODE)
_ADJOINING_PUNCTUATION_SEPARATOR = re.compile(r"^[^\w\s']+", re.UNICODE)
_PUNCTUATED_DEFER_CUES = ("but", "wait", "if", "unless")
_AFFIRMATIVE_IDIOMS = frozenset({"yes why not"})

AFFIRM = frozenset(
    {
        "yes",
        "yeah",
        "yep",
        "yup",
        "correct",
        "confirm",
        "confirmed",
        "send",
        "do it",
        "go ahead",
        "affirmative",
        "ok",
        "okay",
        "sure",
        "right",
    }
)

NEGATE = frozenset(
    {
        "no",
        "nope",
        "not",
        "cannot",
        "can't",
        "cant",
        "won't",
        "wont",
        "cancel",
        "abort",
        "stop",
        "don't",
        "dont",
        "do not",
        "deny",
        "denied",
        "reject",
        "rejected",
        "decline",
        "declined",
        "never mind",
        "nevermind",
        "negative",
        "wrong",
        "scratch",
    }
)


def classify_confirm_reply(text: str) -> str:
    """Return 'yes' | 'no' | 'unclear'."""
    t = normalize_for_match(text)
    # Preserve the parent classifier's fail-closed behavior for punctuated
    # deferrals/conditions. Broad unpunctuated language belongs to B148.
    for affirm in sorted(AFFIRM, key=len, reverse=True):
        if not t.startswith(affirm):
            continue
        tail = t[len(affirm) :]
        separator = _ADJOINING_PUNCTUATION_SEPARATOR.match(tail)
        if separator is None:
            continue
        remainder = tail[separator.end() :].strip()
        normalized_remainder = " ".join(_PUNCTUATION.sub(" ", remainder).split())
        if any(
            normalized_remainder == cue or normalized_remainder.startswith(cue + " ")
            for cue in _PUNCTUATED_DEFER_CUES
        ):
            return "unclear"
    # STT commonly preserves sentence-final punctuation. Confirmation is a
    # small spoken lexicon, so punctuation is non-semantic while apostrophes
    # remain meaningful for negatives such as ``don't``.
    t = " ".join(_PUNCTUATION.sub(" ", t).split())
    if not t:
        return "unclear"
    if t in AFFIRM:
        return "yes"
    if t in _AFFIRMATIVE_IDIOMS:
        return "yes"
    if t in NEGATE:
        return "no"
    # A bounded negative/refusal anywhere in a longer response wins over an
    # affirmative. This is deliberately conservative for permission and
    # destructive confirmations.
    padded = f" {t} "
    for n in sorted(NEGATE, key=len, reverse=True):
        if f" {n} " in padded:
            return "no"
    for a in sorted(AFFIRM, key=len, reverse=True):
        if t == a or t.startswith(a + " ") or t.endswith(" " + a):
            return "yes"
    return "unclear"

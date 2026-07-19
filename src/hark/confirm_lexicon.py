"""Spoken confirmation / cancel lexicon for ask --confirm."""

from __future__ import annotations

import re

from hark.listen_end import normalize_for_match

_PUNCTUATION = re.compile(r"[^\w\s']+", re.UNICODE)
_AFFIRMATIVE_IDIOMS = frozenset({"yes why not"})

# Defer / condition / hedge markers. Whole-word (or multi-word phrase) match
# after punctuation strip. Any hit blocks immediate approval so multi-clause
# spoken replies like "yes but wait" / "yes if tests pass" cannot authorize R2/R3
# (B142 punctuated + B148 unpunctuated).
_DEFER_CONDITION_HEDGE = frozenset(
    {
        "but",
        "wait",
        "hold on",
        "hang on",
        "if",
        "unless",
        "after",
        "until",
        "maybe",
        "perhaps",
        "later",
    }
)

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


def _contains_whole_phrase(padded: str, phrase: str) -> bool:
    """True if ``phrase`` appears as whole words inside space-padded text."""
    return f" {phrase} " in padded


def classify_confirm_reply(text: str) -> str:
    """Return 'yes' | 'no' | 'unclear'.

    Only unambiguous immediate approval is ``yes``. Negations win over
    affirmatives; defer/condition/hedge markers fail closed to ``unclear``.
    """
    t = normalize_for_match(text)
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
        if _contains_whole_phrase(padded, n):
            return "no"
    # Defer, condition, and hedge markers block approval even when an
    # affirmative token is present at the start or end (B148).
    for cue in sorted(_DEFER_CONDITION_HEDGE, key=len, reverse=True):
        if _contains_whole_phrase(padded, cue):
            return "unclear"
    for a in sorted(AFFIRM, key=len, reverse=True):
        if t == a or t.startswith(a + " ") or t.endswith(" " + a):
            return "yes"
    return "unclear"

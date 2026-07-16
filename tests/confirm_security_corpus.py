"""Fixed security oracle for confirmation-negation tests.

This corpus is intentionally independent of the production lexicon.  Unicode
case generation may expand these phrases, but must never discover which
phrases matter by importing the implementation under test.
"""

from __future__ import annotations


NORMATIVE_NEGATION_CORPUS = frozenset(
    {
        "no",
        "nope",
        "not",
        "cannot",
        "can't",
        "won't",
        "cancel",
        "abort",
        "stop",
        "don't",
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

NORMATIVE_CONTRACTIONS = tuple(
    sorted(phrase for phrase in NORMATIVE_NEGATION_CORPUS if phrase.count("'") == 1)
)

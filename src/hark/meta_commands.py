"""Spoken meta-command lexicon for Mode A answer windows.

During an answer window (after Hark speaks a blocked agent's question and starts
listening) the operator may say a *control* phrase instead of an answer:

  - ``repeat``  — say the question again
  - ``skip``    — skip this bound event / question
  - ``next``    — move on to the next waiting agent
  - ``status``  — report the queue / what is waiting
  - ``cancel``  — abandon this answer window (do not send)

These MUST be recognised so they are honoured as meta-commands and never
delivered to the worker agent as a prompt (B009).

Matching is deliberately conservative against false positives (hijacking a real
answer):

  - Only a **whole-utterance** control phrase matches; "skip the failing test and
    continue" is a normal answer and does *not* match.
  - A ``hark`` / ``hey hark`` / ``ok hark`` prefix is an **unambiguous** escape
    hatch: "hark skip" always classifies, whatever the bare word would do.
  - Conversational bare tokens that are plausible literal answers ("again",
    "pardon") are intentionally excluded from bare matching — say "say that
    again" or "hark repeat" instead.

Residual risk: a bare single control word (e.g. "skip", "next") given as a
literal answer to an agent question would still classify. This is accepted
because such an answer is highly unlikely in an answer window; use the ``hark``
prefix when in doubt.
"""

from __future__ import annotations

import re

from hark.listen_end import normalize_for_match

# Canonical meta commands.
REPEAT = "repeat"
SKIP = "skip"
NEXT = "next"
STATUS = "status"
CANCEL = "cancel"

META_COMMANDS: tuple[str, ...] = (REPEAT, SKIP, NEXT, STATUS, CANCEL)

# Whole-utterance trigger phrases → canonical command.
# Keep phrases short and unambiguous; normalization lowercases, straightens
# apostrophes, and collapses whitespace before matching.
_PHRASES: dict[str, str] = {
    # repeat
    "repeat": REPEAT,
    "repeat that": REPEAT,
    "repeat it": REPEAT,
    "repeat the question": REPEAT,
    "say that again": REPEAT,
    "say it again": REPEAT,
    "read that again": REPEAT,
    "read it again": REPEAT,
    "come again": REPEAT,
    "one more time": REPEAT,
    "what was that": REPEAT,
    # skip
    "skip": SKIP,
    "skip it": SKIP,
    "skip this": SKIP,
    "skip that": SKIP,
    "skip this one": SKIP,
    "skip this agent": SKIP,
    "skip this question": SKIP,
    # next
    "next": NEXT,
    "next one": NEXT,
    "next agent": NEXT,
    "next please": NEXT,
    "go to the next": NEXT,
    "go to next": NEXT,
    "move on": NEXT,
    "move to the next": NEXT,
    "move to the next one": NEXT,
    # status
    "status": STATUS,
    "status report": STATUS,
    "status update": STATUS,
    "queue status": STATUS,
    "what's the status": STATUS,
    "what is the status": STATUS,
    "what's waiting": STATUS,
    "what is waiting": STATUS,
    "what's in the queue": STATUS,
    "what is in the queue": STATUS,
    "how many are waiting": STATUS,
    "how many waiting": STATUS,
    # cancel
    "cancel": CANCEL,
    "cancel that": CANCEL,
    "cancel this": CANCEL,
    "cancel it": CANCEL,
    "never mind": CANCEL,
    "nevermind": CANCEL,
    "forget it": CANCEL,
}

# Optional unambiguous control prefix ("hark skip", "hey hark next", ...).
_CONTROL_PREFIXES: tuple[str, ...] = (
    "hey hark",
    "ok hark",
    "okay hark",
    "hark",
)

_TRAIL_PUNCT = re.compile(r"[\s\.\!\?\,\;\:…]+$")
_LEAD_PUNCT = re.compile(r"^[\s\.\!\?\,\;\:…]+")


def _normalize(text: str) -> str:
    norm = normalize_for_match(text)
    norm = _TRAIL_PUNCT.sub("", norm)
    return _LEAD_PUNCT.sub("", norm).strip()


def _strip_control_prefix(norm: str) -> str | None:
    """If ``norm`` begins with a control prefix, return the remainder, else None."""
    for prefix in _CONTROL_PREFIXES:
        if norm == prefix:
            continue  # bare wake word is not itself a command
        if norm.startswith(prefix + " ") or norm.startswith(prefix + ","):
            rest = norm[len(prefix):]
            rest = _LEAD_PUNCT.sub("", rest).strip()
            if rest:
                return rest
    return None


def classify_meta_command(text: str) -> str | None:
    """Return the canonical meta-command for a transcript, or ``None``.

    A ``hark``-prefixed control phrase is authoritative. Otherwise only a
    whole-utterance control phrase matches; anything with additional substantive
    content is treated as a normal answer (returns ``None``).
    """
    norm = _normalize(text)
    if not norm:
        return None
    remainder = _strip_control_prefix(norm)
    if remainder is not None:
        return _PHRASES.get(remainder)
    return _PHRASES.get(norm)

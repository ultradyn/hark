"""Activation-phrase detection for ambient (non-answer) listening.

Policy:
  - Ambient path MUST NOT open cloud STT until an activation phrase fires.
  - Short local snippets (default ~2 s) are scanned by a small local model.
  - Full dictation after wake still uses cloud STT.
"""

from __future__ import annotations

import json
import math
import re
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from hark.listen_end import normalize_for_match

DEFAULT_ACTIVATION_PHRASES: tuple[str, ...] = (
    "hey iris",
    "hey mercury",
    "hey hark",
    "hey herald",
    "hello iris",
    "hello mercury",
    "hello hark",
    "hello herald",
    "okay iris",
    "okay mercury",
    "okay hark",
    "ok hark",
)
# Persona defaults: Iris (f) / Mercury (m) + product aliases hark/herald.
# TTS pairing (setup / docs): Iris→eve, Mercury→leo — independent of wake names.
DEFAULT_WAKE_NAMES: tuple[str, ...] = ("iris", "mercury", "hark", "herald")
DEFAULT_WAKE_MODE = "names"

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\.\!\?\,\;\:…]+")


@dataclass(frozen=True)
class WakeHit:
    phrase: str
    remainder: str
    raw: str
    confidence: float = 1.0
    backend: str = "text"


# Common small-model mishears for product wake words (seed aliases).
_HARK_ALIASES = frozenset(
    {
        "hark",
        "hook",
        "hawk",
        "heart",
        "hard",
        "hork",
        "harkh",
        "ark",
        "mark",  # sometimes
    }
)
_HERALD_ALIASES = frozenset(
    {
        "herald",
        "harold",
        "herold",
        "arrow",
        "erald",
        "herrold",
    }
)
# Built-in seed aliases keyed by canonical name (only applied when that name is active).
_SEED_NAME_ALIASES: dict[str, frozenset[str]] = {
    "hark": _HARK_ALIASES,
    "herald": _HERALD_ALIASES,
}
# High-confidence bare tokens (narrower than full seed aliases).
_BARE_SEED: dict[str, frozenset[str]] = {
    "hark": frozenset({"hark"}),
    "herald": frozenset(
        {"herald", "harold", "herold", "herrold", "erald"}
    ),
}
# Greating / attention prefixes before a product name (fuzzy path).
_PREFIXES = ("hey", "hello", "okay", "ok", "hi", "yo", "sup")
# Leading fillers vosk often prefixes on short snips (stripped for bare match).
_LEADING_FILLERS = frozenset(
    {
        "um",
        "uh",
        "erm",
        "ah",
        "oh",
        "so",
        "well",
        "like",
        "and",
        "yes",
        "yeah",
    }
)


@dataclass
class WakePolicy:
    """How ambient activation is customized and expanded.

    * **names** (default): configure product names (hark/herald/…). Matching
      is greating+name, bare name, seed+learned name aliases. Full-phrase
      extras still match exactly when listed in ``phrases``.
    * **phrases**: configure entire trigger phrases only. No name fuzzy/bare;
      learned expansions are full-phrase alternates.
    """

    mode: str = DEFAULT_WAKE_MODE  # "names" | "phrases"
    names: list[str] = field(default_factory=lambda: list(DEFAULT_WAKE_NAMES))
    prefixes: tuple[str, ...] = _PREFIXES
    # Exact full phrases (phrase-mode primary list, and/or extras in names mode)
    phrases: list[str] = field(default_factory=list)
    # alias token (lower) → canonical name (lower); includes learned
    name_aliases: dict[str, str] = field(default_factory=dict)
    # learned / extra full-phrase alternates
    phrase_aliases: list[str] = field(default_factory=list)
    learn: bool = True

    def normalized_mode(self) -> str:
        m = (self.mode or DEFAULT_WAKE_MODE).strip().lower()
        if m in ("phrase", "phrases", "full", "full_phrase", "full-phrase"):
            return "phrases"
        return "names"

    def canonical_names(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for n in self.names:
            s = str(n).strip().lower()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    def name_token_map(self) -> dict[str, str]:
        """Map any wakeable name token (seed + learned) → canonical name."""
        m: dict[str, str] = {}
        for canon in self.canonical_names():
            m[canon] = canon
            for seed in _SEED_NAME_ALIASES.get(canon, ()):
                m[seed] = canon
            for bare in _BARE_SEED.get(canon, ()):
                m[bare] = canon
        for alias, canon in self.name_aliases.items():
            ak = str(alias).strip().lower()
            ck = str(canon).strip().lower()
            if ak and ck:
                m[ak] = ck
        return m

    def bare_tokens(self) -> dict[str, str]:
        """Tokens allowed as bare (no greating) wake → canonical name."""
        m: dict[str, str] = {}
        for canon in self.canonical_names():
            m[canon] = canon
            for bare in _BARE_SEED.get(canon, ()):
                m[bare] = canon
            # Learned aliases for this canon also bare-wake (operator taught them)
            for alias, c in self.name_aliases.items():
                if c.lower() == canon:
                    m[alias.lower()] = canon
        return m

    def exact_phrases(self) -> list[str]:
        """All full phrases that match exactly (configured + learned alternates)."""
        raw = list(self.phrases) + list(self.phrase_aliases)
        if self.normalized_mode() == "names" and not raw:
            # Display/compat synthetic list only when no extras — matching uses names
            return list(DEFAULT_ACTIVATION_PHRASES)
        seen: set[str] = set()
        out: list[str] = []
        for p in raw:
            s = str(p).strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        return out

    def display_phrases(self) -> list[str]:
        """Phrases for doctor / ambient.armed / near-miss events."""
        if self.normalized_mode() == "phrases":
            return self.exact_phrases() or list(DEFAULT_ACTIVATION_PHRASES)
        # names: greating×names samples + any full-phrase extras
        samples: list[str] = []
        names = self.canonical_names() or list(DEFAULT_WAKE_NAMES)
        for pref in ("hey", "hello", "okay", "ok"):
            for n in names:
                samples.append(f"{pref} {n}")
        extras = [p for p in self.phrases if p]
        # de-dupe
        seen: set[str] = set()
        out: list[str] = []
        for p in samples + extras + list(self.phrase_aliases):
            k = p.lower()
            if k not in seen:
                seen.add(k)
                out.append(p)
        return out

    def merge_learned(
        self,
        name_aliases: dict[str, str] | None = None,
        phrase_aliases: list[str] | None = None,
    ) -> WakePolicy:
        na = dict(self.name_aliases)
        if name_aliases:
            for k, v in name_aliases.items():
                na[str(k).strip().lower()] = str(v).strip().lower()
        pa = list(self.phrase_aliases)
        if phrase_aliases:
            seen = {p.lower() for p in pa}
            for p in phrase_aliases:
                s = str(p).strip().lower()
                if s and s not in seen:
                    seen.add(s)
                    pa.append(s)
        return WakePolicy(
            mode=self.mode,
            names=list(self.names),
            prefixes=self.prefixes,
            phrases=list(self.phrases),
            name_aliases=na,
            phrase_aliases=pa,
            learn=self.learn,
        )


def default_wake_policy() -> WakePolicy:
    return WakePolicy(
        mode=DEFAULT_WAKE_MODE,
        names=list(DEFAULT_WAKE_NAMES),
        phrases=[],
    )


def policy_from_phrases(
    phrases: list[str] | tuple[str, ...] | None,
) -> WakePolicy:
    """Infer a policy from a flat phrase list (legacy / tests).

    If any phrase mentions hark/herald → names mode with defaults.
    Otherwise → phrases-only mode (exclusive custom triggers).
    """
    plist = [str(p).strip() for p in (phrases or DEFAULT_ACTIVATION_PHRASES) if p]
    if not plist:
        return default_wake_policy()
    joined = " ".join(plist).lower()
    if "hark" in joined or "herald" in joined:
        return WakePolicy(
            mode="names",
            names=list(DEFAULT_WAKE_NAMES),
            phrases=[p for p in plist if p.lower() not in {d.lower() for d in DEFAULT_ACTIVATION_PHRASES}],
        )
    return WakePolicy(mode="phrases", names=[], phrases=plist)


def match_activation(
    text: str,
    phrases: list[str] | tuple[str, ...] = DEFAULT_ACTIVATION_PHRASES,
    *,
    anywhere: bool = False,
    policy: WakePolicy | None = None,
) -> WakeHit | None:
    """Match activation at start of text, or anywhere in a short wake snippet.

    Prefer passing an explicit :class:`WakePolicy`. When *policy* is None, a
    policy is inferred from *phrases* (legacy).
    """
    pol = policy if policy is not None else policy_from_phrases(phrases)
    raw = text or ""
    norm = normalize_for_match(raw)
    norm = _PUNCT.sub(" ", norm)
    norm = _WS.sub(" ", norm).strip()
    if not norm:
        return None

    # Exact full phrases (configured + learned phrase aliases)
    exact = list(pol.exact_phrases()) if pol.normalized_mode() == "phrases" else list(
        pol.phrases
    ) + list(pol.phrase_aliases)
    # Also try display/default-style phrases when provided via *phrases* arg
    if policy is None and phrases:
        exact = list(phrases) + [p for p in exact if p not in phrases]
    ordered = sorted(
        (normalize_for_match(p) for p in exact if p and str(p).strip()),
        key=len,
        reverse=True,
    )
    hit = _match_exact_phrases(norm, raw, ordered, anywhere=anywhere)
    if hit is not None:
        return hit

    if pol.normalized_mode() == "phrases":
        # Phrases mode: no name fuzzy/bare — only exact + learned alternates
        return None

    # Names mode: greating+name, bare name, seeds + learned aliases
    fuzzy = _match_fuzzy_wake(norm, pol)
    if fuzzy is not None:
        return fuzzy
    bare = _match_bare_product(norm, pol)
    if bare is not None:
        return bare

    # Legacy path when policy inferred from default phrases: also exact-match
    # the classic DEFAULT list so "hey hark" hits even with empty extras.
    if policy is None:
        legacy = sorted(
            (normalize_for_match(p) for p in phrases if p and str(p).strip()),
            key=len,
            reverse=True,
        )
        return _match_exact_phrases(norm, raw, legacy, anywhere=anywhere)
    # Configured names mode: also accept exact hey <name> strings as phrases
    samples = [
        normalize_for_match(f"{pref} {n}")
        for pref in pol.prefixes
        for n in pol.canonical_names()
    ]
    return _match_exact_phrases(norm, raw, sorted(samples, key=len, reverse=True), anywhere=anywhere)


def _match_exact_phrases(
    norm: str,
    raw: str,
    ordered: list[str],
    *,
    anywhere: bool,
) -> WakeHit | None:
    for p in ordered:
        if not p:
            continue
        if norm == p:
            return WakeHit(phrase=p, remainder="", raw=raw, backend="text")
        if norm.startswith(p + " "):
            return WakeHit(
                phrase=p, remainder=norm[len(p) :].strip(), raw=raw, backend="text"
            )
        if anywhere:
            padded = f" {norm} "
            needle = f" {p} "
            idx = padded.find(needle)
            if idx >= 0:
                after = padded[idx + len(needle) :].strip()
                return WakeHit(phrase=p, remainder=after, raw=raw, backend="text")
            if norm.endswith(" " + p):
                return WakeHit(phrase=p, remainder="", raw=raw, backend="text")
    return None


def _content_start(words: list[str]) -> int:
    """Index of first non-filler token (um/uh/yeah…), or 0 if none strip."""
    i = 0
    while i < len(words) and words[i] in _LEADING_FILLERS:
        i += 1
    return i


def _match_fuzzy_wake(norm: str, policy: WakePolicy) -> WakeHit | None:
    """Prefix + product name (configured names + seed/learned aliases)."""
    token_map = policy.name_token_map()
    if not token_map:
        return None
    prefixes = set(policy.prefixes)
    words = norm.split()
    for i, w in enumerate(words):
        if w not in prefixes:
            continue
        if i + 1 >= len(words):
            continue
        nxt = words[i + 1]
        canon = token_map.get(nxt)
        if canon is None and nxt.startswith("har") and len(nxt) <= 6:
            # Soft har* only for active seed families
            for c in policy.canonical_names():
                if c in ("hark", "herald") and c in token_map.values():
                    if c == "hark" and nxt in _HARK_ALIASES:
                        canon = "hark"
                        break
        if canon is None:
            continue
        rem = " ".join(words[i + 2 :])
        return WakeHit(
            phrase=f"{w} {canon}",
            remainder=rem,
            raw=norm,
            confidence=0.7,
            backend="text-fuzzy",
        )
    return None


def _match_bare_product(norm: str, policy: WakePolicy) -> WakeHit | None:
    """Wake on bare product name without greating (after optional fillers)."""
    bare_map = policy.bare_tokens()
    if not bare_map:
        return None
    words = norm.split()
    if not words:
        return None
    i = _content_start(words)
    if i >= len(words):
        return None
    w = words[i]
    canon = bare_map.get(w)
    if canon is None:
        return None
    # Classic idiom — not a wake.
    if canon == "hark" and i + 1 < len(words) and words[i + 1] == "back":
        return None
    rem = " ".join(words[i + 1 :])
    return WakeHit(
        phrase=canon,
        remainder=rem,
        raw=norm,
        confidence=0.85,
        backend="text-bare",
    )


def suggest_learn_from_near_miss(
    miss: NearMiss,
    policy: WakePolicy,
) -> tuple[str, str, str | None] | None:
    """If *miss* should expand the learned set, return (kind, value, canonical).

    kind is ``name`` or ``phrase``. Does not write disk.
    """
    if not policy.learn:
        return None
    norm = _normalize_wake_text(miss.text)
    if not norm:
        return None
    # Don't learn successful activations
    if match_activation(norm, policy=policy, anywhere=True) is not None:
        return None

    mode = policy.normalized_mode()
    words = norm.split()
    if mode == "phrases":
        # Learn short near-miss as full-phrase alternate when close enough
        if miss.score < 0.5 or len(words) > 5:
            return None
        # Skip bare greating-only
        if len(words) == 1 and words[0] in set(policy.prefixes):
            return None
        if norm in {p.lower() for p in policy.exact_phrases()}:
            return None
        return ("phrase", norm, None)

    # names mode: extract product-like token and map to best configured name
    names = policy.canonical_names()
    if not names:
        return None
    token_map = policy.name_token_map()
    candidates: list[str] = []
    for w in words:
        if w in set(policy.prefixes) or w in _LEADING_FILLERS:
            continue
        candidates.append(w)
    if not candidates:
        return None
    best_alias = ""
    best_canon = ""
    best_sc = 0.0
    for tok in candidates:
        if tok in token_map:
            continue  # already known
        for canon in names:
            sc = _name_similarity(tok, canon)
            if sc > best_sc:
                best_sc = sc
                best_alias = tok
                best_canon = canon
    # Near-miss already passed intentionality gates — allow slightly lower
    # char-sim for short product tokens (e.g. vosk "hoc"≈hark).
    min_sc = (
        0.28
        if miss.reason
        in ("prefix_product_near", "short_product_near", "family_token")
        else _NEAR_MISS_PRODUCT_SIM
    )
    if best_sc < min_sc or not best_alias:
        return None
    # Min length 3 + stopword denylist (is→iris from TTS bleed, etc.).
    # Seeds remain separate via _SEED_NAME_ALIASES / name_token_map.
    from hark.wake_learn import is_learnable_name_alias

    if not is_learnable_name_alias(best_alias):
        return None
    return ("name", best_alias, best_canon)


def _name_similarity(token: str, canon: str) -> float:
    if not token or not canon:
        return 0.0
    if token == canon:
        return 1.0
    # Seed families
    if canon == "hark" and token in _HARK_ALIASES:
        return 1.0
    if canon == "herald" and token in _HERALD_ALIASES:
        return 1.0
    raw = _char_similarity(token, canon)
    if canon in ("hark", "herald"):
        raw = max(raw, _product_token_score(token) * (1.0 if canon == "hark" else 0.95))
    if token[0] != canon[0]:
        raw *= 0.5
    return raw


# ---------------------------------------------------------------------------
# Plausible near-misses (failed wake that still looks intentional)
# ---------------------------------------------------------------------------

# Hard cap: longer ASR fragments are ordinary speech, never surfaced.
_NEAR_MISS_MAX_WORDS = 5
# Full-phrase character similarity floor (edit distance ratio).
_NEAR_MISS_PHRASE_SIM = 0.55
# Second-token similarity to product names (hoc≈hark is weak char-wise).
_NEAR_MISS_PRODUCT_SIM = 0.34
# Accept bare greating / incomplete wake (single prefix token).
_NEAR_MISS_PREFIX_ONLY = True

NEAR_MISS_INSTRUCTIONS = (
    "Plausible failed wake attempt(s) — not a successful activation. "
    "Review attempts[].text. Wake customization is either name-based "
    "([ambient] wake_mode=names, names=[...]) or full-phrase "
    "(wake_mode=phrases, trigger_phrases=[...]). Near-misses auto-expand "
    "learned alternates under ~/.local/state/hark/wake_learned.json without "
    "restart (ambient.wake_learned events). To pin permanently: names mode → "
    "names / extra_names; phrases mode → trigger_phrases / "
    "extra_trigger_phrases. SIGHUP reloads config; learning does not need it. "
    "See docs/CUSTOM_WAKE.md. Diagnostic only — do not invent answers or treat "
    "as a prompt."
)


@dataclass(frozen=True)
class NearMiss:
    """A failed wake snippet that is close enough to review."""

    text: str
    best_phrase: str
    score: float
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "best_phrase": self.best_phrase,
            "score": round(self.score, 4),
            "reason": self.reason,
        }


def _normalize_wake_text(text: str) -> str:
    norm = normalize_for_match(text or "")
    norm = _PUNCT.sub(" ", norm)
    return _WS.sub(" ", norm).strip()


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Two-row DP
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def _char_similarity(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return 1.0 - (_levenshtein(a, b) / max(len(a), len(b)))


def _product_token_score(token: str) -> float:
    """How much *token* looks like hark / herald (or known aliases)."""
    if not token:
        return 0.0
    if token in _HARK_ALIASES or token in _HERALD_ALIASES:
        return 1.0
    if token.startswith("har") and len(token) <= 6:
        return 0.85
    raw = max(
        _char_similarity(token, "hark"),
        _char_similarity(token, "herald"),
        _char_similarity(token, "harold"),
    )
    # Everyday words that share a few letters with hark/herald must not win
    # (thanks≈hark by edit distance). Prefer same initial as product family.
    if token[0] not in "ha":
        raw *= 0.45
    # Extreme length mismatch is almost never a wake mishear.
    if len(token) >= 7 and raw < 0.75:
        raw *= 0.5
    return raw


def _product_side_ok(words: list[str], best_phrase: str) -> bool:
    """True if non-prefix tokens look like the phrase product side (or wake family)."""
    phrase_prod = [w for w in best_phrase.split() if w not in _PREFIXES]
    if not phrase_prod:
        return True
    input_prod = [w for w in words if w not in _PREFIXES]
    if not input_prod:
        return False
    best = 0.0
    for iw in input_prod:
        # Family scorer (aliases + discounted edit distance to hark/herald)
        best = max(best, _product_token_score(iw))
        for pw in phrase_prod:
            # Canonical product names: do not re-score via raw edit distance
            # (``thanks``≈``hark``); family scorer already applied.
            if (
                pw in ("hark", "herald")
                or pw in _HARK_ALIASES
                or pw in _HERALD_ALIASES
            ):
                continue
            # Custom trigger tokens (e.g. prompt / dictation)
            best = max(best, 1.0 if iw == pw else _char_similarity(iw, pw))
    return best >= _NEAR_MISS_PRODUCT_SIM


def _phrase_similarity(norm: str, phrase: str) -> float:
    """Blend full-string edit sim with *ordered* token overlap.

    One exact greating token must not inflate a weak product token
    (``hey everyone`` must not score like ``hey herald``).
    """
    p = _normalize_wake_text(phrase)
    if not p:
        return 0.0
    full = _char_similarity(norm, p)
    nw = norm.split()
    pw = p.split()
    if not nw or not pw:
        return full
    # Ordered compare on the leading |pw| tokens (wake is at start of short ASR)
    scores: list[float] = []
    for i, tw in enumerate(pw):
        if i < len(nw):
            rw = nw[i]
            scores.append(1.0 if rw == tw else _char_similarity(rw, tw))
        else:
            scores.append(0.0)
    if not scores:
        return full
    token_avg = sum(scores) / len(scores)
    # Gate: multi-token phrases need every token at least weakly similar,
    # otherwise fall back to full-string only (avoids hey+unrelated).
    if len(scores) >= 2 and min(scores) < 0.30:
        ordered = full
    else:
        ordered = max(full, token_avg)
    extra = max(0, len(nw) - len(pw))
    penalty = min(0.15, 0.04 * extra)
    return max(0.0, ordered - penalty)


def plausible_near_miss(
    text: str,
    phrases: list[str] | tuple[str, ...] = DEFAULT_ACTIVATION_PHRASES,
    *,
    policy: WakePolicy | None = None,
) -> NearMiss | None:
    """Return a NearMiss if *text* looks like a failed activation attempt.

    Rejects empty/quiet, successful matches, and long unrelated multi-word
    speech. Accepts short fragments with high phrase similarity, wake-family
    tokens, prefix+almost-product patterns, or bare activation prefixes.
    """
    pol = policy if policy is not None else policy_from_phrases(phrases)
    raw = (text or "").strip()
    if not raw:
        return None
    norm = _normalize_wake_text(raw)
    if not norm:
        return None
    # Already a hit — not a near-miss.
    if match_activation(norm, phrases, anywhere=True, policy=pol) is not None:
        return None

    words = norm.split()
    n_words = len(words)
    if n_words > _NEAR_MISS_MAX_WORDS:
        return None

    ordered_phrases = [
        _normalize_wake_text(p) for p in pol.display_phrases() if p and str(p).strip()
    ]
    if not ordered_phrases:
        ordered_phrases = list(DEFAULT_ACTIVATION_PHRASES)

    best_phrase = ordered_phrases[0]
    best_score = 0.0
    for p in ordered_phrases:
        s = _phrase_similarity(norm, p)
        if s > best_score:
            best_score = s
            best_phrase = p

    has_prefix = any(w in _PREFIXES for w in words)
    product_scores = [_product_token_score(w) for w in words if w not in _PREFIXES]
    best_product = max(product_scores) if product_scores else 0.0
    has_family = best_product >= 0.85 or any(
        w in _HARK_ALIASES or w in _HERALD_ALIASES for w in words
    )

    # 1) Strong full/token similarity to a configured phrase — but require the
    #    non-prefix side to look intentional, not just "okay …" / greating-only.
    if best_score >= _NEAR_MISS_PHRASE_SIM and _product_side_ok(words, best_phrase):
        return NearMiss(
            text=norm,
            best_phrase=best_phrase,
            score=best_score,
            reason="phrase_similarity",
        )

    # 2) Bare prefix only (incomplete wake: "hey", "hello")
    if (
        _NEAR_MISS_PREFIX_ONLY
        and n_words == 1
        and words[0] in _PREFIXES
    ):
        # Prefer a phrase that starts with this prefix
        pref = words[0]
        bp = next((p for p in ordered_phrases if p.startswith(pref)), best_phrase)
        return NearMiss(
            text=norm,
            best_phrase=bp,
            score=0.5,
            reason="prefix_only",
        )

    # 3) Prefix + short product-like second token (hey hoc, hey ho)
    if has_prefix and n_words <= 4:
        for i, w in enumerate(words):
            if w not in _PREFIXES:
                continue
            if i + 1 >= n_words:
                continue
            nxt = words[i + 1]
            ps = _product_token_score(nxt)
            # Short second tokens after a wake greating are suspicious even
            # when edit distance is weak (vosk "hoc" / "ho" / "ha").
            # Require product-ish initial (h/a) or real product similarity —
            # not everyday words like "there" / "thanks" / "everyone".
            short_attempt = (
                2 <= len(nxt) <= 5
                and nxt[0] in "ha"
                and ps >= 0.20
            )
            if ps >= _NEAR_MISS_PRODUCT_SIM or short_attempt:
                score = max(best_score, 0.45 + 0.4 * ps, 0.5 if short_attempt else 0.0)
                target = "hark" if ps >= _char_similarity(nxt, "herald") else "herald"
                # Prefer matching configured phrase with this prefix
                bp = next(
                    (p for p in ordered_phrases if p.startswith(w) and target in p),
                    next((p for p in ordered_phrases if p.startswith(w)), best_phrase),
                )
                return NearMiss(
                    text=norm,
                    best_phrase=bp,
                    score=min(0.95, score),
                    reason="prefix_product_near",
                )

    # 4) Short fragment with a known family token but missing/wrong greating
    #    e.g. "a hawk", "harold" — only when very short (avoids "hark back…").
    if has_family and n_words <= 3:
        return NearMiss(
            text=norm,
            best_phrase=best_phrase,
            score=max(best_score, 0.55, best_product * 0.7),
            reason="family_token",
        )

    # 5) Very short (≤2 words) with moderate product similarity
    if n_words <= 2 and best_product >= _NEAR_MISS_PRODUCT_SIM:
        return NearMiss(
            text=norm,
            best_phrase=best_phrase,
            score=max(best_score, best_product * 0.6),
            reason="short_product_near",
        )

    return None


def near_miss_group_size(group_index: int) -> int:
    """Surfacing schedule: 1, then 2, then 2, then groups of 3 forever."""
    if group_index <= 0:
        return 1
    if group_index <= 2:
        return 2
    return 3


@dataclass
class NearMissAccumulator:
    """Buffer plausible misses and emit groups on the Mode A schedule."""

    total: int = 0
    group_index: int = 0
    pending: list[NearMiss] = field(default_factory=list)

    def add(self, miss: NearMiss) -> list[NearMiss] | None:
        """Append a miss; return a group ready to surface, else None."""
        self.total += 1
        self.pending.append(miss)
        need = near_miss_group_size(self.group_index)
        if len(self.pending) >= need:
            group = list(self.pending)
            self.pending.clear()
            self.group_index += 1
            return group
        return None

    def reset_pending(self) -> None:
        """Drop unsurfaced misses (e.g. after a successful wake)."""
        self.pending.clear()


def make_wake_near_miss_event(
    attempts: list[NearMiss],
    *,
    total_near_misses: int,
    group_index: int,
    phrases: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """HEP event for Mode A monitor: grouped plausible wake failures."""
    from hark.events import new_event_id, utc_now_iso

    return {
        "schema": "hark.event.v1",
        "kind": "ambient.wake_near_miss",
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "priority": 35,
        "disposition": "info",
        "count": len(attempts),
        "total_near_misses": total_near_misses,
        # group_index of the group just emitted (0-based)
        "group_index": max(0, group_index - 1),
        "attempts": [m.to_dict() for m in attempts],
        "phrases": list(phrases) if phrases is not None else list(DEFAULT_ACTIVATION_PHRASES),
        "instructions": NEAR_MISS_INSTRUCTIONS,
    }


def pcm16_rms(pcm16_le: bytes) -> float:
    if len(pcm16_le) < 2:
        return 0.0
    n = len(pcm16_le) // 2
    # sample a subset for speed
    step = max(1, n // 2000)
    acc = 0.0
    count = 0
    for i in range(0, n, step):
        (sample,) = struct.unpack_from("<h", pcm16_le, i * 2)
        acc += float(sample) * float(sample)
        count += 1
    if count == 0:
        return 0.0
    return math.sqrt(acc / count) / 32768.0


class WakeBackend(Protocol):
    name: str

    def score_snippet(self, pcm16_le: bytes, sample_rate: int = 16000) -> WakeHit | None:
        ...


class TextProbeBackend:
    name = "text_probe"

    def __init__(
        self,
        phrases: list[str] | tuple[str, ...] | None = None,
        *,
        policy: WakePolicy | None = None,
    ) -> None:
        self.policy = policy if policy is not None else policy_from_phrases(phrases)
        self.phrases = list(
            phrases
            if phrases is not None
            else self.policy.display_phrases()
        )
        self.last_text: str = ""
        self.last_rms: float = 0.0

    def score_snippet(self, pcm16_le: bytes, sample_rate: int = 16000) -> WakeHit | None:
        self.last_text = ""
        if pcm16_le.startswith(b"TXT:"):
            text = pcm16_le[4:].decode("utf-8", errors="replace")
            self.last_text = text
            hit = match_activation(
                text, self.phrases, anywhere=True, policy=self.policy
            )
            if hit:
                return WakeHit(
                    phrase=hit.phrase,
                    remainder=hit.remainder,
                    raw=hit.raw,
                    backend=self.name,
                )
        return None


# Prefixes used when building Sherpa KWS keyword lists (subset of fuzzy prefixes).
# Keep short greating forms that operators actually say for wake.
_KWS_PREFIXES: tuple[str, ...] = ("hey", "hello", "okay", "ok", "hi")


def kws_keyword_specs(
    policy: WakePolicy | None = None,
    *,
    phrases: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[str, str, float, float]]:
    """Build (spoken_phrase, keyword_id, boost, threshold) rows for open-vocab KWS.

    *spoken_phrase* is space-separated uppercase words for BPE encoding.
    *keyword_id* is the @label returned by sherpa (underscores, no spaces).
    Boost/threshold tuned from B069 probe fixtures (greating tighter than bare).
    """
    pol = policy if policy is not None else (
        policy_from_phrases(phrases) if phrases is not None else default_wake_policy()
    )
    rows: list[tuple[str, str, float, float]] = []
    seen_ids: set[str] = set()

    def add(spoken: str, *, boost: float, thr: float) -> None:
        words = [w for w in spoken.strip().upper().split() if w]
        if not words:
            return
        spoken_u = " ".join(words)
        kid = "_".join(words)
        if kid in seen_ids:
            return
        seen_ids.add(kid)
        rows.append((spoken_u, kid, boost, thr))

    if pol.normalized_mode() == "phrases":
        for p in pol.exact_phrases():
            add(p, boost=1.5, thr=0.2)
        return rows

    names = pol.canonical_names() or list(DEFAULT_WAKE_NAMES)
    # Canonical + learned name tokens that should fire as wake words
    name_tokens: list[tuple[str, str]] = []  # token, canonical
    for n in names:
        name_tokens.append((n, n))
    for alias, canon in pol.name_aliases.items():
        a = str(alias).strip().lower()
        c = str(canon).strip().lower()
        if a and c and c in names:
            name_tokens.append((a, c))
    # de-dupe tokens, keep first canonical
    seen_tok: set[str] = set()
    uniq_tokens: list[tuple[str, str]] = []
    for tok, canon in name_tokens:
        if tok not in seen_tok:
            seen_tok.add(tok)
            uniq_tokens.append((tok, canon))

    for pref in _KWS_PREFIXES:
        for tok, _canon in uniq_tokens:
            # Multi-word greating+name: higher boost, lower threshold (easier hit)
            add(f"{pref} {tok}", boost=2.0, thr=0.15)
    for tok, _canon in uniq_tokens:
        # Bare name: slightly stricter threshold to limit mid-sentence fires
        add(tok, boost=1.5, thr=0.25)
    # Exact full-phrase extras / learned phrase aliases
    for p in list(pol.phrases) + list(pol.phrase_aliases):
        add(p, boost=1.5, thr=0.2)
    return rows


def encode_kws_keywords_file(
    specs: list[tuple[str, str, float, float]],
    *,
    bpe_model: str | Path,
    tokens_txt: str | Path,
    dest: str | Path,
) -> Path:
    """Write a sherpa keywords.txt (BPE tokens + :boost #thr @ID) from specs."""
    dest_p = Path(dest)
    dest_p.parent.mkdir(parents=True, exist_ok=True)
    if not specs:
        # KeywordSpotter requires a non-empty file; never-matching dummy
        dest_p.write_text("▁Z Z Z Z :0.01 #0.99 @HARK_NEVER\n", encoding="utf-8")
        return dest_p

    bpe_model = Path(bpe_model)
    tokens_txt = Path(tokens_txt)
    lines_out: list[str] = []

    # Prefer in-process sentencepiece (fast); fall back to sherpa-onnx-cli
    sp = None
    try:
        import sentencepiece as spm  # type: ignore

        sp = spm.SentencePieceProcessor()
        sp.load(str(bpe_model))
    except Exception:
        sp = None

    if sp is not None:
        for spoken, kid, boost, thr in specs:
            pieces = sp.encode(spoken, out_type=str)
            if not pieces:
                continue
            lines_out.append(
                f"{' '.join(pieces)} :{boost} #{thr} @{kid}"
            )
    else:
        # CLI path: write raw phrases, convert
        raw = dest_p.with_suffix(".raw.txt")
        raw_lines = [
            f"{spoken} :{boost} #{thr} @{kid}"
            for spoken, kid, boost, thr in specs
        ]
        raw.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        import shutil
        import subprocess

        cli = shutil.which("sherpa-onnx-cli")
        if not cli:
            raise RuntimeError(
                "keyword BPE encode needs sentencepiece or sherpa-onnx-cli "
                "(uv sync --extra wake-sherpa)"
            )
        subprocess.run(
            [
                cli,
                "text2token",
                "--tokens",
                str(tokens_txt),
                "--tokens-type",
                "bpe",
                "--bpe-model",
                str(bpe_model),
                str(raw),
                str(dest_p),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return dest_p

    dest_p.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
    return dest_p


def resolve_sherpa_kws_model_files(model_dir: str | Path) -> dict[str, Path]:
    """Locate encoder/decoder/joiner/tokens/bpe under a KWS model tree (prefer int8)."""
    root = Path(model_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"sherpa KWS model dir missing: {root}")

    def pick(*names: str) -> Path:
        for n in names:
            p = root / n
            if p.is_file():
                return p
        raise FileNotFoundError(
            f"sherpa KWS model incomplete under {root}: need one of {names}"
        )

    return {
        "tokens": pick("tokens.txt"),
        "bpe": pick("bpe.model"),
        "encoder": pick(
            "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "encoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        ),
        "decoder": pick(
            "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
        ),
        "joiner": pick(
            "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "joiner-epoch-12-avg-2-chunk-16-left-64.onnx",
        ),
    }


def is_sherpa_kws_model_dir(path: str | Path | None) -> bool:
    if not path:
        return False
    try:
        resolve_sherpa_kws_model_files(path)
        return True
    except (OSError, FileNotFoundError):
        return False


def keyword_id_to_phrase(keyword_id: str) -> str:
    """Map sherpa @HEY_HARK → 'hey hark'."""
    s = (keyword_id or "").strip()
    if s.startswith("@"):
        s = s[1:]
    return s.replace("_", " ").strip().lower()


class SherpaKwsWakeBackend:
    """Open-vocab keyword spotting via sherpa-onnx (English GigaSpeech 3.3M).

    Optional engine (``ambient.engine = "sherpa_kws"``). Vosk remains default.
    Keywords rebuild from :class:`WakePolicy` on policy/config reload without
    process restart. Missing model/package → clear RuntimeError (fail-open docs:
    leave engine on vosk or install model via download script).
    """

    name = "sherpa_kws"

    def __init__(
        self,
        model_path: str,
        phrases: list[str] | tuple[str, ...] | None = None,
        *,
        policy: WakePolicy | None = None,
        energy_floor: float = 0.003,
        keywords_path: str | Path | None = None,
        num_threads: int = 2,
    ) -> None:
        self.model_path = model_path
        self.policy = policy if policy is not None else policy_from_phrases(phrases)
        self.phrases = list(
            phrases
            if phrases is not None
            else self.policy.display_phrases()
        )
        self.energy_floor = energy_floor
        self.num_threads = num_threads
        self._files = resolve_sherpa_kws_model_files(model_path)
        if keywords_path is None:
            # Prefer XDG state; fall back to temp for tests without home
            try:
                from hark.paths import state_dir

                kdir = state_dir()
            except Exception:
                kdir = Path(tempfile.gettempdir()) / "hark"
            keywords_path = kdir / "sherpa_kws_keywords.txt"
        self.keywords_path = Path(keywords_path)
        self._spotter = None
        self._np = None
        self.last_text: str = ""
        self.last_rms: float = 0.0
        self.snippets_scored: int = 0
        self.snippets_skipped_quiet: int = 0
        self._keyword_ids: list[str] = []
        self.rebuild_keywords(self.policy)

    def rebuild_keywords(self, policy: WakePolicy | None = None) -> None:
        """Regenerate keywords file + KeywordSpotter from policy (config reload).

        No-op when the keyword graph signature is unchanged and a spotter is
        already loaded — continuous ambient must not reload ONNX every hop.
        """
        if policy is not None:
            self.policy = policy
            self.phrases = policy.display_phrases()
        specs = kws_keyword_specs(self.policy, phrases=self.phrases)
        sig = tuple(specs)
        if (
            sig == getattr(self, "_keyword_sig", None)
            and self._spotter is not None
            and self.keywords_path.is_file()
        ):
            return
        self._keyword_sig = sig
        self._keyword_ids = [kid for _s, kid, _b, _t in specs]
        encode_kws_keywords_file(
            specs,
            bpe_model=self._files["bpe"],
            tokens_txt=self._files["tokens"],
            dest=self.keywords_path,
        )
        # Force spotter rebuild with new graph
        self._spotter = None

    def _ensure(self) -> None:
        if self._spotter is not None:
            return
        try:
            import numpy as np  # type: ignore
            import sherpa_onnx  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "sherpa-onnx not installed; uv sync --extra wake-sherpa "
                "or pip install sherpa-onnx sentencepiece"
            ) from exc
        self._np = np
        if not self.keywords_path.is_file():
            self.rebuild_keywords(self.policy)
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(self._files["tokens"]),
            encoder=str(self._files["encoder"]),
            decoder=str(self._files["decoder"]),
            joiner=str(self._files["joiner"]),
            keywords_file=str(self.keywords_path),
            num_threads=self.num_threads,
            sample_rate=16000,
            feature_dim=80,
            max_active_paths=4,
            keywords_score=1.0,
            keywords_threshold=0.25,
            num_trailing_blanks=1,
            provider="cpu",
        )

    def score_snippet(self, pcm16_le: bytes, sample_rate: int = 16000) -> WakeHit | None:
        rms = pcm16_rms(pcm16_le)
        self.last_rms = rms
        if rms < self.energy_floor:
            self.snippets_skipped_quiet += 1
            self.last_text = ""
            return None

        self._ensure()
        assert self._spotter is not None and self._np is not None
        self.snippets_scored += 1

        # int16 LE → float32 in [-1, 1]
        n = len(pcm16_le) // 2
        if n == 0:
            self.last_text = ""
            return None
        pcm = self._np.frombuffer(pcm16_le, dtype=self._np.int16).astype(
            self._np.float32
        ) / 32768.0

        # Streaming KWS: feed ~100 ms chunks and decode as ready. Dumping the
        # whole buffer + long tail in one accept_waveform over-triggers near
        # misses (B069 probe used chunked decode for clean hit/miss separation).
        stream = self._spotter.create_stream()
        step = max(1, int(0.1 * sample_rate))
        detected = ""
        for i in range(0, len(pcm), step):
            stream.accept_waveform(sample_rate, pcm[i : i + step])
            while self._spotter.is_ready(stream):
                self._spotter.decode_stream(stream)
                r = self._spotter.get_result(stream)
                if r:
                    detected = r
                    self._spotter.reset_stream(stream)
                    break
            if detected:
                break
        if not detected:
            tail = self._np.zeros(int(0.5 * sample_rate), dtype=self._np.float32)
            stream.accept_waveform(sample_rate, tail)
            stream.input_finished()
            while self._spotter.is_ready(stream):
                self._spotter.decode_stream(stream)
                r = self._spotter.get_result(stream)
                if r:
                    detected = r
                    break

        self.last_text = detected or ""
        if not detected:
            return None

        phrase = keyword_id_to_phrase(detected)
        # Prefer policy display form if close
        for disp in self.phrases:
            if normalize_for_match(disp) == normalize_for_match(phrase):
                phrase = disp.lower()
                break
        return WakeHit(
            phrase=phrase,
            remainder="",
            raw=detected,
            confidence=0.9,
            backend=self.name,
        )


class VoskWakeBackend:
    """Tiny local ASR on short snippets — model loaded once and reused."""

    name = "vosk"

    def __init__(
        self,
        model_path: str,
        phrases: list[str] | tuple[str, ...] | None = None,
        *,
        policy: WakePolicy | None = None,
        # ~0.003 catches soft close-talk; quiet room often sits ~0.001
        energy_floor: float = 0.003,
    ) -> None:
        self.model_path = model_path
        self.policy = policy if policy is not None else policy_from_phrases(phrases)
        self.phrases = list(
            phrases
            if phrases is not None
            else self.policy.display_phrases()
        )
        self.energy_floor = energy_floor
        self._model = None
        self._Rec = None
        self._sample_rate = 16000
        self.last_text: str = ""
        self.last_rms: float = 0.0
        self.snippets_scored: int = 0
        self.snippets_skipped_quiet: int = 0

    def _ensure(self, sample_rate: int) -> None:
        if self._model is not None:
            return
        try:
            from vosk import KaldiRecognizer, Model, SetLogLevel  # type: ignore

            SetLogLevel(-1)  # silence spam into ambient logs
        except ImportError as exc:
            raise RuntimeError(
                "vosk not installed; pip install vosk or uv sync --extra wake"
            ) from exc
        self._model = Model(self.model_path)
        self._Rec = KaldiRecognizer
        self._sample_rate = sample_rate

    def score_snippet(self, pcm16_le: bytes, sample_rate: int = 16000) -> WakeHit | None:
        rms = pcm16_rms(pcm16_le)
        self.last_rms = rms
        if rms < self.energy_floor:
            self.snippets_skipped_quiet += 1
            self.last_text = ""
            return None

        self._ensure(sample_rate)
        self.snippets_scored += 1
        rec = self._Rec(self._model, sample_rate)
        # Feed in chunks (vosk prefers ~0.2–0.5s blocks)
        frame = sample_rate * 2 // 5  # 0.2s of int16 mono = sr*0.2*2 bytes
        frame = max(3200, frame)
        for i in range(0, len(pcm16_le), frame):
            rec.AcceptWaveform(pcm16_le[i : i + frame])
        try:
            data = json.loads(rec.FinalResult())
        except Exception:
            return None
        text = str(data.get("text") or "").strip()
        self.last_text = text
        if not text:
            return None
        hit = match_activation(
            text, self.phrases, anywhere=True, policy=self.policy
        )
        if not hit:
            return None
        return WakeHit(
            phrase=hit.phrase,
            remainder=hit.remainder,
            raw=text,
            confidence=0.8,
            backend=self.name,
        )


def build_wake_backend(
    engine: str,
    *,
    phrases: list[str] | tuple[str, ...] | None = None,
    model_path: str | None = None,
    policy: WakePolicy | None = None,
) -> WakeBackend:
    engine = (engine or "vosk").lower()
    pol = policy
    plist = list(phrases) if phrases is not None else None
    if pol is None and plist is not None:
        pol = policy_from_phrases(plist)
    elif pol is not None and plist is None:
        plist = pol.display_phrases()
    elif pol is None:
        pol = default_wake_policy()
        plist = pol.display_phrases()
    if engine in ("off", "none", "disabled"):
        return TextProbeBackend(plist, policy=pol)
    if engine in ("text_probe", "mock", "test"):
        return TextProbeBackend(plist, policy=pol)
    if engine == "vosk":
        if not model_path:
            raise RuntimeError(
                "ambient.engine=vosk requires ambient.model_path "
                "(run ./scripts/setup-ambient.sh)"
            )
        return VoskWakeBackend(model_path, plist, policy=pol)
    if engine in ("sherpa_kws", "sherpa", "kws"):
        if not model_path:
            raise RuntimeError(
                "ambient.engine=sherpa_kws requires ambient.model_path "
                "(run ./scripts/download-sherpa-kws-model.sh)"
            )
        if not is_sherpa_kws_model_dir(model_path):
            raise RuntimeError(
                f"ambient.model_path is not a sherpa KWS model tree: {model_path} "
                "(need tokens.txt, bpe.model, encoder/decoder/joiner onnx — "
                "run ./scripts/download-sherpa-kws-model.sh)"
            )
        return SherpaKwsWakeBackend(model_path, plist, policy=pol)
    raise ValueError(f"unknown ambient wake engine: {engine!r}")

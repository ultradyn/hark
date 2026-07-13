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
from dataclasses import dataclass, field
from typing import Any, Protocol

from hark.listen_end import normalize_for_match

DEFAULT_ACTIVATION_PHRASES: tuple[str, ...] = (
    "hey hark",
    "hey herald",
    "hello hark",
    "hello herald",
    "okay hark",
    "ok hark",
)

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[\.\!\?\,\;\:…]+")


@dataclass(frozen=True)
class WakeHit:
    phrase: str
    remainder: str
    raw: str
    confidence: float = 1.0
    backend: str = "text"


# Common small-model mishears for product wake words
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
_PREFIXES = ("hey", "hello", "okay", "ok", "hi")


def match_activation(
    text: str,
    phrases: list[str] | tuple[str, ...] = DEFAULT_ACTIVATION_PHRASES,
    *,
    anywhere: bool = False,
) -> WakeHit | None:
    """Match activation at start of text, or anywhere in a short wake snippet.

    Also accepts common vosk mishears: \"hey hook\" → hey hark, \"hey harold\" → hey herald.
    """
    raw = text or ""
    norm = normalize_for_match(raw)
    norm = _PUNCT.sub(" ", norm)
    norm = _WS.sub(" ", norm).strip()
    if not norm:
        return None

    ordered = sorted(
        (normalize_for_match(p) for p in phrases if p and str(p).strip()),
        key=len,
        reverse=True,
    )
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

    # Fuzzy hey/okay + hark|herald mishears only when the active phrase list
    # still includes a product wake (defaults or explicit). A full replace with
    # only custom phrases (e.g. trigger_phrases = ["start prompt"]) stays exclusive.
    if _phrases_allow_product_fuzzy(ordered):
        fuzzy = _match_fuzzy_wake(norm)
        if fuzzy is not None:
            return fuzzy
    return None


def _phrases_allow_product_fuzzy(normalized_phrases: list[str]) -> bool:
    for p in normalized_phrases:
        if "hark" in p or "herald" in p:
            return True
    return False


def _match_fuzzy_wake(norm: str) -> WakeHit | None:
    words = norm.split()
    for i, w in enumerate(words):
        if w not in _PREFIXES:
            continue
        if i + 1 >= len(words):
            continue
        nxt = words[i + 1]
        # strip trailing punctuation already done
        if nxt in _HARK_ALIASES or nxt.startswith("har") and len(nxt) <= 6:
            # avoid matching "hard drive" alone without hey — we require prefix
            if nxt in _HARK_ALIASES or nxt in ("hark", "hook", "hawk", "hork"):
                rem = " ".join(words[i + 2 :])
                return WakeHit(
                    phrase=f"{w} hark",
                    remainder=rem,
                    raw=norm,
                    confidence=0.7,
                    backend="text-fuzzy",
                )
        if nxt in _HERALD_ALIASES:
            rem = " ".join(words[i + 2 :])
            return WakeHit(
                phrase=f"{w} herald",
                remainder=rem,
                raw=norm,
                confidence=0.7,
                backend="text-fuzzy",
            )
    return None


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
    "Review attempts[].text against configured activation phrases. "
    "If the operator is clearly trying to wake Hark, evaluate whether to add "
    "or adjust a phrase/alias: [ambient] extra_trigger_phrases (append) or "
    "activation_phrases / trigger_phrases (replace list). Fuzzy product "
    "mishears may also warrant a code/config alias. "
    "After updating config, reload ambient: restart Mode A ambient "
    "(e.g. restart the ambient/hark process); SIGHUP config reload is not "
    "supported yet. Diagnostic only — do not invent answers or treat as a prompt."
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
) -> NearMiss | None:
    """Return a NearMiss if *text* looks like a failed activation attempt.

    Rejects empty/quiet, successful matches, and long unrelated multi-word
    speech. Accepts short fragments with high phrase similarity, wake-family
    tokens, prefix+almost-product patterns, or bare activation prefixes.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    norm = _normalize_wake_text(raw)
    if not norm:
        return None
    # Already a hit — not a near-miss.
    if match_activation(norm, phrases, anywhere=True) is not None:
        return None

    words = norm.split()
    n_words = len(words)
    if n_words > _NEAR_MISS_MAX_WORDS:
        return None

    ordered_phrases = [
        _normalize_wake_text(p) for p in phrases if p and str(p).strip()
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

    def __init__(self, phrases: list[str] | tuple[str, ...] | None = None) -> None:
        self.phrases = list(phrases or DEFAULT_ACTIVATION_PHRASES)
        self.last_text: str = ""
        self.last_rms: float = 0.0

    def score_snippet(self, pcm16_le: bytes, sample_rate: int = 16000) -> WakeHit | None:
        self.last_text = ""
        if pcm16_le.startswith(b"TXT:"):
            text = pcm16_le[4:].decode("utf-8", errors="replace")
            self.last_text = text
            hit = match_activation(text, self.phrases, anywhere=True)
            if hit:
                return WakeHit(
                    phrase=hit.phrase,
                    remainder=hit.remainder,
                    raw=hit.raw,
                    backend=self.name,
                )
        return None


class VoskWakeBackend:
    """Tiny local ASR on short snippets — model loaded once and reused."""

    name = "vosk"

    def __init__(
        self,
        model_path: str,
        phrases: list[str] | tuple[str, ...] | None = None,
        *,
        # ~0.003 catches soft close-talk; quiet room often sits ~0.001
        energy_floor: float = 0.003,
    ) -> None:
        self.model_path = model_path
        self.phrases = list(phrases or DEFAULT_ACTIVATION_PHRASES)
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
        hit = match_activation(text, self.phrases, anywhere=True)
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
    phrases: list[str] | tuple[str, ...],
    model_path: str | None = None,
) -> WakeBackend:
    engine = (engine or "vosk").lower()
    if engine in ("off", "none", "disabled"):
        return TextProbeBackend(phrases)
    if engine in ("text_probe", "mock", "test"):
        return TextProbeBackend(phrases)
    if engine == "vosk":
        if not model_path:
            raise RuntimeError(
                "ambient.engine=vosk requires ambient.model_path "
                "(run ./scripts/setup-ambient.sh)"
            )
        return VoskWakeBackend(model_path, phrases)
    raise ValueError(f"unknown ambient wake engine: {engine!r}")

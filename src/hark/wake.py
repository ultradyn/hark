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
from dataclasses import dataclass
from typing import Protocol

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

    def score_snippet(self, pcm16_le: bytes, sample_rate: int = 16000) -> WakeHit | None:
        if pcm16_le.startswith(b"TXT:"):
            text = pcm16_le[4:].decode("utf-8", errors="replace")
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

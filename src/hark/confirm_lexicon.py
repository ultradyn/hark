"""Spoken confirmation / cancel lexicon for ask --confirm."""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from functools import cache

from hark.listen_end import APOSTROPHE_EQUIVALENTS, normalize_for_match

_PUNCTUATION = re.compile(r"[^\w\s']+", re.UNICODE)
_ADJOINING_PUNCTUATION_SEPARATOR = re.compile(r"^[^\w\s']+", re.UNICODE)
_PUNCTUATED_DEFER_CUES = ("but", "wait", "if", "unless")
_AFFIRMATIVE_IDIOMS = frozenset({"yes why not"})
_SUPPORTED_APOSTROPHE_SEPARATORS = APOSTROPHE_EQUIVALENTS | {"'"}
_DECOMPOSED_SPACING_ACUTE = " \u0301"
# This limits one canonical ordering/composition segment, not transcript length.
# Human speech text should never need hundreds of marks on one starter; bounding
# the segment avoids the quadratic worst case in CPython's Unicode normalizer.
_MAX_NORMALIZATION_SEGMENT_CHARS = 256

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

_CONTRACTION_PARTS = tuple(
    tuple(phrase.split("'", 1)) for phrase in NEGATE if phrase.count("'") == 1
)
_CONTRACTION_SEPARATOR_PATTERNS = tuple(
    re.compile(
        rf"{re.escape(left)}(?P<separator>[^a-z0-9]+){re.escape(right)}",
        re.IGNORECASE,
    )
    for left, right in _CONTRACTION_PARTS
)


@dataclass(frozen=True)
class _SourceSpan:
    start: int
    end: int


@dataclass(frozen=True)
class _NormalizedView:
    text: str
    source_spans: tuple[_SourceSpan, ...]


@dataclass(frozen=True)
class _ContractionProvenance:
    has_unsupported_separator: bool
    canonical_input: str
    normalization_rejected: bool = False


def _direct_composition_result(first: str, second: str) -> str:
    """Resolve one bounded canonical composition without whole-input NFKC."""
    pair = first + second
    normalized = unicodedata.normalize("NFC", pair)
    return normalized if len(normalized) == 1 and normalized != pair else ""


def _advance_direct_composition_tail(tail: str, decomposition: str) -> str:
    for char in decomposition:
        if unicodedata.combining(char) != 0:
            tail = ""
        else:
            composite = _direct_composition_result(tail, char) if tail else ""
            tail = composite or char
    return tail


def _normalization_segments_are_bounded(text: str) -> bool:
    """Check NFKC segment sizes using only constant-size normalization calls."""
    segment_size = 0
    composition_tail = ""
    for char in text:
        decomposition = unicodedata.normalize("NFKD", char)
        first = decomposition[0] if decomposition else ""
        continues_composition = bool(
            composition_tail
            and first
            and _direct_composition_result(composition_tail, first)
        )
        joins_segment = bool(segment_size) and (
            not first or unicodedata.combining(first) != 0 or continues_composition
        )
        if segment_size and not joins_segment:
            segment_size = 0
            composition_tail = ""
        segment_size += 1
        if segment_size > _MAX_NORMALIZATION_SEGMENT_CHARS:
            return False
        composition_tail = _advance_direct_composition_tail(
            composition_tail, decomposition
        )
    return True


def _normalize_with_provenance(
    text: str,
    form: str,
    source_spans: tuple[_SourceSpan, ...] | None = None,
) -> _NormalizedView:
    """Normalize in linear normalization-safe segments with raw provenance.

    A segment starts at a normalization starter and owns characters that can
    reorder or canonically compose with its tail (including algorithmic Hangul).
    Normalizing each disjoint segment once is equivalent to whole-string
    normalization while keeping provenance as one shared raw interval per segment.
    The caller first bounds every segment, making this O(input + normalized output)
    despite the normalizer's quadratic behavior on an unbounded mark sequence.
    """
    if form not in {"NFKC", "NFD"}:
        raise ValueError(f"unsupported provenance normalization form: {form}")
    input_spans = source_spans or tuple(_SourceSpan(i, i + 1) for i in range(len(text)))
    decomposition_form = "NFKD" if form == "NFKC" else "NFD"
    output_parts: list[str] = []
    output_spans: list[_SourceSpan] = []
    segment_chars: list[str] = []
    segment_start = 0
    segment_end = 0
    composition_tail = ""

    def flush_segment() -> None:
        if not segment_chars:
            return
        normalized_segment = unicodedata.normalize(form, "".join(segment_chars))
        span = _SourceSpan(segment_start, segment_end)
        output_parts.append(normalized_segment)
        output_spans.extend([span] * len(normalized_segment))

    for char, char_span in zip(text, input_spans, strict=True):
        decomposition = unicodedata.normalize(decomposition_form, char)
        first = decomposition[0] if decomposition else ""
        continues_composition = bool(
            form == "NFKC"
            and composition_tail
            and first
            and _composition_result(composition_tail, first)
        )
        joins_segment = bool(segment_chars) and (
            not first or unicodedata.combining(first) != 0 or continues_composition
        )
        if segment_chars and not joins_segment:
            flush_segment()
            segment_chars.clear()
            composition_tail = ""
        segment_chars.append(char)
        if len(segment_chars) == 1:
            segment_start = char_span.start
            segment_end = char_span.end
        else:
            segment_start = min(segment_start, char_span.start)
            segment_end = max(segment_end, char_span.end)
        if form == "NFKC":
            composition_tail = _advance_composition_tail(
                composition_tail, decomposition
            )
    flush_segment()
    return _NormalizedView("".join(output_parts), tuple(output_spans))


@cache
def _canonical_composition_map() -> dict[tuple[str, str], str]:
    """Derive every non-algorithmic canonical composition from Unicode data."""
    compositions: dict[tuple[str, str], str] = {}
    for codepoint in range(sys.maxunicode + 1):
        composite = chr(codepoint)
        decomposition = unicodedata.decomposition(composite)
        if not decomposition or decomposition.startswith("<"):
            continue
        parts = decomposition.split()
        if len(parts) != 2:
            continue
        first, second = (chr(int(part, 16)) for part in parts)
        if unicodedata.normalize("NFC", first + second) == composite:
            compositions[(first, second)] = composite
    return compositions


def _hangul_composition(first: str, second: str) -> str:
    first_codepoint = ord(first)
    second_codepoint = ord(second)
    if 0x1100 <= first_codepoint <= 0x1112 and 0x1161 <= second_codepoint <= 0x1175:
        l_index = first_codepoint - 0x1100
        v_index = second_codepoint - 0x1161
        return chr(0xAC00 + (l_index * 21 + v_index) * 28)
    if (
        0xAC00 <= first_codepoint <= 0xD7A3
        and (first_codepoint - 0xAC00) % 28 == 0
        and 0x11A8 <= second_codepoint <= 0x11C2
    ):
        return chr(first_codepoint + second_codepoint - 0x11A7)
    return ""


def _composition_result(first: str, second: str) -> str:
    return _hangul_composition(first, second) or _canonical_composition_map().get(
        (first, second), ""
    )


def _advance_composition_tail(tail: str, decomposition: str) -> str:
    for char in decomposition:
        if unicodedata.combining(char) != 0:
            tail = ""
        else:
            composite = _composition_result(tail, char) if tail else ""
            tail = composite or char
    return tail


def _compatibility_views(text: str) -> tuple[_NormalizedView, _NormalizedView]:
    compatibility = _normalize_with_provenance(text, "NFKC")
    decomposed = _normalize_with_provenance(
        compatibility.text,
        "NFD",
        compatibility.source_spans,
    )
    return compatibility, decomposed


def _raw_span_for_output(
    raw: str,
    view: _NormalizedView,
    start: int,
    end: int,
) -> tuple[int, int, str]:
    indices: set[int] = set()
    for span in view.source_spans[start:end]:
        indices.update((span.start, span.end))
    raw_start = min(indices)
    raw_end = max(indices)
    return raw_start, raw_end, raw[raw_start:raw_end]


def _canonical_input_with_replacements(
    text: str,
    replacements: dict[int, int],
) -> str:
    """Replace nonoverlapping raw spans with apostrophes in one linear pass."""
    if not replacements:
        return text
    parts: list[str] = []
    cursor = 0
    for start in range(len(text)):
        end = replacements.get(start)
        if end is None or start < cursor:
            continue
        parts.extend((text[cursor:start], "'"))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def _is_unicode_word_continuation(char: str) -> bool:
    category = unicodedata.category(char)
    return category[0] in {"L", "N", "M"} or category == "Pc"


def _is_complete_unicode_token(text: str, match: re.Match[str]) -> bool:
    if match.start() > 0 and _is_unicode_word_continuation(text[match.start() - 1]):
        return False
    return not (
        match.end() < len(text) and _is_unicode_word_continuation(text[match.end()])
    )


def _has_complete_contraction_candidate(text: str) -> bool:
    return any(
        _is_complete_unicode_token(text, match)
        for pattern in _CONTRACTION_SEPARATOR_PATTERNS
        for match in pattern.finditer(text)
    )


def _analyze_contraction_provenance(text: str) -> _ContractionProvenance:
    """Validate raw separators behind compatibility-normalized contractions.

    The whole candidate separator must be one declared apostrophe equivalent,
    or the exact SPACE+COMBINING ACUTE compatibility expansion of U+00B4.
    Candidate skeletons are found in the same NFKC space used by classification.
    Every normalized output character retains its raw source indices, so the
    separator decision uses original code points rather than lossy output.

    A linear constant-input normalization scan rejects canonical segments over
    the conservative bound before whole-string NFKC. For bounded segments,
    provenance construction is O(input + normalized output): it normalizes each
    disjoint starter/combining or composition segment once and retains one shared
    raw interval per segment. Replies beyond the segment bound fail closed.
    """
    if not _normalization_segments_are_bounded(text):
        return _ContractionProvenance(False, text, normalization_rejected=True)
    compatibility_text = unicodedata.normalize("NFKC", text)
    decomposed_text = unicodedata.normalize("NFD", compatibility_text)
    if not (
        _has_complete_contraction_candidate(compatibility_text)
        or _has_complete_contraction_candidate(decomposed_text)
    ):
        return _ContractionProvenance(False, text)

    compatibility, decomposed = _compatibility_views(text)
    has_unsupported_separator = False
    replacements: dict[int, int] = {}
    for view in (compatibility, decomposed):
        for pattern in _CONTRACTION_SEPARATOR_PATTERNS:
            for match in pattern.finditer(view.text):
                if not _is_complete_unicode_token(view.text, match):
                    continue
                _, _, raw_separator = _raw_span_for_output(
                    text,
                    view,
                    *match.span("separator"),
                )
                if raw_separator == _DECOMPOSED_SPACING_ACUTE:
                    continue
                if (
                    len(raw_separator) == 1
                    and raw_separator in _SUPPORTED_APOSTROPHE_SEPARATORS
                ):
                    continue
                has_unsupported_separator = True

    for pattern in _CONTRACTION_SEPARATOR_PATTERNS:
        for match in pattern.finditer(compatibility.text):
            if not _is_complete_unicode_token(compatibility.text, match):
                continue
            raw_start, raw_end, raw_separator = _raw_span_for_output(
                text, compatibility, *match.span("separator")
            )
            if raw_separator == _DECOMPOSED_SPACING_ACUTE:
                replacements[raw_start] = raw_end
    canonical_input = _canonical_input_with_replacements(text, replacements)
    return _ContractionProvenance(has_unsupported_separator, canonical_input)


def classify_confirm_reply(text: str) -> str:
    """Return 'yes' | 'no' | 'unclear'."""
    # Capture provenance before shared NFKC can erase or transform it. The
    # scanner's NFD view also covers callers that already normalized input.
    raw = text or ""
    provenance = _analyze_contraction_provenance(raw)
    if provenance.normalization_rejected:
        return "unclear"
    t = normalize_for_match(provenance.canonical_input)
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
    if provenance.has_unsupported_separator:
        return "unclear"
    for a in sorted(AFFIRM, key=len, reverse=True):
        if t == a or t.startswith(a + " ") or t.endswith(" " + a):
            return "yes"
    return "unclear"

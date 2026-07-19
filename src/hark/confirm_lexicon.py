"""Spoken confirmation / cancel lexicon for ask --confirm."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass

_PUNCTUATION = re.compile(r"[^\w\s']+", re.UNICODE)
_CONFIRM_WHITESPACE = re.compile(r"\s+")
_AFFIRMATIVE_IDIOMS = frozenset({"yes why not"})
_SUPPORTED_APOSTROPHE_SEPARATORS = frozenset(
    {
        "'",
        "`",  # grave accent / common STT substitution
        "\u00b4",  # acute accent
        "\u02b9",  # modifier letter prime
        "\u02bc",  # modifier letter apostrophe
        "\u1fef",  # Greek varia (canonically equivalent to grave)
        "\u2018",  # left single quotation mark
        "\u2019",  # right single quotation mark
        "\u201b",  # single high-reversed-9 quotation mark
        "\u2032",  # prime
        "\uff40",  # fullwidth grave accent
    }
)
# U+02BB and underscore are deliberately malformed contraction separators, but
# Unicode classifies them as Lm/Pc word bases.  Keep these pinned controls in the
# malformed-separator path instead of treating them as ordinary multilingual text.
_DECLARED_MALFORMED_WORD_BASE_SEPARATORS = frozenset({"_", "\u02bb"})
# These separator-shaped controls remain fail-closed when combined with raw
# whitespace even though that whitespace is not candidate-owned.  They are a
# separate malformed-apostrophe policy, not permission for the direct matcher
# to reconnect arbitrary evidence across a later prose boundary.
_DECLARED_MALFORMED_APOSTROPHE_SEPARATORS = frozenset({"\u2033"})
_CONFIRM_APOSTROPHE_TRANSLATION = str.maketrans(
    {variant: "'" for variant in _SUPPORTED_APOSTROPHE_SEPARATORS}
)
_DECOMPOSED_SPACING_ACUTE = " \u0301"
_NORMALIZED_ALPHABETIC_COMPATIBILITY_WHITESPACE = tuple(
    unicodedata.normalize("NFKD", source) for source in ("\ufdfa", "\ufdfb")
)
# This limits one canonical ordering/composition segment, not transcript length.
# Human speech text should never need hundreds of marks on one starter; bounding
# the segment avoids the quadratic worst case in CPython's Unicode normalizer.
_MAX_NORMALIZATION_SEGMENT_CHARS = 256

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
class _RawProjection:
    text: str
    raw_indices: tuple[int, ...]
    compatibility_sources: tuple[bool, ...]


@dataclass(frozen=True)
class _ProjectionFacts:
    """Constant-time boundary and raw-provenance queries for one projection."""

    word_base_before: tuple[bool, ...]
    word_base_at_or_after: tuple[bool, ...]
    raw_source_is_whitespace: tuple[bool, ...]
    raw_compatibility_prefix: tuple[int, ...]
    whitespace_prefix: tuple[int, ...]
    significant_prefix: tuple[int, ...]
    nonalphabetic_significant_prefix: tuple[int, ...]


@dataclass(frozen=True)
class _ContractionProvenance:
    has_unsupported_separator: bool
    canonical_input: str
    normalization_rejected: bool = False


@dataclass
class _NormalizedBridgeCandidate:
    """Constant-space state for one normalization-closed bridge candidate."""

    left: tuple[int, int] | None = None
    last_raw_whitespace: int | None = None
    alpha_seen: bool = False

    def start(
        self,
        left: tuple[int, int] | None,
        *,
        raw_whitespace: int | None = None,
    ) -> None:
        self.left = left
        self.last_raw_whitespace = raw_whitespace
        self.alpha_seen = False

    def observe_previous(self, text: str, index: int) -> None:
        if self.left is None or index <= self.left[1]:
            return
        previous = text[index - 1]
        if previous.isalpha():
            self.alpha_seen = True

    def cross_raw_whitespace(
        self,
        index: int,
        *,
        belongs_to_initial_boundary: bool,
    ) -> None:
        if self.left is None:
            return
        # The first raw space can begin the externally normalized fallback.
        # Every later raw-space source ends ownership. Spaces projected inside
        # U+FDFA/U+FDFB never enter this method because their attributed raw
        # source is the same non-whitespace code point validated at start().
        if not belongs_to_initial_boundary:
            self.start(None)
        else:
            self.last_raw_whitespace = index


def _direct_composition_result(first: str, second: str) -> str:
    """Resolve one bounded composition without normalizing attacker-sized text."""
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


def _build_raw_projection(text: str) -> _RawProjection | None:
    """Return a raw-preserving NFKD view, or ``None`` for an oversized segment.

    Per-codepoint NFKD exposes compatibility expansions without allowing them to
    erase the raw character that produced them. Comparing it with per-codepoint
    NFD detects recursive compatibility ancestry, including sources whose direct
    decomposition is canonical. The same pass bounds canonical ordering and
    composition segments before the classifier performs whole-input NFKC. All
    normalization calls here receive one or two input code points.
    """
    segment_size = 0
    composition_tail = ""
    output: list[str] = []
    raw_indices: list[int] = []
    compatibility_sources: list[bool] = []
    for raw_index, char in enumerate(text):
        decomposition = unicodedata.normalize("NFKD", char)
        canonical_decomposition = unicodedata.normalize("NFD", char)
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
            return None
        output.append(decomposition)
        raw_indices.extend([raw_index] * len(decomposition))
        compatibility_sources.append(decomposition != canonical_decomposition)
        composition_tail = _advance_direct_composition_tail(
            composition_tail, decomposition
        )
    return _RawProjection(
        "".join(output), tuple(raw_indices), tuple(compatibility_sources)
    )


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


def _is_confirmation_word_base(char: str) -> bool:
    category = unicodedata.category(char)
    return category[0] in {"L", "N"} or category == "Pc"


def _is_boundary_transparent(char: str) -> bool:
    category = unicodedata.category(char)
    return category[0] == "M" or category == "Cf"


def _build_projection_facts(raw: str, projection: _RawProjection) -> _ProjectionFacts:
    """Precompute facts that bridge scanning otherwise has to rediscover.

    Transparent M/Cf runs can be attacker-sized. Carrying the nearest opaque
    category in both directions makes every candidate boundary check O(1).
    The raw prefix table similarly attributes every source character in a
    projection span without rescanning overlapping candidates. Per-projection
    raw-whitespace flags distinguish a literal prose boundary from whitespace
    emitted by an effective compatibility source, including sources such as
    non-breaking space that are themselves classified as whitespace.
    """
    projection_text = projection.text
    raw_word_base_before: list[bool] = []
    previous_raw_opaque_is_word_base = False
    for char in raw:
        raw_word_base_before.append(previous_raw_opaque_is_word_base)
        if not _is_boundary_transparent(char):
            previous_raw_opaque_is_word_base = _is_confirmation_word_base(char)
    raw_word_base_before.append(previous_raw_opaque_is_word_base)

    raw_word_base_at_or_after = [False] * (len(raw) + 1)
    next_raw_opaque_is_word_base = False
    for raw_index in range(len(raw) - 1, -1, -1):
        char = raw[raw_index]
        if not _is_boundary_transparent(char):
            next_raw_opaque_is_word_base = _is_confirmation_word_base(char)
        raw_word_base_at_or_after[raw_index] = next_raw_opaque_is_word_base

    word_base_before = [False] * (len(projection_text) + 1)
    previous_opaque_is_word_base = False
    for index, char in enumerate(projection_text):
        raw_index = projection.raw_indices[index]
        if index == 0 or raw_index != projection.raw_indices[index - 1]:
            # Compatibility decomposition may end in punctuation even though
            # its raw source is a letter/number. At a raw-source boundary, the
            # source category—not an emitted artifact—owns token attachment.
            previous_opaque_is_word_base = raw_word_base_before[raw_index]
        word_base_before[index] = previous_opaque_is_word_base
        if not _is_boundary_transparent(char):
            previous_opaque_is_word_base = _is_confirmation_word_base(char)
    word_base_before[-1] = raw_word_base_before[-1]

    word_base_at_or_after = [False] * (len(projection_text) + 1)
    next_opaque_is_word_base = False
    for index in range(len(projection_text) - 1, -1, -1):
        raw_index = projection.raw_indices[index]
        if (
            index == len(projection_text) - 1
            or raw_index != projection.raw_indices[index + 1]
        ):
            next_opaque_is_word_base = raw_word_base_at_or_after[raw_index + 1]
        char = projection_text[index]
        if not _is_boundary_transparent(char):
            next_opaque_is_word_base = _is_confirmation_word_base(char)
        if index == 0 or raw_index != projection.raw_indices[index - 1]:
            next_opaque_is_word_base = raw_word_base_at_or_after[raw_index]
        word_base_at_or_after[index] = next_opaque_is_word_base

    raw_compatibility_prefix = [0]
    for is_compatibility_source in projection.compatibility_sources:
        raw_compatibility_prefix.append(
            raw_compatibility_prefix[-1] + is_compatibility_source
        )
    # Preserve the raw code point's whitespace identity independently of its
    # compatibility provenance.  A compatibility space such as NBSP is still a
    # real prose boundary when it is a later, distinct raw source.  Conversely,
    # spaces merely emitted inside the declared U+FDFA/U+FDFB expansion inherit
    # a non-whitespace raw source and remain internal to that exact source span.
    raw_source_is_whitespace = tuple(
        raw[raw_index].isspace() for raw_index in projection.raw_indices
    )
    whitespace_prefix = [0]
    significant_prefix = [0]
    nonalphabetic_significant_prefix = [0]
    for char in projection_text:
        whitespace_prefix.append(whitespace_prefix[-1] + char.isspace())
        is_significant = not _is_boundary_transparent(char)
        significant_prefix.append(significant_prefix[-1] + is_significant)
        nonalphabetic_significant_prefix.append(
            nonalphabetic_significant_prefix[-1]
            + (is_significant and not char.isalpha())
        )
    return _ProjectionFacts(
        tuple(word_base_before),
        tuple(word_base_at_or_after),
        raw_source_is_whitespace,
        tuple(raw_compatibility_prefix),
        tuple(whitespace_prefix),
        tuple(significant_prefix),
        tuple(nonalphabetic_significant_prefix),
    )


def _is_complete_confirmation_span(
    facts: _ProjectionFacts, start: int, end: int
) -> bool:
    return not facts.word_base_before[start] and not facts.word_base_at_or_after[end]


def _ascii_literal_at(text: str, start: int, literal: str) -> bool:
    end = start + len(literal)
    return end <= len(text) and text[start:end].lower() == literal


def _normalized_compatibility_whitespace_source_at(
    projection: _RawProjection, start: int
) -> int | None:
    """Raw-source index owning one complete declared expansion, if any."""
    for expansion in _NORMALIZED_ALPHABETIC_COMPATIBILITY_WHITESPACE:
        end = start + len(expansion)
        if not projection.text.startswith(expansion, start) or end > len(
            projection.text
        ):
            continue
        raw_index = projection.raw_indices[start]
        if (
            projection.raw_indices[end - 1] == raw_index
            and projection.compatibility_sources[raw_index]
        ):
            return raw_index
    return None


def _raw_material_for_projection_span(
    raw: str,
    projection: _RawProjection,
    start: int,
    end: int,
) -> tuple[int, int, str]:
    raw_start = projection.raw_indices[start]
    raw_end = projection.raw_indices[end - 1] + 1
    return raw_start, raw_end, raw[raw_start:raw_end]


def _is_supported_apostrophe_material(raw_material: str) -> bool:
    """Whether raw separator material is one declared apostrophe equivalent."""
    return raw_material == _DECOMPOSED_SPACING_ACUTE or (
        len(raw_material) == 1 and raw_material in _SUPPORTED_APOSTROPHE_SEPARATORS
    )


def _projection_span_is_supported_apostrophe_material(
    raw: str,
    projection: _RawProjection,
    start: int,
    end: int,
) -> bool:
    """Check declared separator ownership without slicing an unbounded bridge."""
    raw_start = projection.raw_indices[start]
    raw_end = projection.raw_indices[end - 1] + 1
    raw_length = raw_end - raw_start
    if raw_length == 1:
        return raw[raw_start] in _SUPPORTED_APOSTROPHE_SEPARATORS
    return raw_length == len(_DECOMPOSED_SPACING_ACUTE) and all(
        raw[raw_start + offset] == expected
        for offset, expected in enumerate(_DECOMPOSED_SPACING_ACUTE)
    )


def _has_observable_normalized_bridge_evidence(
    facts: _ProjectionFacts, start: int, end: int
) -> bool:
    """Recognize bridge evidence that survives external normalization.

    Fully collapsed alphabetic compatibility material is indistinguishable from
    literal alphabetic input and must classify identically. Marks, Format code
    points, and nonalphabetic expansion material remain observable. Raw source
    provenance is checked separately before this fallback.
    """
    whitespace_count = facts.whitespace_prefix[end] - facts.whitespace_prefix[start]
    significant_start = facts.significant_prefix[start]
    significant_end = facts.significant_prefix[end]
    significant_length = significant_end - significant_start
    # M*/Cf material remains observable even when compatibility normalization
    # also emitted literal whitespace.  Whitespace alone is not evidence: raw
    # prose boundaries must continue to separate ordinary words.
    if significant_length != end - start:
        return True
    nonalphabetic_prefix = facts.nonalphabetic_significant_prefix
    nonalphabetic_count = nonalphabetic_prefix[end] - nonalphabetic_prefix[start]
    return nonalphabetic_count > whitespace_count


def _projection_span_has_compatibility_source(
    projection: _RawProjection,
    facts: _ProjectionFacts,
    start: int,
    end: int,
) -> bool:
    raw_start = projection.raw_indices[start]
    raw_end = projection.raw_indices[end - 1] + 1
    prefix = facts.raw_compatibility_prefix
    return prefix[raw_end] != prefix[raw_start]


def _projection_span_owns_raw_source_edges(
    projection: _RawProjection,
    start: int,
    end: int,
) -> bool:
    """Whether a projected span consumes its first and last raw sources."""
    raw_indices = projection.raw_indices
    starts_at_raw_source = start == 0 or raw_indices[start - 1] != raw_indices[start]
    ends_at_raw_source = (
        end == len(raw_indices) or raw_indices[end] != raw_indices[end - 1]
    )
    return starts_at_raw_source and ends_at_raw_source


def _raw_whitespace_belongs_to_initial_boundary(
    projection: _RawProjection,
    facts: _ProjectionFacts,
    initial_index: int | None,
    current_index: int,
) -> bool:
    """Whether two projected spaces belong to one initial raw source.

    A separator may start at one raw whitespace source.  Any distinct later
    whitespace source is a prose boundary and expires the candidate.  Spaces
    merely projected from a non-whitespace compatibility source (for example
    the internal spaces of U+FDFA/U+FDFB) inherit that non-whitespace raw
    identity and therefore never reach this ownership check as raw whitespace.
    """
    return bool(
        initial_index is not None
        and facts.raw_source_is_whitespace[initial_index]
        and facts.raw_source_is_whitespace[current_index]
        and projection.raw_indices[initial_index]
        == projection.raw_indices[current_index]
    )


def _assess_direct_separator_span(
    projection: _RawProjection,
    facts: _ProjectionFacts,
    start: int,
    end: int,
) -> tuple[bool, bool]:
    """Return ``(accepted, expired_at_later_whitespace)`` for a direct span.

    The regular expression is deliberately ASCII-shaped so it can expose
    compatibility-expanded skeletons, but that also means Unicode word bases
    appear to be separator characters.  Let the provenance-aware bridge scan
    decide those cases, including whether a later raw whitespace ends the
    candidate.  The declared malformed Lm/Pc controls must remain separator
    evidence rather than becoming ordinary prose.
    """
    # A raw whitespace source belongs to this candidate only when it begins the
    # separator.  Do not retroactively claim the first whitespace encountered
    # after other evidence: the scanner expires its candidate at that boundary.
    initial_raw_whitespace = start if facts.raw_source_is_whitespace[start] else None
    for index in range(start, end):
        if facts.raw_source_is_whitespace[index]:
            if initial_raw_whitespace is None or not (
                _raw_whitespace_belongs_to_initial_boundary(
                    projection,
                    facts,
                    initial_raw_whitespace,
                    index,
                )
            ):
                return False, True
        projected_character = projection.text[index]
        if (
            _is_confirmation_word_base(projected_character)
            and projected_character not in _DECLARED_MALFORMED_WORD_BASE_SEPARATORS
        ):
            return False, False
    return True, False


def _direct_separator_span_respects_boundaries(
    projection: _RawProjection,
    facts: _ProjectionFacts,
    start: int,
    end: int,
) -> bool:
    """Keep the ASCII-shaped regex inside the scanner's boundary policy."""
    accepted, _ = _assess_direct_separator_span(projection, facts, start, end)
    return accepted


def _projection_span_contains_declared_apostrophe_material(
    raw: str,
    projection: _RawProjection,
    start: int,
    end: int,
) -> bool:
    """Recognize explicit apostrophe-shaped material without bridge slicing.

    Composite apostrophe separators remain fail-closed independently of raw
    whitespace ownership.  Keeping that policy separate prevents the direct
    matcher from treating an arbitrary later whitespace as an initial boundary.
    """
    raw_start = projection.raw_indices[start]
    raw_end = projection.raw_indices[end - 1] + 1
    for raw_index in range(raw_start, raw_end):
        char = raw[raw_index]
        if (
            char in _SUPPORTED_APOSTROPHE_SEPARATORS
            or char in _DECLARED_MALFORMED_WORD_BASE_SEPARATORS
            or char in _DECLARED_MALFORMED_APOSTROPHE_SEPARATORS
        ):
            return True
    return False


def _iter_compatibility_bridge_spans(
    raw: str, projection: _RawProjection, facts: _ProjectionFacts
) -> Iterator[tuple[int, int]]:
    """Yield compatibility-origin bridges attached within one raw prose token.

    Keep the earliest unresolved start until a real raw-whitespace boundary so
    a later same-left candidate cannot shadow prior compatibility provenance.
    The latest start is retained separately for observable normalized-evidence
    checks.
    """
    text = projection.text
    for left, right in _CONTRACTION_PARTS:
        earliest_left: tuple[int, int] | None = None
        latest_left: tuple[int, int] | None = None
        normalized = _NormalizedBridgeCandidate()
        for index in range(len(text)):
            normalized.observe_previous(text, index)
            if facts.raw_source_is_whitespace[index]:
                is_initial_normalized_boundary = (
                    normalized.left is not None
                    and _raw_whitespace_belongs_to_initial_boundary(
                        projection,
                        facts,
                        normalized.last_raw_whitespace,
                        index,
                    )
                )
                if not is_initial_normalized_boundary:
                    earliest_left = None
                    latest_left = None
                normalized.cross_raw_whitespace(
                    index,
                    belongs_to_initial_boundary=is_initial_normalized_boundary,
                )
            left_end = index + len(left)
            is_left_start = (
                _ascii_literal_at(text, index, left)
                and left_end < len(text)
                and not facts.word_base_before[index]
            )
            if is_left_start and facts.raw_source_is_whitespace[left_end]:
                # This is not an ordinary in-token candidate. Retain it only
                # for the normalized compatibility-whitespace fallback below.
                normalized.start((index, left_end), raw_whitespace=left_end)
                raw_source = projection.raw_indices[left_end]
                if projection.compatibility_sources[raw_source]:
                    # A compatibility-space code point at the first separator
                    # boundary is still observable raw provenance. Keep the
                    # ordinary candidate until a later raw-space source ends it.
                    latest_left = (index, left_end)
                    if earliest_left is None:
                        earliest_left = latest_left
            elif is_left_start:
                latest_left = (index, left_end)
                if earliest_left is None:
                    earliest_left = latest_left
                # Exactly two effective compatibility-whitespace sources begin
                # with alphabetic material before their internal spaces. Keep
                # those normalization-equivalent strings fail-closed without
                # treating every ordinary non-ASCII word as provenance.
                compatibility_whitespace_raw_source = (
                    _normalized_compatibility_whitespace_source_at(projection, left_end)
                )
                normalized.start(
                    latest_left
                    if compatibility_whitespace_raw_source is not None
                    else None,
                )
            active_left = normalized.left or latest_left
            if index <= (active_left[1] if active_left is not None else -1):
                continue
            right_end = index + len(right)
            if not _ascii_literal_at(text, index, right):
                continue
            if (
                facts.word_base_at_or_after[right_end]
                and _projection_span_owns_raw_source_edges(projection, index, right_end)
                and not _projection_span_has_compatibility_source(
                    projection, facts, index, right_end
                )
            ):
                # The first matching right fragment owns the candidate's raw
                # suffix boundary.  If that boundary is still inside a word,
                # the candidate is an embedded control; a later literal or
                # compatibility-emitted ``t`` cannot be reselected as a new
                # terminus for the same left side (for example ``can__tt``).
                earliest_left = None
                latest_left = None
                normalized.start(None)
                continue
            if earliest_left is not None and latest_left is not None:
                if not facts.raw_source_is_whitespace[
                    index - 1
                ] and _is_complete_confirmation_span(facts, latest_left[0], right_end):
                    earliest_separator_span = (earliest_left[1], index)
                    earliest_is_supported_apostrophe = (
                        _projection_span_is_supported_apostrophe_material(
                            raw, projection, *earliest_separator_span
                        )
                    )
                    if not earliest_is_supported_apostrophe:
                        if _projection_span_has_compatibility_source(
                            projection, facts, *earliest_separator_span
                        ):
                            yield earliest_separator_span
                            continue
                    latest_separator_span = (latest_left[1], index)
                    latest_is_unsupported_evidence = False
                    if latest_separator_span != earliest_separator_span:
                        latest_is_unsupported_evidence = (
                            not _projection_span_is_supported_apostrophe_material(
                                raw, projection, *latest_separator_span
                            )
                            and _has_observable_normalized_bridge_evidence(
                                facts, *latest_separator_span
                            )
                        )
                    if (
                        not earliest_is_supported_apostrophe
                        and _has_observable_normalized_bridge_evidence(
                            facts, *earliest_separator_span
                        )
                    ) or latest_is_unsupported_evidence:
                        yield earliest_separator_span
                        continue
            # Compatibility whitespace becomes indistinguishable from literal
            # prose whitespace after NFKC/NFKD. Retain a separate fallback from
            # the last left side, but inspect observable evidence across the
            # complete candidate bridge: a later collapsed compatibility space
            # must not discard an earlier surviving M*/Cf/nonalphabetic source.
            # The final-segment and whole-token gates still keep ordinary prose
            # whitespace from reconnecting unrelated words.
            if (
                normalized.left is not None
                and normalized.last_raw_whitespace is not None
                and index > normalized.last_raw_whitespace + 1
                and not facts.raw_source_is_whitespace[index - 1]
                and normalized.alpha_seen
                and _is_complete_confirmation_span(facts, normalized.left[0], right_end)
                and _has_observable_normalized_bridge_evidence(
                    facts, normalized.left[1], index
                )
            ):
                yield (normalized.left[1], index)


def _analyze_contraction_provenance(text: str) -> _ContractionProvenance:
    """Validate raw material inside compatibility-expanded contractions.

    The raw-preserving NFKD projection exposes compatibility characters before
    they can erase a refusal skeleton. Candidate material must be one declared
    apostrophe equivalent or the exact SPACE+COMBINING ACUTE normalized form of
    U+00B4. Projection and scanning are O(input + projected output); an oversized
    canonical segment fails closed before whole-input normalization.
    """
    projection = _build_raw_projection(text)
    if projection is None:
        return _ContractionProvenance(False, text, normalization_rejected=True)
    facts = _build_projection_facts(text, projection)
    has_unsupported_separator = False
    replacements: dict[int, int] = {}

    def validate_material(separator_start: int, separator_end: int) -> None:
        nonlocal has_unsupported_separator
        raw_start, raw_end, raw_material = _raw_material_for_projection_span(
            text, projection, separator_start, separator_end
        )
        if raw_material == _DECOMPOSED_SPACING_ACUTE:
            replacements[raw_start] = raw_end
            return
        if _is_supported_apostrophe_material(raw_material):
            return
        has_unsupported_separator = True

    for pattern in _CONTRACTION_SEPARATOR_PATTERNS:
        for match in pattern.finditer(projection.text):
            separator_start, separator_end = match.span("separator")
            if not _is_complete_confirmation_span(facts, match.start(), match.end()):
                continue
            direct_accepted, expired_at_later_whitespace = (
                _assess_direct_separator_span(
                    projection,
                    facts,
                    separator_start,
                    separator_end,
                )
            )
            if direct_accepted:
                validate_material(separator_start, separator_end)
            elif expired_at_later_whitespace and (
                _projection_span_contains_declared_apostrophe_material(
                    text,
                    projection,
                    separator_start,
                    separator_end,
                )
            ):
                # Composite apostrophe-like separators remain a dedicated
                # fail-closed policy even when a later prose boundary means the
                # ordinary bridge candidate itself has expired.
                has_unsupported_separator = True

    for _ in _iter_compatibility_bridge_spans(text, projection, facts):
        has_unsupported_separator = True
        break
    canonical_input = _canonical_input_with_replacements(text, replacements)
    return _ContractionProvenance(has_unsupported_separator, canonical_input)


def _normalize_confirmation_for_match(text: str) -> str:
    text = text.translate(_CONFIRM_APOSTROPHE_TRANSLATION)
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_CONFIRM_APOSTROPHE_TRANSLATION)
    return _CONFIRM_WHITESPACE.sub(" ", text.lower().strip())


def _is_format_or_mark(char: str) -> bool:
    """True for Unicode Format (Cf) or Mark (M*) code points."""
    category = unicodedata.category(char)
    return category == "Cf" or category.startswith("M")


def _strip_format_and_marks(text: str) -> str:
    """Drop Format/Mark material so it cannot split confirmation tokens (B159).

    Punctuation normalization maps non-word characters to spaces. Interior
    Format characters such as U+200B ZERO WIDTH SPACE would otherwise break a
    multi-letter NEGATE token (``c\\u200bancel`` → ``c ancel``) while a leading
    affirmative still wins and authorizes R2.

    NFKD first so combining marks that ``_normalize_confirmation_for_match``
    (NFKC) composed into a single letter (``c\\u0301`` → ``ć``) are
    re-separated and removed.
    Removal is linear in the transcript length; whole-word matching still
    applies to the remaining text (no substring false positives).
    """
    if not text:
        return text
    # NFKD undoes composition from the earlier NFKC pass without reintroducing
    # compatibility characters already folded (fullwidth, ligatures, …).
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not _is_format_or_mark(ch))


def _contains_whole_phrase(padded: str, phrase: str) -> bool:
    """True if ``phrase`` appears as whole words inside space-padded text."""
    return f" {phrase} " in padded


def _strip_balanced_token_apostrophe_quotes(text: str) -> str:
    """Peel balanced apostrophe quotes around whole tokens (B157).

    Apostrophe-family glyphs are preserved so contractions such as ``don't``
    remain meaningful. That same preservation leaves STT/transcript quotation
    marks like ``'can't'`` attached to the token, which blocks whole-word
    ``NEGATE`` matching and can fall through to affirmative approval.

    Double quotes and parentheses are already removed by the generic
    punctuation stripper. Only peel when the *same* whole token both starts and
    ends with ``'`` so internal contraction apostrophes and unbalanced noise are
    left alone.
    """
    tokens: list[str] = []
    for token in text.split():
        while len(token) >= 2 and token[0] == "'" and token[-1] == "'":
            token = token[1:-1]
        if token:
            tokens.append(token)
    return " ".join(tokens)


def _match_ready_confirmation_text(text: str) -> str:
    """Punctuation-fold and peel quotes so lexicon matching sees whole tokens."""
    # STT commonly preserves sentence-final punctuation. Confirmation is a
    # small spoken lexicon, so punctuation is non-semantic while apostrophes
    # remain meaningful for negatives such as ``don't``.
    text = " ".join(_PUNCTUATION.sub(" ", text).split())
    # After apostrophe normalization, peel balanced whole-token quotes so a
    # complete negative contraction such as ``'can't'`` can match NEGATE
    # without weakening whole-token boundaries (B157).
    return _strip_balanced_token_apostrophe_quotes(text)


def _reply_has_negate(text: str) -> bool:
    """True if ``text`` contains a whole-token NEGATE hit."""
    if not text:
        return False
    if text in NEGATE:
        return True
    padded = f" {text} "
    return any(_contains_whole_phrase(padded, n) for n in NEGATE)


def classify_confirm_reply(text: str) -> str:
    """Classify a raw STT transcript as ``yes``, ``no``, or ``unclear``.

    Callers must pass the provider transcript before compatibility
    normalization. Once NFKC/NFKD collapses compatibility characters to literal
    alphabetic text, source provenance is unrecoverable and the two inputs must
    classify identically unless observable marks/Format/separators remain.

    Only unambiguous immediate approval is ``yes``. Negations win over
    affirmatives; defer/condition/hedge markers fail closed to ``unclear``.
    """
    # Capture raw structure before compatibility normalization can erase it.
    raw = text or ""
    provenance = _analyze_contraction_provenance(raw)
    if provenance.normalization_rejected:
        return "unclear"
    t = _normalize_confirmation_for_match(provenance.canonical_input)
    # Pre-strip form: B151 unsupported bridges must not be reclassified as a
    # clean refuse solely because Format/Mark collapse invents a NEGATE skeleton.
    t_pre = _match_ready_confirmation_text(t)
    # Collapse Format/Mark before punctuation→space so interior ZWSP/marks
    # cannot fracture multi-letter refuse tokens (B159).
    t = _match_ready_confirmation_text(_strip_format_and_marks(t))
    if not t:
        return "unclear"
    if t in AFFIRM:
        return "yes"
    if t in _AFFIRMATIVE_IDIOMS:
        return "yes"
    if _reply_has_negate(t):
        # Independent pre-strip refuses (e.g. ``cancel`` beside a malformed
        # contraction) still win. Strip-only refuse skeletons stay unclear.
        if provenance.has_unsupported_separator and not _reply_has_negate(t_pre):
            return "unclear"
        return "no"
    if provenance.has_unsupported_separator:
        return "unclear"
    # Defer, condition, and hedge markers block approval even when an
    # affirmative token is present at the start or end (B142 punctuated + B148).
    padded = f" {t} "
    for cue in sorted(_DEFER_CONDITION_HEDGE, key=len, reverse=True):
        if _contains_whole_phrase(padded, cue):
            return "unclear"
    for a in sorted(AFFIRM, key=len, reverse=True):
        if t == a or t.startswith(a + " ") or t.endswith(" " + a):
            return "yes"
    return "unclear"

from __future__ import annotations

import sys
import unicodedata
from functools import cache

from confirm_security_corpus import (
    NORMATIVE_CONTRACTIONS,
    NORMATIVE_NEGATION_CORPUS,
)
from hark.confirm_lexicon import NEGATE as PRODUCTION_NEGATE


# Fail before generating Unicode variants if production drops a normative
# refusal. Case derivation below remains wholly fixed-test-data driven.
assert NORMATIVE_NEGATION_CORPUS <= PRODUCTION_NEGATE
assert "don't" in PRODUCTION_NEGATE

APOSTROPHE_VARIANTS = (
    "\u2018",
    "\u02bc",
    "\u00b4",
    "\uff40",
    "\u1fef",
    "\u201b",
    "\u2032",
    "\u02b9",
)
NORMALIZATION_FORMS = (None, "NFC", "NFD", "NFKC", "NFKD")
PROVENANCE_PRESERVING_NORMALIZATION_FORMS = (None, "NFC", "NFD")
UNSUPPORTED_IN_WORD_FRAGMENTS = (
    "\u00a8",
    "\u2033",
    "\uff3f",
    "\u0301",
    "\u02bb",
    "''",
    "\u2032\u2032",
)
COMPOSITE_CONTRACTION_SEPARATORS = (
    " \u2019",
    "\u2019 ",
    " _",
    "_ ",
    " \u02bb",
    "\u02bb ",
    " \u2033",
    "\u2033 ",
    "\u2019\u02bb",
)
ORDINARY_UNICODE_AFFIRMATIONS = (
    "yes I approve naïvely",
    "yes mañana",
    "yes résumé",
    "yes Ελληνικά",
)
COMPATIBILITY_PROSE_AFFIRMATIONS = (
    "yes I can\u2122 make it",
    "yes I can\u2122 recommend it",
    "yes I can\u2122 certainly do it",
    "yes I can\u2122 help with that",
    "yes I can\u2122 send it",
    "yes I can\u2122 and will do it",
    "yes I can\u2122 take part",
    "yes I can\u2122 make a draft",
)
BENIGN_PROSE_AFFIRMATIONS = (
    "yes candlelight",
    "yes canonical text",
    "yes Canada is great",
    "yes wonderful thought",
    "yes donate it",
    "yes donut",
    "yes wonderment",
    *COMPATIBILITY_PROSE_AFFIRMATIONS,
    "YES CANDLELIGHT",
    "YES CANONICAL TEXT",
    "YES CANADA IS GREAT",
    "YES WONDERFUL THOUGHT",
    "YES DONATE IT",
    "YES DONUT",
    "YES WONDERMENT",
)
WORD_BASE_BOUNDARY_CONTROLS = (
    "écan__t",
    "_can__t",
    "1can__t",
    "can__té",
    "can__t_",
    "can__t1",
    "can__tλ",
)
TRANSPARENT_BOUNDARY_CHARACTERS = (
    "\u0301",  # Mn combining acute
    "\u0903",  # Mc Devanagari sign visarga
    "\u20dd",  # Me combining enclosing circle
    "\ufe0f",  # variation selector
    "\u200b",  # zero-width space
    "\u200d",  # zero-width joiner
    "\u2060",  # word joiner
)
COMPATIBILITY_EXPANSION_REPRODUCTIONS = (
    "\u2122",  # TRADE MARK SIGN -> TM
    "\u2100",  # ACCOUNT OF -> a/c
    "\u33c6",  # C OVER KG -> C/kg
    "\u2474",  # PARENTHESIZED DIGIT ONE -> (1)
)
MULTISOURCE_COMPATIBILITY_REPRODUCTIONS = (
    "\u1d43\u1d47",  # modifier small a + modifier small b -> ab
    "\u1d2c\u1d2e",  # modifier capital A + modifier capital B -> AB
    "\u02b0\u02b0",  # two modifier small h sources -> hh
    "\u2122" + "\u200b" * 6,  # compatibility source plus Format padding
    "\u2122" + "\u200b" * 257,  # attacker-sized Format padding
    "\u2122" * 4,  # repeated multi-character alphabetic expansion
    "\u33a8" * 2,  # repeated mixed letter/symbol/digit expansion
    "\u00a8" * 2,  # two sources that each project compatibility whitespace
)
ALPHABETIC_TRIPLE_COMPATIBILITY_REPRODUCTIONS = (
    "\u1d43\u1d47\u02b0",  # modifier small a + b + h -> abh
    "\u1d2c\u1d2e\u1d34",  # modifier capital A + B + H -> ABH
    "\u02b0\u02b2\u02b7",  # modifier small h + j + w -> hjw
)
COMPATIBILITY_WHITESPACE_ALPHA_REPRODUCTIONS = (
    "\u00a8a",  # compatibility SPACE + mark before literal alpha
    "\u00a0a",  # compatibility whitespace source before literal alpha
    "a\u2002",  # literal alpha before compatibility whitespace source
    "\u1fee\u3396\U0001d569",  # recursive whitespace + square ml + math x
)
SHADOWED_NORMALIZED_BRIDGE_EVIDENCE = (
    ("Cf", "\u00a0\u200b\u00a0\u1d43"),
    ("Mn", "\u00a0\u0301\u00a0\u1d43"),
    ("Po", "\u00a0/\u00a0\u1d43"),
    ("So", "\u00a0\u00a9\u00a0\u1d43"),
    ("Nd", "\u00a01\u00a0\u1d43"),
)
RAW_WORD_BASE_COMPATIBILITY_PREFIXES = (
    "\u0140",  # Ll -> l + MIDDLE DOT
    "\u013f",  # Lu -> L + MIDDLE DOT
    "\u037a",  # Lm -> SPACE + COMBINING GREEK YPOGEGRAMMENI
    "\u215f",  # No -> 1 + FRACTION SLASH
)
PROSE_TRANSPARENT_SUFFIXES = (
    "\u200b",  # Cf ZERO WIDTH SPACE
    "\ufe0f",  # Mn VARIATION SELECTOR-16
    "\u2060",  # Cf WORD JOINER
)
ORDINARY_NONASCII_WORD_BASES = (
    "\u03bb",  # Ll GREEK SMALL LETTER LAMDA
    "\u044f",  # Ll CYRILLIC SMALL LETTER YA
    "\u4e2d",  # Lo CJK UNIFIED IDEOGRAPH-4E2D
    "\u3042",  # Lo HIRAGANA LETTER A
    "\u05d0",  # Lo HEBREW LETTER ALEF
)
ORDINARY_PROSE_BRIDGE_EVIDENCE = (
    ("Cf", "\u200b"),
    ("Mn", "\ufe0f"),
    ("Po", "/"),
    ("So", "\u00a9"),
)
ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES = (
    "\ufdfa",
    "\ufdfb",
)
CONTRACTION_PARTS = tuple(phrase.split("'", 1) for phrase in NORMATIVE_CONTRACTIONS)
EDGE_MATERIAL_REPRODUCTIONS = (
    "yes I can\u203ct\u0301 approve this",
    "yes I can\u203ct\ufe0f approve this",
    "yes I \u0301can\u203ct approve this",
    "yes I can\u200bt\u0301 approve this",
)
UNSUPPORTED_FULLWIDTH_SEPARATORS = (
    "__",
    "\u02bb",
    "\u2033",
    "\u2032\u2032",
    "\u2019\u02bb",
)


def fullwidth_ascii(text: str) -> str:
    return "".join(
        chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text
    )


FULLWIDTH_CONTRACTION_CASES = tuple(
    left_variant + separator + right_variant
    for left, right in CONTRACTION_PARTS
    for left_variant, right_variant in (
        (fullwidth_ascii(left), fullwidth_ascii(right)),
        (fullwidth_ascii(left), right),
        (left, fullwidth_ascii(right)),
    )
    for separator in UNSUPPORTED_FULLWIDTH_SEPARATORS
)
SUPPORTED_FULLWIDTH_CONTRACTION_CASES = tuple(
    left_variant + apostrophe + right_variant
    for left, right in CONTRACTION_PARTS
    for left_variant, right_variant in (
        (fullwidth_ascii(left), fullwidth_ascii(right)),
        (fullwidth_ascii(left), right),
        (left, fullwidth_ascii(right)),
    )
    for apostrophe in APOSTROPHE_VARIANTS
)
FULLWIDTH_WORD_BASE_BOUNDARY_CONTROLS = (
    "éｃａｎ__ｔ",
    "_ｃａｎ__ｔ",
    "ｃａｎ__ｔé",
    "ｃａｎ__ｔ_",
    "ｃａｎ__ｔ1",
)


def has_effective_compatibility_provenance(character: str) -> bool:
    """Whether recursive compatibility normalization changes this source."""
    return unicodedata.normalize("NFKD", character) != unicodedata.normalize(
        "NFD", character
    )


def is_fully_collapsed_alphabetic_material(material: str) -> bool:
    """Whether normalized material has no observable nonalphabetic evidence."""
    projection = unicodedata.normalize("NFKD", material)
    return bool(projection) and all(character.isalpha() for character in projection)


@cache
def alphanumeric_compatibility_expansions() -> tuple[str, ...]:
    """All Unicode code points whose compatibility expansion contains a word base."""
    return tuple(
        char
        for codepoint in range(sys.maxunicode + 1)
        for char in (chr(codepoint),)
        if has_effective_compatibility_provenance(char)
        and any(part.isalnum() for part in unicodedata.normalize("NFKD", char))
    )


@cache
def repeated_symbol_compatibility_bridges() -> tuple[str, ...]:
    """Two-source bridges from every whitespace-free alphanumeric symbol expansion.

    Selection is derived from Unicode provenance and output semantics rather
    than mirroring any production bridge length or alphabetic-shape heuristic.
    """
    return tuple(
        char * 2
        for codepoint in range(sys.maxunicode + 1)
        for char in (chr(codepoint),)
        for expansion in (unicodedata.normalize("NFKD", char),)
        if has_effective_compatibility_provenance(char)
        and unicodedata.category(char)[0] in {"P", "S", "M"}
        and expansion
        and not any(part.isspace() for part in expansion)
        and any(part.isalnum() for part in expansion)
    )


@cache
def recursive_alphanumeric_compatibility_sources() -> tuple[str, ...]:
    """Effective compatibility sources missed by direct decomposition tags."""
    return tuple(
        character
        for character in alphanumeric_compatibility_expansions()
        if not unicodedata.decomposition(character).startswith("<")
    )


@cache
def single_alphabetic_compatibility_sources() -> tuple[str, ...]:
    """Effective sources that normalize to one alphabetic code point."""
    return tuple(
        character
        for character in alphanumeric_compatibility_expansions()
        if len(unicodedata.normalize("NFKD", character)) == 1
        and unicodedata.normalize("NFKD", character).isalpha()
    )


@cache
def alphabetic_compatibility_triples() -> tuple[str, ...]:
    """Overlapping triples that cover every single-alphabetic source."""
    sources = single_alphabetic_compatibility_sources()
    return tuple(
        sources[index]
        + sources[(index + 1) % len(sources)]
        + sources[(index + 2) % len(sources)]
        for index in range(len(sources))
    )


@cache
def leading_compatibility_whitespace_sources() -> tuple[str, ...]:
    """Effective sources whose compatibility expansion starts with whitespace."""
    return tuple(
        character
        for character in map(chr, range(sys.maxunicode + 1))
        if has_effective_compatibility_provenance(character)
        and unicodedata.normalize("NFKD", character).startswith(" ")
    )


@cache
def trailing_compatibility_whitespace_sources() -> tuple[str, ...]:
    """Effective sources whose compatibility expansion ends with whitespace."""
    return tuple(
        character
        for character in map(chr, range(sys.maxunicode + 1))
        if has_effective_compatibility_provenance(character)
        and unicodedata.normalize("NFKD", character).endswith(" ")
    )


@cache
def compatibility_whitespace_sources() -> tuple[str, ...]:
    """Every effective source whose compatibility expansion contains whitespace."""
    return tuple(
        character
        for character in map(chr, range(sys.maxunicode + 1))
        if has_effective_compatibility_provenance(character)
        and any(part.isspace() for part in unicodedata.normalize("NFKD", character))
    )


@cache
def boundary_ending_compatibility_expansions() -> tuple[str, ...]:
    """Compatibility sources whose expansion can introduce another token start."""
    sources = []
    for character in alphanumeric_compatibility_expansions():
        expansion = unicodedata.normalize("NFKD", character)
        opaque = tuple(
            char
            for char in expansion
            if not unicodedata.category(char).startswith("M")
            and unicodedata.category(char) != "Cf"
        )
        if not opaque:
            continue
        final_category = unicodedata.category(opaque[-1])
        final_is_word_base = final_category[0] in {"L", "N"} or final_category == "Pc"
        if not final_is_word_base:
            sources.append(character)
    return tuple(sources)


@cache
def right_fragment_suffix_compatibility_sources() -> tuple[str, ...]:
    """Word-base sources whose expansion can supply a later ``t`` terminus."""
    sources = []
    for character in alphanumeric_compatibility_expansions():
        category = unicodedata.category(character)
        if category[0] not in {"L", "N"} and category != "Pc":
            continue
        opaque = tuple(
            part
            for part in unicodedata.normalize("NFKD", character)
            if not unicodedata.category(part).startswith("M")
            and unicodedata.category(part) != "Cf"
        )
        if opaque and opaque[-1].lower() == "t":
            sources.append(character)
    return tuple(sources)


@cache
def transparent_boundary_codepoints() -> tuple[str, ...]:
    return tuple(
        char
        for codepoint in range(sys.maxunicode + 1)
        for char in (chr(codepoint),)
        if unicodedata.category(char).startswith("M")
        or unicodedata.category(char) == "Cf"
    )


@cache
def word_base_category_representatives() -> tuple[str, ...]:
    categories = {"Lu", "Ll", "Lt", "Lm", "Lo", "Nd", "Nl", "No", "Pc"}
    representatives = {}
    for codepoint in range(sys.maxunicode + 1):
        char = chr(codepoint)
        category = unicodedata.category(char)
        if category in categories and category not in representatives:
            representatives[category] = char
    return tuple(representatives[category] for category in sorted(categories))

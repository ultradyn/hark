import unicodedata

import pytest

import hark.confirm_lexicon as confirm_lexicon
from confirm_security_corpus import (
    NORMATIVE_CONTRACTIONS,
    NORMATIVE_NEGATION_CORPUS,
)
from confirm_unicode_cases import (
    ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES,
    ALPHABETIC_TRIPLE_COMPATIBILITY_REPRODUCTIONS,
    APOSTROPHE_VARIANTS,
    BENIGN_PROSE_AFFIRMATIONS,
    COMPATIBILITY_PROSE_AFFIRMATIONS,
    COMPATIBILITY_EXPANSION_REPRODUCTIONS,
    COMPATIBILITY_WHITESPACE_ALPHA_REPRODUCTIONS,
    COMPOSITE_CONTRACTION_SEPARATORS,
    CONTRACTION_PARTS,
    EDGE_MATERIAL_REPRODUCTIONS,
    FULLWIDTH_CONTRACTION_CASES,
    FULLWIDTH_WORD_BASE_BOUNDARY_CONTROLS,
    MULTISOURCE_COMPATIBILITY_REPRODUCTIONS,
    NORMALIZATION_FORMS,
    ORDINARY_UNICODE_AFFIRMATIONS,
    ORDINARY_NONASCII_WORD_BASES,
    ORDINARY_PROSE_BRIDGE_EVIDENCE,
    PROVENANCE_PRESERVING_NORMALIZATION_FORMS,
    PROSE_TRANSPARENT_SUFFIXES,
    RAW_WORD_BASE_COMPATIBILITY_PREFIXES,
    SHADOWED_NORMALIZED_BRIDGE_EVIDENCE,
    SUPPORTED_FULLWIDTH_CONTRACTION_CASES,
    TRANSPARENT_BOUNDARY_CHARACTERS,
    UNSUPPORTED_IN_WORD_FRAGMENTS,
    WORD_BASE_BOUNDARY_CONTROLS,
    alphabetic_compatibility_triples,
    alphanumeric_compatibility_expansions,
    boundary_ending_compatibility_expansions,
    compatibility_whitespace_sources,
    is_fully_collapsed_alphabetic_material,
    leading_compatibility_whitespace_sources,
    repeated_symbol_compatibility_bridges,
    right_fragment_suffix_compatibility_sources,
    recursive_alphanumeric_compatibility_sources,
    single_alphabetic_compatibility_sources,
    trailing_compatibility_whitespace_sources,
    transparent_boundary_codepoints,
    word_base_category_representatives,
)
from hark.confirm_lexicon import classify_confirm_reply


def test_affirm():
    assert classify_confirm_reply("yes") == "yes"
    assert classify_confirm_reply("OK send it") == "yes"


@pytest.mark.parametrize(
    "reply",
    ["Yes.", " YES! ", "\tYeS.\n", "Okay.", "GO AHEAD!"],
)
def test_affirm_ignores_terminal_punctuation_casing_and_whitespace(reply):
    assert classify_confirm_reply(reply) == "yes"


def test_negate():
    assert classify_confirm_reply("cancel") == "no"
    assert classify_confirm_reply("nope") == "no"


def test_production_negation_lexicon_contains_fixed_security_corpus():
    assert NORMATIVE_NEGATION_CORPUS <= confirm_lexicon.NEGATE
    # Pin this contraction independently: Unicode case generation must fail if
    # production accidentally drops the refusal that motivated this oracle fix.
    assert "don't" in confirm_lexicon.NEGATE


@pytest.mark.parametrize(
    "reply",
    [
        "No, yes.",
        "Not okay.",
        "Yes — cancel.",
        "Yes, I cannot approve this.",
        "Yes, I can't approve this.",
        "Yes, I won't approve this.",
        "Yes, reject it.",
    ],
)
def test_negative_phrase_wins_over_affirmative(reply):
    assert classify_confirm_reply(reply) == "no"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("apostrophe", APOSTROPHE_VARIANTS)
def test_negative_contractions_accept_normalization_closed_apostrophe_family(
    apostrophe, normalization
):
    reply = f"yes I can{apostrophe}t approve this"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)
    assert classify_confirm_reply(reply) == "no"


@pytest.mark.parametrize(
    "reply",
    [
        "yes, I scant approve",
        *(f"yes, can{apostrophe}tastic" for apostrophe in APOSTROPHE_VARIANTS),
    ],
)
def test_negative_contractions_preserve_whole_token_boundaries(reply):
    assert classify_confirm_reply(reply) == "yes"


def test_unknown_in_word_punctuation_cannot_become_approval():
    assert classify_confirm_reply("yes I can\u055at approve this") == "unclear"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("fragment", UNSUPPORTED_IN_WORD_FRAGMENTS)
def test_lossy_or_repeated_in_word_material_cannot_become_approval(
    fragment, normalization
):
    reply = f"yes I can{fragment}t approve this"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)
    assert classify_confirm_reply(reply) == "unclear"


@pytest.mark.parametrize("separator", COMPOSITE_CONTRACTION_SEPARATORS)
def test_composite_contraction_separator_cannot_become_approval(separator):
    assert classify_confirm_reply(f"yes I can{separator}t approve this") == "unclear"


@pytest.mark.parametrize("reply", ORDINARY_UNICODE_AFFIRMATIONS)
def test_ordinary_unicode_words_do_not_block_affirmation(reply):
    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("reply", BENIGN_PROSE_AFFIRMATIONS)
def test_contraction_prefixes_in_benign_prose_do_not_block_affirmation(reply):
    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("reply", COMPATIBILITY_PROSE_AFFIRMATIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
def test_raw_whitespace_ends_compatibility_candidate_before_later_prose(
    reply, normalization
):
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)

    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("token", WORD_BASE_BOUNDARY_CONTROLS)
def test_malformed_contraction_inside_unicode_token_does_not_block_affirmation(token):
    assert classify_confirm_reply(f"yes {token}") == "yes"


def test_standalone_malformed_contraction_still_blocks_affirmation():
    assert classify_confirm_reply("yes can__t") == "unclear"


@pytest.mark.parametrize("token", FULLWIDTH_CONTRACTION_CASES)
def test_fullwidth_or_mixed_contraction_skeleton_preserves_separator_provenance(token):
    assert classify_confirm_reply(f"yes {token}") == "unclear"


@pytest.mark.parametrize("token", SUPPORTED_FULLWIDTH_CONTRACTION_CASES)
def test_fullwidth_or_mixed_contraction_accepts_supported_apostrophe(token):
    assert classify_confirm_reply(f"yes {token}") == "no"


@pytest.mark.parametrize("token", FULLWIDTH_WORD_BASE_BOUNDARY_CONTROLS)
def test_fullwidth_malformed_contraction_inside_unicode_token_does_not_block(token):
    assert classify_confirm_reply(f"yes {token}") == "yes"


@pytest.mark.parametrize("character", COMPATIBILITY_EXPANSION_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
def test_alphanumeric_compatibility_expansion_uses_observable_provenance(
    character, normalization
):
    material = (
        character
        if normalization is None
        else unicodedata.normalize(normalization, character)
    )

    result = classify_confirm_reply(f"yes I can{material}t approve this")
    if normalization in PROVENANCE_PRESERVING_NORMALIZATION_FORMS:
        assert result != "yes"
    elif is_fully_collapsed_alphabetic_material(material):
        assert result == "yes"
    else:
        assert result != "yes"


@pytest.mark.parametrize("bridge", MULTISOURCE_COMPATIBILITY_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_multisource_compatibility_bridge_uses_observable_provenance(
    bridge, normalization, parts
):
    left, right = parts
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )

    result = classify_confirm_reply(f"yes I {left}{material}{right} approve this")
    if normalization in PROVENANCE_PRESERVING_NORMALIZATION_FORMS:
        assert result != "yes"
    elif bridge == "\u00a8\u00a8":
        # Compatibility normalization turns these into two independent
        # SPACE+MARK sources. The second raw space is a prose boundary, so the
        # earlier ``can`` candidate must not reconnect to the later ``t``.
        assert result == "yes"
    elif is_fully_collapsed_alphabetic_material(material):
        assert result == "yes"
    else:
        assert result != "yes"


@pytest.mark.parametrize("bridge", ALPHABETIC_TRIPLE_COMPATIBILITY_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_alphabetic_triple_compatibility_bridge_uses_observable_provenance(
    bridge, normalization, parts
):
    left, right = parts
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )

    result = classify_confirm_reply(f"yes I {left}{material}{right} approve this")
    if normalization in PROVENANCE_PRESERVING_NORMALIZATION_FORMS:
        assert result != "yes"
    else:
        assert is_fully_collapsed_alphabetic_material(material)
        assert result == "yes"


@pytest.mark.parametrize("bridge", COMPATIBILITY_WHITESPACE_ALPHA_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", PROVENANCE_PRESERVING_NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_compatibility_whitespace_with_alphabetic_material_applies_boundary_policy(
    bridge, normalization, parts
):
    left, right = parts
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )

    result = classify_confirm_reply(f"yes I {left}{material}{right} approve this")
    if bridge == "a\u2002":
        assert result == "yes"
    else:
        assert result != "yes"


def test_all_compatibility_whitespace_sources_apply_raw_boundary_policy():
    leading_sources = leading_compatibility_whitespace_sources()
    trailing_sources = trailing_compatibility_whitespace_sources()
    mismatches = []
    audited = 0

    source_bridges = (
        *(
            (source + suffix, False)
            for source in leading_sources
            for suffix in ("a", "\u200ba")
        ),
        *(
            (prefix + source, source.isspace())
            for source in trailing_sources
            for prefix in ("a", "a\u200b")
        ),
    )
    for bridge, should_approve in source_bridges:
        for normalization in PROVENANCE_PRESERVING_NORMALIZATION_FORMS:
            material = (
                bridge
                if normalization is None
                else unicodedata.normalize(normalization, bridge)
            )
            for left, right in CONTRACTION_PARTS:
                audited += 1
                reply = f"yes I {left}{material}{right} approve this"
                approved = classify_confirm_reply(reply) == "yes"
                if approved is not should_approve:
                    mismatches.append(
                        (
                            tuple(map(ord, bridge)),
                            normalization,
                            left,
                            right,
                            approved,
                        )
                    )
                    if len(mismatches) == 20:
                        break
            if len(mismatches) == 20:
                break
        if len(mismatches) == 20:
            break

    assert len(leading_sources) >= 65
    assert len(trailing_sources) >= 15
    assert audited >= 1_400
    assert mismatches == []


def test_normalized_compatibility_whitespace_keeps_all_transparent_evidence():
    transparent = transparent_boundary_codepoints()
    normalizations = ("NFKC", "NFKD")
    failures = []
    audited = 0

    for edge in transparent:
        bridge = "\u00a0" + edge + "\u1d43"
        for normalization in normalizations:
            material = unicodedata.normalize(normalization, bridge)
            for left, right in CONTRACTION_PARTS:
                audited += 1
                reply = f"yes I {left}{material}{right} approve this"
                if classify_confirm_reply(reply) == "yes":
                    failures.append((f"U+{ord(edge):04X}", normalization, left, right))
                    if len(failures) == 20:
                        break
            if len(failures) == 20:
                break
        if len(failures) == 20:
            break

    assert all(
        unicodedata.category(character).startswith("M")
        or unicodedata.category(character) == "Cf"
        for character in transparent
    )
    assert {"\u0301", "\u200b", "\u200d", "\ufe0f"} <= set(transparent)
    assert audited == (
        len(transparent) * len(normalizations) * len(NORMATIVE_CONTRACTIONS)
    )
    assert failures == []


def test_all_normalized_compatibility_whitespace_sources_respect_later_boundaries():
    sources = compatibility_whitespace_sources()
    normalizations = ("NFKC", "NFKD")
    approvals = []
    audited = 0

    for source in sources:
        bridge = source + "\u200b\u1d43"
        for normalization in normalizations:
            material = unicodedata.normalize(normalization, bridge)
            for left, right in CONTRACTION_PARTS:
                audited += 1
                reply = f"yes I {left}{material}{right} approve this"
                if classify_confirm_reply(reply) == "yes":
                    approvals.append(
                        (f"U+{ord(source):04X}", normalization, left, right)
                    )

    assert all(
        unicodedata.normalize("NFKD", source) != unicodedata.normalize("NFD", source)
        and any(
            character.isspace() for character in unicodedata.normalize("NFKD", source)
        )
        for source in sources
    )
    assert {"\u00a0", "\u00a8", "\u2002"} <= set(sources)
    assert audited == len(sources) * len(normalizations) * len(NORMATIVE_CONTRACTIONS)
    expected_approvals = [
        (f"U+{ord(source):04X}", normalization, left, right)
        for source in ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES
        for normalization in normalizations
        for left, right in CONTRACTION_PARTS
    ]
    assert approvals == expected_approvals


@pytest.mark.parametrize(
    "bridge",
    (
        "\u00a0\u200b\u1d43",  # SPACE + Cf + compatibility alpha
        "\u00a8\u1d43",  # SPACE + combining diaeresis + compatibility alpha
        "\u00a0/\u1d43",  # SPACE + nonalphabetic significant evidence
    ),
)
@pytest.mark.parametrize("normalization", ("NFKC", "NFKD"))
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_normalized_whitespace_with_surviving_bridge_evidence_fails_closed(
    bridge, normalization, parts
):
    left, right = parts
    material = unicodedata.normalize(normalization, bridge)
    reply = f"yes I {left}{material}{right} approve this"

    assert classify_confirm_reply(reply) == "unclear"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
@pytest.mark.parametrize(("category", "bridge"), SHADOWED_NORMALIZED_BRIDGE_EVIDENCE)
def test_later_literal_whitespace_terminates_collapsed_shadowed_evidence(
    category, bridge, parts, normalization
):
    left, right = parts
    assert unicodedata.category(bridge[1]) == category
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )
    reply = f"yes I {left}{material}{right} approve this"

    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
@pytest.mark.parametrize("evidence", ("\u200b", "\u0301", "/", "©", "1"))
@pytest.mark.parametrize(
    "bridge_template",
    (
        "\u00a0{evidence}\u1d43",
        "\u00a0\u1d43{evidence}",
        "\u00a0{evidence}\u1d43{evidence}",
    ),
    ids=("evidence-before-alpha", "evidence-after-alpha", "evidence-both-sides"),
)
def test_observable_bridge_evidence_is_order_independent(
    bridge_template, evidence, parts, normalization
):
    left, right = parts
    bridge = bridge_template.format(evidence=evidence)
    reply = f"yes I {left}{bridge}{right} approve this"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)

    assert classify_confirm_reply(reply) == "unclear"


@pytest.mark.parametrize("evidence", ("\u200b", "\ufe0f", "\u0301", "/", "©"))
@pytest.mark.parametrize("boundary", (" ", "\t", "\n", "\u2028"))
def test_later_literal_prose_boundary_terminates_bridge_candidate(evidence, boundary):
    assert classify_confirm_reply(f"yes I can {evidence}do{boundary}it") == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("initial_boundary", (" ", "\u00a0"))
@pytest.mark.parametrize("later_boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize(
    ("category", "evidence"),
    (("Cf", "\u200b"), ("Mn", "\u0301"), ("Po", "/"), ("So", "©")),
)
def test_direct_separator_respects_later_raw_whitespace_expiry(
    category, evidence, later_boundary, initial_boundary, normalization
):
    assert unicodedata.category(evidence) == category
    raw_reply = f"yes I can{initial_boundary}{evidence}{later_boundary}t approve this"
    reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize(
    ("category", "evidence"),
    (("Cf", "\u200b"), ("Mn", "\u0301"), ("Po", "/"), ("So", "©")),
)
def test_direct_separator_cannot_claim_first_later_raw_whitespace(
    category, evidence, boundary, normalization
):
    assert unicodedata.category(evidence) == category
    raw_reply = f"yes I can{evidence}{boundary}t approve this"
    reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize(
    ("category", "evidence"),
    (("Cf", "\u200b"), ("Mn", "\u0301"), ("Po", "/"), ("So", "©")),
)
def test_direct_and_scanner_share_later_whitespace_ownership(
    category, evidence, boundary, normalization
):
    assert unicodedata.category(evidence) == category
    raw_reply = f"yes I can{evidence}{boundary}t approve this"
    reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )
    projection = confirm_lexicon._build_raw_projection(reply)
    assert projection is not None
    facts = confirm_lexicon._build_projection_facts(reply, projection)

    direct_candidates = [
        match
        for pattern in confirm_lexicon._CONTRACTION_SEPARATOR_PATTERNS
        for match in pattern.finditer(projection.text)
        if confirm_lexicon._is_complete_confirmation_span(
            facts, match.start(), match.end()
        )
        and confirm_lexicon._direct_separator_span_respects_boundaries(
            projection,
            facts,
            *match.span("separator"),
        )
    ]
    scanner_candidates = list(
        confirm_lexicon._iter_compatibility_bridge_spans(reply, projection, facts)
    )

    assert direct_candidates == scanner_candidates == []


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize("evidence", ("\u200b", "\u0301", "/", "©"))
def test_direct_separator_preserves_candidate_owned_initial_boundary(
    evidence, boundary, normalization
):
    raw_reply = f"yes I can{boundary}{evidence}t approve this"
    reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    assert classify_confirm_reply(reply) != "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize(
    "boundary",
    tuple(source for source in compatibility_whitespace_sources() if source.isspace()),
    ids=lambda source: f"U+{ord(source):04X}",
)
def test_later_independent_compatibility_space_terminates_bridge_candidate(
    boundary, normalization
):
    raw_reply = f"yes I can \u200bdo{boundary}it"
    reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    if normalization in ("NFKC", "NFKD"):
        assert reply == "yes I can \u200bdo it"
    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("source", ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES)
@pytest.mark.parametrize(
    "boundary",
    tuple(source for source in compatibility_whitespace_sources() if source.isspace()),
    ids=lambda source: f"U+{ord(source):04X}",
)
def test_later_independent_compatibility_space_expires_declared_source_span(
    boundary, source, normalization
):
    raw_reply = f"yes I can{source}\u200bᵃ{boundary}t approve this"
    reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    if normalization in ("NFKC", "NFKD"):
        expected = (
            "yes I can"
            + unicodedata.normalize(normalization, source)
            + "\u200ba t approve this"
        )
        assert reply == expected
        assert source not in reply
    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize(
    "word_base",
    ("\u03bb", "\u0661", "\u203f"),
    ids=("unicode-letter", "unicode-number", "connector-punctuation"),
)
@pytest.mark.parametrize("initial_boundary", ("", " "))
def test_unicode_word_base_ends_direct_separator_candidate_before_later_space(
    initial_boundary, word_base, boundary, normalization
):
    raw_reply = f"yes I can{initial_boundary}{word_base}{boundary}t approve this"
    reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize("source", ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES)
def test_declared_compatibility_source_span_expires_before_unrelated_right_fragment(
    source, boundary, normalization
):
    raw_reply = f"yes I can{source}{boundary}t approve this"
    reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("evidence", ("\u200b", "\ufe0f", "\u0301", "/", "©"))
def test_observable_bridge_without_later_prose_boundary_remains_fail_closed(evidence):
    assert classify_confirm_reply(f"yes I can {evidence}dot") == "unclear"


@pytest.mark.parametrize("apostrophe", ("'", "`", "\u2019", *APOSTROPHE_VARIANTS))
@pytest.mark.parametrize(
    ("opening_quote", "closing_quote"),
    (("'", "'"), ("\u2018", "\u2019")),
)
def test_quoted_supported_contractions_fail_closed(opening_quote, closing_quote, apostrophe):
    """B157: balanced apostrophe-family quotes around a complete negative."""
    reply = f"yes {opening_quote}can{apostrophe}t{closing_quote} approve this"
    assert classify_confirm_reply(reply) == "no"


@pytest.mark.parametrize(
    "reply",
    [
        "yes 'can't'",
        "yes ‘can’t’",
        "yes ‘can’t’ approve this",
        "yes 'won't'",
        "yes 'don't' do it",
        "'can't'",
        "yes 'no'",
        "yes 'cancel'",
        # Double quotes / parentheses already strip as generic punctuation.
        'yes "can\'t"',
        "yes (can't)",
    ],
)
def test_quoted_negative_contractions_and_refusals_fail_closed(reply):
    """B157 acceptance: quoted complete negatives must not authorize approval."""
    assert classify_confirm_reply(reply) == "no"


@pytest.mark.parametrize(
    "reply",
    [
        # Whole-token boundaries: peeling outer quotes must not invent can't.
        "yes 'scant'",
        "yes scant",
        "yes can''tastic",
        # Unbalanced / non-wrapper apostrophes stay out of the peel path.
        "yes 'can't",
        "yes can't'",
    ],
)
def test_quoted_peel_preserves_whole_token_and_unbalanced_boundaries(reply):
    assert classify_confirm_reply(reply) != "no"


def test_quoted_peel_preserves_affirmative_idiom_and_b142_deferral():
    assert classify_confirm_reply("yes why not") == "yes"
    assert classify_confirm_reply("Yes, why not?") == "yes"
    assert classify_confirm_reply("Yes, but wait.") == "unclear"
    assert classify_confirm_reply("yes but wait") == "unclear"
    # Quoted affirmative alone remains approval; quotes are non-semantic wrappers.
    assert classify_confirm_reply("'yes'") == "yes"


@pytest.mark.parametrize("apostrophe", ("'", "`", "\u2019", *APOSTROPHE_VARIANTS))
def test_unquoted_supported_contractions_remain_negative(apostrophe):
    reply = f"yes can{apostrophe}t approve this"
    assert classify_confirm_reply(reply) == "no"


@pytest.mark.parametrize("normalization", ("NFKC", "NFKD"))
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_fully_collapsed_whitespace_bridge_equals_identical_literal_input(
    normalization, parts
):
    left, right = parts
    source_reply = unicodedata.normalize(
        normalization,
        f"yes I {left}\u00a0\u00a0\u1d43{right} approve this",
    )
    literal_material = unicodedata.normalize(
        normalization,
        "\u00a0\u00a0\u1d43",
    )
    literal_reply = f"yes I {left}{literal_material}{right} approve this"

    assert source_reply == literal_reply
    assert (
        classify_confirm_reply(source_reply)
        == classify_confirm_reply(literal_reply)
        == "yes"
    )


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize(("category", "bridge"), SHADOWED_NORMALIZED_BRIDGE_EVIDENCE)
@pytest.mark.parametrize("template", ("écan{}t", "can{}té"))
def test_shadowed_bridge_evidence_preserves_whole_token_boundaries(
    category, bridge, normalization, template
):
    assert unicodedata.category(bridge[1]) == category
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )

    assert classify_confirm_reply(f"yes {template.format(material)}") == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("prefix", RAW_WORD_BASE_COMPATIBILITY_PREFIXES)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_raw_word_base_category_owns_compatibility_decomposition_boundary(
    parts, prefix, normalization
):
    left, right = parts
    assert unicodedata.category(prefix)[0] in {"L", "N"}
    reply = f"yes {prefix}{left}__{right}"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)

    result = classify_confirm_reply(reply)
    if normalization in PROVENANCE_PRESERVING_NORMALIZATION_FORMS:
        assert result == "yes"
    else:
        # Once compatibility normalization erases the source category, only
        # the literal punctuation-bearing expansion remains observable.
        assert result == "unclear"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("transparent", PROSE_TRANSPARENT_SUFFIXES)
@pytest.mark.parametrize(
    "template",
    ("yes I can do i{}t", "yes I can do{} it"),
)
def test_literal_prose_whitespace_resets_bridge_before_later_transparent_word(
    template, transparent, normalization
):
    reply = template.format(transparent)
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)

    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize(("category", "evidence"), ORDINARY_PROSE_BRIDGE_EVIDENCE)
@pytest.mark.parametrize("word_base", ORDINARY_NONASCII_WORD_BASES)
def test_literal_prose_whitespace_resets_ordinary_nonascii_word_candidate(
    word_base, category, evidence, normalization
):
    assert unicodedata.category(word_base)[0] == "L"
    assert unicodedata.category(evidence) == category
    reply = f"yes I can{word_base} do {evidence}at"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)

    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("word_base", ORDINARY_NONASCII_WORD_BASES)
def test_nonascii_prose_reset_does_not_hide_later_standalone_candidate(word_base):
    assert classify_confirm_reply(f"yes I can{word_base} do can__t") == "unclear"


@pytest.mark.parametrize("normalization", ("NFKC", "NFKD"))
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
@pytest.mark.parametrize("source", ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES)
def test_collapsed_alphabetic_compatibility_whitespace_equals_literal_prose(
    source, parts, normalization
):
    left, right = parts
    material = unicodedata.normalize(normalization, source + "\u200b\u1d43")
    source_reply = unicodedata.normalize(
        normalization, f"yes I {left}{source}\u200b\u1d43{right} approve this"
    )
    literal_reply = f"yes I {left}{material}{right} approve this"

    assert source_reply == literal_reply
    assert (
        classify_confirm_reply(source_reply)
        == classify_confirm_reply(literal_reply)
        == "yes"
    )


@pytest.mark.parametrize("normalization", PROVENANCE_PRESERVING_NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
@pytest.mark.parametrize("source", ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES)
def test_raw_alphabetic_compatibility_whitespace_source_remains_fail_closed(
    source, parts, normalization
):
    left, right = parts
    reply = f"yes I {left}{source}\u200b\u1d43{right} approve this"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)

    assert classify_confirm_reply(reply) == "unclear"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\n", "\u2028"))
@pytest.mark.parametrize(
    "evidence_and_alpha",
    ("\u200b\u1d43", "\u1d43\u200b", "\u200b\u1d43\u200b"),
    ids=("evidence-before-alpha", "evidence-after-alpha", "evidence-both-sides"),
)
@pytest.mark.parametrize("source", ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES)
def test_later_literal_boundary_expires_single_source_compatibility_exception(
    source, evidence_and_alpha, boundary, normalization
):
    raw_reply = f"yes I can{source}{evidence_and_alpha}{boundary}do it"
    reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    if normalization in ("NFKC", "NFKD"):
        literal_reply = (
            "yes I can"
            + unicodedata.normalize(normalization, source + evidence_and_alpha)
            + boundary
            + "do it"
        )
        assert reply == literal_reply
        assert classify_confirm_reply(literal_reply) == "yes"
    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize(
    "reply",
    (
        "yes I can at",
        "yes I can make it",
        "yes I can certainly do it",
        "yes I can recommend it",
    ),
)
def test_ordinary_raw_whitespace_remains_a_hard_prose_boundary(reply):
    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("bridge", ALPHABETIC_TRIPLE_COMPATIBILITY_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", ("NFKC", "NFKD"))
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_collapsed_compatibility_triple_equals_identical_literal_input(
    bridge, normalization, parts
):
    left, right = parts
    source_reply = unicodedata.normalize(
        normalization, f"yes I {left}{bridge}{right} approve this"
    )
    literal_material = unicodedata.normalize(normalization, bridge)
    literal_reply = f"yes I {left}{literal_material}{right} approve this"

    assert source_reply == literal_reply
    assert (
        classify_confirm_reply(source_reply)
        == classify_confirm_reply(literal_reply)
        == "yes"
    )


def test_all_recursive_alphanumeric_compatibility_sources_fail_closed():
    sources = recursive_alphanumeric_compatibility_sources()
    failures = []

    for source in sources:
        for normalization in NORMALIZATION_FORMS:
            material = (
                source
                if normalization is None
                else unicodedata.normalize(normalization, source)
            )
            for left, right in CONTRACTION_PARTS:
                reply = f"yes I {left}{material}{right} approve this"
                if classify_confirm_reply(reply) == "yes":
                    failures.append(
                        (f"U+{ord(source):04X}", normalization, left, right)
                    )

    assert len(sources) >= 3
    assert failures == []


def test_all_single_alphabetic_sources_fail_closed_with_provenance():
    sources = single_alphabetic_compatibility_sources()
    failures = []
    audited = 0

    for source in sources:
        for normalization in PROVENANCE_PRESERVING_NORMALIZATION_FORMS:
            material = (
                source
                if normalization is None
                else unicodedata.normalize(normalization, source)
            )
            for left, right in CONTRACTION_PARTS:
                audited += 1
                if classify_confirm_reply(f"yes I {left}{material}{right}") == "yes":
                    failures.append(
                        (f"U+{ord(source):04X}", normalization, left, right)
                    )
                    if len(failures) == 20:
                        break
            if len(failures) == 20:
                break
        if len(failures) == 20:
            break

    assert len(sources) >= 2_000
    assert audited >= 18_000
    assert failures == []


def test_alphabetic_compatibility_triples_fail_closed_with_provenance():
    triples = alphabetic_compatibility_triples()
    failures = []
    audited = 0

    for bridge in triples:
        for normalization in PROVENANCE_PRESERVING_NORMALIZATION_FORMS:
            material = (
                bridge
                if normalization is None
                else unicodedata.normalize(normalization, bridge)
            )
            for left, right in CONTRACTION_PARTS:
                audited += 1
                if classify_confirm_reply(f"yes I {left}{material}{right}") == "yes":
                    failures.append(
                        (
                            tuple(f"U+{ord(source):04X}" for source in bridge),
                            normalization,
                            left,
                            right,
                        )
                    )
                    if len(failures) == 20:
                        break
            if len(failures) == 20:
                break
        if len(failures) == 20:
            break

    assert len(triples) >= 2_000
    assert audited >= 18_000
    assert failures == []


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_later_same_left_cannot_shadow_earlier_compatibility_source(
    normalization, parts
):
    left, right = parts
    reply = f"yes I {left}\u2474{left}x{right} approve this"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)

    assert classify_confirm_reply(reply) != "yes"


def test_boundary_ending_compatibility_sources_survive_repeated_starts():
    failures = []
    sources = boundary_ending_compatibility_expansions()
    audited = 0

    for source in sources:
        for normalization in NORMALIZATION_FORMS:
            material = (
                source
                if normalization is None
                else unicodedata.normalize(normalization, source)
            )
            for repeated_starts in (1, 3, 7):
                for left, right in CONTRACTION_PARTS:
                    audited += 1
                    shadowing_prefix = (f"{left}x)" * (repeated_starts - 1)) + left
                    reply = (
                        f"yes I {left}{material}{shadowing_prefix}x{right} approve this"
                    )
                    if classify_confirm_reply(reply) == "yes":
                        failures.append(
                            (
                                f"U+{ord(source):04X}",
                                normalization,
                                repeated_starts,
                                left,
                                right,
                            )
                        )
                        if len(failures) == 20:
                            break
                if len(failures) == 20:
                    break
            if len(failures) == 20:
                break
        if len(failures) == 20:
            break

    assert len(sources) >= 150
    assert audited >= 6_750
    assert failures == []


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("suffix", ["t", "\u1d40", "\uff34", "\U0001d413"])
def test_boundary_invalid_first_right_fragment_owns_repeated_suffix(
    normalization, suffix
):
    reply = f"yes can__t{suffix}"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)

    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("suffix", ["1", "é", "λ"])
def test_word_base_suffix_controls_remain_outside_standalone_contraction(
    normalization, suffix
):
    reply = f"yes can__t{suffix}"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)

    assert classify_confirm_reply(reply) == "yes"


def test_all_compatibility_word_base_right_suffixes_keep_first_terminus_owner():
    failures = []
    sources = right_fragment_suffix_compatibility_sources()
    audited = 0

    for source in sources:
        for normalization in NORMALIZATION_FORMS:
            material = (
                source
                if normalization is None
                else unicodedata.normalize(normalization, source)
            )
            for left, right in CONTRACTION_PARTS:
                audited += 1
                reply = f"yes {left}__{right}{material}"
                if classify_confirm_reply(reply) != "yes":
                    failures.append(
                        (f"U+{ord(source):04X}", normalization, left, right)
                    )

    assert len(sources) == 33
    assert audited == 495
    assert failures == []


@pytest.mark.parametrize("normalization", PROVENANCE_PRESERVING_NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_raw_compatibility_provenance_precedes_normalized_shape_bounds(
    normalization, parts
):
    left, right = parts
    bridge = "\u2122" + "x" * 257
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )

    assert (
        classify_confirm_reply(f"yes I {left}{material}{right} approve this") != "yes"
    )


def test_all_repeated_symbol_compatibility_bridges_fail_closed():
    failures = []
    bridges = repeated_symbol_compatibility_bridges()
    audited = 0

    for bridge in bridges:
        for normalization in PROVENANCE_PRESERVING_NORMALIZATION_FORMS:
            material = (
                bridge
                if normalization is None
                else unicodedata.normalize(normalization, bridge)
            )
            for left, right in CONTRACTION_PARTS:
                audited += 1
                reply = f"yes I {left}{material}{right} approve this"
                if classify_confirm_reply(reply) == "yes":
                    failures.append(
                        (f"U+{ord(bridge[0]):04X}", normalization, left, right)
                    )
                    if len(failures) == 20:
                        break
            if len(failures) == 20:
                break
        if len(failures) == 20:
            break

    assert len(bridges) >= 800
    assert audited >= 7_500
    assert failures == []


def test_all_attributable_alphanumeric_compatibility_expansions_fail_closed():
    failures = []
    expansions = alphanumeric_compatibility_expansions()
    audited = 0

    for character in expansions:
        for normalization in PROVENANCE_PRESERVING_NORMALIZATION_FORMS:
            material = (
                character
                if normalization is None
                else unicodedata.normalize(normalization, character)
            )
            for left, right in CONTRACTION_PARTS:
                audited += 1
                reply = f"yes I {left}{material}{right} approve this"
                if classify_confirm_reply(reply) == "yes":
                    failures.append(
                        (f"U+{ord(character):04X}", normalization, left, right)
                    )
                    if len(failures) == 20:
                        break
            if len(failures) == 20:
                break
        if len(failures) == 20:
            break

    assert len(expansions) >= 3000
    assert audited >= 30_000
    assert failures == []


@pytest.mark.parametrize("reply", EDGE_MATERIAL_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
def test_transparent_edge_material_cannot_hide_malformed_contraction(
    reply, normalization
):
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)
    assert classify_confirm_reply(reply) == "unclear"


@pytest.mark.parametrize("edge", TRANSPARENT_BOUNDARY_CHARACTERS)
def test_extend_format_and_variation_boundaries_remain_fail_closed(edge):
    assert classify_confirm_reply(f"yes I {edge}can__t approve") == "unclear"
    assert classify_confirm_reply(f"yes I can__t{edge} approve") == "unclear"


def test_all_unicode_extend_and_format_edges_remain_fail_closed():
    failures = []
    edges = transparent_boundary_codepoints()

    for edge in edges:
        replies = (f"yes I {edge}can__t", f"yes I can__t{edge}")
        if any(classify_confirm_reply(reply) == "yes" for reply in replies):
            failures.append(f"U+{ord(edge):04X}")
            if len(failures) == 20:
                break

    assert len(edges) >= 2000
    assert failures == []


def test_every_word_base_category_still_blocks_embedded_candidate():
    for base in word_base_category_representatives():
        assert classify_confirm_reply(f"yes {base}can__t") == "yes"
        assert classify_confirm_reply(f"yes can__t{base}") == "yes"


def _alternating_out_of_order_marks(length):
    return ("\u0315\u0300" * ((length + 1) // 2))[:length]


@pytest.mark.parametrize(
    "prefix",
    [
        "",  # leading combining marks
        "a",  # starter plus combining marks
        "\u1100\u1161\u11a8",  # algorithmic Hangul L/V/T composition
        "\u09c7\u09be",  # Bengali CCC-zero canonical composition
        "\u00b4",  # compatibility SPACE + COMBINING ACUTE expansion
    ],
)
def test_normalization_segment_guard_accepts_limit_and_rejects_next_char(
    monkeypatch, prefix
):
    limit = confirm_lexicon._MAX_NORMALIZATION_SEGMENT_CHARS
    at_limit = prefix + _alternating_out_of_order_marks(limit - len(prefix))
    over_limit = at_limit + "\u0315"
    real_normalize = confirm_lexicon.unicodedata.normalize
    calls = []

    def counted_normalize(form, text):
        calls.append((form, len(text)))
        return real_normalize(form, text)

    monkeypatch.setattr(confirm_lexicon.unicodedata, "normalize", counted_normalize)

    assert classify_confirm_reply(at_limit) == "unclear"
    assert any(form == "NFKC" and length == len(at_limit) for form, length in calls)

    calls.clear()
    assert classify_confirm_reply(over_limit) == "unclear"
    assert all(length <= 2 for _, length in calls)


def test_long_pathological_combining_segment_fails_before_whole_normalization(
    monkeypatch,
):
    real_normalize = confirm_lexicon.unicodedata.normalize
    normalized_lengths = []

    def counted_normalize(form, text):
        normalized_lengths.append(len(text))
        return real_normalize(form, text)

    monkeypatch.setattr(confirm_lexicon.unicodedata, "normalize", counted_normalize)
    raw = _alternating_out_of_order_marks(8000)

    assert classify_confirm_reply(raw) == "unclear"
    assert len(normalized_lengths) == (
        2 * (confirm_lexicon._MAX_NORMALIZATION_SEGMENT_CHARS + 1)
    )
    assert max(normalized_lengths) == 1


def test_segment_guard_does_not_cap_ordinary_text_or_decomposed_spacing_acute():
    long_ordinary_reply = "yes " + "naïve Ελληνικά " * 600

    assert classify_confirm_reply(long_ordinary_reply) == "yes"
    assert classify_confirm_reply("yes I can \u0301t approve this") == "no"


def test_classifier_reconstructs_many_decomposed_apostrophes_in_linear_work(
    monkeypatch,
):
    real_normalize = confirm_lexicon.unicodedata.normalize
    real_rebuild = confirm_lexicon._canonical_input_with_replacements
    normalization_work = 0
    rebuild_samples = []

    def counted_normalize(form, text):
        nonlocal normalization_work
        normalization_work += len(text)
        return real_normalize(form, text)

    def counted_rebuild(text, replacements):
        class CountingReplacements(dict):
            get_calls = 0

            def get(self, key, default=None):
                self.get_calls += 1
                return super().get(key, default)

        counted = CountingReplacements(replacements)
        result = real_rebuild(text, counted)
        rebuild_samples.append(
            (len(text), len(counted), counted.get_calls, len(result))
        )
        return result

    monkeypatch.setattr(confirm_lexicon.unicodedata, "normalize", counted_normalize)
    monkeypatch.setattr(
        confirm_lexicon,
        "_canonical_input_with_replacements",
        counted_rebuild,
    )

    def measure(repetitions):
        nonlocal normalization_work
        normalization_work = 0
        rebuild_samples.clear()
        raw = "can \u0301t " * repetitions

        assert classify_confirm_reply(raw) == "no"
        assert rebuild_samples == [
            (len(raw), repetitions, len(raw), len(raw) - repetitions)
        ]
        return normalization_work

    small_work = measure(1024)
    large_work = measure(2048)

    assert large_work <= 2 * small_work + 16


def test_transparent_boundary_scan_has_linear_operation_count(monkeypatch):
    real_is_transparent = confirm_lexicon._is_boundary_transparent
    real_is_word_base = confirm_lexicon._is_confirmation_word_base
    operation_count = 0

    def counted_is_transparent(char):
        nonlocal operation_count
        operation_count += 1
        return real_is_transparent(char)

    def counted_is_word_base(char):
        nonlocal operation_count
        operation_count += 1
        return real_is_word_base(char)

    monkeypatch.setattr(
        confirm_lexicon, "_is_boundary_transparent", counted_is_transparent
    )
    monkeypatch.setattr(
        confirm_lexicon, "_is_confirmation_word_base", counted_is_word_base
    )

    def measure(size):
        nonlocal operation_count
        operation_count = 0
        reply = "yes " + "\u200b" * size + "can" + "t" * size

        assert classify_confirm_reply(reply) == "yes"
        return operation_count

    small_work = measure(2048)
    large_work = measure(4096)

    assert large_work <= 2 * small_work + 32


def test_direct_separator_unicode_boundary_scan_has_linear_operation_count(
    monkeypatch,
):
    real_is_word_base = confirm_lexicon._is_confirmation_word_base
    operation_count = 0

    def counted_is_word_base(char):
        nonlocal operation_count
        operation_count += 1
        return real_is_word_base(char)

    monkeypatch.setattr(
        confirm_lexicon, "_is_confirmation_word_base", counted_is_word_base
    )

    def measure(size):
        nonlocal operation_count
        operation_count = 0
        reply = "yes can" + "\u200b" * size + "\u03bb t approve this"

        assert classify_confirm_reply(reply) == "yes"
        return operation_count

    small_work = measure(2048)
    large_work = measure(4096)

    assert large_work <= 2 * small_work + 32


@pytest.mark.parametrize(
    "reply",
    [
        "yes I cant approve this",
        "yes I wont approve this",
        "yes I dont approve this",
    ],
)
def test_apostropheless_negative_contractions_win_over_affirmative(reply):
    assert classify_confirm_reply(reply) == "no"


@pytest.mark.parametrize(
    "reply",
    [
        "yes the cantilever design is approved",
        "yes I want wonton soup",
    ],
)
def test_apostropheless_negative_contractions_match_whole_words(reply):
    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize(
    "reply",
    [
        "Yes, but wait.",
        "Okay, wait a second.",
        "Yes, if the tests pass.",
        "Yes, wait.",
        "Yes, but.",
        "Okay, if.",
        "Yes, unless.",
        "Yes; wait.",
        "Yes—wait.",
        "Yes. Wait.",
        "Yes: wait.",
        "Sure; if tests pass.",
        "Yes? If tests pass.",
        "Yes… unless reviewed.",
        "Okay… wait.",
        "Go ahead—unless reviewed.",
        "Yes—but go ahead.",
    ],
)
def test_punctuated_defer_remains_unclear(reply):
    """B142 parent behavior: punctuation must not turn deferral into approval."""
    assert classify_confirm_reply(reply) == "unclear"


def test_yes_why_not_is_affirmative_idiom():
    assert classify_confirm_reply("yes why not") == "yes"
    assert classify_confirm_reply("Yes, why not?") == "yes"


def test_unclear():
    assert classify_confirm_reply("maybe later purple") == "unclear"


@pytest.mark.parametrize(
    "reply",
    [
        "yes but wait",
        "yes hold on",
        "okay wait a second",
        "sure after I review it",
        "yes if the tests pass",
        "yes unless the tests fail",
        "yes maybe",
        "yeah hang on",
        "yes until the tests pass",
        "sure later",
        "ok perhaps",
        "yes but go ahead",
        "go ahead unless reviewed",
    ],
)
def test_deferred_conditional_hedged_not_immediate_yes(reply):
    """B148: unpunctuated defer/condition/hedge must not authorize immediately."""
    assert classify_confirm_reply(reply) == "unclear"


@pytest.mark.parametrize(
    "reply",
    [
        "yes",
        "OK send it",
        "yes go ahead",
        "sure send it",
        "okay do it",
        "yes please",
        "yes send it now",
        "yes why not",
    ],
)
def test_unambiguous_immediate_approval_still_yes(reply):
    assert classify_confirm_reply(reply) == "yes"


# B159: Format (Cf) / Mark (M*) material inside multi-letter NEGATE tokens must
# not break whole-token matching into spaces that let a leading affirmative win.
_ZWSP = "\u200b"  # ZERO WIDTH SPACE (Cf)
_ZWNJ = "\u200c"  # ZERO WIDTH NON-JOINER (Cf)
_COMBINING_ACUTE = "\u0301"  # COMBINING ACUTE ACCENT (Mn)


@pytest.mark.parametrize(
    "reply",
    [
        f"yes I ca{_ZWSP}n’t approve this",
        f"yes c{_ZWSP}ancel",
        f"yes n{_ZWSP}ot",
        f"yes a{_ZWSP}bort",
        f"yes re{_ZWSP}ject",
        f"yes de{_ZWSP}cline",
        f"yes can{_ZWSP}cel it",
        f"yes I wo{_ZWSP}n’t approve this",
        f"yes do{_ZWSP}n’t",
        f"yes n{_ZWNJ}ot",
        f"yes c{_COMBINING_ACUTE}ancel",
        f"yes{_ZWSP} c{_ZWSP}ancel{_ZWSP}",
    ],
)
def test_format_or_mark_inside_negative_token_fails_closed(reply):
    """B159: interior Format/Mark must not authorize R2 via broken NEGATE tokens."""
    assert classify_confirm_reply(reply) == "no"


@pytest.mark.parametrize(
    "reply",
    [
        # Whole-word controls: strip must not create substring NEGATE hits.
        f"yes the c{_ZWSP}antilever design is approved",
        f"yes I want wo{_ZWSP}nton soup",
        f"yes n{_ZWSP}ote that it looks good",
        f"yes the ab{_ZWSP}out page is fine",
        # Benign prose / edge Format outside negative tokens still approves.
        f"yes{_ZWSP}",
        f"{_ZWSP}yes",
        f"yes go{_ZWSP} ahead",
    ],
)
def test_format_strip_preserves_whole_word_boundaries_and_benign_yes(reply):
    assert classify_confirm_reply(reply) == "yes"


def test_format_strip_scales_linearly_on_long_zwsp_runs():
    """Bounded scaling: long Cf runs must not change classification or explode."""
    poison = "c" + (_ZWSP * 10_000) + "ancel"
    assert classify_confirm_reply(f"yes {poison}") == "no"
    assert classify_confirm_reply("yes" + (_ZWSP * 10_000)) == "yes"

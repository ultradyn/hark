import unicodedata

import pytest

import hark.confirm_lexicon as confirm_lexicon
from hark.confirm_lexicon import NEGATE, classify_confirm_reply


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


_APOSTROPHE_VARIANTS = (
    "\u2018",  # submitted: left single quotation mark
    "\u02bc",  # submitted: modifier letter apostrophe
    "\u00b4",  # submitted: acute accent
    "\uff40",  # fullwidth grave accent
    "\u1fef",  # Greek varia
    "\u201b",  # single high-reversed-9 quotation mark
    "\u2032",  # prime
    "\u02b9",  # modifier letter prime
)
_NORMALIZATION_FORMS = (None, "NFC", "NFD", "NFKC", "NFKD")
_UNSUPPORTED_IN_WORD_FRAGMENTS = (
    "\u00a8",  # compatibility spacing diaeresis
    "\u2033",  # compatibility double prime
    "\uff3f",  # compatibility fullwidth low line
    "\u0301",  # combining acute, which NFC composes into a letter
    "\u02bb",  # unsupported modifier letter that Python treats as alphanumeric
    "''",  # repeated supported ASCII marks
    "\u2032\u2032",  # repeated supported Unicode marks
)
_COMPOSITE_CONTRACTION_SEPARATORS = (
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
_ORDINARY_UNICODE_AFFIRMATIONS = (
    "yes I approve naïvely",
    "yes mañana",
    "yes résumé",
    "yes Ελληνικά",
)
_UNICODE_TOKEN_CONTINUATION_CONTROLS = (
    "écan__t",
    "_can__t",
    "\u0301can__t",
    "1can__t",
    "can__té",
    "can__t_",
    "can__t\u0301",
    "can__t1",
)
_UNSUPPORTED_FULLWIDTH_SEPARATORS = (
    "__",
    "\u02bb",
    "\u2033",
    "\u2032\u2032",
    "\u2019\u02bb",
)


def _fullwidth_ascii(text: str) -> str:
    return "".join(
        chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text
    )


_FULLWIDTH_CONTRACTION_CASES = tuple(
    left_variant + separator + right_variant
    for contraction in sorted(phrase for phrase in NEGATE if "'" in phrase)
    for left, right in (contraction.split("'", 1),)
    for left_variant, right_variant in (
        (_fullwidth_ascii(left), _fullwidth_ascii(right)),
        (_fullwidth_ascii(left), right),
        (left, _fullwidth_ascii(right)),
    )
    for separator in _UNSUPPORTED_FULLWIDTH_SEPARATORS
)
_SUPPORTED_FULLWIDTH_CONTRACTION_CASES = tuple(
    left_variant + apostrophe + right_variant
    for contraction in sorted(phrase for phrase in NEGATE if "'" in phrase)
    for left, right in (contraction.split("'", 1),)
    for left_variant, right_variant in (
        (_fullwidth_ascii(left), _fullwidth_ascii(right)),
        (_fullwidth_ascii(left), right),
        (left, _fullwidth_ascii(right)),
    )
    for apostrophe in _APOSTROPHE_VARIANTS
)
_FULLWIDTH_UNICODE_TOKEN_CONTINUATION_CONTROLS = (
    "éｃａｎ__ｔ",
    "_ｃａｎ__ｔ",
    "ｃａｎ__ｔé",
    "ｃａｎ__ｔ_",
    "ｃａｎ__ｔ\u0301",
)


@pytest.mark.parametrize("normalization", _NORMALIZATION_FORMS)
@pytest.mark.parametrize("apostrophe", _APOSTROPHE_VARIANTS)
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
        *(f"yes, can{apostrophe}tastic" for apostrophe in _APOSTROPHE_VARIANTS),
    ],
)
def test_negative_contractions_preserve_whole_token_boundaries(reply):
    assert classify_confirm_reply(reply) == "yes"


def test_unknown_in_word_punctuation_cannot_become_approval():
    assert classify_confirm_reply("yes I can\u055at approve this") == "unclear"


@pytest.mark.parametrize("normalization", _NORMALIZATION_FORMS)
@pytest.mark.parametrize("fragment", _UNSUPPORTED_IN_WORD_FRAGMENTS)
def test_lossy_or_repeated_in_word_material_cannot_become_approval(
    fragment, normalization
):
    reply = f"yes I can{fragment}t approve this"
    if normalization is not None:
        reply = unicodedata.normalize(normalization, reply)
    assert classify_confirm_reply(reply) == "unclear"


@pytest.mark.parametrize("separator", _COMPOSITE_CONTRACTION_SEPARATORS)
def test_composite_contraction_separator_cannot_become_approval(separator):
    assert classify_confirm_reply(f"yes I can{separator}t approve this") == "unclear"


@pytest.mark.parametrize("reply", _ORDINARY_UNICODE_AFFIRMATIONS)
def test_ordinary_unicode_words_do_not_block_affirmation(reply):
    assert classify_confirm_reply(reply) == "yes"


@pytest.mark.parametrize("token", _UNICODE_TOKEN_CONTINUATION_CONTROLS)
def test_malformed_contraction_inside_unicode_token_does_not_block_affirmation(token):
    assert classify_confirm_reply(f"yes {token}") == "yes"


def test_standalone_malformed_contraction_still_blocks_affirmation():
    assert classify_confirm_reply("yes can__t") == "unclear"


@pytest.mark.parametrize("token", _FULLWIDTH_CONTRACTION_CASES)
def test_fullwidth_or_mixed_contraction_skeleton_preserves_separator_provenance(token):
    assert classify_confirm_reply(f"yes {token}") == "unclear"


@pytest.mark.parametrize("token", _SUPPORTED_FULLWIDTH_CONTRACTION_CASES)
def test_fullwidth_or_mixed_contraction_accepts_supported_apostrophe(token):
    assert classify_confirm_reply(f"yes {token}") == "no"


@pytest.mark.parametrize("token", _FULLWIDTH_UNICODE_TOKEN_CONTINUATION_CONTROLS)
def test_fullwidth_malformed_contraction_inside_unicode_token_does_not_block(token):
    assert classify_confirm_reply(f"yes {token}") == "yes"


def test_compatibility_provenance_maps_expanding_character_to_raw_source():
    compatibility, decomposed = confirm_lexicon._compatibility_views("\u2033")

    assert compatibility.text == "\u2032\u2032"
    assert compatibility.source_spans == (
        confirm_lexicon._SourceSpan(0, 1),
        confirm_lexicon._SourceSpan(0, 1),
    )
    assert compatibility.source_spans[0] is compatibility.source_spans[1]
    assert decomposed == compatibility


def test_compatibility_provenance_conservatively_maps_reordered_combining_marks():
    compatibility, decomposed = confirm_lexicon._compatibility_views("a\u0315\u0300")

    assert compatibility.text == "à\u0315"
    assert decomposed.text == "a\u0300\u0315"
    assert all(
        span == confirm_lexicon._SourceSpan(0, 3) for span in decomposed.source_spans
    )
    assert len({id(span) for span in decomposed.source_spans}) == 1


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
def test_normalization_segment_guard_accepts_limit_and_rejects_next_char(prefix):
    limit = confirm_lexicon._MAX_NORMALIZATION_SEGMENT_CHARS
    at_limit = prefix + _alternating_out_of_order_marks(limit - len(prefix))
    over_limit = at_limit + "\u0315"

    assert confirm_lexicon._normalization_segments_are_bounded(at_limit)
    assert not confirm_lexicon._analyze_contraction_provenance(
        at_limit
    ).normalization_rejected
    assert not confirm_lexicon._normalization_segments_are_bounded(over_limit)
    assert confirm_lexicon._analyze_contraction_provenance(
        over_limit
    ).normalization_rejected


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
        confirm_lexicon._MAX_NORMALIZATION_SEGMENT_CHARS + 1
    )
    assert max(normalized_lengths) == 1


def test_segment_guard_does_not_cap_ordinary_text_or_decomposed_spacing_acute():
    long_ordinary_reply = "yes " + "naïve Ελληνικά " * 600

    assert classify_confirm_reply(long_ordinary_reply) == "yes"
    assert classify_confirm_reply("yes I can \u0301t approve this") == "no"


_NON_HANGUL_CCC_ZERO_CANONICAL_COMPOSITIONS = tuple(
    pair
    for pair in confirm_lexicon._canonical_composition_map()
    if unicodedata.combining(pair[1]) == 0
    and not confirm_lexicon._hangul_composition(*pair)
)


def test_unicode_data_includes_all_baseline_ccc_zero_canonical_compositions():
    # Unicode 15.1 has 41 such pairs. Later Unicode releases may add more.
    assert len(_NON_HANGUL_CCC_ZERO_CANONICAL_COMPOSITIONS) >= 41


@pytest.mark.parametrize(
    ("first", "second"), _NON_HANGUL_CCC_ZERO_CANONICAL_COMPOSITIONS
)
def test_segmented_provenance_covers_ccc_zero_canonical_composition(first, second):
    raw = first + second
    compatibility, decomposed = confirm_lexicon._compatibility_views(raw)

    assert compatibility.text == unicodedata.normalize("NFKC", raw)
    assert decomposed.text == unicodedata.normalize("NFD", compatibility.text)
    assert all(
        span == confirm_lexicon._SourceSpan(0, 2) for span in compatibility.source_spans
    )


@pytest.mark.parametrize(
    "raw",
    [
        "\u1100\u1161\u11a8",  # Hangul L/V/T Jamo composition
        "가\u11a8",  # precomposed LV syllable plus trailing T Jamo
        "a\u0315\u0300",  # canonical combining-mark reordering
        "\u00b4",  # compatibility expansion
        "\u2033",  # one-to-many compatibility expansion
        "ｃａｎ__ｔ",  # compatibility-width contraction skeleton
    ],
)
def test_segmented_provenance_matches_whole_string_normalization(raw):
    compatibility, decomposed = confirm_lexicon._compatibility_views(raw)

    assert compatibility.text == unicodedata.normalize("NFKC", raw)
    assert decomposed.text == unicodedata.normalize("NFD", compatibility.text)
    assert len(compatibility.source_spans) == len(compatibility.text)
    assert len(decomposed.source_spans) == len(decomposed.text)


def test_provenance_normalization_work_is_output_sensitive(monkeypatch):
    confirm_lexicon._canonical_composition_map()
    real_normalize = confirm_lexicon.unicodedata.normalize
    work = {"calls": 0, "characters": 0}

    def counted_normalize(form, text):
        work["calls"] += 1
        work["characters"] += len(text)
        return real_normalize(form, text)

    monkeypatch.setattr(confirm_lexicon.unicodedata, "normalize", counted_normalize)
    raw = ("a\u0315\u0300" * 2667)[:8000]

    compatibility, decomposed = confirm_lexicon._compatibility_views(raw)
    output_bound = 2 * (len(raw) + len(compatibility.text))
    assert compatibility.text == unicodedata.normalize("NFKC", raw)
    assert decomposed.text == unicodedata.normalize("NFD", compatibility.text)
    assert work["calls"] <= output_bound + 2
    assert work["characters"] <= output_bound + len(raw) + len(compatibility.text)


@pytest.mark.parametrize(("raw", "expected_calls"), [("\u2033", 6), ("\ufdfa", 38)])
def test_expansion_provenance_work_tracks_normalized_output(
    monkeypatch, raw, expected_calls
):
    confirm_lexicon._canonical_composition_map()
    real_normalize = confirm_lexicon.unicodedata.normalize
    calls = 0

    def counted_normalize(form, text):
        nonlocal calls
        calls += 1
        return real_normalize(form, text)

    monkeypatch.setattr(confirm_lexicon.unicodedata, "normalize", counted_normalize)
    compatibility, _ = confirm_lexicon._compatibility_views(raw)

    assert calls <= expected_calls
    assert len({id(span) for span in compatibility.source_spans}) == 1


def test_classifier_reconstructs_many_decomposed_apostrophes_in_linear_work(
    monkeypatch,
):
    confirm_lexicon._canonical_composition_map()
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

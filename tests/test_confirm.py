import pytest

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

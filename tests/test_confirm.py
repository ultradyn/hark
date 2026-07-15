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


def test_unclear():
    assert classify_confirm_reply("maybe later purple") == "unclear"

"""B009 criterion 2: voice skip/next/status meta-commands in answer windows."""

import pytest

from hark.meta_commands import (
    CANCEL,
    NEXT,
    REPEAT,
    SKIP,
    STATUS,
    classify_meta_command,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("skip", SKIP),
        ("Skip.", SKIP),
        ("skip it", SKIP),
        ("skip this one", SKIP),
        ("next", NEXT),
        ("next one", NEXT),
        ("move on", NEXT),
        ("status", STATUS),
        ("what's the status", STATUS),
        ("what is in the queue", STATUS),
        ("how many are waiting", STATUS),
        ("repeat", REPEAT),
        ("say that again", REPEAT),
        ("what was that?", REPEAT),
        ("cancel", CANCEL),
        ("never mind", CANCEL),
        ("nevermind", CANCEL),
        # Unambiguous "hark"-prefixed control forms (authoritative).
        ("hark skip", SKIP),
        ("hey hark next", NEXT),
        ("ok hark status", STATUS),
        ("hark, cancel", CANCEL),
        ("hark repeat", REPEAT),
    ],
)
def test_classify_recognizes_control_phrases(text, expected):
    assert classify_meta_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # Conversational bare tokens are plausible literal answers — excluded
        # from bare matching to avoid hijacking a real reply.
        "again",
        "pardon",
        # A bare wake word alone is not a command.
        "hark",
        "hey hark",
    ],
)
def test_classify_excludes_conversational_bare_tokens(text):
    assert classify_meta_command(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        # Substantive answers that merely contain a control word must NOT match,
        # otherwise a real reply would be hijacked as a meta-command.
        "skip the failing test and continue",
        "use option two, then move on to the deploy step",
        "the status endpoint returns a 500",
        "next we should refactor the parser",
        "repeat the request to the upstream service",
        "yes",
        "cancel the pending migration and roll back the schema",
    ],
)
def test_classify_ignores_substantive_answers(text):
    assert classify_meta_command(text) is None


def test_run_ask_short_circuits_on_meta_command(monkeypatch):
    """A meta-command in the answer window is returned as control, not sent."""
    import hark.speech as speech
    from hark.speech import ListenResult

    calls = {"confirm_listen": 0, "tts": 0}

    def fake_speak_and_listen(cfg, prompt, **kwargs):
        return (
            {"ok": True, "provider": "test"},
            ListenResult(
                text="skip",
                provider="test",
                duration_ms=100,
                end_mode="silence",
                meta_command="skip",
            ),
        )

    def fake_run_tts(*args, **kwargs):
        calls["tts"] += 1
        return {"ok": True, "provider": "test"}

    def fake_run_listen(*args, **kwargs):
        calls["confirm_listen"] += 1
        return ListenResult(text="", provider="test", duration_ms=0, end_mode="silence")

    monkeypatch.setattr(speech, "speak_and_listen", fake_speak_and_listen)
    monkeypatch.setattr(speech, "run_tts", fake_run_tts)
    monkeypatch.setattr(speech, "run_listen", fake_run_listen)

    class Cfg:
        class confirm:
            mode = "always"

    result = speech.run_ask(cfg=Cfg(), prompt="Allow this action?")

    assert result["ok"] is True
    assert result["meta_command"] == "skip"
    # Must not enter the confirm/readback flow for a control phrase.
    assert calls["confirm_listen"] == 0
    assert calls["tts"] == 0


def test_run_ask_normal_answer_has_no_meta_command(monkeypatch):
    import hark.speech as speech
    from hark.speech import ListenResult

    def fake_speak_and_listen(cfg, prompt, **kwargs):
        return (
            {"ok": True, "provider": "test"},
            ListenResult(
                text="use option two",
                provider="test",
                duration_ms=100,
                end_mode="silence",
                meta_command=None,
            ),
        )

    class Cfg:
        class confirm:
            mode = "never"

    monkeypatch.setattr(speech, "speak_and_listen", fake_speak_and_listen)

    result = speech.run_ask(cfg=Cfg(), prompt="Which option?")
    assert result["ok"] is True
    assert "meta_command" not in result
    assert result["text"] == "use option two"

"""B186: --format text prints only the transcript for ask / listen / tts --listen.

JSON-only stdout loses the operator's answer to naive truncation: the success
payload puts ``text`` second and the ~20-key ``tts`` sub-object last, so
``| tail -c N`` deterministically keeps telemetry and throws away the answer.
Text mode exists so ``answer=$(hark ask --format text "…")`` is the whole
contract; anything with no answer to print keeps the full JSON.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hark import cli
from hark.config import HarkConfig
from hark.exitcodes import ABORT, OK
from hark.speech import ListenResult

ANSWER = "the answer is forty two"


def _ask_args(**over) -> argparse.Namespace:
    base = dict(
        text=["Deploy", "now?"],
        confirm=None,
        end_mode=None,
        provider=None,
        json=False,
        format="text",
        event_id=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _listen_args(**over) -> argparse.Namespace:
    base = dict(
        provider=None,
        end_mode=None,
        json=False,
        format="text",
        event_id=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _tts_args(**over) -> argparse.Namespace:
    base = dict(
        text=["Ready", "to", "ship?"],
        provider=None,
        voice=None,
        no_play=False,
        out=None,
        json=False,
        format="text",
        listen=True,
        end_mode=None,
        event_id=None,
        standalone=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _fake_ask(monkeypatch, result: dict) -> None:
    monkeypatch.setattr("hark.speech.run_ask", lambda *a, **k: dict(result))


def _fake_listen(monkeypatch, result: ListenResult) -> None:
    monkeypatch.setattr("hark.speech.run_listen", lambda *a, **k: result)


def _fake_speak_and_listen(monkeypatch, listened: ListenResult) -> None:
    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: ({"ok": True, "provider": "mock", "voice": "eve"}, listened),
    )


def _ok_ask_result(**over) -> dict:
    base = {
        "ok": True,
        "text": ANSWER,
        "provider": "mock",
        "duration_ms": 900,
        "end_mode": "silence",
        "end_phrase": None,
        "risk": "R1",
        "tts": {"ok": True, "provider": "mock", "voice": "eve"},
        "exit": OK,
    }
    base.update(over)
    return base


# --- ask -------------------------------------------------------------------


def test_ask_format_text_prints_only_the_transcript(monkeypatch, capsys):
    _fake_ask(monkeypatch, _ok_ask_result())

    code = cli.cmd_ask(_ask_args(), HarkConfig())

    assert code == OK
    out = capsys.readouterr().out
    assert out == ANSWER + "\n"


def test_ask_format_json_is_unchanged(monkeypatch, capsys):
    _fake_ask(monkeypatch, _ok_ask_result())

    code = cli.cmd_ask(_ask_args(format="json"), HarkConfig())

    assert code == OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["text"] == ANSWER
    assert payload["tts"]["provider"] == "mock"


def test_ask_without_format_attribute_still_prints_json(monkeypatch, capsys):
    """Library/test callers build Namespaces by hand; absence must mean JSON."""
    _fake_ask(monkeypatch, _ok_ask_result())
    args = _ask_args()
    del args.format

    code = cli.cmd_ask(args, HarkConfig())

    assert code == OK
    assert json.loads(capsys.readouterr().out)["text"] == ANSWER


def test_ask_format_text_failure_keeps_json_and_exit_code(monkeypatch, capsys):
    _fake_ask(
        monkeypatch,
        {
            "ok": False,
            "error": "provider failed",
            "text": "",
            "exit": 4,
            "tts": {"ok": False},
        },
    )

    code = cli.cmd_ask(_ask_args(), HarkConfig())

    assert code == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == "provider failed"


def test_ask_format_text_cancelled_keeps_json(monkeypatch, capsys):
    """A cancelled turn is not an answer: keep the JSON, keep exit ABORT."""
    _fake_ask(
        monkeypatch,
        {
            "ok": False,
            "cancelled": True,
            "text": "half an answ",
            "end_phrase": "cancel",
            "exit": ABORT,
            "tts": None,
        },
    )

    code = cli.cmd_ask(_ask_args(), HarkConfig())

    assert code == ABORT
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancelled"] is True
    assert payload["text"] == "half an answ"


def test_ask_format_text_meta_command_keeps_json(monkeypatch, capsys):
    """meta_command is control, not a transcript to hand back as an answer."""
    _fake_ask(
        monkeypatch,
        _ok_ask_result(meta_command="repeat", text="repeat"),
    )

    code = cli.cmd_ask(_ask_args(), HarkConfig())

    assert code == OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["meta_command"] == "repeat"


def test_ask_format_text_empty_transcript_keeps_json(monkeypatch, capsys):
    """A blank line would strand the caller with no answer and no diagnosis."""
    _fake_ask(monkeypatch, _ok_ask_result(text="   "))

    code = cli.cmd_ask(_ask_args(), HarkConfig())

    assert code == OK
    assert json.loads(capsys.readouterr().out)["ok"] is True


# --- listen ----------------------------------------------------------------


def test_listen_format_text_prints_only_the_transcript(monkeypatch, capsys):
    _fake_listen(
        monkeypatch,
        ListenResult(
            text=ANSWER,
            provider="mock",
            duration_ms=700,
            end_mode="silence",
            stream_id="s1",
        ),
    )

    code = cli.cmd_listen(_listen_args(), HarkConfig())

    assert code == OK
    assert capsys.readouterr().out == ANSWER + "\n"


def test_listen_format_text_cancelled_keeps_json(monkeypatch, capsys):
    _fake_listen(
        monkeypatch,
        ListenResult(
            text="never mind",
            provider="mock",
            duration_ms=300,
            end_mode="silence",
            end_phrase="cancel",
            cancelled=True,
        ),
    )

    code = cli.cmd_listen(_listen_args(), HarkConfig())

    assert code == ABORT
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancelled"] is True


def test_listen_format_text_meta_command_keeps_json(monkeypatch, capsys):
    _fake_listen(
        monkeypatch,
        ListenResult(
            text="repeat",
            provider="mock",
            duration_ms=200,
            end_mode="silence",
            meta_command="repeat",
        ),
    )

    code = cli.cmd_listen(_listen_args(), HarkConfig())

    assert code == OK
    assert json.loads(capsys.readouterr().out)["meta_command"] == "repeat"


# --- tts --listen ----------------------------------------------------------


def test_tts_listen_format_text_prints_only_the_transcript(monkeypatch, capsys):
    _fake_speak_and_listen(
        monkeypatch,
        ListenResult(
            text=ANSWER,
            provider="mock",
            duration_ms=800,
            end_mode="silence",
            stream_id="s2",
        ),
    )

    code = cli.cmd_tts(_tts_args(), HarkConfig())

    assert code == OK
    assert capsys.readouterr().out == ANSWER + "\n"


def test_tts_listen_format_text_cancelled_keeps_json(monkeypatch, capsys):
    _fake_speak_and_listen(
        monkeypatch,
        ListenResult(
            text="forget it",
            provider="mock",
            duration_ms=250,
            end_mode="silence",
            end_phrase="cancel",
            cancelled=True,
        ),
    )

    code = cli.cmd_tts(_tts_args(), HarkConfig())

    assert code == ABORT
    payload = json.loads(capsys.readouterr().out)
    assert payload["cancelled"] is True


def test_tts_without_listen_format_text_keeps_json(monkeypatch, capsys):
    """No listen means no transcript; text mode has nothing to print."""
    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *a, **k: {"ok": True, "provider": "mock", "voice": "eve"},
    )

    code = cli.cmd_tts(_tts_args(listen=False), HarkConfig())

    assert code == OK
    assert json.loads(capsys.readouterr().out)["ok"] is True


# --- flag surface ----------------------------------------------------------


@pytest.mark.parametrize("cmd", ["ask", "listen", "tts"])
def test_format_defaults_to_json(cmd):
    argv = [cmd, "hello"] if cmd in ("ask", "tts") else [cmd]
    args = cli.build_parser().parse_args(argv)
    assert args.format == "json"
    assert args.json is False


@pytest.mark.parametrize("cmd", ["ask", "listen", "tts"])
def test_text_is_an_alias_of_format_text(cmd):
    argv = [cmd, "--text", "hello"] if cmd in ("ask", "tts") else [cmd, "--text"]
    args = cli.build_parser().parse_args(argv)
    assert args.format == "text"


@pytest.mark.parametrize("cmd", ["ask", "tts"])
def test_text_alias_leaves_the_text_positional_alone(cmd):
    """--text must not steal the positional it shares a name with."""
    args = cli.build_parser().parse_args([cmd, "--text", "ship", "it"])
    assert args.text == ["ship", "it"]
    assert args.format == "text"


@pytest.mark.parametrize("cmd", ["ask", "listen", "tts"])
@pytest.mark.parametrize("flag", [["--format", "text"], ["--text"]])
def test_json_and_text_mode_are_mutually_exclusive(cmd, flag, capsys):
    argv = [cmd, "--json", *flag]
    if cmd in ("ask", "tts"):
        argv.append("hello")
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(argv)
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


@pytest.mark.parametrize("cmd", ["ask", "listen", "tts"])
def test_format_flag_is_documented_in_help(cmd):
    """The fallback rule is a contract: it has to be in --help (B186)."""
    parser = cli.build_parser()
    action = next(
        a
        for a in parser._subparsers._group_actions[0].choices[cmd]._actions  # type: ignore[attr-defined]
        if "--format" in a.option_strings
    )
    assert action.help
    assert "json" in action.help.lower()


# --- end-to-end: the shell-capture goal ------------------------------------


def test_main_ask_format_text_stdout_is_exactly_the_answer(monkeypatch, capsys):
    """`answer=$(hark ask --format text "…")` must capture only the answer.

    capsys makes stdout a non-TTY, so the B109 pipe warning fires — it must
    land on stderr, leaving stdout clean without HARK_NO_PIPE_WARN=1.
    """
    monkeypatch.delenv("HARK_NO_PIPE_WARN", raising=False)
    monkeypatch.setattr("hark.stdio._WARNED", False)
    _fake_ask(monkeypatch, _ok_ask_result())

    code = cli.main(["ask", "--confirm", "never", "--format", "text", "Deploy now?"])

    captured = capsys.readouterr()
    assert code == OK
    assert captured.out == ANSWER + "\n"
    assert "warning: stdout is not a TTY" in captured.err

"""B187: --provider must not conflate the TTS and STT legs.

Regression cover for the filed bug: `hark ask --provider minimax "…"` spoke
fine then died on the listen leg with "minimax STT unsupported", because one
flag drove both legs.
"""

from __future__ import annotations

import argparse

import hark.cli as cli
import pytest
from hark.config import HarkConfig
from hark.exitcodes import OK
from hark.providers.legs import split_leg_providers
from hark.speech import ListenResult


# --- pure selection ---------------------------------------------------------


def test_bare_provider_still_drives_both_legs() -> None:
    legs = split_leg_providers(provider="xai")
    assert (legs.tts, legs.stt) == ("xai", "xai")


def test_no_flags_leaves_both_legs_on_config() -> None:
    legs = split_leg_providers()
    assert (legs.tts, legs.stt) == (None, None)


def test_specific_flag_wins_over_shared_provider() -> None:
    legs = split_leg_providers(provider="minimax", stt_provider="custom")
    assert legs.tts == "minimax"
    assert legs.stt == "custom"

    legs = split_leg_providers(provider="google", tts_provider="minimax")
    assert legs.tts == "minimax"
    assert legs.stt == "google"


def test_per_leg_flags_are_independent() -> None:
    legs = split_leg_providers(tts_provider="minimax", stt_provider="custom")
    assert legs.tts == "minimax"
    assert legs.stt == "custom"


def test_blank_values_fall_through_to_the_shared_provider() -> None:
    legs = split_leg_providers(provider="xai", tts_provider="", stt_provider="   ")
    assert (legs.tts, legs.stt) == ("xai", "xai")


def test_blank_everything_is_none_not_empty_string() -> None:
    legs = split_leg_providers(provider="", tts_provider="", stt_provider="")
    assert (legs.tts, legs.stt) == (None, None)


# --- CLI wiring -------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    return cli.build_parser().parse_args(argv)


@pytest.mark.parametrize("command", ["tts", "ask"])
def test_split_flags_exist_on_speak_then_listen_commands(command: str) -> None:
    args = _parse([command, "hello", "--tts-provider", "minimax", "--stt-provider", "custom"])
    assert args.tts_provider == "minimax"
    assert args.stt_provider == "custom"
    assert args.provider is None


def test_listen_accepts_provider_and_stt_provider_together() -> None:
    # STT-only command: the pair names one leg and must not be a usage error.
    args = _parse(["listen", "--provider", "xai", "--stt-provider", "custom"])
    legs = split_leg_providers(
        provider=args.provider,
        stt_provider=args.stt_provider,
    )
    assert legs.stt == "custom"

    # Reverse order on the command line gives the same answer (no dest aliasing).
    args = _parse(["listen", "--stt-provider", "custom", "--provider", "xai"])
    legs = split_leg_providers(
        provider=args.provider,
        stt_provider=args.stt_provider,
    )
    assert legs.stt == "custom"


def test_listen_has_no_tts_provider_flag() -> None:
    with pytest.raises(SystemExit):
        _parse(["listen", "--tts-provider", "minimax"])


def test_cmd_tts_listen_sends_each_provider_to_its_own_leg(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}

    def fake_speak(cfg, text, **kwargs):
        seen.update(kwargs)
        return (
            {"ok": True, "provider": "minimax", "voice": "eve"},
            ListenResult(
                text="yes please",
                provider="custom",
                duration_ms=400,
                end_mode="silence",
                stream_id="s1",
            ),
        )

    monkeypatch.setattr("hark.speech.speak_and_listen", fake_speak)

    args = _parse(
        [
            "tts",
            "question?",
            "--listen",
            "--provider",
            "minimax",
            "--stt-provider",
            "custom",
            "--json",
        ]
    )
    assert cli.cmd_tts(args, HarkConfig()) == OK
    # The bug: minimax reached the STT leg and hard-failed it.
    assert seen["provider"] == "minimax"
    assert seen["stt_provider"] == "custom"
    out = capsys.readouterr().out
    assert "yes please" in out


def test_cmd_tts_without_listen_uses_the_tts_leg(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}

    def fake_run_tts(cfg, text, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "provider": "minimax", "voice": "eve"}

    monkeypatch.setattr("hark.speech.run_tts", fake_run_tts)

    args = _parse(["tts", "hello", "--provider", "google", "--tts-provider", "minimax"])
    assert cli.cmd_tts(args, HarkConfig()) == OK
    assert seen["provider"] == "minimax"
    capsys.readouterr()


def test_cmd_listen_uses_the_stt_leg(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}

    def fake_run_listen(cfg, **kwargs):
        seen.update(kwargs)
        return ListenResult(
            text="ok",
            provider="custom",
            duration_ms=100,
            end_mode="silence",
            stream_id="s2",
        )

    monkeypatch.setattr("hark.speech.run_listen", fake_run_listen)

    args = _parse(["listen", "--provider", "xai", "--stt-provider", "custom"])
    assert cli.cmd_listen(args, HarkConfig()) == OK
    assert seen["provider"] == "custom"
    capsys.readouterr()


def test_cmd_ask_splits_the_legs(monkeypatch, capsys) -> None:
    seen: dict[str, object] = {}

    def fake_run_ask(cfg, prompt, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "text": "sure", "exit": OK}

    monkeypatch.setattr("hark.speech.run_ask", fake_run_ask)

    args = _parse(["ask", "shall", "we?", "--provider", "minimax", "--stt-provider", "custom"])
    assert cli.cmd_ask(args, HarkConfig()) == OK
    assert seen["tts_provider"] == "minimax"
    assert seen["stt_provider"] == "custom"
    capsys.readouterr()


def test_provider_help_documents_the_precedence() -> None:
    parser = cli.build_parser()
    tts_action = next(
        a
        for a in _subparser(parser, "tts")._actions
        if "--provider" in getattr(a, "option_strings", [])
    )
    assert tts_action.help and "--tts-provider" in tts_action.help


def _subparser(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices[name]
    raise AssertionError("no subparsers on the hark parser")

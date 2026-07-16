"""Provider failures keep the ask result and CLI contracts stable."""

from __future__ import annotations

import argparse
import json

import pytest

from hark import cli
from hark.config import HarkConfig
from hark.providers.base import ProviderError
from hark.speech import ListenResult, run_ask


def _initial_answer(monkeypatch) -> HarkConfig:
    cfg = HarkConfig()
    cfg.confirm.mode = "always"
    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: (
            {"ok": True, "provider": "mock", "voice": "eve"},
            ListenResult(
                text="deploy the release",
                provider="mock",
                duration_ms=125,
                end_mode="silence",
                stream_id="answer",
            ),
        ),
    )
    return cfg


def _confirmation_provider_failure(monkeypatch, *, code: object = 23) -> HarkConfig:
    cfg = _initial_answer(monkeypatch)
    monkeypatch.setattr(
        "hark.speech.run_tts", lambda *a, **k: {"ok": True, "provider": "mock"}
    )

    def fail_confirmation(*args, **kwargs):
        raise ProviderError("confirmation provider failed", code=code)  # type: ignore[arg-type]

    monkeypatch.setattr("hark.speech.run_listen", fail_confirmation)
    return cfg


def _readback_provider_failure(monkeypatch, *, code: int | None = None) -> HarkConfig:
    cfg = _initial_answer(monkeypatch)

    def fail_readback(*args, **kwargs):
        if code is None:
            raise ProviderError("readback provider failed")
        raise ProviderError("readback provider failed", code=code)

    def unexpected_listen(*args, **kwargs):
        raise AssertionError("confirmation listen must not run after TTS failure")

    monkeypatch.setattr("hark.speech.run_tts", fail_readback)
    monkeypatch.setattr("hark.speech.run_listen", unexpected_listen)
    return cfg


def test_run_ask_confirmation_provider_error_preserves_answer_and_tts(monkeypatch):
    cfg = _confirmation_provider_failure(monkeypatch)

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result == {
        "ok": False,
        "error": "confirmation provider failed",
        "exit": 23,
        "text": "deploy the release",
        "tts": {"ok": True, "provider": "mock", "voice": "eve"},
    }


def test_cmd_ask_serializes_confirmation_provider_error(monkeypatch, capsys):
    cfg = _confirmation_provider_failure(monkeypatch, code=29)
    args = argparse.Namespace(
        text=["Deploy", "now?"],
        confirm=None,
        end_mode=None,
        provider=None,
        json=True,
        event_id="event-149",
    )

    exit_code = cli.cmd_ask(args, cfg)

    assert exit_code == 29
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "confirmation provider failed",
        "exit": 29,
        "text": "deploy the release",
        "tts": {"ok": True, "provider": "mock", "voice": "eve"},
        "for_event": "event-149",
    }


def test_run_ask_readback_provider_error_uses_default_code(monkeypatch):
    cfg = _readback_provider_failure(monkeypatch)

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result == {
        "ok": False,
        "error": "readback provider failed",
        "exit": 4,
        "text": "deploy the release",
        "tts": {"ok": True, "provider": "mock", "voice": "eve"},
    }


def test_cmd_ask_serializes_readback_provider_error(monkeypatch, capsys):
    cfg = _readback_provider_failure(monkeypatch, code=31)
    args = argparse.Namespace(
        text=["Deploy", "now?"],
        confirm=None,
        end_mode=None,
        provider=None,
        json=True,
        event_id="event-149-readback",
    )

    exit_code = cli.cmd_ask(args, cfg)

    assert exit_code == 31
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "readback provider failed",
        "exit": 31,
        "text": "deploy the release",
        "tts": {"ok": True, "provider": "mock", "voice": "eve"},
        "for_event": "event-149-readback",
    }


def _ask_args(event_id: str) -> argparse.Namespace:
    return argparse.Namespace(
        text=["Deploy", "now?"],
        confirm=None,
        end_mode=None,
        provider=None,
        json=True,
        event_id=event_id,
    )


@pytest.mark.parametrize(
    ("setup_failure", "error"),
    [
        pytest.param(
            _confirmation_provider_failure,
            "confirmation provider failed",
            id="confirmation-listen",
        ),
        pytest.param(
            _readback_provider_failure,
            "readback provider failed",
            id="confirmation-readback",
        ),
    ],
)
def test_run_ask_zero_provider_code_is_nonzero_failure(
    monkeypatch, setup_failure, error
):
    cfg = setup_failure(monkeypatch, code=0)

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result == {
        "ok": False,
        "error": error,
        "exit": 4,
        "text": "deploy the release",
        "tts": {"ok": True, "provider": "mock", "voice": "eve"},
    }


@pytest.mark.parametrize(
    ("setup_failure", "error"),
    [
        pytest.param(
            _confirmation_provider_failure,
            "confirmation provider failed",
            id="confirmation-listen",
        ),
        pytest.param(
            _readback_provider_failure,
            "readback provider failed",
            id="confirmation-readback",
        ),
    ],
)
def test_cmd_ask_zero_provider_code_exits_failure(
    monkeypatch, capsys, setup_failure, error
):
    cfg = setup_failure(monkeypatch, code=0)

    exit_code = cli.cmd_ask(_ask_args("event-154"), cfg)

    assert exit_code == 4
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": error,
        "exit": 4,
        "text": "deploy the release",
        "tts": {"ok": True, "provider": "mock", "voice": "eve"},
        "for_event": "event-154",
    }


@pytest.mark.parametrize("code", [-1, 256, "4", None, True])
def test_run_ask_normalizes_invalid_provider_failure_codes(monkeypatch, code):
    cfg = _confirmation_provider_failure(monkeypatch, code=code)

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result["ok"] is False
    assert result["exit"] == 4


@pytest.mark.parametrize("code", [1, 4, 23, 255])
def test_run_ask_preserves_valid_nonzero_provider_failure_codes(monkeypatch, code):
    cfg = _confirmation_provider_failure(monkeypatch, code=code)

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result["ok"] is False
    assert result["exit"] == code


@pytest.mark.parametrize("code", [0, -1, 256, "4", None, True])
def test_cmd_ask_normalizes_any_failed_result_exit(monkeypatch, capsys, code):
    monkeypatch.setattr(
        "hark.speech.run_ask",
        lambda *args, **kwargs: {
            "ok": False,
            "error": "provider result failed",
            "exit": code,
        },
    )
    exit_code = cli.cmd_ask(_ask_args("event-154-boundary"), HarkConfig())

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["exit"] == 1


@pytest.mark.parametrize("code", [0, -1, 256, "4", None, True])
def test_main_normalizes_uncaught_provider_error_exit(monkeypatch, code):
    def fail_dispatch(*args, **kwargs):
        raise ProviderError("provider failed", code=code)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: HarkConfig())
    monkeypatch.setattr(cli, "dispatch", fail_dispatch)

    assert cli.main(["doctor", "--json"]) == 4

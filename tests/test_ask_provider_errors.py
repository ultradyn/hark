"""Provider failures keep the ask result and CLI contracts stable."""

from __future__ import annotations

import argparse
import json

import pytest

from hark import cli
from hark.config import HarkConfig
from hark.exitcodes import normalize_failure_exit
from hark.providers.base import ProviderError
from hark.speech import ListenResult, run_ask


class _IntExit(int):
    """Runtime int subclass must not widen the exact public-code contract."""


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
        "exit": 4,
        "text": "deploy the release",
        "tts": {"ok": True, "provider": "mock", "voice": "eve"},
    }


def test_cmd_ask_serializes_confirmation_provider_error(monkeypatch, capsys):
    cfg = _confirmation_provider_failure(monkeypatch, code=23)
    args = argparse.Namespace(
        text=["Deploy", "now?"],
        confirm=None,
        end_mode=None,
        provider=None,
        json=True,
        event_id="event-149",
    )

    exit_code = cli.cmd_ask(args, cfg)

    assert exit_code == 4
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "confirmation provider failed",
        "exit": 4,
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
    cfg = _readback_provider_failure(monkeypatch, code=255)
    args = argparse.Namespace(
        text=["Deploy", "now?"],
        confirm=None,
        end_mode=None,
        provider=None,
        json=True,
        event_id="event-149-readback",
    )

    exit_code = cli.cmd_ask(args, cfg)

    assert exit_code == 4
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "readback provider failed",
        "exit": 4,
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


@pytest.mark.parametrize("code", [0, -1, 8, 23, 255, 256, "4", None, True, _IntExit(4)])
def test_run_ask_normalizes_invalid_provider_failure_codes(monkeypatch, code):
    cfg = _confirmation_provider_failure(monkeypatch, code=code)

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result["ok"] is False
    assert result["exit"] == 4


@pytest.mark.parametrize("code", range(1, 8))
def test_run_ask_preserves_canonical_provider_failure_codes(monkeypatch, code):
    cfg = _confirmation_provider_failure(monkeypatch, code=code)

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result["ok"] is False
    assert result["exit"] == code


@pytest.mark.parametrize("code", [0, -1, 8, 23, 255, 256, "4", None, True, _IntExit(4)])
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


@pytest.mark.parametrize("code", range(1, 8))
def test_cmd_ask_preserves_canonical_failed_result_exit(monkeypatch, capsys, code):
    monkeypatch.setattr(
        "hark.speech.run_ask",
        lambda *args, **kwargs: {
            "ok": False,
            "error": "canonical failure",
            "exit": code,
        },
    )

    exit_code = cli.cmd_ask(_ask_args("event-154-canonical"), HarkConfig())

    assert exit_code == code
    assert json.loads(capsys.readouterr().out)["exit"] == code


@pytest.mark.parametrize("code", [0, -1, 8, 23, 255, 256, "4", None, True, _IntExit(4)])
def test_main_normalizes_uncaught_provider_error_exit(monkeypatch, code):
    def fail_dispatch(*args, **kwargs):
        raise ProviderError("provider failed", code=code)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: HarkConfig())
    monkeypatch.setattr(cli, "dispatch", fail_dispatch)

    assert cli.main(["doctor", "--json"]) == 4


@pytest.mark.parametrize("code", range(1, 8))
def test_normalize_failure_exit_preserves_canonical_failures(code):
    assert normalize_failure_exit(code, fallback=4) == code


@pytest.mark.parametrize("code", [0, -1, 8, 23, 255, 256, "4", None, True, _IntExit(4)])
def test_normalize_failure_exit_rejects_noncanonical_codes(code):
    assert normalize_failure_exit(code, fallback=4) == 4


@pytest.mark.parametrize("fallback", [0, 8, True, _IntExit(4)])
def test_normalize_failure_exit_rejects_noncanonical_fallback(fallback):
    with pytest.raises(ValueError, match="canonical failure exit"):
        normalize_failure_exit(4, fallback=fallback)  # type: ignore[arg-type]


def test_cmd_ask_preserves_success_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        "hark.speech.run_ask",
        lambda *args, **kwargs: {"ok": True, "text": "deploy", "exit": 0},
    )

    exit_code = cli.cmd_ask(_ask_args("event-154-success"), HarkConfig())

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "text": "deploy",
        "exit": 0,
        "for_event": "event-154-success",
    }


class _FailureSurfaceEscape(BaseException):
    """Secondary failure which must not replace the caught provider error."""


class _HostileProviderError(ProviderError):
    def __init__(self, hostile_surface: str | None, detail: str = "provider failed"):
        RuntimeError.__init__(self, detail)
        self._hostile_surface = hostile_surface
        self._detail = detail

    def __str__(self) -> str:
        if self._hostile_surface == "detail":
            raise _FailureSurfaceEscape("hostile __str__")
        return self._detail

    @property
    def code(self) -> int:
        if self._hostile_surface == "code":
            raise _FailureSurfaceEscape("hostile code")
        return 7

    @property
    def tts_info(self) -> dict[str, object]:
        if self._hostile_surface == "tts_info":
            raise _FailureSurfaceEscape("hostile tts_info")
        return {"ok": True, "provider": "partial-tts"}


def _ask_failure_at(monkeypatch, boundary: str, exc: ProviderError) -> HarkConfig:
    if boundary == "initial-speak-listen":
        cfg = HarkConfig()

        def fail_initial(*args, **kwargs):
            raise exc

        monkeypatch.setattr("hark.speech.speak_and_listen", fail_initial)
        return cfg

    cfg = _initial_answer(monkeypatch)
    if boundary == "confirmation-readback":

        def fail_readback(*args, **kwargs):
            raise exc

        monkeypatch.setattr("hark.speech.run_tts", fail_readback)
        monkeypatch.setattr(
            "hark.speech.run_listen",
            lambda *args, **kwargs: pytest.fail(
                "confirmation listen ran after readback failure"
            ),
        )
        return cfg

    assert boundary == "confirmation-listen"
    monkeypatch.setattr(
        "hark.speech.run_tts", lambda *args, **kwargs: {"ok": True}
    )

    def fail_confirmation(*args, **kwargs):
        raise exc

    monkeypatch.setattr("hark.speech.run_listen", fail_confirmation)
    return cfg


@pytest.mark.parametrize(
    "boundary",
    ["initial-speak-listen", "confirmation-readback", "confirmation-listen"],
)
@pytest.mark.parametrize("hostile_surface", ["detail", "code"])
def test_ask_provider_boundaries_contain_hostile_baseexception_surfaces(
    monkeypatch, boundary, hostile_surface
):
    cfg = _ask_failure_at(monkeypatch, boundary, _HostileProviderError(hostile_surface))

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result["ok"] is False
    assert result["error"] == (
        "provider failure" if hostile_surface == "detail" else "provider failed"
    )
    assert result["exit"] == (4 if hostile_surface == "code" else 7)
    if boundary == "initial-speak-listen":
        assert result["tts"] == {"ok": True, "provider": "partial-tts"}
        assert "text" not in result
    else:
        assert result["text"] == "deploy the release"
        assert result["tts"] == {"ok": True, "provider": "mock", "voice": "eve"}


def test_initial_ask_contains_hostile_tts_info_baseexception(monkeypatch):
    exc = _HostileProviderError("tts_info")
    cfg = _ask_failure_at(monkeypatch, "initial-speak-listen", exc)

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result == {
        "ok": False,
        "error": "provider failed",
        "exit": 7,
        "tts": None,
    }


class _ProviderTimeoutError(_HostileProviderError, TimeoutError):
    pass


def test_initial_hybrid_provider_timeout_keeps_provider_semantics(monkeypatch):
    exc = _ProviderTimeoutError("code")
    cfg = _ask_failure_at(monkeypatch, "initial-speak-listen", exc)

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result == {
        "ok": False,
        "error": "provider failed",
        "exit": 4,
        "tts": {"ok": True, "provider": "partial-tts"},
    }


@pytest.mark.parametrize("hostile_surface", ["detail", "code"])
def test_main_contains_hostile_provider_baseexception_surfaces(
    monkeypatch, capsys, hostile_surface
):
    exc = _HostileProviderError(hostile_surface)
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: HarkConfig())

    def fail_dispatch(*args, **kwargs):
        raise exc

    monkeypatch.setattr(cli, "dispatch", fail_dispatch)

    assert cli.main(["doctor", "--json"]) == (4 if hostile_surface == "code" else 7)
    detail = capsys.readouterr().err.removeprefix("hark: provider: ").rstrip("\n")
    assert detail == (
        "provider failure" if hostile_surface == "detail" else "provider failed"
    )


def test_provider_detail_is_bounded_and_control_safe_at_ask_boundary(monkeypatch):
    unsafe_detail = "\x1b[31mprovider\nfailed\x00" + ("x" * 1_000)
    cfg = _ask_failure_at(
        monkeypatch,
        "initial-speak-listen",
        _HostileProviderError(None, unsafe_detail),
    )

    result = run_ask(cfg, "Deploy now?", risk_hint="R2")

    assert result["ok"] is False
    assert len(result["error"]) <= 400
    assert all(character.isprintable() for character in result["error"])
    assert result["error"].endswith("...")

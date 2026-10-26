"""Answer-window nudge observations survive direct CLI response boundaries."""

import argparse
import json
from types import SimpleNamespace

import pytest

from hark import cli
from hark.config import HarkConfig
from hark.providers.base import ProviderError


NUDGES = [{"ok": False, "error": "nudge provider unavailable"}]


def args(**values):
    return argparse.Namespace(provider=None, end_mode=None, event_id=None, json=True,
                              format="json", text=["Question"], voice=None,
                              no_play=False, out=None, listen=True, standalone=False, **values)


@pytest.mark.parametrize("command", ["listen", "tts_listen"])
@pytest.mark.parametrize("failure", [False, True])
def test_nudge_diagnostics_emitted_and_recorded_for_empty_answers(monkeypatch, capsys, command, failure):
    records = []
    monkeypatch.setattr(cli, "record_turn", lambda payload, **kw: records.append(payload))
    result = SimpleNamespace(text="", provider="xai", duration_ms=0, end_mode="silence",
                             end_phrase=None, stream_id="s", cancelled=False, meta_command=None,
                             tts_nudges=NUDGES)
    def operation(*a, **kw):
        if failure:
            exc = ProviderError("STT unavailable")
            exc.tts_nudges = NUDGES
            raise exc
        return result if command == "listen" else ({"provider": "xai"}, result)
    monkeypatch.setattr("hark.speech.run_listen" if command == "listen" else
                        "hark.speech.speak_and_listen", operation)
    (cli.cmd_listen if command == "listen" else cli.cmd_tts)(args(), HarkConfig())
    payload = json.loads(capsys.readouterr().out)
    assert payload["tts_nudges"] == NUDGES
    assert records[-1]["tts_nudges"] == NUDGES
    assert payload["ok"] is not failure

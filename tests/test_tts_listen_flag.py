import argparse

import hark.cli as cli
from hark.exitcodes import OK
from hark.speech import ListenResult


def test_tts_listen_flag_chains_listen(monkeypatch, capsys):
    calls = {"tts": 0, "listen": 0}

    def fake_tts(cfg, text, **kwargs):
        calls["tts"] += 1
        assert kwargs.get("play") is True
        # near-end pre-arm should be wired when listening
        return {
            "ok": True,
            "provider": "xai",
            "voice": "eve",
            "mic_muted": True,
        }

    def fake_listen(cfg, **kwargs):
        calls["listen"] += 1
        assert "last_tts" in kwargs
        return ListenResult(
            text="option one",
            provider="xai",
            duration_ms=500,
            end_mode="silence",
            stream_id="stest",
        )

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    args = argparse.Namespace(
        text=["hello", "there"],
        provider=None,
        voice=None,
        no_play=False,
        out=None,
        json=True,
        listen=True,
        end_mode=None,
    )
    # minimal cfg object with audio fields used by cmd_tts
    from hark.config import HarkConfig

    code = cli.cmd_tts(args, HarkConfig())
    assert code == OK
    assert calls["tts"] == 1
    assert calls["listen"] == 1
    out = capsys.readouterr().out
    assert "option one" in out
    assert "tts" in out


def test_tts_without_listen_skips_listen(monkeypatch, capsys):
    calls = {"listen": 0}

    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *a, **k: {"ok": True, "provider": "xai"},
    )

    def boom(*a, **k):
        calls["listen"] += 1
        raise AssertionError("listen should not run")

    monkeypatch.setattr("hark.speech.run_listen", boom)

    args = argparse.Namespace(
        text=["hi"],
        provider=None,
        voice=None,
        no_play=False,
        out=None,
        json=False,
        listen=False,
        end_mode=None,
    )
    from hark.config import HarkConfig

    assert cli.cmd_tts(args, HarkConfig()) == OK
    assert calls["listen"] == 0

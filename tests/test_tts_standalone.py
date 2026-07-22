"""B160: `hark tts --standalone` — one-shot TTS from any cwd, server optional.

The flag promises self-contained execution: when a hark server (harkd or
mode-a workers) is live its coordination is used, otherwise everything runs
in-process. Detection is pidfile-based under the XDG state dir, so it must
work from any working directory.
"""

import argparse
import json
import os

import hark.cli as cli
from hark.config import HarkConfig
from hark.exitcodes import OK


def _fake_tts_result() -> dict:
    return {
        "ok": True,
        "provider": "mock",
        "voice": "eve",
        "mic_muted": False,
    }


def _ns(**overrides) -> argparse.Namespace:
    base = dict(
        text=["hello"],
        provider=None,
        voice=None,
        no_play=False,
        out=None,
        json=False,
        listen=False,
        end_mode=None,
        standalone=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _write_live_pidfile(state_root, name: str) -> None:
    """Write a pidfile claiming our own (live) pid for a daemon probe."""
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / name).write_text(f"{os.getpid()}\n", encoding="utf-8")


def test_standalone_flag_off_keeps_output_unchanged(monkeypatch, capsys):
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: _fake_tts_result())

    code = cli.cmd_tts(_ns(standalone=False, json=True), HarkConfig())

    assert code == OK
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert "standalone" not in out
    assert "server" not in out


def test_standalone_reports_no_server(monkeypatch, capsys, isolated_state_home):
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: _fake_tts_result())

    code = cli.cmd_tts(_ns(standalone=True, json=True), HarkConfig())

    assert code == OK
    out = json.loads(capsys.readouterr().out)
    assert out["standalone"] is True
    assert out["server"] == "none"


def test_standalone_detects_running_harkd(monkeypatch, capsys, isolated_state_home):
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: _fake_tts_result())
    _write_live_pidfile(isolated_state_home / "hark", "harkd.pid")

    code = cli.cmd_tts(_ns(standalone=True, json=True), HarkConfig())

    assert code == OK
    out = json.loads(capsys.readouterr().out)
    assert out["standalone"] is True
    assert out["server"] == "detected"


def test_standalone_detects_mode_a_workers(monkeypatch, capsys):
    """A live mode-a worker probe also counts as a running server."""
    from hark.daemon import ProcessProbe

    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: _fake_tts_result())
    monkeypatch.setattr(
        "hark.daemon.probe_mode_a",
        lambda: ProcessProbe(running=True, pids=[os.getpid()]),
    )

    code = cli.cmd_tts(_ns(standalone=True, json=True), HarkConfig())

    assert code == OK
    out = json.loads(capsys.readouterr().out)
    assert out["server"] == "detected"


def test_standalone_compact_output_includes_mode(monkeypatch, capsys):
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: _fake_tts_result())

    code = cli.cmd_tts(_ns(standalone=True), HarkConfig())

    assert code == OK
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["standalone"] is True
    assert out["server"] == "none"


def test_standalone_listen_output_includes_mode(monkeypatch, capsys):
    from hark.speech import ListenResult

    def fake_speak_and_listen(cfg, text, **kwargs):
        return (
            _fake_tts_result(),
            ListenResult(
                text="hi",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="s-b160",
            ),
        )

    monkeypatch.setattr("hark.speech.speak_and_listen", fake_speak_and_listen)

    code = cli.cmd_tts(_ns(standalone=True, listen=True, json=True), HarkConfig())

    assert code == OK
    out = json.loads(capsys.readouterr().out)
    assert out["standalone"] is True
    assert out["server"] == "none"
    assert out["text"] == "hi"


def test_standalone_cancelled_listen_keeps_mode_fields(monkeypatch, capsys):
    """The cancelled payload carries the same standalone fields as success."""
    from hark.exitcodes import ABORT
    from hark.speech import ListenResult

    def fake_speak_and_listen(cfg, text, **kwargs):
        return (
            _fake_tts_result(),
            ListenResult(
                text="cancel",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                end_phrase="cancel",
                cancelled=True,
                stream_id="s-b160-cancel",
            ),
        )

    monkeypatch.setattr("hark.speech.speak_and_listen", fake_speak_and_listen)

    code = cli.cmd_tts(_ns(standalone=True, listen=True, json=True), HarkConfig())

    assert code == ABORT
    out = json.loads(capsys.readouterr().out)
    assert out["cancelled"] is True
    assert out["standalone"] is True
    assert out["server"] == "none"


def test_standalone_works_from_any_cwd(monkeypatch, capsys, tmp_path):
    """Run through cli.main from an unrelated cwd with an empty config root."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("HARK_CONFIG", raising=False)
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: _fake_tts_result())

    code = cli.main(["tts", "--standalone", "--no-play", "hello", "world"])

    assert code == OK
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["standalone"] is True
    assert out["server"] == "none"


def test_once_alias_maps_to_standalone(monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("HARK_CONFIG", raising=False)
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: _fake_tts_result())

    code = cli.main(["tts", "--once", "--no-play", "hello"])

    assert code == OK
    out = json.loads(capsys.readouterr().out)
    assert out["standalone"] is True
    assert out["server"] == "none"


def test_server_detection_failure_falls_back(monkeypatch, capsys):
    """A broken probe must never break a one-shot run (self-contained fallback)."""
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: _fake_tts_result())

    def boom(*a, **k):
        raise RuntimeError("state dir unreadable")

    monkeypatch.setattr("hark.daemon.probe_harkd", boom)

    code = cli.cmd_tts(_ns(standalone=True, json=True), HarkConfig())

    assert code == OK
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["server"] == "none"

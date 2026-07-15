"""Guided setup (B070): config merge + setup-complete flag."""

from __future__ import annotations

import json
from pathlib import Path

from hark.setup_flow import (
    SETUP_SCHEMA_VERSION,
    SetupAnswers,
    apply_answers_to_config,
    load_setup_complete,
    persona_defaults,
    run_setup,
    setup_needs_run,
    write_setup_complete,
)
from hark.exitcodes import OK


def test_persona_defaults():
    names_f, voice_f = persona_defaults("feminine")
    assert "iris" in names_f
    assert voice_f == "eve"
    names_m, voice_m = persona_defaults("masculine")
    assert "mercury" in names_m
    assert voice_m == "leo"


def test_write_and_load_setup_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    ans = SetupAnswers(
        persona="feminine",
        wake_engine="vosk",
        tts_voice="eve",
        wake_names=["iris", "hark"],
    )
    path = write_setup_complete(ans, hark_version="0.1.0")
    data = load_setup_complete(path)
    assert data is not None
    assert data["hark_version"] == "0.1.0"
    assert data["setup_schema_version"] == SETUP_SCHEMA_VERSION
    assert data["answers"]["wake_engine"] == "vosk"
    assert data["answers"]["tts_voice"] == "eve"
    needs, missing = setup_needs_run(data)
    assert needs is False
    assert missing == []


def test_setup_needs_run_when_missing():
    needs, missing = setup_needs_run({})
    assert needs is True


def test_setup_needs_run_empty_sessions():
    """B116: setup-complete with empty sessions must re-prompt sessions."""
    data = {
        "setup_schema_version": SETUP_SCHEMA_VERSION,
        "answers": {
            "persona": "feminine",
            "wake_engine": "vosk",
            "tts_voice": "eve",
            "wake_names": ["iris"],
            "sessions": [],
        },
    }
    needs, missing = setup_needs_run(data)
    assert needs is True
    assert "sessions" in missing


def test_setup_needs_run_sessions_without_id():
    data = {
        "setup_schema_version": SETUP_SCHEMA_VERSION,
        "answers": {
            "persona": "feminine",
            "wake_engine": "vosk",
            "tts_voice": "eve",
            "wake_names": ["iris"],
            "sessions": [{"ssh": "box"}],  # no id
        },
    }
    needs, missing = setup_needs_run(data)
    assert needs is True
    assert "sessions" in missing


def test_run_setup_sessions_ssh_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = tmp_path / "config" / "hark" / "config.toml"
    code = run_setup(
        non_interactive=True,
        persona="feminine",
        wake_engine="vosk",
        sessions="local,work=ssh:workbox",
        skip_doctor=True,
        skip_download=True,
        force=True,
        config_path=cfg,
    )
    assert code == OK
    text = cfg.read_text(encoding="utf-8")
    assert "[[herdr.sessions]]" in text
    assert 'id = "local"' in text
    assert 'id = "work"' in text
    assert 'ssh = "workbox"' in text
    flag = tmp_path / "state" / "hark" / "setup-complete.json"
    data = json.loads(flag.read_text(encoding="utf-8"))
    assert data["answers"]["sessions"] == [
        {"id": "local"},
        {"id": "work", "ssh": "workbox"},
    ]
    needs, missing = setup_needs_run(data)
    assert needs is False
    assert missing == []


def test_apply_answers_writes_engine_and_names(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[tts]
voice = "old"

[ambient]
enabled = false
engine = "vosk"
names = ["hark"]
""",
        encoding="utf-8",
    )
    ans = SetupAnswers(
        persona="masculine",
        wake_names=["mercury", "hark", "herald"],
        tts_voice="leo",
        wake_engine="vosk",
        sessions=[{"id": "local"}],
    )
    apply_answers_to_config(ans, config_path=cfg)
    text = cfg.read_text(encoding="utf-8")
    assert 'voice = "leo"' in text
    assert 'engine = "vosk"' in text
    assert "mercury" in text
    assert "[[herdr.sessions]]" in text
    assert 'id = "local"' in text


def test_run_setup_non_interactive(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = tmp_path / "config" / "hark" / "config.toml"
    code = run_setup(
        non_interactive=True,
        persona="feminine",
        wake_engine="vosk",
        skip_doctor=True,
        skip_download=True,
        force=True,
        config_path=cfg,
    )
    assert code == OK
    assert cfg.is_file()
    text = cfg.read_text(encoding="utf-8")
    assert 'engine = "vosk"' in text
    assert "iris" in text
    flag = tmp_path / "state" / "hark" / "setup-complete.json"
    assert flag.is_file()
    data = json.loads(flag.read_text(encoding="utf-8"))
    assert data["answers"]["persona"] == "feminine"
    assert data["answers"]["tts_voice"] == "eve"


def test_run_setup_masculine_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = tmp_path / "config.toml"
    code = run_setup(
        non_interactive=True,
        persona="masculine",
        wake_engine="defer",
        skip_doctor=True,
        skip_download=True,
        force=True,
        config_path=cfg,
    )
    assert code == OK
    text = cfg.read_text(encoding="utf-8")
    assert 'voice = "leo"' in text
    # defer → vosk
    assert 'engine = "vosk"' in text

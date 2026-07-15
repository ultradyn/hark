"""B125: session profile from structured startup interview."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hark.session_profile import (
    apply_mode_to_config,
    autonomy_instructions,
    load_profile,
    mode_config_patch,
    mode_ready_tts,
    normalize_autonomy,
    normalize_mode,
    normalize_scope,
    profile_path,
    save_profile,
    set_profile,
    should_start_watch,
    SessionProfile,
)


def test_normalize_scope_aliases():
    assert normalize_scope("session_local") == "session_local"
    assert normalize_scope("local") == "session_local"
    assert normalize_scope("herdr") == "herdr"
    assert normalize_scope("watch herdr") == "herdr"
    with pytest.raises(ValueError):
        normalize_scope("banana")


def test_normalize_mode_and_autonomy():
    assert normalize_mode("conversation") == "conversation"
    assert normalize_mode("streaming") == "conversation"
    assert normalize_mode("radio") == "radio"
    assert normalize_mode("auto-end") == "auto_end"
    assert normalize_autonomy("silent") == "silent"
    assert normalize_autonomy("blocked only") == "blocked_only"
    assert normalize_autonomy("babysit") == "babysit"


def test_mode_config_patch():
    assert mode_config_patch("conversation")["ambient.streaming"] is True
    assert mode_config_patch("radio")["listen.end_mode"] == "radio"
    assert mode_config_patch("auto_end")["listen.end_mode"] == "silence"
    assert mode_config_patch("auto_end")["ambient.streaming"] is False


def test_save_load_should_start_watch(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert should_start_watch() is True  # no profile → default herdr
    set_profile(scope="session_local", mode="conversation", autonomy="silent")
    prof = load_profile()
    assert prof is not None
    assert prof.scope == "session_local"
    assert prof.mode == "conversation"
    assert should_start_watch(prof) is False
    set_profile(scope="herdr")
    assert should_start_watch() is True


def test_apply_mode_to_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[listen]
end_mode = "radio"

[ambient]
enabled = true
streaming = false
""",
        encoding="utf-8",
    )
    report = apply_mode_to_config("conversation", config_path=cfg)
    assert report["ok"] is True
    text = cfg.read_text(encoding="utf-8")
    assert "streaming = true" in text
    report2 = apply_mode_to_config("auto_end", config_path=cfg)
    text2 = cfg.read_text(encoding="utf-8")
    assert 'end_mode = "silence"' in text2
    assert "streaming = false" in text2
    assert report2["mode"] == "auto_end"


def test_autonomy_instructions_and_ready_tts():
    assert "silent" in autonomy_instructions("silent").lower() or "quiet" in autonomy_instructions("silent").lower()
    assert "block" in autonomy_instructions("blocked_only").lower()
    tts = mode_ready_tts("conversation", wake_label="hey iris")
    assert "hey iris" in tts
    assert "conversation" in tts.lower()
    tts_r = mode_ready_tts("radio", wake_label="hey hark")
    assert "okay hark send" in tts_r or "over" in tts_r


def test_cmd_session_profile_set_show(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from argparse import Namespace
    from hark.session_profile import cmd_session_profile

    rc = cmd_session_profile(
        Namespace(
            session_profile_cmd="set",
            scope="session_local",
            autonomy="proactive",
            role="pair on B125",
            mode="conversation",
            apply=False,
            json=True,
        )
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["profile"]["scope"] == "session_local"
    assert out["profile"]["role"] == "pair on B125"
    rc2 = cmd_session_profile(Namespace(session_profile_cmd="show", json=True))
    assert rc2 == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["profile"]["mode"] == "conversation"

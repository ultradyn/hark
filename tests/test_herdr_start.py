"""B056 — HerdrClient session list/ensure + agent start (mocked)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from hark.config import SessionConfig
from hark.herdr.client import (
    HerdrClient,
    HerdrError,
    parse_agent_start,
    parse_session_list,
)


def test_parse_session_list_live_shape():
    data = {
        "sessions": [
            {
                "default": True,
                "name": "default",
                "running": True,
                "session_dir": "/home/u/.config/herdr",
                "socket_path": "/home/u/.config/herdr/herdr.sock",
            },
            {
                "default": False,
                "name": "swarm",
                "running": True,
                "session_dir": "/home/u/.config/herdr/sessions/swarm",
                "socket_path": "/home/u/.config/herdr/sessions/swarm/herdr.sock",
            },
        ]
    }
    rows = parse_session_list(data)
    assert len(rows) == 2
    assert rows[0].name == "default" and rows[0].default and rows[0].running
    assert rows[1].name == "swarm" and rows[1].socket_path.endswith("herdr.sock")


def test_parse_agent_start_live_shape():
    data = {
        "id": "cli:agent:start",
        "result": {
            "agent": {
                "agent_status": "unknown",
                "cwd": "/tmp",
                "focused": False,
                "name": "hark-probe",
                "pane_id": "w8:p17",
                "revision": 0,
                "tab_id": "w8:t6",
                "terminal_id": "term_abc",
                "workspace_id": "w8",
            },
            "argv": ["true"],
            "type": "agent_started",
        },
    }
    info = parse_agent_start(data, session_id="local")
    assert info.pane_id == "w8:p17"
    assert info.session_id == "local"
    assert info.agent == "hark-probe"
    assert info.cwd == "/tmp"
    assert info.target == "local/w8:p17"


def test_start_agent_builds_argv(monkeypatch):
    client = HerdrClient(SessionConfig(id="local"), herdr_bin="herdr")
    captured: list[list[str]] = []

    def fake_run_json(args, timeout=15.0):
        captured.append(list(args))
        return {
            "result": {
                "agent": {
                    "pane_id": "w1:p2",
                    "name": "codex",
                    "agent_status": "idle",
                    "cwd": "/src/proj",
                }
            }
        }

    monkeypatch.setattr(client, "run_json", fake_run_json)
    info = client.start_agent(
        "codex",
        ["/bin/codex", "--yolo"],
        cwd="/src/proj",
        split="right",
        focus=False,
    )
    assert info.pane_id == "w1:p2"
    assert captured[0][:3] == ["agent", "start", "codex"]
    assert "--cwd" in captured[0]
    assert "/src/proj" in captured[0]
    assert "--split" in captured[0]
    assert "right" in captured[0]
    assert "--no-focus" in captured[0]
    # argv after --
    dash = captured[0].index("--")
    assert captured[0][dash + 1 :] == ["/bin/codex", "--yolo"]


def test_start_agent_empty_argv():
    client = HerdrClient(SessionConfig(id="local"))
    with pytest.raises(HerdrError, match="non-empty"):
        client.start_agent("x", [])


def test_ensure_session_idempotent_when_running(monkeypatch):
    client = HerdrClient(SessionConfig(id="local"), herdr_bin="herdr")
    monkeypatch.setattr(
        client,
        "list_sessions",
        lambda: parse_session_list(
            {
                "sessions": [
                    {
                        "name": "swarm",
                        "running": True,
                        "default": False,
                        "socket_path": "/tmp/s.sock",
                    }
                ]
            }
        ),
    )
    popen = MagicMock()
    monkeypatch.setattr("hark.herdr.client.subprocess.Popen", popen)
    info = client.ensure_session("swarm")
    assert info.running and info.name == "swarm"
    popen.assert_not_called()

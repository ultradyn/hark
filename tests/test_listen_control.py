from hark.listen_control import (
    agent_control_block,
    clear_active_listen,
    consume_listen_action,
    poll_listen_action,
    read_active,
    register_active_listen,
    request_listen_action,
)
from hark.partial import make_partial_event


def test_register_and_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    # paths use XDG_STATE_HOME/hark — ensure clean
    clear_active_listen()
    register_active_listen("sfixture99", mode="radio")
    active = read_active()
    assert active is not None
    assert active["stream_id"] == "sfixture99"
    assert "listen-end" in active["end_cmd"]

    r = request_listen_action("finish", stream_id="sfixture99", reason="test")
    assert r["ok"] is True
    assert poll_listen_action("sfixture99") == "finish"
    assert consume_listen_action("sfixture99") == "finish"
    assert poll_listen_action("sfixture99") is None
    clear_active_listen("sfixture99")
    assert read_active() is None


def test_cancel_action(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    register_active_listen("scancel1")
    r = request_listen_action("cancel", stream_id="scancel1")
    assert r["ok"]
    assert consume_listen_action("scancel1") == "cancel"
    clear_active_listen("scancel1")


def test_no_active_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    r = request_listen_action("finish")
    assert r["ok"] is False


def test_partial_includes_agent_control():
    ev = make_partial_event(stream_id="s1", seq=1, text="how do I stop this?")
    assert "agent_control" in ev
    ac = ev["agent_control"]
    assert "listen-end" in ac["end_recording"]
    assert "--cancel" in ac["cancel_recording"]
    assert "stop" in ac["hint"].lower() or "finish" in ac["hint"].lower()
    assert "HOLD" in ev["instructions"] or "end_recording" in ev["instructions"]


def test_agent_control_block():
    b = agent_control_block("sabc")
    assert "sabc" in b["end_recording"]

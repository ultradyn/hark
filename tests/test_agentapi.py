"""Pure helpers + thin agentapi inject (B049 Antigravity Mode A)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from hark.agentapi import (
    ENV_CONVERSATION_ID,
    ENV_LS_ADDRESS,
    WAKE_PREAMBLE,
    AgyEnv,
    build_send_message_argv,
    deliver_line,
    deliver_lines,
    format_wake_payload,
    parse_monitor_line,
    read_agy_env,
    resolve_agy_env,
    resolve_agy_env_from_environ,
    send_message,
    status_dict,
    write_agy_env,
)


def test_agy_env_normalized_strips():
    e = AgyEnv(ls_address="  http://127.0.0.1:9  ", conversation_id="  abc  ").normalized()
    assert e.ls_address == "http://127.0.0.1:9"
    assert e.conversation_id == "abc"


def test_agy_env_rejects_empty():
    with pytest.raises(ValueError):
        AgyEnv(ls_address="", conversation_id="x").normalized()
    with pytest.raises(ValueError):
        AgyEnv(ls_address="http://x", conversation_id="").normalized()


def test_write_read_agy_env_roundtrip(tmp_path: Path):
    path = tmp_path / "agy-env.json"
    env = AgyEnv(ls_address="http://127.0.0.1:4242", conversation_id="conv-1")
    wrote = write_agy_env(env, path=path)
    assert wrote == path
    loaded = read_agy_env(path)
    assert loaded == env
    raw = json.loads(path.read_text())
    assert raw["ls_address"] == "http://127.0.0.1:4242"
    assert raw["conversation_id"] == "conv-1"


def test_read_agy_env_missing_and_corrupt(tmp_path: Path):
    assert read_agy_env(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("not-json", encoding="utf-8")
    assert read_agy_env(bad) is None
    bad.write_text('{"ls_address": 1, "conversation_id": "x"}', encoding="utf-8")
    assert read_agy_env(bad) is None


def test_resolve_from_environ():
    env = {
        ENV_LS_ADDRESS: "http://127.0.0.1:1",
        ENV_CONVERSATION_ID: "c1",
    }
    got = resolve_agy_env_from_environ(env)
    assert got is not None
    assert got.conversation_id == "c1"
    assert resolve_agy_env_from_environ({}) is None
    assert resolve_agy_env_from_environ({ENV_LS_ADDRESS: "x"}) is None


def test_resolve_prefer_file_vs_environ(tmp_path: Path):
    path = tmp_path / "agy-env.json"
    write_agy_env(
        AgyEnv(ls_address="http://file", conversation_id="file-id"),
        path=path,
    )
    environ = {
        ENV_LS_ADDRESS: "http://proc",
        ENV_CONVERSATION_ID: "proc-id",
    }
    r_file = resolve_agy_env(path=path, environ=environ, prefer="file")
    assert r_file is not None and r_file.conversation_id == "file-id"
    r_env = resolve_agy_env(path=path, environ=environ, prefer="environ")
    assert r_env is not None and r_env.conversation_id == "proc-id"


def test_format_wake_payload_string_and_dict():
    body = format_wake_payload('{"kind":"agent.blocked"}')
    assert body.startswith(WAKE_PREAMBLE)
    assert '{"kind":"agent.blocked"}' in body
    body2 = format_wake_payload({"kind": "ambient.prompt", "text": "hi"})
    assert "ambient.prompt" in body2
    assert "hi" in body2
    with pytest.raises(ValueError):
        format_wake_payload("   ")
    bare = format_wake_payload("x", preamble="")
    assert bare == "x"


def test_parse_monitor_line():
    assert parse_monitor_line("") is None
    assert parse_monitor_line("not json") is None
    assert parse_monitor_line("[1,2]") is None
    d = parse_monitor_line('{"kind":"agent.blocked","event_id":"e1"}')
    assert d == {"kind": "agent.blocked", "event_id": "e1"}


def test_build_send_message_argv():
    argv = build_send_message_argv("cid", "hello there", title="hark mode-a")
    assert argv == [
        "agy",
        "agentapi",
        "send-message",
        "--title=hark mode-a",
        "cid",
        "hello there",
    ]
    with pytest.raises(ValueError):
        build_send_message_argv("", "x")
    with pytest.raises(ValueError):
        build_send_message_argv("c", "")


def test_send_message_dry_run(tmp_path: Path):
    path = tmp_path / "agy-env.json"
    write_agy_env(
        AgyEnv(ls_address="http://127.0.0.1:9", conversation_id="conv"),
        path=path,
    )
    r = send_message("payload", path=path, dry_run=True)
    assert r.ok and r.dry_run
    assert r.argv[0] == "agy"
    assert "agentapi" in r.argv
    assert "conv" in r.argv
    assert "payload" in r.argv


def test_send_message_missing_env(tmp_path: Path):
    r = send_message("x", path=tmp_path / "missing.json", dry_run=True)
    assert not r.ok
    assert r.error and "no agy env" in r.error


def test_send_message_subprocess_ok(tmp_path: Path):
    path = tmp_path / "agy-env.json"
    write_agy_env(
        AgyEnv(ls_address="http://127.0.0.1:9", conversation_id="conv"),
        path=path,
    )

    class FakeProc:
        returncode = 0
        stdout = "ok"
        stderr = ""

    with patch("hark.agentapi.which_agy", return_value="/usr/bin/agy"):
        with patch("hark.agentapi.subprocess.run", return_value=FakeProc()) as run:
            r = send_message("hi", path=path, dry_run=False)
            assert r.ok
            assert run.called
            kwargs = run.call_args.kwargs
            assert kwargs["env"][ENV_LS_ADDRESS] == "http://127.0.0.1:9"
            assert kwargs["env"][ENV_CONVERSATION_ID] == "conv"


def test_send_message_missing_binary(tmp_path: Path):
    path = tmp_path / "agy-env.json"
    write_agy_env(
        AgyEnv(ls_address="http://127.0.0.1:9", conversation_id="conv"),
        path=path,
    )
    with patch("hark.agentapi.which_agy", return_value=None):
        r = send_message("hi", path=path)
        assert not r.ok
        assert r.error and "not found" in r.error


def test_deliver_line_skips_blank(tmp_path: Path):
    path = tmp_path / "agy-env.json"
    write_agy_env(
        AgyEnv(ls_address="http://127.0.0.1:9", conversation_id="conv"),
        path=path,
    )
    assert deliver_line("  \n", path=path, dry_run=True) is None
    r = deliver_line(
        '{"kind":"agent.blocked","event_id":"e"}',
        path=path,
        dry_run=True,
    )
    assert r is not None and r.ok and r.dry_run
    # wrapped payload contains preamble
    assert any(WAKE_PREAMBLE[:20] in a for a in r.argv)


def test_deliver_lines_stop_on_error(tmp_path: Path):
    path = tmp_path / "agy-env.json"
    write_agy_env(
        AgyEnv(ls_address="http://127.0.0.1:9", conversation_id="conv"),
        path=path,
    )
    lines = [
        '{"kind":"a"}',
        '{"kind":"b"}',
    ]
    with patch(
        "hark.agentapi.send_message",
        side_effect=[
            type("R", (), {"ok": False, "error": "fail"})(),
            type("R", (), {"ok": True, "error": None})(),
        ],
    ):
        out = deliver_lines(lines, path=path, stop_on_error=True, dry_run=True)
        assert len(out) == 1
        assert not out[0].ok


def test_status_dict(tmp_path: Path):
    path = tmp_path / "agy-env.json"
    write_agy_env(
        AgyEnv(ls_address="http://file", conversation_id="fid"),
        path=path,
    )
    info = status_dict(
        path=path,
        environ={ENV_LS_ADDRESS: "http://p", ENV_CONVERSATION_ID: "pid"},
        agy_bin="agy-not-real-bin-xyz",
    )
    assert info["file"]["conversation_id"] == "fid"
    assert info["process"]["conversation_id"] == "pid"
    assert info["resolved"]["conversation_id"] == "fid"
    assert info["agy_found"] is False


def test_cli_register_and_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hark.cli import main

    env_path = tmp_path / "agy-env.json"
    monkeypatch.setenv(ENV_LS_ADDRESS, "http://127.0.0.1:7777")
    monkeypatch.setenv(ENV_CONVERSATION_ID, "cli-conv")
    rc = main(["agentapi", "register", "--path", str(env_path), "--json"])
    assert rc == 0
    data = json.loads(env_path.read_text())
    assert data["conversation_id"] == "cli-conv"

    # status
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc2 = main(["agentapi", "status", "--path", str(env_path), "--json"])
    assert rc2 == 0
    st = json.loads(buf.getvalue())
    assert st["resolved"]["conversation_id"] == "cli-conv"


def test_cli_send_dry_run(tmp_path: Path):
    from hark.cli import main

    env_path = tmp_path / "agy-env.json"
    write_agy_env(
        AgyEnv(ls_address="http://127.0.0.1:1", conversation_id="c"),
        path=env_path,
    )
    rc = main(
        [
            "agentapi",
            "send",
            "hello",
            "--path",
            str(env_path),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0


def test_cli_deliver_stdin_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hark.cli import main

    env_path = tmp_path / "agy-env.json"
    write_agy_env(
        AgyEnv(ls_address="http://127.0.0.1:1", conversation_id="c"),
        path=env_path,
    )
    monkeypatch.setattr(
        "sys.stdin",
        __import__("io").StringIO(
            '{"kind":"agent.blocked","event_id":"e1"}\n\nnot-json\n'
        ),
    )
    rc = main(
        [
            "agentapi",
            "deliver",
            "--path",
            str(env_path),
            "--dry-run",
            "--json",
        ]
    )
    # not-json still gets delivered as raw string line after strip — deliver_line
    # accepts any non-blank line; only follow-monitor filters non-JSON.
    assert rc == 0

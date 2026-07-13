"""B057 — hark agent-start / session CLI (parser + mocked client)."""

from __future__ import annotations

from hark.cli import build_parser
from hark.config import AgentsConfig, HarkConfig, SessionConfig
from hark.exitcodes import OK
from hark.herdr.client import AgentInfo, NamedSessionInfo


def test_parser_agent_start_and_session():
    p = build_parser()
    a = p.parse_args(
        ["agent-start", "codex", "--cwd", "/tmp", "--prompt", "hi", "--json"]
    )
    assert a.cmd == "agent-start"
    assert a.agent == "codex"
    assert a.cwd == "/tmp"
    assert a.prompt == "hi"
    assert a.json is True

    b = p.parse_args(["session", "list", "--json"])
    assert b.cmd == "session" and b.session_cmd == "list"

    c = p.parse_args(["session", "ensure", "swarm"])
    assert c.session_cmd == "ensure" and c.name == "swarm"


def test_cmd_session_list_json(monkeypatch, capsys):
    from hark import cli as cli_mod

    class FakeClient:
        def list_sessions(self):
            return [
                NamedSessionInfo(
                    name="default",
                    running=True,
                    default=True,
                    socket_path="/tmp/h.sock",
                )
            ]

    monkeypatch.setattr(cli_mod, "_client_for", lambda cfg, sid: FakeClient())
    cfg = HarkConfig(sessions=[SessionConfig(id="local")])
    args = build_parser().parse_args(["session", "list", "--json"])
    code = cli_mod.cmd_session(args, cfg)
    assert code == OK
    out = capsys.readouterr().out
    assert "default" in out
    assert "running" in out


def test_cmd_agent_start_mocked(monkeypatch, capsys):
    from hark import cli as cli_mod

    class FakeClient:
        def start_agent(self, name, argv, **kw):
            return AgentInfo(
                session_id="local",
                pane_id="w1:p1",
                agent=name,
                status="idle",
                cwd=kw.get("cwd"),
            )

        def send_text(self, pane_id, text, *, submit=True):
            self.sent = (pane_id, text, submit)

        def ensure_session(self, name, **kw):
            return NamedSessionInfo(name=name, running=True)

    fake = FakeClient()
    monkeypatch.setattr(cli_mod, "_client_for", lambda cfg, sid: fake)
    monkeypatch.setattr(
        "hark.agents.resolve.resolve_agent_argv",
        lambda name, **kw: type(
            "R",
            (),
            {
                "agent_key": "codex",
                "argv": ["/bin/codex"],
                "source": "canonical",
                "command": "/bin/codex",
            },
        )(),
    )
    # also patch adhoc fallback path not used
    cfg = HarkConfig(
        sessions=[SessionConfig(id="local")],
        agents=AgentsConfig(),
    )
    args = build_parser().parse_args(
        ["agent-start", "codex", "--cwd", "/src", "--prompt", "go", "--json"]
    )
    code = cli_mod.cmd_agent_start(args, cfg)
    assert code == OK
    out = capsys.readouterr().out
    assert "w1:p1" in out
    assert fake.sent == ("w1:p1", "go", True)

"""B057 — hark agent-start / session CLI (parser + mocked client)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hark.agents.resolve import AGENT_CATALOG
from hark.cli import build_parser
from hark.config import AgentsConfig, HarkConfig, SessionConfig
from hark.exitcodes import OK, USAGE
from hark.herdr.client import AgentInfo, NamedSessionInfo


MULTIWORD_CATALOG_NAMES = tuple(
    (name, spec.key, spec.canonical)
    for spec in AGENT_CATALOG
    for name in (*spec.aliases, *spec.names)
    if " " in name
)


class RecordingClient:
    def __init__(self):
        self.started: list[tuple[str, list[str], dict[str, object]]] = []
        self.sent: tuple[str, str, bool] | None = None

    def start_agent(self, name, argv, **kw):
        self.started.append((name, list(argv), kw))
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


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _run_agent_start(
    monkeypatch,
    capsys,
    tmp_path: Path,
    argv: list[str],
    *,
    overrides: dict[str, str] | None = None,
):
    from hark import cli as cli_mod

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(bindir))
    client = RecordingClient()
    monkeypatch.setattr(cli_mod, "_client_for", lambda cfg, sid: client)
    cfg = HarkConfig(
        sessions=[SessionConfig(id="local")],
        agents=AgentsConfig(cli=overrides or {}),
    )
    args = build_parser().parse_args(["agent-start", *argv])
    code = cli_mod.cmd_agent_start(args, cfg)
    captured = capsys.readouterr()
    return code, client, captured, bindir


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


def test_agent_start_unknown_safe_path_binary_falls_back_to_adhoc(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    binary = _executable(bindir / "custom-agent")

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, ["custom-agent", "--json"]
    )

    assert code == OK
    assert len(client.started) == 1
    assert client.started[0][1] == [str(binary)]
    assert '"source": "adhoc"' in captured.out


def test_agent_start_known_unsafe_alias_does_not_fall_back(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gcc = _executable(bindir / "gcc")
    (bindir / "cc").symlink_to(gcc)

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, ["cc"]
    )

    assert code == USAGE
    assert client.started == []
    assert "cc" in captured.err
    assert "no safe executable" in captured.err


def test_agent_start_reject_list_hit_does_not_fall_back(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    coderabbit = _executable(bindir / "coderabbit")
    (bindir / "cr").symlink_to(coderabbit)

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, ["cr"]
    )

    assert code == USAGE
    assert client.started == []
    assert "cr" in captured.err
    assert "no safe executable" in captured.err


@pytest.mark.parametrize(
    ("command", "target_name"),
    (("cc --version", "gcc"), ("cr review", "coderabbit")),
)
def test_agent_start_quoted_known_unsafe_alias_does_not_fall_back(
    monkeypatch, capsys, tmp_path: Path, command: str, target_name: str
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = _executable(bindir / target_name)
    (bindir / command.split()[0]).symlink_to(target)

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, [command]
    )

    assert code == USAGE
    assert client.started == []
    assert command.split()[0] in captured.err
    assert "no safe executable" in captured.err


def test_agent_start_quoted_safe_catalog_command_preserves_args(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    codex = _executable(bindir / "codex")

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, ["codex --version", "--json"]
    )

    assert code == OK
    assert client.started[0][1] == [str(codex), "--version"]
    assert '"agent_key": "codex"' in captured.out


@pytest.mark.parametrize(
    ("catalog_name", "expected_key", "canonical"),
    MULTIWORD_CATALOG_NAMES,
)
def test_agent_start_multiword_catalog_names_use_longest_match(
    monkeypatch,
    capsys,
    tmp_path: Path,
    catalog_name: str,
    expected_key: str,
    canonical: str,
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable = _executable(bindir / canonical)
    first_word = bindir / catalog_name.split()[0]
    if first_word != executable:
        _executable(first_word)

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, [f"{catalog_name} --probe", "--json"]
    )

    assert code == OK
    assert client.started[0][1] == [str(executable), "--probe"]
    assert f'"agent_key": "{expected_key}"' in captured.out
    assert '"source": "canonical"' in captured.out


def test_agent_start_catalog_prefix_ambiguity_keeps_unknown_command_adhoc(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable = _executable(bindir / "open")

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, ["open sesame", "--json"]
    )

    assert code == OK
    assert client.started[0][1] == [str(executable), "sesame"]
    assert '"agent_key": "adhoc"' in captured.out


def test_agent_start_singleword_catalog_prefix_keeps_remaining_args(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable = _executable(bindir / "claude")

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, ["claude custom", "--json"]
    )

    assert code == OK
    assert client.started[0][1] == [str(executable), "custom"]
    assert '"agent_key": "claude"' in captured.out


def test_agent_start_unknown_non_executable_does_not_start_pane(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "custom-agent").write_text("not executable\n")

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, ["custom-agent"]
    )

    assert code == USAGE
    assert client.started == []
    assert "custom-agent" in captured.err


@pytest.mark.parametrize("relative", (False, True))
def test_agent_start_implicit_non_executable_path_does_not_start_pane(
    monkeypatch, capsys, tmp_path: Path, relative: bool
):
    target = tmp_path / "plain-agent"
    target.write_text("not executable\n")
    target.chmod(0o644)
    monkeypatch.chdir(tmp_path)
    command = f"./{target.name}" if relative else str(target)

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, [command]
    )

    assert code == USAGE
    assert client.started == []
    assert command in captured.err
    assert "regular executable" in captured.err


@pytest.mark.parametrize("relative", (False, True))
def test_agent_start_implicit_unknown_executable_path_regression(
    monkeypatch, capsys, tmp_path: Path, relative: bool
):
    target = _executable(tmp_path / "custom-agent")
    monkeypatch.chdir(tmp_path)
    command = f"./{target.name}" if relative else str(target)

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, [command, "--json"]
    )

    assert code == OK
    assert client.started[0][1] == [command]
    assert '"source": "adhoc"' in captured.out


def test_agent_start_unsafe_override_symlink_does_not_fall_back(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "codex")
    override = bindir / "missing-override"
    override.symlink_to(bindir / "missing-target")

    code, client, captured, _ = _run_agent_start(
        monkeypatch,
        capsys,
        tmp_path,
        ["codex"],
        overrides={"codex": str(override)},
    )

    assert code == USAGE
    assert client.started == []
    assert "override" in captured.err
    assert "codex" in captured.err


@pytest.mark.parametrize("relative", (False, True))
@pytest.mark.parametrize("suffix", ("/", "/."))
def test_agent_start_override_file_with_directory_syntax_does_not_start_pane(
    monkeypatch,
    capsys,
    tmp_path: Path,
    relative: bool,
    suffix: str,
):
    validation_cwd = tmp_path / "validation"
    validation_cwd.mkdir()
    target = _executable(validation_cwd / "custom-codex")
    monkeypatch.chdir(validation_cwd)
    base = f"./{target.name}" if relative else str(target)
    override = f"{base}{suffix}"

    code, client, captured, _ = _run_agent_start(
        monkeypatch,
        capsys,
        tmp_path,
        ["codex"],
        overrides={"codex": override},
    )

    assert code == USAGE
    assert client.started == []
    assert "codex" in captured.err
    assert "override" in captured.err


@pytest.mark.parametrize(
    ("agent", "override", "target_name"),
    (("claude", "cc", "gcc"), ("cursor-agent", "cr", "coderabbit")),
)
def test_agent_start_rejected_override_does_not_start_pane(
    monkeypatch,
    capsys,
    tmp_path: Path,
    agent: str,
    override: str,
    target_name: str,
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = _executable(bindir / target_name)
    (bindir / override).symlink_to(target)

    code, client, captured, _ = _run_agent_start(
        monkeypatch,
        capsys,
        tmp_path,
        [agent],
        overrides={agent: override},
    )

    assert code == USAGE
    assert client.started == []
    assert agent in captured.err
    assert override in captured.err
    assert "override" in captured.err


def test_agent_start_malformed_override_does_not_fall_back(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "codex")

    code, client, captured, _ = _run_agent_start(
        monkeypatch,
        capsys,
        tmp_path,
        ["codex"],
        overrides={"codex": "'unterminated"},
    )

    assert code == USAGE
    assert client.started == []
    assert "malformed override" in captured.err
    assert "codex" in captured.err


def test_agent_start_empty_override_does_not_fall_back(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _executable(bindir / "codex")

    code, client, captured, _ = _run_agent_start(
        monkeypatch,
        capsys,
        tmp_path,
        ["codex"],
        overrides={"codex": ""},
    )

    assert code == USAGE
    assert client.started == []
    assert "empty override" in captured.err
    assert "codex" in captured.err


def test_agent_start_empty_command_does_not_start_pane(
    monkeypatch, capsys, tmp_path: Path
):
    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, [""]
    )

    assert code == USAGE
    assert client.started == []
    assert "empty agent" in captured.err


def test_agent_start_explicit_adhoc_remains_intentional(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gcc = _executable(bindir / "gcc")
    cc = bindir / "cc"
    cc.symlink_to(gcc)

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, ["cc", "--adhoc", "--json"]
    )

    assert code == OK
    assert client.started[0][1] == [str(cc)]
    assert '"source": "adhoc"' in captured.out


def test_agent_start_valid_override_regression(monkeypatch, capsys, tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    custom = _executable(bindir / "my-codex")

    code, client, captured, _ = _run_agent_start(
        monkeypatch,
        capsys,
        tmp_path,
        ["codex", "--json"],
        overrides={"codex": "my-codex"},
    )

    assert code == OK
    assert client.started[0][1] == [str(custom)]
    assert '"source": "override"' in captured.out


def test_agent_start_valid_override_prefix_regression(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    custom = _executable(bindir / "my-codex")

    code, client, captured, _ = _run_agent_start(
        monkeypatch,
        capsys,
        tmp_path,
        ["codex", "--json"],
        overrides={"codex": "my-codex --configured"},
    )

    assert code == OK
    assert client.started[0][1] == [str(custom), "--configured"]
    assert '"source": "override"' in captured.out


def test_agent_start_relative_override_is_pinned_before_launch_cwd(
    monkeypatch, capsys, tmp_path: Path
):
    validation_cwd = tmp_path / "validation"
    launch_cwd = tmp_path / "launch"
    validation_cwd.mkdir()
    launch_cwd.mkdir()
    _executable(validation_cwd / "custom-codex")
    sentinel = _executable(launch_cwd / "custom-codex")
    monkeypatch.chdir(validation_cwd)

    code, client, captured, _ = _run_agent_start(
        monkeypatch,
        capsys,
        tmp_path,
        ["codex", "--cwd", str(launch_cwd), "--json"],
        overrides={"codex": "./custom-codex --configured"},
    )

    assert code == OK
    assert client.started[0][1] == [
        f"{validation_cwd}/./custom-codex",
        "--configured",
    ]
    assert client.started[0][1][0] != str(sentinel.resolve())
    assert client.started[0][2]["cwd"] == str(launch_cwd)
    assert '"source": "override"' in captured.out


def test_agent_start_normal_catalog_resolution_regression(
    monkeypatch, capsys, tmp_path: Path
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    codex = _executable(bindir / "codex")

    code, client, captured, _ = _run_agent_start(
        monkeypatch, capsys, tmp_path, ["codex", "--json"]
    )

    assert code == OK
    assert client.started[0][1] == [str(codex)]
    assert '"agent_key": "codex"' in captured.out
    assert '"source": "canonical"' in captured.out

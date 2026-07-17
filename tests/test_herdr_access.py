from __future__ import annotations

import io
import json
import os
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from hark.config import HarkConfig, SessionConfig, load_config, resolve_session_socket
from hark.exitcodes import HERDR
from hark.herdr.access import HerdrSessionAccess, active_client
from hark.herdr.client import (
    AgentInfo,
    HerdrClient,
    HerdrError,
    HerdrSessionHealth,
    NamedSessionInfo,
)


class FakeTunnel:
    def __init__(self, local_socket: Path):
        self.local_socket = local_socket
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


class FakeClient:
    def __init__(self, session: SessionConfig):
        self.session = session
        self.socket_path = Path(session.socket or "/default/herdr.sock")
        self.ensure_calls: list[tuple[str, bool]] = []
        self.health_calls = 0

    def ensure_session(self, name: str, *, start: bool = True) -> NamedSessionInfo:
        self.ensure_calls.append((name, start))
        return NamedSessionInfo(
            name=name,
            running=True,
            socket_path=f"/named/{name}/herdr.sock",
        )

    def health(self):
        self.health_calls += 1
        return {"ok": True}


def remote_session(**overrides) -> SessionConfig:
    session = SessionConfig(
        id="workbox",
        ssh="dev@workbox",
        herdr_bin="/opt/remote/herdr",
        label="Work box",
        remote_socket="/run/user/1000/herdr.sock",
    )
    return replace(session, **overrides)


def test_local_custom_socket_uses_configured_transport_without_tunnel(tmp_path):
    configured = SessionConfig(
        id="desk",
        socket=str(tmp_path / "custom.sock"),
        herdr_bin="/opt/local/herdr",
        label="Desk",
        remote_socket="/unused/metadata.sock",
    )
    tunnel_calls: list[object] = []
    made: list[SessionConfig] = []

    with HerdrSessionAccess(
        HarkConfig(sessions=[configured]),
        client_factory=lambda session: made.append(session) or FakeClient(session),
        tunnel_factory=lambda *args, **kwargs: tunnel_calls.append((args, kwargs)),
    ) as access:
        client = access.client("desk")
        assert access.client("desk") is client

    assert made == [configured]
    assert tunnel_calls == []


def test_explicit_socket_with_ssh_is_authoritative_and_preserved(tmp_path):
    configured = remote_session(socket=str(tmp_path / "external-forward.sock"))
    made: list[SessionConfig] = []

    with HerdrSessionAccess(
        HarkConfig(sessions=[configured]),
        client_factory=lambda session: made.append(session) or FakeClient(session),
        tunnel_factory=lambda *_a, **_kw: pytest.fail("must not replace custom socket"),
    ) as access:
        access.client("workbox")

    assert made == [configured]


def test_remote_session_establishes_once_and_preserves_configuration(tmp_path):
    configured = remote_session()
    tunnel = FakeTunnel(tmp_path / "tunnels" / "workbox.sock")
    tunnel_calls: list[tuple[str, str, str | None]] = []
    made: list[SessionConfig] = []

    def make_tunnel(session_id, ssh, *, remote_socket=None):
        tunnel_calls.append((session_id, ssh, remote_socket))
        return tunnel

    with HerdrSessionAccess(
        HarkConfig(sessions=[configured]),
        client_factory=lambda session: made.append(session) or FakeClient(session),
        tunnel_factory=make_tunnel,
    ) as access:
        first = access.client("workbox")
        assert access.client("workbox") is first
        effective = first.session
        assert effective.socket == str(tunnel.local_socket)
        assert effective.ssh == configured.ssh
        assert effective.herdr_bin == configured.herdr_bin
        assert effective.label == configured.label
        assert effective.remote_socket == configured.remote_socket

    assert tunnel_calls == [("workbox", "dev@workbox", "/run/user/1000/herdr.sock")]
    assert made == [effective]
    assert tunnel.stops == 1


def test_tunnel_failure_includes_session_and_never_constructs_local_client():
    made: list[SessionConfig] = []

    with HerdrSessionAccess(
        HarkConfig(sessions=[remote_session()]),
        client_factory=lambda session: made.append(session) or FakeClient(session),
        tunnel_factory=lambda *_a, **_kw: (_ for _ in ()).throw(
            RuntimeError("permission denied")
        ),
    ) as access:
        with pytest.raises(HerdrError, match="workbox.*permission denied"):
            access.client("workbox")

    assert made == []


def test_unknown_session_never_falls_back_to_local():
    with HerdrSessionAccess(
        HarkConfig(sessions=[SessionConfig(id="local")]),
        client_factory=FakeClient,
    ) as access:
        with pytest.raises(HerdrError, match="unknown configured.*missing"):
            access.client("missing")


def test_ambient_local_socket_env_does_not_override_configured_ssh(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[[herdr.sessions]]
id = "workbox"
ssh = "dev@workbox"
remote_socket = "/run/user/1000/herdr.sock"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/ambient/local/herdr.sock")

    configured = load_config(config_path).session_by_id("workbox")

    assert configured is not None
    assert configured.socket is None
    assert configured.ssh == "dev@workbox"


def test_local_named_selection_uses_configured_base_and_preserves_metadata(tmp_path):
    configured = SessionConfig(
        id="desk",
        socket=str(tmp_path / "custom.sock"),
        herdr_bin="/opt/local/herdr",
        label="Desk",
        remote_socket="/metadata.sock",
    )
    made: list[FakeClient] = []

    def make_client(session):
        client = FakeClient(session)
        made.append(client)
        return client

    with HerdrSessionAccess(
        HarkConfig(sessions=[configured]), client_factory=make_client
    ) as access:
        selected = access.named_client("desk", "swarm")

    assert made[0].session == configured
    assert made[0].ensure_calls == [("swarm", True)]
    assert selected.session == replace(
        configured, id="swarm", socket="/named/swarm/herdr.sock"
    )


def test_remote_named_selection_preserves_explicit_transport_and_never_starts_server(
    tmp_path,
):
    configured = remote_session(socket=str(tmp_path / "external-forward.sock"))
    made: list[FakeClient] = []

    def make_client(session):
        client = FakeClient(session)
        made.append(client)
        return client

    with HerdrSessionAccess(
        HarkConfig(sessions=[configured]),
        client_factory=make_client,
        tunnel_factory=lambda *_a, **_kw: pytest.fail("custom socket must win"),
    ) as access:
        selected = access.named_client("workbox", "swarm")

    assert len(made) == 1
    assert selected.session == replace(configured, id="swarm")
    assert selected.ensure_calls == []
    assert selected.health_calls == 1


def test_remote_named_selection_tunnels_configured_destination_without_server_start(
    tmp_path,
):
    configured = remote_session()
    tunnel = FakeTunnel(tmp_path / "named.sock")
    made: list[FakeClient] = []
    tunnel_calls: list[tuple[str, str, str | None]] = []

    def make_tunnel(session_id, ssh, *, remote_socket=None):
        tunnel_calls.append((session_id, ssh, remote_socket))
        return tunnel

    def make_client(session):
        client = FakeClient(session)
        made.append(client)
        return client

    with HerdrSessionAccess(
        HarkConfig(sessions=[configured]),
        client_factory=make_client,
        tunnel_factory=make_tunnel,
    ) as access:
        selected = access.named_client("workbox", "swarm")

    assert tunnel_calls == [
        ("workbox", "dev@workbox", "/run/user/1000/herdr.sock")
    ]
    assert selected.session == replace(
        configured, id="swarm", socket=str(tunnel.local_socket)
    )
    assert selected.ensure_calls == []
    assert selected.health_calls == 1
    assert tunnel.stops == 1


def test_remote_named_default_destination_is_derived_without_starting_server(tmp_path):
    configured = remote_session(remote_socket=None)
    calls: list[str | None] = []
    tunnel = FakeTunnel(tmp_path / "named.sock")

    with HerdrSessionAccess(
        HarkConfig(sessions=[configured]),
        client_factory=FakeClient,
        tunnel_factory=lambda _id, _ssh, *, remote_socket=None: (
            calls.append(remote_socket) or tunnel
        ),
    ) as access:
        selected = access.named_client("workbox", "swarm")

    expected = "~/.config/herdr/sessions/swarm/herdr.sock"
    assert calls == [expected]
    assert selected.session.remote_socket == expected
    assert selected.ensure_calls == []


def test_active_client_requires_shared_access_scope():
    cfg = HarkConfig(sessions=[SessionConfig(id="local")])

    with pytest.raises(RuntimeError, match="outside a session access scope"):
        active_client(cfg, "local")

    with HerdrSessionAccess(cfg, client_factory=FakeClient) as access:
        assert active_client(cfg, "local") is access.client("local")


def test_resolved_remote_socket_matches_shared_tunnel_adapter(monkeypatch, tmp_path):
    from hark.herdr import tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod, "cache_dir", lambda: tmp_path)
    configured = remote_session()

    assert resolve_session_socket(configured) == tunnel_mod.tunnel_socket_path(
        configured.id,
        configured.ssh,
        remote_socket=configured.remote_socket,
    )


def test_managed_tunnel_path_stays_bindable_with_long_cache_and_session(
    monkeypatch, tmp_path
):
    from hark.herdr import tunnel as tunnel_mod

    long_cache = tmp_path / ("context-mode-cache-segment-" * 5)
    monkeypatch.setattr(tunnel_mod, "cache_dir", lambda: long_cache)

    path = tunnel_mod.tunnel_socket_path(
        "workbox-with-an-intentionally-very-long-configured-session-identity",
        "dev@workbox",
        remote_socket="/run/user/1000/herdr.sock",
    )

    assert len(os.fsencode(path)) <= 100
    assert not path.is_relative_to(long_cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
    finally:
        listener.close()
        path.unlink(missing_ok=True)


def test_unavailable_short_tunnel_root_names_session_in_failure(
    monkeypatch, tmp_path
):
    from hark.herdr import tunnel as tunnel_mod

    long_cache = tmp_path / ("configured-cache-" * 8)
    long_fallback = tmp_path / ("fallback-root-" * 8)
    long_fallback.mkdir()
    monkeypatch.setattr(tunnel_mod, "cache_dir", lambda: long_cache)
    monkeypatch.setattr(tunnel_mod, "_SHORT_SOCKET_BASES", (long_fallback,))

    with pytest.raises(RuntimeError, match="Herdr session 'workbox'"):
        tunnel_mod.tunnel_socket_path("workbox", "dev@workbox")


class FakeSshProcess:
    def __init__(self):
        self.dead = False

    def poll(self):
        return 0 if self.dead else None

    def terminate(self):
        self.dead = True

    def kill(self):
        self.dead = True

    def wait(self, timeout=None):
        self.dead = True
        return 0


def _patch_test_tunnel_start(monkeypatch, tunnel_mod):
    starts = []
    listeners = []

    def start(tunnel):
        tunnel.local_socket.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(tunnel.local_socket))
        listener.listen(1)
        tunnel.proc = FakeSshProcess()
        starts.append(tunnel)
        listeners.append(listener)
        return tunnel.local_socket

    monkeypatch.setattr(tunnel_mod.Tunnel, "start", start)
    return starts, listeners


def test_tunnel_adapter_reuses_live_process_until_last_lease(monkeypatch, tmp_path):
    from hark.herdr import tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod, "cache_dir", lambda: tmp_path)
    starts, listeners = _patch_test_tunnel_start(monkeypatch, tunnel_mod)
    tunnel_mod._TUNNELS.clear()

    first = tunnel_mod.ensure_tunnel(
        "workbox", "dev@workbox", remote_socket="/run/herdr.sock"
    )
    second = tunnel_mod.ensure_tunnel(
        "workbox", "dev@workbox", remote_socket="/run/herdr.sock"
    )

    assert first.local_socket == second.local_socket
    assert len(starts) == 1
    first.stop()
    assert starts[0].proc.poll() is None
    second.stop()
    assert starts[0].proc.poll() == 0
    assert not os.path.lexists(first.local_socket)
    assert tunnel_mod._TUNNELS == {}
    for listener in listeners:
        listener.close()


def test_dead_cached_tunnel_is_replaced_before_reuse(monkeypatch, tmp_path):
    from hark.herdr import tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod, "cache_dir", lambda: tmp_path)
    starts, listeners = _patch_test_tunnel_start(monkeypatch, tunnel_mod)
    tunnel_mod._TUNNELS.clear()

    stale = tunnel_mod.ensure_tunnel("workbox", "dev@workbox")
    stale._record.tunnel.proc.dead = True
    replacement = tunnel_mod.ensure_tunnel("workbox", "dev@workbox")

    assert replacement._record is not stale._record
    assert len(starts) == 2
    replacement.stop()
    stale.stop()
    assert tunnel_mod._TUNNELS == {}
    for listener in listeners:
        listener.close()


def test_tunnel_paths_are_process_scoped_to_prevent_cross_process_unlink(
    monkeypatch, tmp_path
):
    from hark.herdr import tunnel as tunnel_mod

    monkeypatch.setattr(tunnel_mod, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr(tunnel_mod.os, "getpid", lambda: 101)
    first = tunnel_mod.tunnel_socket_path("workbox", "dev@workbox")
    monkeypatch.setattr(tunnel_mod.os, "getpid", lambda: 202)
    second = tunnel_mod.tunnel_socket_path("workbox", "dev@workbox")

    assert first != second
    assert "101" in first.name
    assert "202" in second.name


def test_watch_uses_shared_access_and_reports_tunnel_failure(monkeypatch):
    import hark.watch as watch

    monkeypatch.setattr(
        watch,
        "ensure_tunnel",
        lambda session_id, *_a, **_kw: (_ for _ in ()).throw(
            RuntimeError(f"ssh refused for {session_id}")
        ),
    )
    cfg = HarkConfig(sessions=[remote_session()])
    out = io.StringIO()

    assert watch.run_watch(cfg, transport="socket", out=out) == HERDR
    event = json.loads(out.getvalue())
    assert event["session_id"] == "workbox"
    assert "workbox" in event["error"]
    assert "ssh refused" in event["error"]


def test_cli_status_uses_shared_access_and_does_not_fallback(monkeypatch, capsys):
    from hark import cli

    monkeypatch.setattr(
        cli,
        "ensure_tunnel",
        lambda session_id, *_a, **_kw: (_ for _ in ()).throw(
            RuntimeError(f"ssh refused for {session_id}")
        ),
    )
    args = cli.build_parser().parse_args(["status", "--session", "workbox", "--json"])

    assert cli.cmd_status(args, HarkConfig(sessions=[remote_session()])) == HERDR
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"][0]["session_id"] == "workbox"
    assert "ssh refused" in payload["errors"][0]["error"]


def test_dashboard_session_snapshot_uses_shared_access(monkeypatch):
    from hark.dashboard import api

    monkeypatch.setattr(
        api,
        "ensure_tunnel",
        lambda session_id, *_a, **_kw: (_ for _ in ()).throw(
            RuntimeError(f"ssh refused for {session_id}")
        ),
    )

    payload = api.herdr_sessions_snapshot(HarkConfig(sessions=[remote_session()]))

    assert payload["ok"] is False
    assert payload["sessions"][0]["session_id"] == "workbox"
    assert "ssh refused" in payload["sessions"][0]["error"]


def test_herdr_client_retains_remote_binary_metadata_but_runs_local_binary(
    monkeypatch, tmp_path
):
    configured = remote_session(socket=str(tmp_path / "forward.sock"))
    monkeypatch.setattr("hark.herdr.client.shutil.which", lambda _name: "/usr/bin/herdr")

    client = HerdrClient(configured)

    assert client.session.herdr_bin == "/opt/remote/herdr"
    assert client.herdr_bin == "/usr/bin/herdr"


class SurfaceClient(FakeClient):
    instances: list["SurfaceClient"] = []

    def __init__(self, session):
        super().__init__(session)
        self.calls: list[tuple] = []
        type(self).instances.append(self)

    def list_agents(self):
        self.calls.append(("list_agents",))
        return []

    def read_pane(self, pane_id, *, lines=60):
        self.calls.append(("read_pane", pane_id, lines))
        return "pane text"

    def send_text(self, pane_id, text, *, submit=True):
        self.calls.append(("send_text", pane_id, text, submit))

    def send_keys(self, pane_id, keys):
        self.calls.append(("send_keys", pane_id, list(keys)))

    def get_agent(self, pane_id):
        self.calls.append(("get_agent", pane_id))
        return None

    def socket_exists(self):
        return True


def _patch_remote_surface(monkeypatch, module, tmp_path):
    tunnel = FakeTunnel(tmp_path / "workbox.sock")
    SurfaceClient.instances = []
    monkeypatch.setattr(module, "HerdrClient", SurfaceClient)
    monkeypatch.setattr(module, "ensure_tunnel", lambda *_a, **_kw: tunnel)
    return tunnel


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (["status", "--session", "workbox", "--json"], ("list_agents",)),
        (["context", "workbox/w1:p1"], ("read_pane", "w1:p1", 60)),
        (
            ["reply", "workbox/w1:p1", "yes"],
            ("send_text", "w1:p1", "yes", True),
        ),
        (["keys", "workbox/w1:p1", "enter"], ("send_keys", "w1:p1", ["enter"])),
    ],
)
def test_cli_herdr_surfaces_use_shared_remote_access(
    monkeypatch, tmp_path, argv, expected_call
):
    from hark import cli

    tunnel = _patch_remote_surface(monkeypatch, cli, tmp_path)
    args = cli.build_parser().parse_args(argv)

    assert cli.dispatch(args, HarkConfig(sessions=[remote_session()])) == 0
    assert len(SurfaceClient.instances) == 1
    assert expected_call in SurfaceClient.instances[0].calls
    assert tunnel.stops == 1


def test_bound_cli_answer_resolves_remote_client_inside_shared_scope(
    monkeypatch, tmp_path
):
    from hark import answering, cli

    tunnel = _patch_remote_surface(monkeypatch, cli, tmp_path)

    def answer(event_id, *, text, keys, store, client_for):
        client_for("workbox").send_text("w1:p1", text)
        return SimpleNamespace(
            ok=True,
            event_id=event_id,
            target="workbox/w1:p1",
            status="delivered",
            reason=None,
        )

    monkeypatch.setattr(answering, "answer_bound_event", answer)
    args = cli.build_parser().parse_args(["answer", "evt-1", "--text", "yes"])

    assert cli.dispatch(args, HarkConfig(sessions=[remote_session()])) == 0
    assert SurfaceClient.instances[0].calls == [
        ("send_text", "w1:p1", "yes", True)
    ]
    assert tunnel.stops == 1


def test_doctor_uses_shared_remote_access(monkeypatch, tmp_path):
    import hark.doctor as doctor

    class DoctorClient(SurfaceClient):
        def health(self):
            return HerdrSessionHealth(
                session_id=self.session.id,
                ok=True,
                socket=str(self.socket_path),
                agent_count=0,
            )

    tunnel = FakeTunnel(tmp_path / "doctor.sock")
    monkeypatch.setattr(doctor, "HerdrClient", DoctorClient)
    monkeypatch.setattr(doctor, "ensure_tunnel", lambda *_a, **_kw: tunnel)
    monkeypatch.setattr(doctor, "all_provider_status", lambda: [])
    out = io.StringIO()

    doctor.run_doctor(
        HarkConfig(sessions=[remote_session()]),
        as_json=True,
        out=out,
        err=io.StringIO(),
    )

    report = json.loads(out.getvalue())
    assert report["sessions"][0]["session_id"] == "workbox"
    assert report["sessions"][0]["ok"] is True
    assert tunnel.stops == 1


def test_dashboard_context_and_bound_delivery_use_shared_remote_access(
    monkeypatch, tmp_path
):
    from hark.dashboard import api

    tunnel = _patch_remote_surface(monkeypatch, api, tmp_path)
    cfg = HarkConfig(sessions=[remote_session()])

    context = api.context_snapshot(cfg, "workbox", "w1:p1", lines=7)

    assert context["text"] == "pane text"
    assert SurfaceClient.instances[0].calls[:2] == [
        ("read_pane", "w1:p1", 7),
        ("get_agent", "w1:p1"),
    ]
    assert tunnel.stops == 1

    second_tunnel = _patch_remote_surface(monkeypatch, api, tmp_path)

    def answer(event_id, *, text, keys, client_for, register_fallback):
        client_for("workbox").send_text("w1:p1", text)
        return SimpleNamespace(
            status="delivered",
            reason=None,
            to_payload=lambda: {
                "ok": True,
                "event_id": event_id,
                "status": "delivered",
            },
        )

    monkeypatch.setattr(api, "answer_bound_event", answer)
    status, payload = api.answer_action(cfg, {"event_id": "evt-1", "text": "yes"})

    assert status == 200
    assert payload["ok"] is True
    assert SurfaceClient.instances[0].calls == [
        ("send_text", "w1:p1", "yes", True)
    ]
    assert second_tunnel.stops == 1


def test_agent_start_remote_named_session_preserves_transport_and_never_starts_server(
    monkeypatch, capsys, tmp_path
):
    from hark import cli

    made: list[SurfaceClient] = []

    class StartClient(SurfaceClient):
        def __init__(self, session):
            super().__init__(session)
            made.append(self)

        def ensure_session(self, *_a, **_kw):
            pytest.fail("remote named selection must not start a Herdr server")

        def start_agent(self, name, argv, **kwargs):
            self.calls.append(("start_agent", name, list(argv)))
            return AgentInfo(
                session_id=self.session.id,
                pane_id="w1:p1",
                agent=name,
                status="idle",
                cwd=kwargs.get("cwd"),
            )

    tunnel = FakeTunnel(tmp_path / "agent-start.sock")
    monkeypatch.setattr(cli, "HerdrClient", StartClient)
    monkeypatch.setattr(cli, "ensure_tunnel", lambda *_a, **_kw: tunnel)
    monkeypatch.setattr(
        "hark.agents.resolve.resolve_flexible",
        lambda *_a, **_kw: SimpleNamespace(
            agent_key="codex",
            argv=["/usr/bin/codex"],
            source="canonical",
        ),
    )
    configured = remote_session()
    args = cli.build_parser().parse_args(
        [
            "agent-start",
            "codex",
            "--session",
            "workbox",
            "--herdr-session",
            "swarm",
            "--json",
        ]
    )

    assert cli.dispatch(args, HarkConfig(sessions=[configured])) == 0
    assert len(made) == 1
    assert made[0].session == replace(
        configured,
        id="swarm",
        socket=str(tunnel.local_socket),
    )
    assert made[0].ensure_calls == []
    assert ("start_agent", "codex", ["/usr/bin/codex"]) in made[0].calls
    assert "swarm/w1:p1" in capsys.readouterr().out
    assert tunnel.stops == 1

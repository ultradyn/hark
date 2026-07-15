import errno
import io
import json

import hark.cli as cli
import hark.watch as watch
from hark.config import HarkConfig, SessionConfig
from hark.delivery import BoundEvent, DeliveryStore
from hark.herdr.client import AgentInfo, HerdrError
from hark.herdr.socket_client import is_expected_disconnect


class FakeWatchStore:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def register_from_hep(self, event: dict[str, object]) -> None:
        self.events.append(event)


class FakeWatchClient:
    def __init__(self, session: SessionConfig, *, read_error: Exception | None = None) -> None:
        self.session = session
        self.read_error = read_error
        self.read_pane_calls: list[tuple[str, int]] = []

    def socket_exists(self) -> bool:
        return False

    def list_agents(self) -> list[AgentInfo]:
        return [
            AgentInfo(
                session_id=self.session.id,
                pane_id="w1:p1",
                agent="codex",
                status="blocked",
                revision=3,
            )
        ]

    def read_pane(self, pane_id: str, lines: int = 60) -> str:
        self.read_pane_calls.append((pane_id, lines))
        if self.read_error:
            raise self.read_error
        return "Allow this action?"


def _run_once(monkeypatch, *, read_error: Exception | None = None):
    client: FakeWatchClient | None = None

    def make_client(session: SessionConfig) -> FakeWatchClient:
        nonlocal client
        client = FakeWatchClient(session, read_error=read_error)
        return client

    store = FakeWatchStore()
    monkeypatch.setattr(watch, "HerdrClient", make_client)
    monkeypatch.setattr(watch, "DeliveryStore", lambda: store)
    out = io.StringIO()
    cfg = HarkConfig(sessions=[SessionConfig(id="local")])
    assert watch.run_watch(cfg, transport="poll", once=True, out=out) == 0
    assert client is not None
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    return client, store, events


def test_default_watch_reads_and_registers_fingerprinted_blocked_event(monkeypatch):
    client, store, events = _run_once(monkeypatch)

    blocked = next(event for event in events if event["kind"] == "agent.blocked")
    assert client.read_pane_calls == [("w1:p1", 100)]
    assert blocked["question"]["fingerprint"]
    assert blocked.get("pane_capture") and "Allow this action?" in blocked["pane_capture"]["text"]
    assert store.events == [blocked]


def test_watch_emits_but_does_not_register_unbound_blocked_event(monkeypatch):
    client, store, events = _run_once(monkeypatch, read_error=HerdrError("unavailable"))

    blocked = next(event for event in events if event["kind"] == "agent.blocked")
    assert client.read_pane_calls == [("w1:p1", 100)]
    assert blocked["question"]["fingerprint"] is None
    assert store.events == []


def test_watch_cli_reads_questions_by_default_and_keeps_legacy_flag(monkeypatch):
    args = cli.build_parser().parse_args(["watch", "--once"])
    legacy_args = cli.build_parser().parse_args(["watch", "--once", "--read-questions"])
    calls: list[dict[str, object]] = []

    def fake_run_watch(cfg, **kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_watch", fake_run_watch)
    assert cli.dispatch(args, HarkConfig()) == 0
    assert cli.dispatch(legacy_args, HarkConfig()) == 0
    assert [call["read_questions"] for call in calls] == [True, True]


def test_socket_lifecycle_event_invalidates_bound_target(monkeypatch, tmp_path):
    from hark.herdr import socket_client

    store = DeliveryStore(tmp_path / "events.jsonl")
    store.save_event(
        BoundEvent(
            event_id="evt-pending",
            session_id="local",
            pane_id="w1:p1",
            pane_revision=3,
            question_fingerprint="blake2b:abc",
        )
    )
    client = FakeWatchClient(SessionConfig(id="local"))
    client.socket_path = tmp_path / "herdr.sock"
    emitted: list[dict[str, object]] = []
    raw = {
        "method": "events.notify",
        "params": {
            "event": {
                "type": "pane.closed",
                "data": {"pane": {"id": "w1:p1", "revision": 4}},
            }
        },
    }

    def fake_subscribe(_socket_path, on_event):
        on_event(raw)

    monkeypatch.setattr(socket_client, "run_subscribe_loop", fake_subscribe)

    assert watch._watch_socket(
        client,
        classifier=watch.EdgeTracker(),
        interest={"blocked"},
        emit=emitted.append,
        heartbeat_s=60,
        sessions=["local"],
        read_pane=None,
        store=store,
    ) == 0

    invalidated = [event for event in emitted if event["kind"] == "target.invalidated"]
    assert len(invalidated) == 1
    assert invalidated[0]["session_id"] == "local"
    assert invalidated[0]["target"]["pane_id"] == "w1:p1"
    assert invalidated[0]["disposition"] == "invalidated"
    assert invalidated[0]["invalidated_event_ids"] == ["evt-pending"]
    assert store.get("evt-pending").status == "invalidated"


def test_socket_lifecycle_event_from_self_is_not_forwarded(monkeypatch, tmp_path):
    from hark.herdr import socket_client
    from hark.self_detect import SelfIdentity

    store = DeliveryStore(tmp_path / "events.jsonl")
    store.save_event(
        BoundEvent(
            event_id="evt-self",
            session_id="local",
            pane_id="wG:p3",
            pane_revision=3,
            question_fingerprint="blake2b:abc",
        )
    )
    client = FakeWatchClient(SessionConfig(id="local"))
    client.socket_path = tmp_path / "herdr.sock"
    emitted: list[dict[str, object]] = []

    def fake_subscribe(_socket_path, on_event):
        on_event({"type": "pane.closed", "pane_id": "wG:p3"})

    monkeypatch.setattr(socket_client, "run_subscribe_loop", fake_subscribe)

    assert watch._watch_socket(
        client,
        classifier=watch.EdgeTracker(),
        interest={"blocked"},
        emit=emitted.append,
        heartbeat_s=60,
        sessions=["local"],
        read_pane=None,
        store=store,
        self_ident=SelfIdentity(pane_id="wG:p3", socket_path=str(client.socket_path)),
    ) == 0

    assert not [event for event in emitted if event["kind"] == "target.invalidated"]
    assert store.get("evt-self").status == "pending"


def test_is_expected_disconnect_classifies_pipe_and_reset():
    assert is_expected_disconnect(BrokenPipeError(errno.EPIPE, "Broken pipe"))
    assert is_expected_disconnect(
        ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
    )
    assert is_expected_disconnect(EOFError("Herdr socket closed"))
    assert is_expected_disconnect(OSError(errno.EPIPE, "Broken pipe"))
    assert is_expected_disconnect(OSError(errno.ECONNREFUSED, "Connection refused"))
    assert is_expected_disconnect(FileNotFoundError("herdr.sock"))
    # String form as seen in "socket watch failed, poll: [Errno 32] Broken pipe"
    assert is_expected_disconnect(RuntimeError("[Errno 32] Broken pipe"))
    assert is_expected_disconnect(RuntimeError("[Errno 104] Connection reset by peer"))
    assert not is_expected_disconnect(RuntimeError("subscribe failed: unknown method"))
    assert not is_expected_disconnect(RuntimeError("ping failed: not authorized"))


def test_watch_socket_reconnects_quietly_on_broken_pipe(monkeypatch, tmp_path):
    from hark.herdr import socket_client

    attempts = {"n": 0}
    sleeps: list[float] = []

    def fake_subscribe(_socket_path, on_event):
        attempts["n"] += 1
        if attempts["n"] < 4:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        raise KeyboardInterrupt()

    monkeypatch.setattr(socket_client, "run_subscribe_loop", fake_subscribe)
    monkeypatch.setattr(watch.time, "sleep", lambda s: sleeps.append(s))

    client = FakeWatchClient(SessionConfig(id="local"))
    client.socket_path = tmp_path / "herdr.sock"
    emitted: list[dict[str, object]] = []

    assert (
        watch._watch_socket(
            client,
            classifier=watch.EdgeTracker(),
            interest={"blocked"},
            emit=emitted.append,
            heartbeat_s=60,
            sessions=["local"],
            read_pane=None,
            store=None,
        )
        == 0
    )

    assert attempts["n"] == 4
    assert len(sleeps) == 3  # backoff between reconnects
    errors = [e for e in emitted if e["kind"] == "watch.error"]
    # Rate-limited: many Broken pipes → a single watch.error in the window
    assert len(errors) == 1
    assert "reconnecting" in str(errors[0]["error"])
    assert "Broken pipe" in str(errors[0]["error"])


def test_watch_socket_rate_limits_expected_disconnect_errors(monkeypatch, tmp_path):
    from hark.herdr import socket_client

    clock = {"t": 1000.0}
    attempts = {"n": 0}

    def fake_subscribe(_socket_path, on_event):
        attempts["n"] += 1
        if attempts["n"] <= 5:
            raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
        raise KeyboardInterrupt()

    monkeypatch.setattr(socket_client, "run_subscribe_loop", fake_subscribe)
    monkeypatch.setattr(watch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(watch.time, "monotonic", lambda: clock["t"])

    client = FakeWatchClient(SessionConfig(id="local"))
    client.socket_path = tmp_path / "herdr.sock"
    emitted: list[dict[str, object]] = []

    # Advance clock only after first emit window so later reconnects suppress.
    orig_allow = watch._WatchErrorLimiter.allow

    def allow_then_advance(self):
        allowed, suppressed = orig_allow(self)
        if allowed:
            # next disconnects stay inside the interval
            clock["t"] += 1.0
        return allowed, suppressed

    monkeypatch.setattr(watch._WatchErrorLimiter, "allow", allow_then_advance)

    assert (
        watch._watch_socket(
            client,
            classifier=watch.EdgeTracker(),
            interest={"blocked"},
            emit=emitted.append,
            heartbeat_s=60,
            sessions=["local"],
            read_pane=None,
            store=None,
        )
        == 0
    )

    errors = [e for e in emitted if e["kind"] == "watch.error"]
    assert len(errors) == 1
    assert attempts["n"] == 6


def test_watch_socket_real_failure_propagates_for_poll_fallback(monkeypatch, tmp_path):
    from hark.herdr import socket_client

    def fake_subscribe(_socket_path, on_event):
        raise RuntimeError("subscribe failed: unknown method")

    monkeypatch.setattr(socket_client, "run_subscribe_loop", fake_subscribe)
    monkeypatch.setattr(watch.time, "sleep", lambda _s: None)

    client = FakeWatchClient(SessionConfig(id="local"))
    client.socket_path = tmp_path / "herdr.sock"
    emitted: list[dict[str, object]] = []

    try:
        watch._watch_socket(
            client,
            classifier=watch.EdgeTracker(),
            interest={"blocked"},
            emit=emitted.append,
            heartbeat_s=60,
            sessions=["local"],
            read_pane=None,
            store=None,
        )
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "unknown method" in str(exc)

    assert raised
    # Real failures are not swallowed as quiet reconnect errors inside the loop.
    reconnect_errors = [
        e
        for e in emitted
        if e["kind"] == "watch.error" and "reconnecting" in str(e.get("error", ""))
    ]
    assert reconnect_errors == []


def test_run_watch_poll_fallback_rate_limits_expected_disconnect(monkeypatch):
    """Outer fallback: expected disconnect → at most rate-limited watch.error."""

    def make_client(session: SessionConfig) -> FakeWatchClient:
        c = FakeWatchClient(session)
        c.socket_path = "/tmp/fake-herdr.sock"
        return c

    def boom_socket(*_a, **_k):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    monkeypatch.setattr(watch, "HerdrClient", make_client)
    monkeypatch.setattr(watch, "DeliveryStore", lambda: FakeWatchStore())
    monkeypatch.setattr(watch, "_watch_socket", boom_socket)
    monkeypatch.setattr(FakeWatchClient, "socket_exists", lambda self: True)
    # once=True skips socket; interrupt poll after fallback.
    monkeypatch.setattr(watch.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))

    out = io.StringIO()
    cfg = HarkConfig(sessions=[SessionConfig(id="local")])
    assert watch.run_watch(cfg, transport="socket", once=False, out=out) == 0
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    errors = [e for e in events if e["kind"] == "watch.error"]
    assert len(errors) == 1
    assert "Broken pipe" in errors[0]["error"]
    assert "poll" in errors[0]["error"]


def test_run_watch_poll_fallback_always_emits_real_socket_failure(monkeypatch):
    def make_client(session: SessionConfig) -> FakeWatchClient:
        c = FakeWatchClient(session)
        c.socket_path = "/tmp/fake-herdr.sock"
        return c

    def boom_socket(*_a, **_k):
        raise RuntimeError("subscribe failed: unknown method")

    monkeypatch.setattr(watch, "HerdrClient", make_client)
    monkeypatch.setattr(watch, "DeliveryStore", lambda: FakeWatchStore())
    monkeypatch.setattr(watch, "_watch_socket", boom_socket)
    monkeypatch.setattr(FakeWatchClient, "socket_exists", lambda self: True)
    monkeypatch.setattr(watch.time, "sleep", lambda _s: (_ for _ in ()).throw(KeyboardInterrupt()))

    out = io.StringIO()
    cfg = HarkConfig(sessions=[SessionConfig(id="local")])
    assert watch.run_watch(cfg, transport="socket", once=False, out=out) == 0
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    errors = [e for e in events if e["kind"] == "watch.error"]
    assert len(errors) == 1
    assert "unknown method" in errors[0]["error"]

"""B061: hark serve end-to-end (real HTTP over loopback, tmp state dir)."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from http import HTTPStatus
from pathlib import Path

import pytest

from hark.config import load_config
from hark.dashboard import api as dash_api
from hark.dashboard.server import DashboardServer
from hark.dashboard.tailer import parse_cursor, read_page
from hark.delivery import BoundEvent, DeliveryStore
from hark.herdr.client import AgentInfo
from hark.state_feed import CursorPosition, format_cursor, parse_cursor_positions


@pytest.fixture()
def state(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("HARK_DASHBOARD_TOKEN", raising=False)
    s = tmp_path / "state" / "hark"
    s.mkdir(parents=True)
    return s


def _server(tmp_path, *, token: str | None = None, require_token: bool = False):
    cfg_path = tmp_path / "config.toml"
    lines = ["[dashboard]", 'host = "127.0.0.1"', "port = 0"]
    if token:
        lines.append(f'token = "{token}"')
    if require_token:
        lines.append("require_token = true")
    cfg_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cfg = load_config(cfg_path)
    server = DashboardServer(cfg, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _conn(server) -> http.client.HTTPConnection:
    return http.client.HTTPConnection(*server.server_address, timeout=5)


def _get_json(server, path, headers=None):
    c = _conn(server)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    body = json.loads(r.read())
    c.close()
    return r.status, body


def _post_json(server, path, obj, headers=None):
    c = _conn(server)
    payload = json.dumps(obj)
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    c.request("POST", path, body=payload, headers=hdrs)
    r = c.getresponse()
    raw = r.read()
    body = json.loads(raw) if raw else {}
    cookies = r.getheader("Set-Cookie")
    c.close()
    return r.status, body, cookies


def _write_jsonl(path: Path, *objs: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for obj in objs:
            fh.write(json.dumps(obj) + "\n")


def _open_stream(server, path: str, headers: dict[str, str] | None = None):
    connection = _conn(server)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream"
    return connection, response


def _read_sse_event(response) -> tuple[str, dict]:
    event_id = None
    data = None
    while True:
        line = response.fp.readline().decode()
        if not line:
            raise AssertionError("SSE connection ended before an event")
        if line.startswith("id: "):
            event_id = line[4:].rstrip("\n")
        elif line.startswith("data: "):
            data = json.loads(line[6:])
        elif line == "\n" and data is not None:
            assert event_id == data["cursor"]
            return event_id, data


def _pause_first_replay_snapshot(
    monkeypatch,
    dashboard_server,
    snapshot_taken: threading.Event,
    release_snapshot: threading.Event,
    *,
    timeout: float,
):
    """Pause after the first lazy replay iterator reaches its snapshot EOF."""
    real_iter_replay = dashboard_server.iter_replay_records
    first_call = True

    def paused_iter_replay(*args, **kwargs):
        nonlocal first_call
        iterator = iter(real_iter_replay(*args, **kwargs))
        if first_call:
            first_call = False
            try:
                first = next(iterator)
            except StopIteration:
                first = None
            snapshot_taken.set()
            assert release_snapshot.wait(timeout), (
                "test did not release replay snapshot"
            )
            if first is not None:
                yield first
        yield from iterator

    monkeypatch.setattr(dashboard_server, "iter_replay_records", paused_iter_replay)
    return real_iter_replay


HEP_BLOCKED = {
    "schema": "hark.event.v1",
    "kind": "agent.blocked",
    "event_id": "01JTESTBLOCKED000000000001",
    "observed_at": "2026-07-13T14:00:00.000Z",
    "session_id": "local",
    "target": {"pane_id": "w1:p6", "pane_revision": 3},
    "question": {
        "text": "Allow this action?",
        "fingerprint": None,  # filled by test
        "risk": "R1",
    },
}


def test_localhost_no_auth_and_config_redaction(state, tmp_path):
    server = _server(tmp_path)
    try:
        status, body = _get_json(server, "/api/v1/config")
        assert status == 200 and body["redacted"] is True
        dash = body["config"]["dashboard"]
        assert dash["token_configured"] is False
        assert "token" not in dash
    finally:
        server.shutdown()


def test_nonlocal_bind_without_token_refused(state, tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    with pytest.raises(ValueError, match="refusing non-localhost"):
        DashboardServer(cfg, "0.0.0.0", 0)


def test_auth_flow_cookie_and_bearer(state, tmp_path):
    server = _server(tmp_path, token="sekrit-token", require_token=True)
    try:
        status, body = _get_json(server, "/api/v1/config")
        assert status == 401 and body["error"]["code"] == "unauthorized"

        status, body = _get_json(
            server, "/api/v1/config", headers={"Authorization": "Bearer sekrit-token"}
        )
        assert status == 200

        status, body, _ = _post_json(server, "/api/v1/auth", {"token": "wrong"})
        assert status == 401

        status, body, cookie = _post_json(
            server, "/api/v1/auth", {"token": "sekrit-token"}
        )
        assert status == 200 and body == {"ok": True}
        assert cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie

        session = cookie.split(";")[0]
        status, body = _get_json(server, "/api/v1/config", headers={"Cookie": session})
        assert status == 200
    finally:
        server.shutdown()


def test_events_backfill_and_page(state, tmp_path):
    _write_jsonl(state / "watch.jsonl", HEP_BLOCKED)
    _write_jsonl(
        state / "system.jsonl",
        {
            "ts": 1.0,
            "seq": 1,
            "level": "info",
            "component": "tts",
            "event": "tts.ok",
            "message": "ok",
            "data": {},
            "pid": 1,
        },
    )
    server = _server(tmp_path)
    try:
        status, body = _get_json(server, "/api/v1/events")
        assert status == 200 and body["ok"]
        sources = {e["source"] for e in body["events"]}
        assert {"watch", "system"} <= sources
        for e in body["events"]:
            assert e["schema"] == "hark.dashboard.v1"

        system_event = next(
            e for e in body["events"] if e["payload"].get("event") == "tts.ok"
        )
        watch_event = next(e for e in body["events"] if e["source"] == "watch")
        assert parse_cursor(system_event["cursor"]) == {"system": 1}
        assert parse_cursor(watch_event["cursor"]) == {"system": 1, "watch": 1}

        status, body = _get_json(server, "/api/v1/events?sources=system")
        assert {e["source"] for e in body["events"]} == {"system"}
    finally:
        server.shutdown()


def test_rotated_legacy_cursor_replays_new_incarnation_over_rest_and_sse(
    state, tmp_path
):
    _write_jsonl(state / "watch.jsonl", {**HEP_BLOCKED, "event_id": "new", "n": 1})
    server = _server(tmp_path)
    connection = _conn(server)
    try:
        status, page = _get_json(
            server,
            "/api/v1/events?since=watch%3A100&sources=watch",
        )
        assert status == 200
        assert [event["payload"]["n"] for event in page["events"]] == [1]
        rest_position = parse_cursor_positions(page["cursor"])["watch"]
        assert rest_position.seq == 1
        assert rest_position.incarnation is not None

        connection.request(
            "GET",
            "/api/v1/stream?sources=watch",
            headers={"Last-Event-ID": "watch:100"},
        )
        response = connection.getresponse()
        assert response.status == 200

        def read_event():
            data = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                line = response.fp.readline().decode()
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                elif line == "\n" and data is not None:
                    return data
            raise AssertionError("no SSE event within timeout")

        assert read_event()["type"] == "hello"
        replay = read_event()
        assert replay["source"] == "watch"
        assert replay["payload"]["n"] == 1
        sse_position = parse_cursor_positions(replay["cursor"])["watch"]
        assert sse_position == rest_position
    finally:
        connection.close()
        server.shutdown()


@pytest.mark.parametrize("replacement_count", [2, 3, 5])
def test_same_prefix_rewrite_replays_over_fresh_rest_and_sse(
    state, tmp_path, replacement_count
):
    path = state / "watch.jsonl"
    _write_jsonl(path, {"n": "same"}, {"n": "old-1"}, {"n": "old-2"})
    _, old_cursor, _ = read_page(
        state,
        since=None,
        sources={"watch"},
        history_limit=100,
    )
    old_position = parse_cursor_positions(old_cursor)["watch"]
    assert old_position.seq == 3

    path.write_text("", encoding="utf-8")
    expected = ["same"] + [f"new-{n}" for n in range(1, replacement_count)]
    _write_jsonl(path, *({"n": value} for value in expected))
    server = _server(tmp_path)
    connection = _conn(server)
    try:
        status, page = _get_json(
            server,
            f"/api/v1/events?since={old_cursor}&sources=watch",
        )
        assert status == 200
        assert [event["payload"]["n"] for event in page["events"]] == expected
        rest_position = parse_cursor_positions(page["cursor"])["watch"]
        assert rest_position.seq == replacement_count
        assert rest_position.checkpoint != old_position.checkpoint

        connection.request(
            "GET",
            "/api/v1/stream?sources=watch",
            headers={"Last-Event-ID": old_cursor},
        )
        response = connection.getresponse()
        assert response.status == 200

        def read_event():
            data = None
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                line = response.fp.readline().decode()
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                elif line == "\n" and data is not None:
                    return data
            raise AssertionError("no SSE event within timeout")

        assert read_event()["type"] == "hello"
        replay = [read_event() for _ in range(replacement_count)]
        assert [event["payload"]["n"] for event in replay] == expected
        assert parse_cursor_positions(replay[-1]["cursor"])["watch"] == rest_position
    finally:
        connection.close()
        server.shutdown()


def test_stream_hello_and_live_event(state, tmp_path):
    server = _server(tmp_path)
    c = _conn(server)
    try:
        c.request("GET", "/api/v1/stream")
        r = c.getresponse()
        assert r.status == 200
        assert r.getheader("Content-Type") == "text/event-stream"

        def read_event(timeout_s=10.0):
            deadline = time.monotonic() + timeout_s
            data = None
            while time.monotonic() < deadline:
                line = r.fp.readline().decode()
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                elif line == "\n" and data is not None:
                    return data
            raise AssertionError("no SSE event within timeout")

        hello = read_event()
        assert hello["type"] == "hello"
        assert hello["payload"]["kind"] == "serve.hello"
        assert set(hello["payload"]["sources"]) >= {"watch", "serve"}

        _write_jsonl(
            state / "watch.jsonl",
            {**HEP_BLOCKED, "event_id": "live-1", "n": 1},
            {**HEP_BLOCKED, "event_id": "live-2", "n": 2},
        )
        watch_events = []
        deadline = time.monotonic() + 5.0
        while len(watch_events) < 2 and time.monotonic() < deadline:
            event = read_event(timeout_s=max(0.1, deadline - time.monotonic()))
            if event.get("source") == "watch":
                watch_events.append(event)
        assert len(watch_events) == 2
        first, second = watch_events
        assert first["payload"]["kind"] == "agent.blocked"
        assert [parse_cursor(e["cursor"])["watch"] for e in (first, second)] == [
            1,
            2,
        ]
    finally:
        c.close()
        server.shutdown()


def test_serve_publish_cannot_snapshot_unpublished_durable_cursor(state):
    from hark.dashboard.server import Hub, TailPump

    hub = Hub()
    pump = TailPump(hub, state, poll_s=0.001)
    subscriber = hub.subscribe()
    follower_advanced = threading.Event()
    release_durable_publish = threading.Event()
    real_publish_record = pump._publish_record

    def paused_publish_record(record):
        # TailPump.run has already received the record, so the follower's raw
        # composite cursor is watch:1.  Hold the exact pre-publication gap.
        follower_advanced.set()
        assert release_durable_publish.wait(5)
        real_publish_record(record)
        pump.stop()

    pump._publish_record = paused_publish_record
    _write_jsonl(
        state / "watch.jsonl",
        {**HEP_BLOCKED, "event_id": "publication-race", "n": 1},
    )
    pump.start()
    try:
        assert follower_advanced.wait(5)
        assert parse_cursor(pump.tailer.composite_cursor())["watch"] == 1

        pump.publish_serve({"kind": "serve.dictation", "state": "recording"})
        serve = subscriber.get(timeout=5)
        assert serve["source"] == "serve"
        assert parse_cursor(serve["cursor"])["watch"] == 0

        # A reconnect after this serve frame still replays the durable record.
        records, _, _ = read_page(
            state,
            since=serve["cursor"],
            sources={"watch"},
            limit=10,
        )
        assert [record.payload["event_id"] for record in records] == [
            "publication-race"
        ]

        release_durable_publish.set()
        durable = subscriber.get(timeout=5)
        assert durable["source"] == "watch"
        assert parse_cursor(durable["cursor"])["watch"] == 1
    finally:
        release_durable_publish.set()
        pump.stop()
        pump.join(timeout=5)
        assert not pump.is_alive()


def test_fresh_hello_snapshot_is_atomic_with_subscription(state, tmp_path):
    server = _server(tmp_path)
    subscribed = threading.Event()
    release_subscribe = threading.Event()
    publish_attempted = threading.Event()
    real_subscribe = server.hub.subscribe
    real_publish_record = server.pump._publish_record
    result = {}

    def paused_subscribe():
        subscriber = real_subscribe()
        subscribed.set()
        assert release_subscribe.wait(5)
        return subscriber

    def observed_publish(record):
        publish_attempted.set()
        real_publish_record(record)

    def connect():
        result["stream"] = _open_stream(server, "/api/v1/stream?sources=watch")

    server.hub.subscribe = paused_subscribe
    server.pump._publish_record = observed_publish
    connector = threading.Thread(target=connect, daemon=True)
    connection = None
    resumed = None
    try:
        connector.start()
        assert subscribed.wait(5)
        _write_jsonl(
            state / "watch.jsonl",
            {**HEP_BLOCKED, "event_id": "fresh-race", "n": 1},
        )
        assert publish_attempted.wait(5)
        release_subscribe.set()
        connector.join(timeout=5)
        assert not connector.is_alive()
        connection, response = result["stream"]
        hello_cursor, hello = _read_sse_event(response)
        assert hello["type"] == "hello"
        assert parse_cursor(hello_cursor)["watch"] == 0
        connection.close()
        connection = None

        resumed, response = _open_stream(
            server,
            "/api/v1/stream?sources=watch",
            headers={"Last-Event-ID": hello_cursor},
        )
        assert _read_sse_event(response)[0] == hello_cursor
        event_cursor, event = _read_sse_event(response)
        assert event["payload"]["event_id"] == "fresh-race"
        assert parse_cursor(event_cursor)["watch"] == 1
    finally:
        release_subscribe.set()
        if connection is not None:
            connection.close()
        if resumed is not None:
            resumed.close()
        server.shutdown()


def test_stream_reconnect_after_hello_and_each_replay_frame_is_lossless(
    state, tmp_path
):
    rows = [{**HEP_BLOCKED, "event_id": f"event-{n}", "n": n} for n in range(3)]
    _write_jsonl(state / "watch.jsonl", *rows)
    server = _server(tmp_path)
    cursor = "watch:0"
    seen: list[int] = []
    try:
        # First drop immediately after hello.  Its id must repeat the requested
        # cursor rather than advertise the pump's current EOF.
        connection, response = _open_stream(server, "/api/v1/stream?since=watch%3A0")
        hello_id, hello = _read_sse_event(response)
        assert hello["type"] == "hello"
        assert hello_id == cursor
        connection.close()

        # Drop after each individual replay record and reconnect from exactly
        # the frame the browser received.  No later record may be skipped.
        for expected in range(3):
            connection, response = _open_stream(
                server,
                "/api/v1/stream",
                headers={"Last-Event-ID": cursor},
            )
            next_hello_id, next_hello = _read_sse_event(response)
            assert next_hello["type"] == "hello"
            assert next_hello_id == cursor
            cursor, event = _read_sse_event(response)
            seen.append(event["payload"]["n"])
            assert parse_cursor(cursor)["watch"] == expected + 1
            connection.close()

        assert seen == [0, 1, 2]
    finally:
        server.shutdown()


def test_stream_reconnect_nonmonotonic_timestamps_never_skips_same_source(
    state, tmp_path
):
    rows = [
        {**HEP_BLOCKED, "event_id": "event-0", "n": 0, "ts": 30.0},
        {**HEP_BLOCKED, "event_id": "event-1", "n": 1, "ts": 10.0},
        {**HEP_BLOCKED, "event_id": "event-2", "n": 2, "ts": 20.0},
    ]
    _write_jsonl(state / "watch.jsonl", *rows)
    server = _server(tmp_path)
    cursor = "watch:0"
    seen: list[int] = []
    try:
        for expected in range(3):
            connection, response = _open_stream(
                server, "/api/v1/stream", headers={"Last-Event-ID": cursor}
            )
            hello_id, _ = _read_sse_event(response)
            assert hello_id == cursor
            cursor, event = _read_sse_event(response)
            seen.append(event["payload"]["n"])
            assert parse_cursor(cursor)["watch"] == expected + 1
            connection.close()
        assert seen == [0, 1, 2]
    finally:
        server.shutdown()


def test_resumed_rest_limit_cursor_does_not_skip_front_records(state, tmp_path):
    _write_jsonl(
        state / "watch.jsonl",
        *({**HEP_BLOCKED, "event_id": f"event-{n}", "n": n} for n in range(3)),
    )
    server = _server(tmp_path)
    try:
        status, first = _get_json(
            server, "/api/v1/events?since=watch%3A0&sources=watch&limit=2"
        )
        assert status == 200
        assert [event["payload"]["n"] for event in first["events"]] == [0, 1]
        assert [
            parse_cursor(event["cursor"])["watch"] for event in first["events"]
        ] == [1, 2]
        assert parse_cursor(first["cursor"])["watch"] == 2
        assert first["complete"] is False

        status, second = _get_json(
            server,
            f"/api/v1/events?since={first['cursor']}&sources=watch&limit=2",
        )
        assert status == 200
        assert [event["payload"]["n"] for event in second["events"]] == [2]
        assert parse_cursor(second["cursor"])["watch"] == 3
        assert second["complete"] is True
    finally:
        server.shutdown()


def test_stream_replay_preserves_source_filter_and_unseen_cursor(state, tmp_path):
    _write_jsonl(
        state / "watch.jsonl", {**HEP_BLOCKED, "event_id": "watch", "n": "watch"}
    )
    _write_jsonl(state / "ambient.jsonl", {"kind": "ambient.prompt", "n": "ambient"})
    server = _server(tmp_path)
    connection = None
    try:
        connection, response = _open_stream(
            server,
            "/api/v1/stream?sources=watch&since=watch%3A0%2Cambient%3A0",
        )
        hello_id, _ = _read_sse_event(response)
        assert hello_id == "watch:0,ambient:0"
        event_id, event = _read_sse_event(response)
        assert event["source"] == "watch"
        assert event["payload"]["n"] == "watch"
        assert parse_cursor(event_id) == {"watch": 1, "ambient": 0}
    finally:
        if connection is not None:
            connection.close()
        server.shutdown()


@pytest.mark.parametrize(
    "raw_cursor",
    (
        "watch%3A0%0Aid%3A%20watch%3A999",
        "watch%3A1%2C",
        "watch%3A1%2C%2Cambient%3A2",
        "watch%3A1%2Cwatch%3A2",
        "watch%3A-1",
    ),
)
def test_stream_rejects_invalid_cursor_before_sse_headers(state, tmp_path, raw_cursor):
    server = _server(tmp_path)
    try:
        connection = _conn(server)
        connection.request("GET", f"/api/v1/stream?since={raw_cursor}")
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == HTTPStatus.BAD_REQUEST
        assert response.getheader("Content-Type") == "application/json; charset=utf-8"
        assert body["error"]["code"] == "bad_cursor"
        connection.close()
    finally:
        server.shutdown()


def test_stream_canonicalizes_cursor_before_sse_id(state, tmp_path):
    server = _server(tmp_path)
    connection = None
    try:
        connection, response = _open_stream(server, "/api/v1/stream?since=watch%3A000")
        hello_id, hello = _read_sse_event(response)
        assert hello["type"] == "hello"
        assert hello_id == "watch:0"
    finally:
        if connection is not None:
            connection.close()
        server.shutdown()


def test_rest_to_stream_handoff_captures_concurrent_append_and_live_reconnect(
    state, tmp_path, monkeypatch
):
    import hark.dashboard.server as dashboard_server

    _write_jsonl(state / "watch.jsonl", {**HEP_BLOCKED, "event_id": "event-0", "n": 0})
    server = _server(tmp_path)
    connection = None
    try:
        status, page = _get_json(server, "/api/v1/events?sources=watch")
        assert status == 200
        assert [e["payload"]["n"] for e in page["events"]] == [0]
        assert parse_cursor(page["cursor"])["watch"] == 1

        snapshot_taken = threading.Event()
        release_snapshot = threading.Event()
        real_iter_replay = _pause_first_replay_snapshot(
            monkeypatch,
            dashboard_server,
            snapshot_taken,
            release_snapshot,
            timeout=5,
        )
        connection, response = _open_stream(
            server, f"/api/v1/stream?since={page['cursor']}"
        )
        hello_id, hello = _read_sse_event(response)
        assert hello["type"] == "hello"
        assert hello_id == page["cursor"]
        assert snapshot_taken.wait(5), "stream replay did not reach snapshot boundary"

        # This append occurs after the REST/replay snapshot but after the SSE
        # queue subscription.  It must arrive through the live side.
        _write_jsonl(
            state / "watch.jsonl", {**HEP_BLOCKED, "event_id": "event-1", "n": 1}
        )
        # Drop in the handoff gap: replay has taken its snapshot, but the
        # queued live event has not been written.  Reconnecting from hello's
        # unchanged id must recover the append from durable history.
        connection.close()
        connection = None
        release_snapshot.set()
        monkeypatch.setattr(dashboard_server, "iter_replay_records", real_iter_replay)
        connection, response = _open_stream(
            server,
            "/api/v1/stream",
            headers={"Last-Event-ID": page["cursor"]},
        )
        resumed_hello_id, _ = _read_sse_event(response)
        assert resumed_hello_id == page["cursor"]
        cursor, appended = _read_sse_event(response)
        assert appended["payload"]["n"] == 1
        assert parse_cursor(cursor)["watch"] == 2
        connection.close()
        connection = None

        # Reconnect after a delivered live record, append again, and prove the
        # next record is replayed/live without a hole.
        _write_jsonl(
            state / "watch.jsonl", {**HEP_BLOCKED, "event_id": "event-2", "n": 2}
        )
        connection, response = _open_stream(
            server, "/api/v1/stream", headers={"Last-Event-ID": cursor}
        )
        replay_hello_id, _ = _read_sse_event(response)
        assert replay_hello_id == cursor
        final_cursor, final = _read_sse_event(response)
        assert final["payload"]["n"] == 2
        assert parse_cursor(final_cursor)["watch"] == 3
    finally:
        if connection is not None:
            connection.close()
        server.shutdown()


def test_initial_replay_drops_covered_live_queue_duplicate(
    state, tmp_path, monkeypatch
):
    import hark.dashboard.server as dashboard_server

    server = _server(tmp_path)
    replay_entered = threading.Event()
    release_replay = threading.Event()
    real_replay = dashboard_server.DashboardHandler._sse_replay

    def paused_replay(handler, since, wanted):
        replay_entered.set()
        assert release_replay.wait(5)
        return real_replay(handler, since, wanted)

    monkeypatch.setattr(dashboard_server.DashboardHandler, "_sse_replay", paused_replay)
    connection = None
    try:
        connection, response = _open_stream(
            server, "/api/v1/stream?sources=watch&since=watch%3A0"
        )
        assert _read_sse_event(response)[0] == "watch:0"
        assert replay_entered.wait(5)
        _write_jsonl(
            state / "watch.jsonl",
            {**HEP_BLOCKED, "event_id": "overlap-1", "n": 1},
        )
        with server.hub._lock:
            subscriber = server.hub._subs[0]
        deadline = time.monotonic() + 5
        while subscriber.qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert subscriber.qsize() >= 1
        release_replay.set()

        _, first = _read_sse_event(response)
        assert first["payload"]["event_id"] == "overlap-1"
        _write_jsonl(
            state / "watch.jsonl",
            {**HEP_BLOCKED, "event_id": "overlap-2", "n": 2},
        )
        _, second = _read_sse_event(response)
        assert second["payload"]["event_id"] == "overlap-2"
    finally:
        release_replay.set()
        if connection is not None:
            connection.close()
        server.shutdown()


def test_late_publish_after_empty_overlap_drain_is_still_deduplicated(
    state, tmp_path, monkeypatch
):
    import hark.dashboard.server as dashboard_server

    server = _server(tmp_path)
    publish_entered = threading.Event()
    release_publish = threading.Event()
    publish_finished = threading.Event()
    drain_observed_empty = threading.Event()
    real_publish_record = server.pump._publish_record
    real_drain = dashboard_server.DashboardHandler._drain_replay_overlap
    first_publish = True

    def blocked_first_publish(record):
        nonlocal first_publish
        if first_publish:
            first_publish = False
            publish_entered.set()
            assert release_publish.wait(5)
        real_publish_record(record)
        publish_finished.set()

    def drain_then_release_publish(handler, subscriber, stream_cursor, wanted):
        retained = real_drain(handler, subscriber, stream_cursor, wanted)
        assert subscriber.empty()
        drain_observed_empty.set()
        release_publish.set()
        assert publish_finished.wait(5)
        return retained

    server.pump._publish_record = blocked_first_publish
    monkeypatch.setattr(
        dashboard_server.DashboardHandler,
        "_drain_replay_overlap",
        drain_then_release_publish,
    )
    connection = None
    try:
        _write_jsonl(
            state / "watch.jsonl",
            {**HEP_BLOCKED, "event_id": "late-overlap-1", "n": 1},
        )
        assert publish_entered.wait(5)

        connection, response = _open_stream(
            server, "/api/v1/stream?sources=watch&since=watch%3A0"
        )
        assert _read_sse_event(response)[0] == "watch:0"
        first_cursor, first = _read_sse_event(response)
        assert drain_observed_empty.wait(5)
        assert first["payload"]["event_id"] == "late-overlap-1"
        assert parse_cursor(first_cursor)["watch"] == 1

        _write_jsonl(
            state / "watch.jsonl",
            {**HEP_BLOCKED, "event_id": "late-overlap-2", "n": 2},
        )
        second_cursor, second = _read_sse_event(response)
        assert second["payload"]["event_id"] == "late-overlap-2"
        assert parse_cursor(second_cursor)["watch"] == 2
    finally:
        release_publish.set()
        if connection is not None:
            connection.close()
        server.shutdown()


def test_live_loop_delivers_rewritten_seq_below_old_highwater(state, tmp_path):
    from hark.dashboard.tailer import SourceTailer
    from hark.state_feed import format_cursor

    _write_jsonl(
        state / "watch.jsonl",
        {**HEP_BLOCKED, "event_id": "rewrite-old-1", "n": 1},
        {**HEP_BLOCKED, "event_id": "rewrite-old-2", "n": 2},
        {**HEP_BLOCKED, "event_id": "rewrite-old-3", "n": 3},
    )
    # Proved cursor at watch:1 so replay starts at the second record.
    proved = SourceTailer(state / "watch.jsonl", source="watch")
    proved.seek_to(1)
    since = format_cursor({"watch": proved.cursor_position})
    proved.close()
    server = _server(tmp_path)
    connection = None
    try:
        connection, response = _open_stream(
            server, f"/api/v1/stream?sources=watch&since={since}"
        )
        assert _read_sse_event(response)[0] == since
        replay_cursor, replay_two = _read_sse_event(response)
        highwater_cursor, replay_three = _read_sse_event(response)
        assert replay_two["payload"]["event_id"] == "rewrite-old-2"
        assert replay_three["payload"]["event_id"] == "rewrite-old-3"
        assert parse_cursor(highwater_cursor)["watch"] == 3

        positions = parse_cursor_positions(replay_cursor)
        replay_position = positions["watch"]
        assert replay_position.incarnation is not None
        assert replay_position.checkpoint is not None
        conflicting_checkpoint = (
            "0" * 32 if replay_position.checkpoint != "0" * 32 else "1" * 32
        )
        positions["watch"] = CursorPosition(
            seq=replay_position.seq,
            incarnation=replay_position.incarnation,
            checkpoint=conflicting_checkpoint,
            byte_offset=replay_position.byte_offset,
        )
        conflicting_cursor = format_cursor(positions)
        server.hub.publish(
            {
                "schema": "hark.dashboard.v1",
                "type": "event",
                "source": "watch",
                "cursor": conflicting_cursor,
                "payload": {
                    **HEP_BLOCKED,
                    "event_id": "rewrite-new-2",
                    "n": "replacement",
                },
            }
        )

        delivered_cursor, delivered = _read_sse_event(response)
        assert delivered_cursor == conflicting_cursor
        assert delivered["payload"]["event_id"] == "rewrite-new-2"
    finally:
        if connection is not None:
            connection.close()
        server.shutdown()


def test_rest_to_stream_handoff_captures_rotation_after_snapshot(
    state, tmp_path, monkeypatch
):
    import hark.dashboard.server as dashboard_server

    watch = state / "watch.jsonl"
    _write_jsonl(watch, {**HEP_BLOCKED, "event_id": "old", "n": "old"})
    server = _server(tmp_path)
    connection = None
    try:
        _, page = _get_json(server, "/api/v1/events?sources=watch")
        snapshot_taken = threading.Event()
        release_snapshot = threading.Event()
        _pause_first_replay_snapshot(
            monkeypatch,
            dashboard_server,
            snapshot_taken,
            release_snapshot,
            timeout=5,
        )
        connection, response = _open_stream(
            server, f"/api/v1/stream?since={page['cursor']}"
        )
        _read_sse_event(response)  # hello
        assert snapshot_taken.wait(5)

        os.replace(watch, state / "watch.jsonl.1")
        _write_jsonl(watch, {**HEP_BLOCKED, "event_id": "new", "n": "new"})
        release_snapshot.set()
        _, event = _read_sse_event(response)
        assert event["payload"]["n"] == "new"
    finally:
        if connection is not None:
            connection.close()
        server.shutdown()


def test_stream_queue_overflow_durably_catches_up_and_reconnects(
    state, tmp_path, monkeypatch
):
    import hark.dashboard.server as dashboard_server

    server = _server(tmp_path)
    connection = None
    try:
        snapshot_taken = threading.Event()
        release_snapshot = threading.Event()
        real_iter_replay = _pause_first_replay_snapshot(
            monkeypatch,
            dashboard_server,
            snapshot_taken,
            release_snapshot,
            timeout=10,
        )
        connection, response = _open_stream(
            server, "/api/v1/stream?sources=watch&since=watch%3A0"
        )
        hello_id, _ = _read_sse_event(response)
        assert hello_id == "watch:0"
        assert snapshot_taken.wait(5)

        total = dashboard_server.SUBSCRIBER_QUEUE_SIZE + 105
        _write_jsonl(
            state / "watch.jsonl",
            *(
                {**HEP_BLOCKED, "event_id": f"overflow-{n}", "n": n}
                for n in range(total)
            ),
        )
        with server.hub._lock:
            subscriber = server.hub._subs[0]
        deadline = time.monotonic() + 10
        while not subscriber.overflowed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert subscriber.overflowed, "subscriber queue did not overflow"

        release_snapshot.set()
        delivered_before_drop = total - 55
        cursor = hello_id
        seen: list[int] = []
        for expected in range(delivered_before_drop):
            cursor, event = _read_sse_event(response)
            seen.append(event["payload"]["n"])
            assert parse_cursor(cursor)["watch"] == expected + 1
        assert seen == list(range(delivered_before_drop))

        connection.close()
        connection = None
        monkeypatch.setattr(dashboard_server, "iter_replay_records", real_iter_replay)
        connection, response = _open_stream(
            server,
            "/api/v1/stream?sources=watch",
            headers={"Last-Event-ID": cursor},
        )
        resumed_hello, _ = _read_sse_event(response)
        assert resumed_hello == cursor
        tail: list[int] = []
        for expected in range(delivered_before_drop, total):
            _, event = _read_sse_event(response)
            tail.append(event["payload"]["n"])
        assert tail == list(range(delivered_before_drop, total))
    finally:
        release_snapshot.set()
        if connection is not None:
            connection.close()
        server.shutdown()


def test_sse_replay_uses_one_streaming_forward_scan(state, monkeypatch):
    import hark.dashboard.server as dashboard_server

    _write_jsonl(
        state / "watch.jsonl",
        *({**HEP_BLOCKED, "event_id": f"paged-{n}", "n": n} for n in range(5)),
    )
    replay_scans = 0
    real_iter_replay = dashboard_server.iter_replay_records

    def recording_iter_replay(*args, **kwargs):
        nonlocal replay_scans
        replay_scans += 1
        yield from real_iter_replay(*args, **kwargs)

    monkeypatch.setattr(dashboard_server, "iter_replay_records", recording_iter_replay)

    class FakeHandler:
        server = type("Server", (), {"state": state})()
        delivered = []

        def __init__(self):
            self._delivered_witnesses = dashboard_server.DeliveredDurableWitnesses()

        def _sse_write(self, envelope):
            self.delivered.append(envelope)

    handler = FakeHandler()
    cursor = dashboard_server.DashboardHandler._sse_replay(
        handler, "watch:0", {"watch"}
    )

    assert replay_scans == 1
    assert [envelope["payload"]["n"] for envelope in handler.delivered] == list(
        range(5)
    )
    assert [
        parse_cursor(envelope["cursor"])["watch"] for envelope in handler.delivered
    ] == [1, 2, 3, 4, 5]
    assert parse_cursor(cursor)["watch"] == 5


def test_sse_replay_disconnect_stops_materializing_unseen_suffix(state, monkeypatch):
    import hark.dashboard.tailer as dashboard_tailer
    from hark.dashboard.server import DashboardHandler, DeliveredDurableWitnesses

    _write_jsonl(
        state / "watch.jsonl",
        *({**HEP_BLOCKED, "event_id": f"streamed-{n}", "n": n} for n in range(5000)),
    )
    materialized = 0
    real_record_ts = dashboard_tailer._record_ts

    def counted_record_ts(record):
        nonlocal materialized
        materialized += 1
        return real_record_ts(record)

    monkeypatch.setattr(dashboard_tailer, "_record_ts", counted_record_ts)

    class FakeHandler:
        server = type("Server", (), {"state": state})()
        delivered = 0

        def __init__(self):
            self._delivered_witnesses = DeliveredDurableWitnesses()

        def _sse_write(self, envelope):
            self.delivered += 1
            if self.delivered == 2:
                raise BrokenPipeError

    handler = FakeHandler()
    with pytest.raises(BrokenPipeError):
        DashboardHandler._sse_replay(handler, "watch:0", {"watch"})

    assert handler.delivered == 2
    assert materialized == 2


def test_failed_sse_write_is_not_recorded_as_delivered(state):
    from hark.dashboard.server import DashboardHandler, DeliveredDurableWitnesses

    _write_jsonl(
        state / "watch.jsonl",
        {**HEP_BLOCKED, "event_id": "failed-write", "n": 1},
    )

    class FakeHandler:
        server = type("Server", (), {"state": state})()

        def __init__(self):
            self._delivered_witnesses = DeliveredDurableWitnesses()

        def _sse_write(self, envelope):
            raise BrokenPipeError

    handler = FakeHandler()
    with pytest.raises(BrokenPipeError):
        DashboardHandler._sse_replay(handler, "watch:0", {"watch"})

    assert len(handler._delivered_witnesses) == 0


def test_filtered_replay_sources_are_not_witnessed(state):
    from hark.dashboard.server import DashboardHandler, DeliveredDurableWitnesses

    _write_jsonl(
        state / "watch.jsonl",
        {**HEP_BLOCKED, "event_id": "wanted", "n": 1},
    )
    _write_jsonl(
        state / "ambient.jsonl",
        {"kind": "ambient.prompt", "event_id": "filtered", "n": 2},
    )

    class FakeHandler:
        server = type("Server", (), {"state": state})()

        def __init__(self):
            self._delivered_witnesses = DeliveredDurableWitnesses()
            self.delivered = []

        def _sse_write(self, envelope):
            self.delivered.append(envelope)

    handler = FakeHandler()
    DashboardHandler._sse_replay(
        handler,
        "watch:0,ambient:0",
        {"watch"},
    )

    assert [envelope["source"] for envelope in handler.delivered] == ["watch"]
    assert len(handler._delivered_witnesses) == 1
    assert handler._delivered_witnesses.covers(handler.delivered[0])


def test_delivered_witness_covers_only_exact_full_proof():
    from hark.dashboard.server import DeliveredDurableWitnesses

    def envelope(source, cursor, payload=None):
        return {"source": source, "cursor": cursor, "payload": payload or {}}

    witnesses = DeliveredDurableWitnesses()
    assert not witnesses.covers(envelope("watch", "watch:4"))
    assert not witnesses.covers(envelope("serve", "serve:1"))

    incarnation_a = "a" * 32
    incarnation_b = "b" * 32
    checkpoint = "c" * 32
    other_checkpoint = "d" * 32
    proved_one = f"watch:1@{incarnation_a}~{checkpoint}~10"
    witnesses.add(envelope("watch", proved_one))
    assert witnesses.covers(envelope("watch", proved_one))
    assert not witnesses.covers(
        envelope("watch", f"watch:1@{incarnation_a}~{other_checkpoint}~10")
    )
    assert not witnesses.covers(
        envelope("watch", f"watch:1@{incarnation_a}~{checkpoint}~11")
    )
    assert not witnesses.covers(
        envelope("watch", f"watch:2@{incarnation_a}~{other_checkpoint}~20")
    )
    assert not witnesses.covers(
        envelope("watch", f"watch:1@{incarnation_a}~{checkpoint}")
    )
    witnesses_other = DeliveredDurableWitnesses()
    witnesses_other.add(envelope("watch", f"watch:1@{incarnation_b}~{checkpoint}~10"))
    assert not witnesses_other.covers(envelope("watch", proved_one))


def test_delivered_witness_eviction_is_duplicate_safe(monkeypatch):
    import hark.dashboard.server as dashboard_server

    monkeypatch.setattr(dashboard_server, "SUBSCRIBER_QUEUE_SIZE", 1)
    witnesses = dashboard_server.DeliveredDurableWitnesses()
    incarnation = "a" * 32

    def envelope(seq):
        return {
            "source": "watch",
            "cursor": f"watch:{seq}@{incarnation}~{str(seq) * 32}~{seq * 10}",
            "payload": {},
        }

    first, second, third = envelope(1), envelope(2), envelope(3)
    witnesses.add(first)
    witnesses.add(second)
    witnesses.add(third)

    assert not witnesses.covers(first)  # eviction delivers; it never loses data
    assert witnesses.covers(second)
    assert witnesses.covers(third)


def test_overflow_fence_rejects_serve_ahead_of_dropped_durable(monkeypatch):
    import hark.dashboard.server as dashboard_server

    monkeypatch.setattr(dashboard_server, "SUBSCRIBER_QUEUE_SIZE", 1)
    hub = dashboard_server.Hub()
    subscriber = hub.subscribe()
    first = {"source": "watch", "cursor": "watch:1"}
    dropped = {"source": "watch", "cursor": "watch:2"}
    unsafe_serve = {"source": "serve", "cursor": "watch:2,serve:1"}

    hub.publish(first)
    hub.publish(dropped)
    assert subscriber.overflowed
    assert subscriber.get_nowait() == first

    # Even with capacity available again, the overflow fence stays closed
    # until replay recovers the dropped durable record.
    hub.publish(unsafe_serve)
    assert subscriber.empty()


def test_overflow_retry_carries_serve_retained_before_late_overflow():
    from hark.dashboard.server import (
        DashboardHandler,
        DeliveredDurableWitnesses,
        SubscriberQueue,
    )

    serve = {
        "schema": "hark.dashboard.v1",
        "type": "event",
        "source": "serve",
        "cursor": "watch:1,serve:1",
        "payload": {"kind": "serve.dictation"},
    }
    covered = {
        "schema": "hark.dashboard.v1",
        "type": "event",
        "source": "watch",
        "cursor": f"watch:1@{'a' * 32}~{'b' * 32}~10",
        "payload": {"kind": "agent.blocked"},
    }

    class LateOverflowQueue(SubscriberQueue):
        def __init__(self):
            super().__init__()
            self.removals = 0
            self.triggered = False

        def get_nowait(self):
            envelope = super().get_nowait()
            self.removals += 1
            if self.removals == 2 and not self.triggered:
                self.triggered = True
                self.mark_overflow()
            return envelope

    class FakeHandler:
        replay_calls = 0

        def __init__(self):
            self._delivered_witnesses = DeliveredDurableWitnesses()

        def _sse_replay(self, since, wanted):
            self.replay_calls += 1
            self._delivered_witnesses.add(covered)
            return covered["cursor"]

    subscriber = LateOverflowQueue()
    subscriber.put_nowait(serve)
    subscriber.put_nowait(covered)
    subscriber.mark_overflow()
    handler = FakeHandler()

    cursor, retained = DashboardHandler._recover_subscriber_overflow(
        handler, subscriber, "watch:0", None
    )

    assert cursor == covered["cursor"]
    assert handler.replay_calls == 2
    assert retained == [serve]


def test_overflow_recovery_preserves_queue_order_and_bounds_retained(monkeypatch):
    import hark.dashboard.server as dashboard_server

    monkeypatch.setattr(dashboard_server, "SUBSCRIBER_QUEUE_SIZE", 3)
    durable = {
        "schema": "hark.dashboard.v1",
        "type": "event",
        "source": "watch",
        "cursor": "watch:2",
        "payload": {"kind": "agent.blocked"},
    }
    serve = {
        "schema": "hark.dashboard.v1",
        "type": "event",
        "source": "serve",
        "cursor": "watch:2,serve:1",
        "payload": {"kind": "serve.dictation"},
    }

    class FakeHandler:
        def __init__(self):
            self._delivered_witnesses = dashboard_server.DeliveredDurableWitnesses()

        def _sse_replay(self, since, wanted):
            return "watch:1"

    subscriber = dashboard_server.SubscriberQueue()
    subscriber.put_nowait(durable)
    subscriber.put_nowait(serve)
    subscriber.mark_overflow()

    _, retained = dashboard_server.DashboardHandler._recover_subscriber_overflow(
        FakeHandler(), subscriber, "watch:1", None
    )

    # Sending the serve envelope first would advertise watch:2 and let a
    # disconnect skip the durable watch event that was actually queued first.
    assert retained == [durable, serve]
    assert len(retained) <= dashboard_server.SUBSCRIBER_QUEUE_SIZE


def test_overflow_recovery_bounds_non_durable_carry_across_retries(monkeypatch):
    import hark.dashboard.server as dashboard_server

    monkeypatch.setattr(dashboard_server, "SUBSCRIBER_QUEUE_SIZE", 3)

    def serve(n):
        return {
            "schema": "hark.dashboard.v1",
            "type": "event",
            "source": "serve",
            "cursor": f"serve:{n + 1}",
            "payload": {"kind": "serve.dictation", "n": n},
        }

    class RepeatedLateOverflowQueue(dashboard_server.SubscriberQueue):
        def __init__(self):
            super().__init__()
            self.removals = 0

        def get_nowait(self):
            envelope = super().get_nowait()
            self.removals += 1
            if self.removals in (3, 6):
                self.mark_overflow()
            return envelope

    subscriber = RepeatedLateOverflowQueue()
    for n in range(3):
        subscriber.put_nowait(serve(n))
    subscriber.mark_overflow()

    class FakeHandler:
        replay_calls = 0

        def __init__(self):
            self._delivered_witnesses = dashboard_server.DeliveredDurableWitnesses()

        def _sse_replay(self, since, wanted):
            self.replay_calls += 1
            if self.replay_calls > 1:
                offset = (self.replay_calls - 1) * 3
                for n in range(offset, offset + 3):
                    subscriber.put_nowait(serve(n))
            return since

    _, retained = dashboard_server.DashboardHandler._recover_subscriber_overflow(
        FakeHandler(), subscriber, "watch:0", None
    )

    assert [envelope["payload"]["n"] for envelope in retained] == [6, 7, 8]
    assert len(retained) == dashboard_server.SUBSCRIBER_QUEUE_SIZE


def test_overflow_drain_retains_append_after_replay_snapshot(
    state, tmp_path, monkeypatch
):
    import hark.dashboard.server as dashboard_server

    monkeypatch.setattr(dashboard_server, "SUBSCRIBER_QUEUE_SIZE", 3)
    real_replay = dashboard_server.DashboardHandler._sse_replay
    initial_snapshot = threading.Event()
    release_initial = threading.Event()
    recovery_snapshot = threading.Event()
    release_recovery = threading.Event()
    replay_calls = 0

    def paused_replay(handler, since, wanted):
        nonlocal replay_calls
        replay_calls += 1
        result = real_replay(handler, since, wanted)
        if replay_calls == 2:
            recovery_snapshot.set()
            assert release_recovery.wait(10)
        return result

    _pause_first_replay_snapshot(
        monkeypatch,
        dashboard_server,
        initial_snapshot,
        release_initial,
        timeout=10,
    )
    monkeypatch.setattr(dashboard_server.DashboardHandler, "_sse_replay", paused_replay)
    server = _server(tmp_path)
    connection = None
    try:
        connection, response = _open_stream(
            server, "/api/v1/stream?sources=watch&since=watch%3A0"
        )
        _read_sse_event(response)  # hello
        assert initial_snapshot.wait(5)

        _write_jsonl(
            state / "watch.jsonl",
            *({**HEP_BLOCKED, "event_id": f"race-{n}", "n": n} for n in range(4)),
        )
        with server.hub._lock:
            subscriber = server.hub._subs[0]
        deadline = time.monotonic() + 5
        while not subscriber.overflowed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert subscriber.overflowed
        subscriber.get_nowait()  # create one slot without clearing overflow

        release_initial.set()
        assert recovery_snapshot.wait(10)
        assert not subscriber.overflowed

        # The disk snapshot/high-water is watch:4.  This append is published
        # into the newly available queue slot without overflowing, so recovery
        # must retain it instead of treating every queued item as covered.
        _write_jsonl(
            state / "watch.jsonl", {**HEP_BLOCKED, "event_id": "race-4", "n": 4}
        )
        deadline = time.monotonic() + 5
        while subscriber.qsize() < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert subscriber.qsize() == 3
        assert not subscriber.overflowed
        release_recovery.set()

        seen = []
        for expected in range(5):
            cursor, event = _read_sse_event(response)
            seen.append(event["payload"]["n"])
            assert parse_cursor(cursor)["watch"] == expected + 1
        assert seen == [0, 1, 2, 3, 4]
    finally:
        release_initial.set()
        release_recovery.set()
        if connection is not None:
            connection.close()
        server.shutdown()


def test_stream_spectrum_coalesced(state, tmp_path):
    """B087: serve.spectrum frames appear on SSE without advancing history."""
    from hark.audio.spectrum import make_spectrum_payload

    server = _server(tmp_path)
    c = _conn(server)
    try:
        c.request("GET", "/api/v1/stream")
        r = c.getresponse()
        assert r.status == 200

        def read_event(timeout_s=10.0):
            deadline = time.monotonic() + timeout_s
            data = None
            while time.monotonic() < deadline:
                line = r.fp.readline().decode()
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                elif line == "\n" and data is not None:
                    return data
            raise AssertionError("no SSE event within timeout")

        hello = read_event()
        assert hello["type"] == "hello"

        server.hub.set_spectrum(
            make_spectrum_payload([0.1, 0.5, 0.9], recording=True, source="listen")
        )
        # may need a couple of frames if coalescing races; wait for spectrum
        deadline = time.monotonic() + 5.0
        spec = None
        while time.monotonic() < deadline:
            ev = read_event(timeout_s=2.0)
            if ev.get("payload", {}).get("kind") == "serve.spectrum":
                spec = ev
                break
        assert spec is not None
        p = spec["payload"]
        assert p["kind"] == "serve.spectrum"
        assert p["recording"] is True
        assert p["bands"] == [0.1, 0.5, 0.9]
        assert p["source"] == "listen"
        # Spectrum is not persisted; cursor is a composite resume token only.
        assert isinstance(spec["cursor"], str)
        # spectrum must not pollute JSONL event pages
        status, body = _get_json(server, "/api/v1/events")
        assert status == 200
        assert not any(
            (e.get("payload") or {}).get("kind") == "serve.spectrum"
            for e in body["events"]
        )
    finally:
        c.close()
        server.shutdown()


class FakeHerdrClient:
    def __init__(self, live: AgentInfo, pane_text: str) -> None:
        self.live = live
        self.pane_text = pane_text
        self.sent_text: list[tuple[str, str]] = []

    def get_agent(self, pane_id):
        return self.live

    def read_pane(self, pane_id, lines=60):
        return self.pane_text

    def send_text(self, pane_id, text):
        self.sent_text.append((pane_id, text))

    def send_keys(self, pane_id, keys):
        pass


def test_answer_register_on_demand(state, tmp_path, monkeypatch):
    from hark.events import extract_question_excerpt
    from hark.fingerprint import question_fingerprint

    pane_text = "Allow this action?\n  1. Yes\n  2. No\n"
    fp = question_fingerprint(extract_question_excerpt(pane_text))
    hep = json.loads(json.dumps(HEP_BLOCKED))
    hep["question"]["fingerprint"] = fp
    _write_jsonl(state / "watch.jsonl", hep)

    live = AgentInfo(
        session_id="local",
        pane_id="w1:p6",
        agent="claude",
        status="blocked",
        revision=3,
    )
    fake = FakeHerdrClient(live, pane_text)
    monkeypatch.setattr(dash_api, "_client_for", lambda cfg, sid: fake)

    server = _server(tmp_path)
    try:
        status, body, _ = _post_json(
            server,
            "/api/v1/answer",
            {"event_id": hep["event_id"], "text": "yes please"},
        )
        assert status == 200, body
        assert body["status"] == "delivered"
        assert fake.sent_text == [("w1:p6", "yes please")]
        # idempotency: second answer refused
        status, body, _ = _post_json(
            server,
            "/api/v1/answer",
            {"event_id": hep["event_id"], "text": "again"},
        )
        assert status == 409 and body["status"] == "rejected"
        assert body["detail"] == "already_delivered"
    finally:
        server.shutdown()


def test_answer_stale_revision_rejected(state, tmp_path, monkeypatch):
    store = DeliveryStore()
    store.save_event(
        BoundEvent(
            event_id="evstale",
            session_id="local",
            pane_id="w1:p1",
            pane_revision=3,
            question_fingerprint="blake2b:x",
        )
    )
    live = AgentInfo(
        session_id="local",
        pane_id="w1:p1",
        agent="claude",
        status="blocked",
        revision=9,
    )
    monkeypatch.setattr(
        dash_api, "_client_for", lambda cfg, sid: FakeHerdrClient(live, "Q?")
    )
    server = _server(tmp_path)
    try:
        status, body, _ = _post_json(
            server, "/api/v1/answer", {"event_id": "evstale", "text": "hi"}
        )
        assert status == 409 and body["detail"] == "stale_revision"
    finally:
        server.shutdown()


def test_answer_unknown_event_404(state, tmp_path):
    server = _server(tmp_path)
    try:
        status, body, _ = _post_json(
            server, "/api/v1/answer", {"event_id": "nope", "text": "hi"}
        )
        assert status == 404 and body["detail"] == "unknown_event"
    finally:
        server.shutdown()


def test_prompt_appends_ambient_and_returns_event_id(state, tmp_path):
    server = _server(tmp_path)
    try:
        status, body, _ = _post_json(
            server, "/api/v1/prompt", {"text": "check the deploy"}
        )
        assert status == 200 and body["ok"]
        lines = (state / "ambient.jsonl").read_text().splitlines()
        obj = json.loads(lines[-1])
        assert obj["kind"] == "ambient.prompt"
        assert obj["event_id"] == body["event_id"]
        assert obj["final"] is True and obj["text"] == "check the deploy"
    finally:
        server.shutdown()


def test_deliveries_and_usage_snapshots(state, tmp_path):
    store = DeliveryStore()
    store.save_event(
        BoundEvent(
            event_id="e1",
            session_id="local",
            pane_id="w1:p1",
            pane_revision=1,
            question_fingerprint="fp",
        )
    )
    store.mark("e2", "delivered")
    server = _server(tmp_path)
    try:
        status, body = _get_json(server, "/api/v1/deliveries")
        assert status == 200
        assert [p["event_id"] for p in body["pending"]] == ["e1"]
        assert body["recent"][-1]["event_id"] == "e2"

        status, body = _get_json(server, "/api/v1/usage")
        assert status == 200 and "summary" in body
    finally:
        server.shutdown()


def test_placeholder_index_when_no_bundle(state, tmp_path):
    server = _server(tmp_path)
    try:
        if server.static_root is not None:
            pytest.skip("webui bundle present")
        c = _conn(server)
        c.request("GET", "/")
        r = c.getresponse()
        assert r.status == 200
        assert b"hark webui is running" in r.read()
        c.close()
    finally:
        server.shutdown()


@pytest.fixture(params=("webui_dist", "dist"))
def static_root(request, tmp_path, monkeypatch) -> Path:
    """Exercise the packaged and development bundle layouts identically."""
    if request.param == "webui_dist":
        root = tmp_path / "package" / "webui_dist"
    else:
        root = tmp_path / "repo" / "webui" / "dist"
    root.mkdir(parents=True)
    (root / "index.html").write_text("<h1>dashboard</h1>", encoding="utf-8")
    monkeypatch.setattr("hark.dashboard.server.resolve_static_root", lambda: root)
    return root


def _get_static(server, path: str) -> tuple[int, bytes]:
    status, body, _ = _get_static_with_headers(server, path)
    return status, body


def _get_static_with_headers(server, path: str) -> tuple[int, bytes, dict[str, str]]:
    c = _conn(server)
    try:
        c.request("GET", path)
        response = c.getresponse()
        body = response.read()
        headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, body, headers
    finally:
        c.close()


def test_static_serves_valid_asset(state, tmp_path, static_root):
    asset = static_root / "assets" / "app.js"
    asset.parent.mkdir()
    asset.write_bytes(b"console.log('safe')")
    server = _server(tmp_path)
    try:
        assert _get_static(server, "/assets/app.js") == (
            HTTPStatus.OK,
            b"console.log('safe')",
        )
    finally:
        server.shutdown()


def test_static_contained_missing_path_uses_spa_fallback(state, tmp_path, static_root):
    server = _server(tmp_path)
    try:
        assert _get_static(server, "/settings/profile") == (
            HTTPStatus.OK,
            b"<h1>dashboard</h1>",
        )
    finally:
        server.shutdown()


@pytest.mark.parametrize("path", ("/", "/index.html", "/settings/profile"))
def test_static_in_root_symlink_index_preserves_logical_metadata(
    state, tmp_path, static_root, path
):
    current = static_root / "current-dashboard"
    (static_root / "index.html").replace(current)
    (static_root / "index.html").symlink_to(current.name)
    server = _server(tmp_path)
    try:
        status, body, headers = _get_static_with_headers(server, path)
        assert (status, body) == (HTTPStatus.OK, b"<h1>dashboard</h1>")
        assert headers["content-type"] == "text/html"
        assert headers["cache-control"] == "no-cache"
    finally:
        server.shutdown()


def test_static_rejects_percent_encoded_null_byte(state, tmp_path, static_root):
    server = _server(tmp_path)
    try:
        status, body = _get_static(server, "/%00")
        assert status == HTTPStatus.NOT_FOUND
        assert b"not_found" in body
    finally:
        server.shutdown()


@pytest.mark.parametrize("dot_segment", ("..", "%2e%2e"))
def test_static_rejects_matching_prefix_sibling_traversal(
    state, tmp_path, static_root, dot_segment
):
    sibling = static_root.parent / f"{static_root.name}-secret"
    sibling.mkdir()
    (sibling / "secret.txt").write_bytes(b"TOP-SECRET")
    server = _server(tmp_path)
    try:
        status, body = _get_static(server, f"/{dot_segment}/{sibling.name}/secret.txt")
        assert status == HTTPStatus.NOT_FOUND
        assert b"TOP-SECRET" not in body
    finally:
        server.shutdown()


def test_static_rejects_symlink_escape(state, tmp_path, static_root):
    secret = tmp_path / "outside-secret.txt"
    secret.write_bytes(b"TOP-SECRET")
    (static_root / "secret.txt").symlink_to(secret)
    server = _server(tmp_path)
    try:
        status, body = _get_static(server, "/secret.txt")
        assert status == HTTPStatus.NOT_FOUND
        assert b"TOP-SECRET" not in body
    finally:
        server.shutdown()


def test_static_rejects_spa_fallback_symlink_escape(state, tmp_path, static_root):
    secret = tmp_path / "outside-index.html"
    secret.write_bytes(b"TOP-SECRET")
    (static_root / "index.html").unlink()
    (static_root / "index.html").symlink_to(secret)
    server = _server(tmp_path)
    try:
        status, body = _get_static(server, "/contained-but-missing")
        assert status == HTTPStatus.NOT_FOUND
        assert b"TOP-SECRET" not in body
    finally:
        server.shutdown()


def test_dictation_bad_mode_400_and_no_capture(state, tmp_path, monkeypatch):
    # NEVER let a unit test open the real microphone: a real capture here
    # holds the process-wide MicLease and ducks media, poisoning later tests.
    def forbid(*a, **kw):
        raise AssertionError("test must not start a real capture")

    monkeypatch.setattr("hark.speech.run_listen", forbid)
    server = _server(tmp_path)
    try:
        status, body, _ = _post_json(
            server, "/api/v1/dictation/start", {"mode": "browser"}
        )
        assert status == 400 and body["error"]["code"] == "bad_request"
        status, body, _ = _post_json(server, "/api/v1/dictation/stop", {})
        assert status == 409 and body["error"]["code"] == "no_capture"
    finally:
        server.shutdown()

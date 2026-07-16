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
from hark.dashboard.tailer import parse_cursor
from hark.delivery import BoundEvent, DeliveryStore
from hark.herdr.client import AgentInfo


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
        first = read_event()
        second = read_event()
        assert first["source"] == second["source"] == "watch"
        assert first["payload"]["kind"] == "agent.blocked"
        assert [parse_cursor(e["cursor"])["watch"] for e in (first, second)] == [
            1,
            2,
        ]
    finally:
        c.close()
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

        real_read_page = dashboard_server.read_page
        snapshot_taken = threading.Event()
        release_snapshot = threading.Event()

        def paused_read_page(*args, **kwargs):
            result = real_read_page(*args, **kwargs)
            snapshot_taken.set()
            assert release_snapshot.wait(5), "test did not release replay snapshot"
            return result

        monkeypatch.setattr(dashboard_server, "read_page", paused_read_page)
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
        monkeypatch.setattr(dashboard_server, "read_page", real_read_page)
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
        real_read_page = dashboard_server.read_page
        snapshot_taken = threading.Event()
        release_snapshot = threading.Event()

        def paused_read_page(*args, **kwargs):
            result = real_read_page(*args, **kwargs)
            snapshot_taken.set()
            assert release_snapshot.wait(5)
            return result

        monkeypatch.setattr(dashboard_server, "read_page", paused_read_page)
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
        real_read_page = dashboard_server.read_page
        snapshot_taken = threading.Event()
        release_snapshot = threading.Event()

        def paused_read_page(*args, **kwargs):
            result = real_read_page(*args, **kwargs)
            snapshot_taken.set()
            assert release_snapshot.wait(10), "test did not release replay snapshot"
            return result

        monkeypatch.setattr(dashboard_server, "read_page", paused_read_page)
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
        monkeypatch.setattr(dashboard_server, "read_page", real_read_page)
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


def test_sse_replay_pages_backfill_with_bounded_reads(state, monkeypatch):
    import hark.dashboard.server as dashboard_server

    monkeypatch.setattr(dashboard_server, "SSE_REPLAY_PAGE_SIZE", 2)
    _write_jsonl(
        state / "watch.jsonl",
        *({**HEP_BLOCKED, "event_id": f"paged-{n}", "n": n} for n in range(5)),
    )
    read_limits = []
    real_read_page = dashboard_server.read_page

    def recording_read_page(*args, **kwargs):
        read_limits.append(kwargs["limit"])
        return real_read_page(*args, **kwargs)

    monkeypatch.setattr(dashboard_server, "read_page", recording_read_page)

    class FakeHandler:
        server = type("Server", (), {"state": state})()
        delivered = []

        def _sse_write(self, envelope):
            self.delivered.append(envelope)

    handler = FakeHandler()
    cursor = dashboard_server.DashboardHandler._sse_replay(
        handler, "watch:0", {"watch"}
    )

    assert read_limits == [2, 2, 2]
    assert [envelope["payload"]["n"] for envelope in handler.delivered] == list(
        range(5)
    )
    assert [
        parse_cursor(envelope["cursor"])["watch"] for envelope in handler.delivered
    ] == [1, 2, 3, 4, 5]
    assert parse_cursor(cursor)["watch"] == 5


def test_overflow_drain_discards_only_cursor_covered_envelopes():
    from hark.dashboard.server import _durable_envelope_covered

    def envelope(source, cursor, payload=None):
        return {"source": source, "cursor": cursor, "payload": payload or {}}

    highwater = "watch:4,bound:2,delivery:3"
    assert _durable_envelope_covered(envelope("watch", "watch:4"), highwater)
    assert not _durable_envelope_covered(envelope("watch", "watch:5"), highwater)
    assert _durable_envelope_covered(
        envelope("delivery", "bound:2", {"type": "bound"}), highwater
    )
    assert not _durable_envelope_covered(
        envelope("delivery", "delivery:4", {"type": "outcome"}), highwater
    )
    assert not _durable_envelope_covered(envelope("serve", "serve:1"), highwater)


def test_overflow_retry_carries_serve_retained_before_late_overflow():
    from hark.dashboard.server import DashboardHandler, SubscriberQueue

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
        "cursor": "watch:1",
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

        def _sse_replay(self, since, wanted):
            self.replay_calls += 1
            return "watch:1"

    subscriber = LateOverflowQueue()
    subscriber.put_nowait(serve)
    subscriber.put_nowait(covered)
    subscriber.mark_overflow()
    handler = FakeHandler()

    cursor, retained = DashboardHandler._recover_subscriber_overflow(
        handler, subscriber, "watch:0", None
    )

    assert cursor == "watch:1"
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
    real_read_page = dashboard_server.read_page
    real_replay = dashboard_server.DashboardHandler._sse_replay
    initial_snapshot = threading.Event()
    release_initial = threading.Event()
    recovery_snapshot = threading.Event()
    release_recovery = threading.Event()
    replay_calls = 0

    def paused_read_page(*args, **kwargs):
        result = real_read_page(*args, **kwargs)
        if not initial_snapshot.is_set():
            initial_snapshot.set()
            assert release_initial.wait(10)
        return result

    def paused_replay(handler, since, wanted):
        nonlocal replay_calls
        replay_calls += 1
        result = real_replay(handler, since, wanted)
        if replay_calls == 2:
            recovery_snapshot.set()
            assert release_recovery.wait(10)
        return result

    monkeypatch.setattr(dashboard_server, "read_page", paused_read_page)
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
        cursor_before = hello["cursor"]

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
        # cursor is composite but must not invent a new serve-seq-only event stream
        assert isinstance(spec["cursor"], str)
        # spectrum must not pollute JSONL event pages
        status, body = _get_json(server, "/api/v1/events")
        assert status == 200
        assert not any(
            (e.get("payload") or {}).get("kind") == "serve.spectrum"
            for e in body["events"]
        )
        assert spec["cursor"] == cursor_before
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

"""B061: hark serve end-to-end (real HTTP over loopback, tmp state dir)."""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from hark.config import load_config
from hark.dashboard import api as dash_api
from hark.dashboard.server import DashboardServer
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
    _write_jsonl(state / "system.jsonl", {
        "ts": 1.0, "seq": 1, "level": "info", "component": "tts",
        "event": "tts.ok", "message": "ok", "data": {}, "pid": 1,
    })
    server = _server(tmp_path)
    try:
        status, body = _get_json(server, "/api/v1/events")
        assert status == 200 and body["ok"]
        sources = {e["source"] for e in body["events"]}
        assert {"watch", "system"} <= sources
        for e in body["events"]:
            assert e["schema"] == "hark.dashboard.v1"

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

        _write_jsonl(state / "watch.jsonl", HEP_BLOCKED)
        event = read_event()
        assert event["source"] == "watch"
        assert event["payload"]["kind"] == "agent.blocked"
        assert "watch:1" in event["cursor"]
    finally:
        c.close()
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
            (e.get("payload") or {}).get("kind") == "serve.spectrum" for e in body["events"]
        )
        del cursor_before
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
        session_id="local", pane_id="w1:p6", agent="claude",
        status="blocked", revision=3,
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
    store.save_event(BoundEvent(
        event_id="evstale", session_id="local", pane_id="w1:p1",
        pane_revision=3, question_fingerprint="blake2b:x",
    ))
    live = AgentInfo(
        session_id="local", pane_id="w1:p1", agent="claude",
        status="blocked", revision=9,
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
    store.save_event(BoundEvent(
        event_id="e1", session_id="local", pane_id="w1:p1",
        pane_revision=1, question_fingerprint="fp",
    ))
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


def test_dictation_bad_mode_400_and_no_capture(state, tmp_path, monkeypatch):
    # NEVER let a unit test open the real microphone: a real capture here
    # holds the process-wide MicLease and ducks media, poisoning later tests.
    def forbid(*a, **kw):
        raise AssertionError("test must not start a real capture")

    monkeypatch.setattr("hark.speech.run_listen", forbid)
    server = _server(tmp_path)
    try:
        status, body, _ = _post_json(server, "/api/v1/dictation/start", {"mode": "browser"})
        assert status == 400 and body["error"]["code"] == "bad_request"
        status, body, _ = _post_json(server, "/api/v1/dictation/stop", {})
        assert status == 409 and body["error"]["code"] == "no_capture"
    finally:
        server.shutdown()

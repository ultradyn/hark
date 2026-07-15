"""`hark serve` — stdlib HTTP server for hark.dashboard.v1.

ThreadingHTTPServer (a long-lived SSE stream must never block REST calls);
one multiplexed SSE stream per client; token→cookie auth (EventSource cannot
send Authorization headers). See docs/DASHBOARD.md.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import queue
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from hark import __version__
from hark.config import HarkConfig
from hark.dashboard import api
from hark.dashboard.tailer import MultiTailer, read_page
from hark.events import utc_now_iso
from hark.paths import state_dir
from hark.syslog import log

SCHEMA = "hark.dashboard.v1"
SERVER_NAME = "hark-serve-py"
SOURCES = ["watch", "ambient", "system", "usage", "delivery", "serve"]
COOKIE_NAME = "hark_dash"
MAX_JSON_BODY = 1 << 20  # 1 MiB
MAX_AUDIO_BODY = 32 << 20  # 32 MiB
LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class Hub:
    """Fan-out of stream envelopes to SSE subscribers.

    Spectrum frames (B087) are coalesced: only the latest payload is held and
    polled by each SSE loop — they never enqueue into event queues (would
    starve real events at ~60 fps).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self.serve_seq = 0
        self._spectrum: dict[str, Any] | None = None
        self._spectrum_seq = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, envelope: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(envelope)
            except queue.Full:
                # slow consumer: drop for them, never block the pump
                pass

    def set_spectrum(self, payload: dict[str, Any]) -> None:
        """Overwrite the latest live spectrum frame (coalesced)."""
        with self._lock:
            self._spectrum = payload
            self._spectrum_seq += 1

    def get_spectrum(self) -> tuple[int, dict[str, Any] | None]:
        with self._lock:
            return self._spectrum_seq, self._spectrum


class TailPump(threading.Thread):
    """Follows the state JSONLs and publishes envelopes to the hub."""

    def __init__(self, hub: Hub, state: Path, *, poll_s: float = 0.1) -> None:
        super().__init__(name="hark-serve-tail", daemon=True)
        self.hub = hub
        self.state = state
        self.poll_s = poll_s
        self.tailer = MultiTailer(state)
        # position at end synchronously, before the HTTP server accepts
        # requests — otherwise a record written right after connect can be
        # skipped as history (observed as a flaky stream test)
        self.tailer.start_live()
        self._stop = threading.Event()

    def composite_cursor(self) -> str:
        cur = self.tailer.composite_cursor()
        return f"{cur},serve:{self.hub.serve_seq}" if cur else f"serve:{self.hub.serve_seq}"

    def publish_serve(self, payload: dict[str, Any]) -> None:
        self.hub.serve_seq += 1
        self.hub.publish(
            {
                "schema": SCHEMA,
                "type": "event",
                "source": "serve",
                "cursor": self.composite_cursor(),
                "payload": payload,
            }
        )

    def run(self) -> None:
        while not self._stop.is_set():
            progressed = False
            for rec in self.tailer.poll():
                progressed = True
                self.hub.publish(
                    {
                        "schema": SCHEMA,
                        "type": "event",
                        "source": rec.source,
                        "cursor": self.composite_cursor(),
                        "payload": rec.payload,
                    }
                )
            if not progressed:
                self._stop.wait(self.poll_s)
        self.tailer.close()

    def stop(self) -> None:
        self._stop.set()


class SpectrumPump(threading.Thread):
    """Poll shared ``spectrum.latest`` written by capture processes (B087).

    In-process publishers (host dictation inside serve) also feed the hub via
    ``set_local_publisher``; this pump covers ambient / CLI listen in other
    processes. Latest-frame only — never appends to JSONL.
    """

    def __init__(self, hub: Hub, state: Path, *, poll_s: float = 0.016) -> None:
        super().__init__(name="hark-serve-spectrum", daemon=True)
        self.hub = hub
        self.state = state
        self.poll_s = poll_s
        self._stop = threading.Event()
        self._last_ts: float | None = None
        self._last_recording: bool | None = None

    def run(self) -> None:
        from hark.audio.spectrum import read_latest_spectrum

        while not self._stop.is_set():
            try:
                frame = read_latest_spectrum(self.state)
            except Exception:
                frame = None
            if frame is not None:
                ts = frame.get("ts")
                rec = bool(frame.get("recording"))
                # Only push when frame changes (ts) or recording edge flips
                if ts != self._last_ts or rec != self._last_recording:
                    self._last_ts = float(ts) if isinstance(ts, (int, float)) else None
                    self._last_recording = rec
                    self.hub.set_spectrum(frame)
            self._stop.wait(self.poll_s)

    def stop(self) -> None:
        self._stop.set()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, cfg: HarkConfig, host: str, port: int) -> None:
        self.cfg = cfg
        self.token = cfg.dashboard.token
        self.is_localhost = host in LOCALHOST_HOSTS
        if not self.is_localhost and not self.token:
            raise ValueError(
                "refusing non-localhost bind without [dashboard].token "
                "(see docs/DASHBOARD.md)"
            )
        if cfg.dashboard.require_token and not self.token:
            raise ValueError("[dashboard].require_token is set but no token configured")
        self.auth_required = bool(cfg.dashboard.require_token or not self.is_localhost)
        self.sessions: set[str] = set()
        self.hub = Hub()
        self.state = state_dir()
        self.pump = TailPump(self.hub, self.state)
        self.spectrum_pump = SpectrumPump(self.hub, self.state)
        from hark.dashboard.dictation import HostDictation

        self.host_dictation = HostDictation()
        self.started_at = utc_now_iso()
        self.static_root = resolve_static_root()
        # In-process spectrum from host dictation / same-process capture (B087)
        from hark.audio.spectrum import set_local_publisher

        set_local_publisher(self.hub.set_spectrum)
        super().__init__((host, port), DashboardHandler)

    def serve_forever(self, poll_interval: float = 0.5) -> None:  # type: ignore[override]
        self.pump.start()
        self.spectrum_pump.start()
        log(
            "serve.started",
            component="dashboard",
            bind=f"{self.server_address[0]}:{self.server_address[1]}",
            auth_required=self.auth_required,
        )
        try:
            super().serve_forever(poll_interval)
        finally:
            self.pump.stop()
            self.spectrum_pump.stop()
            try:
                from hark.audio.spectrum import set_local_publisher

                set_local_publisher(None)
            except Exception:
                pass

    def server_close(self) -> None:  # type: ignore[override]
        try:
            self.spectrum_pump.stop()
        except Exception:
            pass
        try:
            from hark.audio.spectrum import set_local_publisher

            set_local_publisher(None)
        except Exception:
            pass
        super().server_close()

    def server_meta(self) -> dict[str, Any]:
        import shutil

        return {
            "name": SERVER_NAME,
            "version": __version__,
            "started_at": self.started_at,
            "bind": f"{self.server_address[0]}:{self.server_address[1]}",
            "auth_required": self.auth_required,
            "tls_terminated": self.cfg.dashboard.tls_terminated,
            "ffmpeg": shutil.which("ffmpeg") is not None,
        }


def resolve_static_root() -> Path | None:
    packaged = Path(__file__).parent / "webui_dist"
    if (packaged / "index.html").is_file():
        return packaged
    repo = Path(__file__).resolve().parents[3] / "webui" / "dist"
    if (repo / "index.html").is_file():
        return repo
    return None


PLACEHOLDER_HTML = """<!doctype html>
<meta charset=\"utf-8\"><title>hark dashboard</title>
<body style=\"font-family:system-ui;background:#0b0f19;color:#f3f4f6;
display:grid;place-items:center;height:100vh;margin:0;padding:1.5rem\">
<div style=\"text-align:center;max-width:36rem\">
<h1>hark webui is running</h1>
<p>webui bundle not found (or was missing when this process started).</p>
<ol style=\"text-align:left;line-height:1.5\">
<li>From the repo: <code>./scripts/build-webui.sh</code>
  (or <code>cd webui &amp;&amp; npm install &amp;&amp; npm run build</code>)</li>
<li><strong>Restart</strong> the server: stop it, then
  <code>hark webui</code> again — a plain browser refresh is not enough if
  the process started before the build.</li>
</ol>
<p>See <code>docs/DASHBOARD.md</code>. API:
<a style=\"color:#a5b4fc\" href=\"/api/v1/health\">/api/v1/health</a></p>
</div>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: DashboardServer

    # ------------------------------------------------------------------ util

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # quiet; structured logging via syslog where it matters

    def _send_json(self, status: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, status: int, code: str, message: str = "") -> None:
        self._send_json(status, {"ok": False, "error": {"code": code, "message": message}})

    def _read_body(self, limit: int = MAX_JSON_BODY) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            self._err(HTTPStatus.BAD_REQUEST, "bad_request", "missing or oversized body")
            return None
        return self.rfile.read(length)

    def _read_json(self) -> dict[str, Any] | None:
        raw = self._read_body()
        if raw is None:
            return None
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            self._err(HTTPStatus.BAD_REQUEST, "bad_request", "invalid JSON")
            return None
        if not isinstance(obj, dict):
            self._err(HTTPStatus.BAD_REQUEST, "bad_request", "expected object")
            return None
        return obj

    # ------------------------------------------------------------------ auth

    def _authed(self) -> bool:
        if not self.server.auth_required:
            return True
        token = self.server.token or ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], token):
            return True
        cookies = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookies.get(COOKIE_NAME)
        return bool(morsel and morsel.value in self.server.sessions)

    def _handle_auth(self) -> None:
        body = self._read_json()
        if body is None:
            return
        token = str(body.get("token") or "")
        if not (self.server.token and hmac.compare_digest(token, self.server.token)):
            self._err(HTTPStatus.UNAUTHORIZED, "unauthorized", "bad token")
            return
        session = secrets.token_urlsafe(32)
        self.server.sessions.add(session)
        payload = json.dumps({"ok": True}).encode()
        self.send_response(HTTPStatus.OK)
        cookie = f"{COOKIE_NAME}={session}; HttpOnly; SameSite=Strict; Path=/"
        if self.server.cfg.dashboard.tls_terminated:
            cookie += "; Secure"
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    # ---------------------------------------------------------------- routes

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        if not path.startswith("/api/"):
            self._serve_static(path)
            return
        if not self._authed():
            self._err(HTTPStatus.UNAUTHORIZED, "unauthorized", "authentication required")
            return
        qs = parse_qs(url.query)
        try:
            if path == "/api/v1/health":
                self._send_json(
                    200, api.health_snapshot(self.server.cfg, self.server.server_meta())
                )
            elif path == "/api/v1/config":
                self._send_json(200, api.config_snapshot(self.server.cfg))
            elif path == "/api/v1/events":
                self._handle_events(qs)
            elif path == "/api/v1/stream":
                self._handle_stream(qs)
            elif path == "/api/v1/herdr/sessions":
                self._send_json(200, api.herdr_sessions_snapshot(self.server.cfg))
            elif path.startswith("/api/v1/herdr/context/"):
                self._handle_context(path, qs)
            elif path == "/api/v1/deliveries":
                self._send_json(200, api.deliveries_snapshot())
            elif path == "/api/v1/usage":
                self._send_json(200, api.usage_snapshot())
            else:
                self._err(HTTPStatus.NOT_FOUND, "not_found", path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # defensive: one bad request must not kill the thread
            try:
                self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", str(exc))
            except Exception:
                pass

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/v1/auth":
            self._handle_auth()
            return
        if not self._authed():
            self._err(HTTPStatus.UNAUTHORIZED, "unauthorized", "authentication required")
            return
        try:
            if path == "/api/v1/answer":
                body = self._read_json()
                if body is not None:
                    status, payload = api.answer_action(self.server.cfg, body)
                    self._send_json(status, payload)
            elif path == "/api/v1/prompt":
                body = self._read_json()
                if body is not None:
                    status, payload = api.prompt_action(body, state=self.server.state)
                    self._send_json(status, payload)
            elif path.startswith("/api/v1/dictation/"):
                self._handle_dictation(path)
            else:
                self._err(HTTPStatus.NOT_FOUND, "not_found", path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._err(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", str(exc))
            except Exception:
                pass

    def _handle_dictation(self, path: str) -> None:
        from hark.dashboard import dictation

        action = path[len("/api/v1/dictation/") :]
        if action == "transcribe":
            raw = self._read_body(limit=MAX_AUDIO_BODY)
            if raw is None:
                return
            status, payload = dictation.transcribe_blob(
                self.server.cfg, raw, self.headers.get("Content-Type") or ""
            )
            self._send_json(status, payload)
        elif action == "start":
            body = self._read_json()
            if body is None:
                return
            if body.get("mode") != "host":
                self._err(HTTPStatus.BAD_REQUEST, "bad_request", "mode must be 'host'")
                return
            status, payload = self.server.host_dictation.start(
                self.server.cfg, self.server.pump.publish_serve
            )
            self._send_json(status, payload)
        elif action in ("stop", "cancel"):
            status, payload = self.server.host_dictation.control(action)
            self._send_json(status, payload)
        else:
            self._err(HTTPStatus.NOT_FOUND, "not_found", path)

    # ---------------------------------------------------------------- events

    def _handle_events(self, qs: dict[str, list[str]]) -> None:
        since = (qs.get("since") or [None])[0]
        sources_raw = (qs.get("sources") or [None])[0]
        sources = set(sources_raw.split(",")) if sources_raw else None
        limit = min(int((qs.get("limit") or ["500"])[0]), 2000)
        records, cursor, complete = read_page(
            self.server.state,
            since=since,
            sources=sources,
            limit=limit,
            history_limit=self.server.cfg.dashboard.history_limit,
        )
        events = [
            {
                "schema": SCHEMA,
                "type": "event",
                "source": r.source,
                "cursor": cursor,
                "payload": r.payload,
            }
            for r in records
        ]
        self._send_json(
            200,
            {"schema": SCHEMA, "ok": True, "events": events, "cursor": cursor, "complete": complete},
        )

    # ---------------------------------------------------------------- stream

    def _sse_write(self, envelope: dict[str, Any]) -> None:
        data = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
        self.wfile.write(f"id: {envelope['cursor']}\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _handle_stream(self, qs: dict[str, list[str]]) -> None:
        sources_raw = (qs.get("sources") or [None])[0]
        wanted = set(sources_raw.split(",")) if sources_raw else None
        since = self.headers.get("Last-Event-ID") or (qs.get("since") or [None])[0]

        # body ends when the connection does (SSE); no keep-alive reuse
        self.close_connection = True
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        pump = self.server.pump
        q = self.server.hub.subscribe()
        try:
            self._sse_write(
                {
                    "schema": SCHEMA,
                    "type": "hello",
                    "source": "serve",
                    "cursor": pump.composite_cursor(),
                    "payload": {
                        "kind": "serve.hello",
                        "server": SERVER_NAME,
                        "version": __version__,
                        "sources": SOURCES,
                    },
                }
            )
            if since:
                records, cursor, _ = read_page(
                    self.server.state, since=since, sources=wanted, limit=1000
                )
                for r in records:
                    self._sse_write(
                        {
                            "schema": SCHEMA,
                            "type": "event",
                            "source": r.source,
                            "cursor": cursor,
                            "payload": r.payload,
                        }
                    )
            last_ping = time.monotonic()
            last_spec_seq = 0
            # Short timeout so spectrum can refresh near 60 fps without a
            # dedicated connection (coalesced latest frame only).
            while True:
                try:
                    envelope = q.get(timeout=0.016)
                except queue.Empty:
                    envelope = None
                if envelope is not None:
                    if wanted is None or envelope["source"] in wanted or envelope["type"] == "hello":
                        self._sse_write(envelope)
                    last_ping = time.monotonic()
                # Live mic spectrum (B087): not stored in history; cursor unchanged
                allow_spec = wanted is None or "serve" in wanted
                if allow_spec:
                    seq, spec = self.server.hub.get_spectrum()
                    if seq != last_spec_seq and spec is not None:
                        last_spec_seq = seq
                        self._sse_write(
                            {
                                "schema": SCHEMA,
                                "type": "event",
                                "source": "serve",
                                "cursor": pump.composite_cursor(),
                                "payload": spec,
                            }
                        )
                        last_ping = time.monotonic()
                if envelope is None and time.monotonic() - last_ping >= 15.0:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.hub.unsubscribe(q)

    # --------------------------------------------------------------- context

    def _handle_context(self, path: str, qs: dict[str, list[str]]) -> None:
        rest = path[len("/api/v1/herdr/context/") :]
        parts = [unquote(p) for p in rest.split("/") if p]
        if len(parts) != 2:
            self._err(HTTPStatus.BAD_REQUEST, "bad_request", "context/<session>/<pane>")
            return
        lines = min(int((qs.get("lines") or ["60"])[0]), 500)
        self._send_json(
            200,
            api.context_snapshot(self.server.cfg, parts[0], parts[1], lines=lines),
        )

    # ---------------------------------------------------------------- static

    def _serve_static(self, path: str) -> None:
        # Re-resolve so a build after `hark webui` started is picked up without
        # requiring an immediate restart (still recommend restart for cleanliness).
        root = resolve_static_root()
        if root is not None:
            self.server.static_root = root
        else:
            root = self.server.static_root
        if root is None:
            body = PLACEHOLDER_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            root = root.resolve()
            rel = unquote(path.lstrip("/")) or "index.html"
            logical_target = root / rel
            target = logical_target.resolve()
        except (OSError, RuntimeError, ValueError):
            self._err(HTTPStatus.NOT_FOUND, "not_found", path)
            return

        # Resolve before checking containment so symlinks cannot escape the
        # static root.  Do not inspect an escaped target: even is_file() would
        # follow it and disclose filesystem state outside the web bundle.
        if not target.is_relative_to(root):
            self._err(HTTPStatus.NOT_FOUND, "not_found", path)
            return

        if not target.is_file():
            # SPA fallback is allowed only to a regular file that also remains
            # inside the resolved root (index.html itself may be a symlink).
            logical_target = root / "index.html"
            try:
                fallback = logical_target.resolve()
            except (OSError, RuntimeError, ValueError):
                self._err(HTTPStatus.NOT_FOUND, "not_found", path)
                return
            if not fallback.is_relative_to(root) or not fallback.is_file():
                self._err(HTTPStatus.NOT_FOUND, "not_found", path)
                return
            target = fallback
        ctype = mimetypes.guess_type(str(logical_target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if (
            logical_target.name == "index.html"
            or logical_target.suffix == ".webmanifest"
        ):
            self.send_header("Cache-Control", "no-cache")
        elif logical_target.name == "sw.js":
            # Service worker must revalidate so a post-build update is not stuck
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)


def build_server(cfg: HarkConfig, *, host: str | None = None, port: int | None = None) -> DashboardServer:
    return DashboardServer(
        cfg,
        host or cfg.dashboard.host,
        cfg.dashboard.port if port is None else port,
    )


def run_serve(cfg: HarkConfig, *, host: str | None = None, port: int | None = None) -> int:
    try:
        server = build_server(cfg, host=host, port=port)
    except ValueError as exc:
        from hark.config import eprint

        eprint(f"hark serve: {exc}")
        return 2
    bind = f"{server.server_address[0]}:{server.server_address[1]}"
    print(f"hark serve: http://{bind}/  (auth {'on' if server.auth_required else 'off'})")
    try:
        from hark.update_check import maybe_print_update_notice

        maybe_print_update_notice(
            enabled=bool(getattr(cfg.update, "enabled", True)),
            repo=getattr(cfg.update, "repo", None),
        )
    except Exception:  # pragma: no cover — never block serve on update check
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.pump.stop()
        server.server_close()
    return 0

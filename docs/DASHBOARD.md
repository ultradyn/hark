# Hark Dashboard Protocol v1 (`hark.dashboard.v1`)

Normative contract between a hark backend and the dashboard webui.
The Python `hark webui` (aliases: `dashboard`, `serve`) implements it today; the Rust port MUST implement the
same endpoints, pass the same fixtures (`fixtures/dashboard/`), and serve the
same webui bundle unmodified. Design rationale: [plans/I003-dashboard.md](plans/I003-dashboard.md).

Schemas: `schemas/dashboard-v1/` · Fixtures: `fixtures/dashboard/` ·
Related: [PROTOCOL.md](PROTOCOL.md) (HEP) · [SAFETY.md](SAFETY.md)

## Quickstart

```bash
hark webui                     # http://127.0.0.1:4136 — no auth on localhost
hark webui --port 5000
hark doctor                    # includes a `dashboard:` posture line
```

Everything workers write (watch, ambient, syslog, usage, deliveries) streams
live; arm workers with `./scripts/run-mode-a.sh` and the feed lights up.
Dictate (◉) captures via the browser mic (needs HTTPS or localhost; server
needs `ffmpeg`) or the host mic (existing listen flow), then submits only
after review — as a bound answer to a pending event or as an operator prompt.

**Phone / remote (tailnet):** set a token, terminate TLS, then open the
`https://…ts.net` URL:

```bash
hark serve --print-token       # → [dashboard].token in config.toml
tailscale serve 4136           # TLS on your *.ts.net name
# config.toml: [dashboard] host = "127.0.0.1"; tls_terminated = true
```

PWA install, notifications, and phone-mic capture are secure-context APIs —
they do not work over plain `http://100.x.y.z` (see Security below).

Webui development: `cd webui && npm run dev` (proxies `/api` to a local
`hark serve`) or `npm run dev:fixtures` (replays the golden contract fixtures,
no backend). Ship it with `scripts/build-webui.sh` (staged into the wheel). **After building, restart `hark webui`** — a browser refresh alone does not reload a process that started when the bundle was missing (fixed to re-scan, but restart is still the reliable path).

## Transport

- **REST + SSE** over HTTP/1.1. All payloads are JSON (UTF-8).
- The live stream is a single **SSE** endpoint (`GET /api/v1/stream`). Messages
  are transport-agnostic JSON objects — a future WebSocket endpoint (Rust port)
  MUST carry byte-identical message objects, one per text frame.
- Clients open **one stream per tab** (browsers cap HTTP/1.1 connections/host
  at ~6); panels share it.
- SSE keepalive: servers SHOULD emit a comment line (`: ping`) at least every
  30 s of silence.

## Stream envelope

Every SSE `data:` line is one envelope (`stream.schema.json`):

```json
{
  "schema": "hark.dashboard.v1",
  "type": "event",
  "source": "watch",
  "cursor": "watch:184,ambient:42,system:9051,usage:77,delivery:12",
  "payload": { "schema": "hark.event.v1", "kind": "agent.blocked", "…": "…" }
}
```

- `type`: `hello` (first message on connect) | `event`.
- `source`: `watch` | `ambient` | `system` | `usage` | `delivery` | `serve`.
- `payload` is **source-shaped** (discriminated union, see below). Consumers
  MUST ignore unknown fields and unknown `source`/`kind` values.
- `cursor` is the **composite cursor**: the full per-source position *after*
  this event, `source:seq` pairs joined by commas. It is also set as the SSE
  `id:` field, so `Last-Event-ID` on reconnect restores every source, not just
  the one that happened to emit last.

### Cursor semantics

- `seq` is the 1-based record index within the current incarnation of the
  backing source (line number for JSONL-backed sources). Monotonic per source
  while the backing file is not rotated.
- Cursors are **opaque to clients** beyond equality/passthrough. Clients MUST
  NOT construct cursors except from `hello`, event envelopes, or page results.
- If a server cannot honor a cursor (rotation, restart, unknown), it MUST fall
  back to a recent tail (its default backfill window) rather than erroring.
  Dashboards are monitoring UIs; a gap beats a dead stream.

### `hello`

First message on every stream connect:

```json
{
  "schema": "hark.dashboard.v1",
  "type": "hello",
  "source": "serve",
  "cursor": "watch:184,ambient:42,system:9051,usage:77,delivery:12",
  "payload": {
    "kind": "serve.hello",
    "server": "hark-serve-py",
    "version": "0.1.6",
    "sources": ["watch", "ambient", "system", "usage", "delivery", "serve"]
  }
}
```

### Payloads by source

| source | payload shape | backing |
|--------|---------------|---------|
| `watch`, `ambient` | HEP `hark.event.v1` object, passed through unchanged | `watch.jsonl`, `ambient.jsonl` |
| `system` | LogEvent: `ts` (float s), `seq`, `level` (`debug\|info\|warn\|error`), `component`, `event`, `message`, `data{}`, `pid` | `system.jsonl` |
| `usage` | UsageEvent: `kind` (`tts\|stt`), `ts`, `provider?`, `voice?`, `ok`, `chars`, `words`, `audio_ms`, `latency_ms`, `error?`, `meta{}` | `usage.jsonl` |
| `delivery` | `{type:"bound", …BoundEvent}` or `{type:"outcome", event_id, status, ts, …}` | `events.jsonl`, `deliveries.jsonl` |
| `serve` | `{kind:"serve.*", …}` server meta: hello, dictation state, live spectrum, degradations | in-process |

Note: HEP payloads are validated structurally (envelope + required HEP core
fields), **not** against `event-v1.schema.json`'s closed kind enum —
`ambient.*`/`announce.*` kinds are intentionally absent there. Unknown kinds
MUST flow through.

## Authentication

Two modes, decided by bind address + config:

- **Localhost bind (default `127.0.0.1`)**: no auth required (configurable
  `require_token = true` to force it).
- **Non-localhost bind**: server MUST refuse to start without a configured
  token, and every `/api/*` request except `POST /api/v1/auth` MUST be
  authenticated.

Flow (`EventSource` cannot set headers, so header-only auth is out):

1. `POST /api/v1/auth` `{"token": "…"}` → `200 {"ok": true}` +
   `Set-Cookie: hark_dash=<session>; HttpOnly; SameSite=Strict; Path=/`
   (+ `Secure` when serving TLS or behind `tls_terminated = true`).
2. All subsequent requests (including the SSE stream) authenticate via the
   cookie. `Authorization: Bearer <token>` is ALSO accepted everywhere (for
   non-browser clients).
3. Unauthenticated → `401 {"ok": false, "error": {"code": "unauthorized"}}`.

Tokens never appear in URLs. Remote (e.g. tailnet) use additionally requires
TLS for browser secure-context APIs — see [DASHBOARD_SECURITY](#security).

## Endpoints

All responses are JSON. Errors use
`{"ok": false, "error": {"code": "<slug>", "message": "…"}}` with an
appropriate HTTP status (`error.schema` in `actions.schema.json`).

| Method + path | Purpose | Schema |
|---|---|---|
| `POST /api/v1/auth` | token → session cookie | `actions.schema.json#authRequest/authResponse` |
| `GET  /api/v1/health` | server + doctor summary | `health.schema.json` |
| `GET  /api/v1/config` | redacted config snapshot | `config.schema.json` |
| `GET  /api/v1/events?since=<cursor>&sources=a,b&limit=N` | backfill page | `events-page.schema.json` |
| `GET  /api/v1/stream?sources=a,b&replay=N` | SSE live stream | `stream.schema.json` per `data:` line |
| `GET  /api/v1/herdr/sessions` | sessions/panes/status map | `herdr-sessions.schema.json` |
| `GET  /api/v1/herdr/context/<session>/<pane>?lines=N` | recent pane context | `context.schema.json` |
| `GET  /api/v1/deliveries` | pending queue + recent outcomes | `deliveries.schema.json` |
| `GET  /api/v1/usage` | usage summary + near-miss groups | `usage.schema.json` |
| `POST /api/v1/answer` | bound answer to a HEP event | `actions.schema.json#answerRequest/answerResponse` |
| `POST /api/v1/prompt` | inject ambient operator prompt | `actions.schema.json#promptRequest/promptResponse` |
| `POST /api/v1/dictation/transcribe` | one-shot browser-audio STT | `actions.schema.json#transcribeResponse` |
| `POST /api/v1/dictation/start\|stop\|cancel` | host-mic capture control | `actions.schema.json#dictation*` |
| `GET  /` + static assets | the webui bundle (same-origin) | — |

### `GET /api/v1/events`

- `since`: composite (or single-source) cursor; omitted → recent tail.
- `sources`: comma filter (default: all).
- `limit`: max events (server clamps; default 500).
- Response: `{ok, events: [envelope…], cursor: "<composite after last>",
  complete: <bool — false if more available before now>}`.

### `POST /api/v1/answer` — safe delivery (normative)

The dashboard is a **delivery owner UI**, so the HEP safety invariants apply
verbatim ([ARCHITECTURE.md](ARCHITECTURE.md)): the browser MUST NOT invent
target IDs — `event_id` MUST come from a received event, and the server binds
delivery to that event's recorded target.

```json
{ "event_id": "01J…", "text": "No, keep the build directory.", "keys": null }
```

- Exactly one of `text` | `keys` (list of key names, e.g. `["2","enter"]`).
- Server resolves the event via the DeliveryStore, **registering on demand**
  from the tailed HEP record when not already registered.
- Server MUST re-validate pane revision + question fingerprint before sending
  (same checks as `hark answer`) and MUST record the outcome idempotently.
- Response `status`: `delivered` | `rejected` (stale/unknown/policy) |
  `uncertain` (write may have landed). Rejections include `error.code`
  `stale_target` | `unknown_event` | `already_delivered` | `bad_request`.

### `POST /api/v1/prompt`

`{"text": "…", "session_id": null}` → appends a final `ambient.prompt` HEP
event to the ambient feed (same shape as a voice wake), so the orchestrator
orchestrator picks it up with its normal judgment. Response includes the new
`event_id`. This is the unbound path; routing stays with the orchestrator.

### Dictation

- **Browser capture** (stateless): record locally (MediaRecorder), then
  `POST /api/v1/dictation/transcribe` with the audio as the request body
  (`Content-Type: audio/webm`, `audio/mp4`, `audio/ogg`, or `audio/wav`).
  Server transcodes to WAV (ffmpeg) if needed, runs the configured STT
  provider, returns `{ok, text, provider, latency_ms}`. `501
  {"error":{"code":"transcode_unavailable"}}` when ffmpeg is missing and the
  body is not WAV.
- **Host capture**: `POST /api/v1/dictation/start {"mode":"host"}` drives the
  local `hark listen` flow (mic lease + ambient pause). Progress and the final
  transcript arrive on the stream as `serve.dictation` payloads
  (`state: recording|transcribing|done|failed|cancelled`, `text?`).
  `stop`/`cancel` control the capture. `409 {"error":{"code":"mic_busy"}}`
  when the mic lease is held.
- Submission of a transcript is a separate, explicit `/answer` or `/prompt`
  call after operator review. Dictation endpoints never deliver.

### Live voice spectrum (B087)

While the host mic is capturing (listen / ask / ambient / host dictation), the
capture path computes short-window FFT band magnitudes and publishes the
**latest frame only** to `spectrum.latest` under the state dir (no JSONL
history, no disk growth). `hark serve` coalesces that frame onto the existing
SSE stream as:

```json
{
  "schema": "hark.dashboard.v1",
  "type": "event",
  "source": "serve",
  "cursor": "<unchanged composite>",
  "payload": {
    "kind": "serve.spectrum",
    "bands": [0.0, 0.12, 0.4, "…"],
    "ts": 1710000000.123,
    "recording": true,
    "sample_rate": 16000,
    "max_hz": 6000,
    "source": "listen"
  }
}
```

- `recording: true` during STT-bound capture (listen / answer); ambient idle
  feed uses `recording: false` so the panel can stay live without looking hot.
- Spectrum frames **do not** advance the composite cursor and **must not** be
  appended to the events timeline (webui treats them as a dedicated signal).
- Target cadence is ~60 fps on the SSE loop (latest-frame coalesce; slow
  clients drop intermediate frames).
- Webui: collapsible spectrum strip under the topbar; auto-expands while
  `recording` unless the operator collapsed it during a recording period
  (preference in `localStorage`).

## Config (`[dashboard]` in `config.toml`)

```toml
[dashboard]
host = "127.0.0.1"     # non-localhost requires token
port = 4136
token = ""              # generate: hark serve --print-token
require_token = false   # force auth even on localhost
tls_terminated = false  # set true behind tailscale serve / reverse proxy (Secure cookies)
history_limit = 2000    # default backfill window per source
```

## Redaction (normative)

Responses MUST Not contain provider credentials or other secret material:

- No API keys, OAuth tokens, session tokens, or password-like values in any
  response body. Provider auth stays summarized as availability booleans +
  source labels (as `hark doctor` already reports).
- The dashboard token itself never appears in any response (only in the
  client→server direction of `/auth`).
- `fixtures/dashboard/config.json` is the golden redacted snapshot;
  `tests/test_dashboard_contract.py` walks every fixture and rejects
  secret-shaped strings (contract regression gate).

## Security

- Default bind `127.0.0.1`. Never expose to the public internet.
- Remote use is tailnet-scoped and **requires TLS** for browser secure-context
  APIs (PWA install, notifications, `getUserMedia`): use `tailscale serve`
  (recommended; terminates TLS on `*.ts.net`) or `tailscale cert` + a TLS
  terminator, with `tls_terminated = true`.
- No CORS headers: the webui is same-origin by construction. Non-browser
  clients use bearer auth.
- `hark doctor` flags: non-localhost bind without token (error);
  remote-looking bind without `tls_terminated` (warning).

## Versioning

- This document + `schemas/dashboard-v1/` are the v1 contract. Additive
  changes (new fields, new sources, new `serve.*` kinds, new endpoints) are
  allowed within v1; consumers MUST ignore what they don't know.
- Breaking changes require `hark.dashboard.v2` under a new schema dir and
  `/api/v2/` prefix.

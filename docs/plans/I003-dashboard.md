# I003 — Live web dashboard: feasibility + design

Status: evaluated 2026-07-13 · verdict: **feasible, ~27 h across 8 tasks** (B060–B067)
Related: [PROTOCOL.md](../PROTOCOL.md) · [ARCHITECTURE.md](../ARCHITECTURE.md) · [HARKD.md](../HARKD.md) · B002 (Rust parity)

## Feasibility verdict

**High.** Nearly all data the dashboard needs already exists in stable, consumable form:

| Dashboard surface (from I003) | Existing source |
|---|---|
| HEP / syslog event stream | `hark monitor` unified feed; `watch.jsonl`, `ambient.jsonl`, `system.jsonl`, `usage.jsonl` under `state_dir()` |
| Herdr state (sessions/panes/status) | `hark.herdr` client (socket/tunnel), `watch.jsonl` history, `targets.py` |
| Chat / pane context | `hark context` path (Herdr pane capture), question/fingerprint fields on HEP events |
| Voice pipeline (mute ring, mic lease, conference) | `mic.lock`, `ambient.pause`, `busy.lock`, conference/media events in `system.jsonl` |
| Queue / delivery | `events.jsonl` + `deliveries.jsonl` (`DeliveryStore`) |
| Config snapshot | `config.py` (redaction required), `doctor.py` |
| Usage / metrics | `usage.jsonl`, wake near-miss groups (B019/B032) |

The genuinely new work is: a versioned dashboard API contract, a thin serving layer,
the webui itself, and the dictation round-trip. No new event plumbing is needed.

## Architecture: contract-first, three pieces

```text
┌────────────────────────────┐      hark.dashboard.v1       ┌──────────────────┐
│ Backend (impl of contract) │  REST snapshots + SSE stream │  Static webui    │
│  v1: Python `hark serve`   │◄────────────────────────────►│  (Vite + TS,     │
│  later: Rust port, same    │      (WS reserved, same      │   no SSR, no     │
│  contract, same webui      │       message schema)        │   backend deps)  │
└─────────────┬──────────────┘                              └──────────────────┘
              │ reads/wraps existing library surfaces only
              ▼
  state JSONLs · herdr client · DeliveryStore · config/doctor · STT providers
```

**Forward compatibility with the Rust port is achieved by the contract, not the
implementation.** The deliverable boundary is `hark.dashboard.v1`:

1. **Contract** — `docs/DASHBOARD.md` + `schemas/dashboard-v1/` JSON Schemas +
   `fixtures/dashboard/` request/response/stream fixtures. The stream is a
   **discriminated envelope** `{source, cursor, payload}`: HEP sources
   (`watch`, `ambient`) carry `hark.event.v1` payloads through unchanged, but
   `system.jsonl` (LogEvent: ts/seq/level/component/…), `usage.jsonl`
   (UsageEvent), and `events/deliveries.jsonl` (DeliveryStore records) are
   **not HEP** and get their own normalized payload schemas. Note also that
   `ambient.*`/`announce.*` kinds are absent from `event-v1.schema.json`'s
   closed kind enum, so the stream schema is defined in `dashboard-v1`, not by
   reusing `event-v1` validation wholesale. Resume uses a **synthetic
   per-source cursor** (`source:seq@incarnation~checkpoint`, with legacy
   `source:seq` accepted; `system.jsonl` etc. have no `event_id`) carried as the
   SSE event id. Snapshot endpoints get their own
   schemas. Fixture-driven, exactly like the B002 Rust parity strategy —
   the Rust `hark serve` passes the same fixtures and the webui runs unmodified.
2. **Backend** — Python v1 is deliberately thin: tail + backfill the JSONL
   files, call existing library functions, serialize per contract. Anything
   clever lives in the already-shared library, so the Rust port re-implements
   serialization, not logic. Two known hardening points: the JSONL tailer must
   buffer partial trailing lines and detect inode rotation (the
   `monitor_feed.follow_state_files` pattern handles truncation only); and
   `/answer` must **register-on-demand** from the tailed HEP event (target +
   fingerprint are in `watch.jsonl`) since `DeliveryStore.get()` only knows
   events registered by a live `hark watch --register-events`.
3. **Frontend** — a standalone static bundle (`webui/`, Vite + TypeScript). It
   only speaks the contract; it is served as static files by whichever backend
   is running (embedded in the Python package now, `rust-embed` later) and can
   be dev-served against fixtures with no backend at all.

### Transport decision: REST + SSE now, WS reserved

- **SSE** for server→browser streaming: implementable with Python stdlib
  (`ThreadingHTTPServer` — plain `HTTPServer` would let one long-lived stream
  block all REST calls; zero new hard deps, matching the lean
  `pyproject.toml`) and trivially in axum later. Auto-reconnect is free:
  `Last-Event-ID` carries the synthetic per-source cursor for backfill.
  **One multiplexed `/stream` per tab** (subscriptions filtered client-side or
  via query params) — HTTP/1.1 browsers cap ~6 connections per host, so panels
  must never open their own SSE connections.
- **REST** for browser→server actions (dictation control, audio upload,
  answer submit) and snapshots.
- **SSE auth**: `EventSource` cannot set an `Authorization` header. Non-local
  auth is a one-time token exchange — `POST /api/v1/auth` with the bearer
  token sets an `HttpOnly` `SameSite=Strict` session cookie; the stream and
  all endpoints then authenticate via cookie, same-origin. (Avoids tokens in
  query strings / server logs; works identically in axum.) Contract task
  (B060) specifies this.
- **WebSocket is reserved, not required**: the contract specifies transport-
  agnostic JSON messages (one object per SSE `data:` line), so a WS endpoint
  carrying identical messages can be added in the Rust port without any schema
  or webui data-model change. This satisfies "WS and/or API" without forcing a
  Python WS dependency for v1.

### Endpoints (sketch — contract task finalizes)

```text
POST /api/v1/auth                       bearer token -> HttpOnly session cookie
GET  /api/v1/health                     server + doctor summary
GET  /api/v1/config                     redacted config snapshot
GET  /api/v1/events?since=<cursor>      backfill window (paged, per-source cursors)
GET  /api/v1/stream                     SSE: enveloped live events, id = cursor
GET  /api/v1/herdr/sessions             sessions/panes/status map
GET  /api/v1/herdr/context/<sess>/<pane>  recent pane context (like `hark context`)
GET  /api/v1/deliveries                 delivery outcomes / pending
GET  /api/v1/usage                      empty-STT rate, near-miss groups, heat
POST /api/v1/dictation/start|stop|cancel   drive capture (browser or host mic)
POST /api/v1/dictation/audio            upload browser audio (webm/opus)
POST /api/v1/answer                     bound submit {event_id, text|keys}
POST /api/v1/prompt                     ambient prompt injection (Mode A pickup)
```

## Dictation design

Two capture modes, one submit path:

- **Browser mic**: MediaRecorder → webm/opus (Safari: mp4/aac) upload →
  **transcode to WAV via `ffmpeg`** → existing STT provider path (providers
  hardcode `audio/wav`; `Provider.transcribe(wav_bytes)` is WAV-only) →
  transcript → operator reviews → submit. `ffmpeg` becomes a **documented
  optional dependency gating browser dictation only** (doctor check; playback
  already uses it opportunistically) — host-mic mode and the rest of the
  dashboard work without it. Live partials during browser capture are **best
  effort / v2**: radio partials come from the segmented live capture loop in
  `speech.py`, which a single uploaded blob doesn't exercise.
- **Host mic**: backend drives the existing `hark listen` flow (mic lease,
  ambient pause) for operators at the machine; this path gets radio partials
  for free.
- **Secure-context caveat**: `getUserMedia` requires HTTPS or `localhost` —
  browser mic from a phone over the tailnet needs the TLS setup below.

Submission is **only** through existing safe paths:

- `POST /answer` → bound `hark answer` semantics (`hark.command.v1` expectations:
  fingerprint + pane revision checked by `DeliveryStore`). Target IDs come from
  the selected HEP event — the browser never invents them (ARCHITECTURE.md
  invariant holds).
- `POST /prompt` → writes an `ambient.prompt` HEP event so the Mode A
  orchestrator handles judgment/routing, identical to a voice wake.

UI states: `idle → recording → transcribing → review → submitted | failed`,
with live partials in radio style when available.

## Security posture

- Bind `127.0.0.1` by default; `[dashboard]` config for host/port.
- Non-localhost bind **requires** a bearer token (config-generated); intended
  exposure is tailnet, never public internet.
- **Remote access requires TLS**: browsers treat plain `http://100.x.y.z`
  (tailnet IP) as an insecure context, which disables Service Workers / PWA
  install, Web Notifications, and `getUserMedia`. Recommended setup:
  `tailscale serve` (terminates TLS on `*.ts.net` and proxies to the local
  bind) or `tailscale cert` + native TLS. Documented in B066; localhost
  desktop use needs none of this.
- Token is exchanged for an `HttpOnly` `SameSite=Strict` cookie (see SSE auth
  above); the raw token never appears in URLs.
- Config endpoint reuses/extends existing secret redaction; contract task adds
  a redaction test over fixtures.
- No CORS wildcard; webui is same-origin (served by the backend).
- Doctor check for misconfiguration (public bind without token).

## Task breakdown (created in backlog)

| Task | Depends on | Est | Scope |
|---|---|---|---|
| B060 contract | — | 3 h | `docs/DASHBOARD.md`, `schemas/dashboard-v1/`, `fixtures/dashboard/`, redaction rules |
| B061 backend `hark serve` | B060 | 4 h | stdlib HTTP+SSE, JSONL tail/backfill, snapshot endpoints, auth, static serving |
| B062 webui scaffold + event stream | B060 | 4 h | Vite+TS app, fixtures dev mode, live tail w/ filters/search/pause/severity |
| B063 Herdr + context surfaces | B061, B062 | 3 h | session/pane map, chat context, blocked/false-done/conference visibility |
| B064 pipeline/queue/config/usage panels | B061, B062 | 3 h | voice pipeline state, deliveries, config+doctor, usage metrics |
| B065 dictation | B061, B062 | 4 h | both capture modes, STT, review, bound submit + prompt injection |
| B066 security + packaging + docs | B063–B065 | 2 h | package static build, redaction audit, docs/site, doctor check |
| B067 beyond-parity polish | B063–B065 | 4 h | PWA + notifications, command palette, timeline scrubber, heatmaps, saved views, TTS audit trail |

Rust port cost later: reimplement B061 against the same contract/fixtures
(axum: REST + SSE + optional WS) and embed the same webui bundle — no webui or
contract rework.

## Beyond-parity features (operator delight — B067 + sprinkled into B062–B065)

Explicitly in scope (owner approved going big). The product story is
*answering agents while away from the keyboard* — the dashboard should be the
best remote surface for that:

- **Phone-first PWA** (killer feature): installable, responsive, works over
  tailnet from a phone; `agent.blocked` fires a Web Notification; tapping it
  deep-links to the event card. Answer the fleet from the couch. **Requires
  the TLS setup in the security section** (`tailscale serve`) — PWA install,
  notifications, and phone mic are all secure-context APIs.
- **One-tap answers**: menu-choice events render their `choices` as buttons on
  the event card → bound `keys` submit (fingerprint-checked). Most blocks are
  answered without typing or speaking.
- **Command palette (⌘K)**: jump to pane/session, filter by kind, trigger
  dictation, answer — keyboard-first for desk use.
- **Timeline scrubber / replay**: scrub back through the event history,
  replay a session's activity; doubles as a debugging tool for wake/STT tuning.
- **Live audio feedback**: VU meter + waveform while recording; radio-style
  partial transcript ticker.
- **Activity heat + sparklines**: per-pane/agent activity, block latency
  (blocked→answered), empty-STT and near-miss trends from `usage.jsonl`.
- **Spoken-audio audit trail**: list what TTS said and when (from
  `system.jsonl`), replayable from the TTS cache where available.
- **Saved views**: named filter sets (e.g. "blocked only", "voice pipeline"),
  persisted in localStorage — no backend state.
- **Premium dark theme** as the default aesthetic (slate + gradient accents),
  light theme supported.

None of these change the contract shape: notifications, palette, scrubber,
views, and heatmaps are pure webui; one-tap answers and audio audit reuse
endpoints already in the sketch (`/answer`, `/events` backfill).

## Open questions (non-blocking, decided at task time)

- Webui framework: plain TS vs preact/lit — pick smallest thing that keeps the
  bundle self-contained (leaning preact; decision recorded in B062).
- History window: how much JSONL backfill to index in-memory vs on-demand
  paging (B061 decides; contract exposes paging either way).
- `hark serve` vs `hark dash serve` naming (B061; NAMING.md precedent applies).

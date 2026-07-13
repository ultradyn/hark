# Hark Event Protocol (HEP) v1

Normalized events so Mode A, Monitor, and future `harkd` share one shape.  
Herdr wire JSON is **external**; HEP is **stable internal**.

## Envelope

Every stdout line from `hark watch` / event bus is one JSON object.

```json
{
  "schema": "hark.event.v1",
  "event_id": "01J…",
  "observed_at": "2026-07-13T14:00:00.000Z",
  "kind": "agent.blocked",
  "priority": 80,
  "session_id": "work",
  "target": {
    "workspace_id": "w1",
    "tab_id": "w1:t1",
    "pane_id": "w1:p6",
    "terminal_id": "term_…",
    "pane_revision": 42,
    "agent": "claude",
    "agent_session": null,
    "friendly_name": "auth reviewer"
  },
  "state": { "from": "working", "to": "blocked", "blocked_epoch": 3 },
  "question": {
    "kind": "permission",
    "text": "Allow running rm -rf build/?",
    "choices": ["Yes", "No"],
    "fingerprint": "blake3:…",
    "confidence": 0.9,
    "risk": "R2"
  },
  "disposition": "pending"
}
```

Consumers **MUST ignore unknown fields**.

## Kinds

| kind | Meaning |
|------|---------|
| `watch.armed` | Watcher started |
| `watch.heartbeat` | Liveness |
| `watch.error` | Recoverable/fatal watch error |
| `agent.blocked` | Needs human input |
| `agent.question_changed` | Still blocked; ask changed |
| `agent.needs_input` | Status done/idle but pane still shows a menu/ask (false done) — treat like blocked |
| `agent.completed` | Transition to done (judgment required; may pair with needs_input) |
| `agent.state_changed` | Other transitions if `--all-transitions` |
| `target.invalidated` | Pane closed/moved; cancel in-flight |
| `answer.transcribed` | (daemon/library) STT finished |
| `answer.confirmation_required` | R2/R3 or auto-unsure |
| `answer.delivered` | Sent successfully |
| `answer.delivery_uncertain` | Write may have landed; reconcile |
| `answer.rejected` | Stale / policy / user cancel |
| `bridge.degraded` / `bridge.recovered` | Herdr/provider issues |
| `ambient.partial` | **Radio mode only** — interim STT while waiting for end phrase |
| `ambient.prompt` | Final ambient operator prompt (`final=true`) |
| `ambient.cancelled` | Operator cancelled mid-capture |

### Partial streaming (radio end mode)

When `[listen] end_mode = "radio"` and `stream_partials = true`, interim transcripts are emitted as:

```json
{
  "schema": "hark.event.v1",
  "kind": "ambient.partial",
  "partial": true,
  "final": false,
  "stream_id": "s…",
  "seq": 1,
  "text": "please open the pull request for…",
  "warning": "PARTIAL TRANSCRIPT — not complete. … HOLD …",
  "instructions": "HOLD RESPONSE. … You MAY run agent_control.end_recording if they clearly want to finish without an exact end phrase …",
  "agent_control": {
    "end_recording": "hark listen-end --stream-id s…",
    "cancel_recording": "hark listen-end --stream-id s… --cancel",
    "hint": "If the operator clearly wants to finish …"
  }
}
```

Mode A agents may finalize a stuck radio capture with `hark listen-end` (or `--cancel`) when the operator’s wording is an informal stop/send, not an exact product end phrase.

Consumers **MUST**:

1. Treat `partial=true` as **non-authoritative**.  
2. **Not** speak to the operator or deliver to a pane based on partials alone.  
3. **May** begin private thinking/planning.  
4. On `ambient.prompt` / final with the same `stream_id`: use that text; discard prior partials.

## Monitor profile (`hark watch --for-monitor`)

Compact line, no secrets, no full terminal dump:

```json
{
  "schema": "hark.event.v1",
  "kind": "agent.blocked",
  "event_id": "01J…",
  "session_id": "work",
  "agent": "claude",
  "name": "auth reviewer",
  "pane_id": "w1:p6",
  "question": "Allow running rm -rf build/?",
  "risk": "R2",
  "instructions": "Use the hark skill; do not invent an answer. hark context work/w1:p6"
}
```

## Dedupe key

```text
(session_id, pane_id, agent_session?, blocked_epoch, question_fingerprint)
```

Reconnect/replay must not re-speak identical asks.

## Debounce

Status edges: **150–400 ms** coalesce.  
`pane.closed` / disconnect: **not** debounced.

## Bound command (delivery)

```json
{
  "schema": "hark.command.v1",
  "request_id": "…",
  "command": "answer.submit",
  "event_id": "01J…",
  "expected": {
    "session_id": "work",
    "pane_id": "w1:p6",
    "pane_revision": 42,
    "question_fingerprint": "blake3:…"
  },
  "text": "No, keep the build directory.",
  "keys": null
}
```

Or menu delivery: `"keys": ["2", "enter"]`.

CLI: `hark answer <event_id> --text "…" | --keys 2 enter`

Rejects if expectation fails (safer than free `hark reply` for production loops).

## JSON Schema

Normative file: `schemas/event-v1.schema.json` (in repo).  
Interaction FSM states (for `harkd` / queue): see prior art interaction schema — optional in Mode A.

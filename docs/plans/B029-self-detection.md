# B029 — Detect self inside herdr; exclude own pane from watch

## Problem

`hark watch` monitors herdr agent panes via `herdr agent list` and emits HEP
events (`agent.blocked`, `agent.needs_input`, …) that a monitor reacts to
(speak/listen/answer). When hark itself runs inside a herdr-managed pane, that
pane appears in `agent list`. Watch then forwards events about, and reads the
pane of, hark's own session — a feedback loop (hark reacting to hark).

## Self identity

Herdr exports these into panes it manages (see `docs/HERDR.md`):

| Var | Meaning |
|-----|---------|
| `HERDR_ENV=1` | running inside a herdr pane |
| `HERDR_PANE_ID` | this pane's id (e.g. `wG:p3`) |
| `HERDR_SOCKET_PATH` | the herdr server socket |
| `HERDR_SESSION` | named session (optional; default/local unset) |

Self = the agent whose `pane_id == HERDR_PANE_ID` on the herdr server reachable
at `HERDR_SOCKET_PATH`.

## Design

New module `src/hark/self_detect.py`:

- `SelfIdentity(pane_id, socket_path, session)` frozen dataclass.
- `detect_self(env=os.environ) -> SelfIdentity | None`:
  - Returns `None` unless `HERDR_ENV` is truthy and `HERDR_PANE_ID` plus a
    valid `HERDR_SOCKET_PATH` are set.
  - Escape hatch: `HARK_WATCH_INCLUDE_SELF` truthy → `None` (disable exclusion).
- `SelfIdentity.matches_agent(agent, *, session_socket, session_is_remote)`:
  - Requires `agent.pane_id == self.pane_id`.
  - Requires both socket paths to resolve to the same realpath.
  - If either socket is unknown or malformed → do not exclude: pane ids can
    collide across configured local servers. Remote/tunnelled sessions are
    never self.

Integration in `src/hark/watch.py`:

- Build `self_ident = detect_self()` once in `run_watch`.
- Filter each client's `list_agents()` through `_filter_self(...)` before
  `tracker.process(...)` in the poll loop, and inside `_watch_socket`
  (`reconcile` + `on_wire`). Filtering before `process` also prevents self-pane
  reads (`question_for`), and lifecycle notifications use the same identity
  check before invalidating/forwarding a target.
- Surface the excluded self target on `watch.armed` (`self_target`) and in
  `monitor_profile` for observability.

## Validation

- Unit tests for `detect_self` (env matrix + escape hatch) and `matches_agent`
  (socket match/mismatch, unknown socket local vs remote, pane mismatch).
- Watch integration tests: self pane produces no `agent.*` events and no
  pane reads; non-self panes still emit; remote panes unaffected.
- Full `pytest` suite (serial) stays green.

--- SUMMARY ---

- Root issue: hark running inside a herdr pane sees its own pane in `agent
  list`, so `watch` forwards/reads events about itself (feedback loop).
- Fix: detect self via herdr's `HERDR_ENV`/`HERDR_PANE_ID`/`HERDR_SOCKET_PATH`
  env vars and filter the self pane out of watch before edge-detection, so no
  events are emitted and the self pane is never read.
- Matching is socket-scoped (realpath of `HERDR_SOCKET_PATH` vs the session
  socket); when a socket is unknown, pane-id match is trusted only for local
  sessions, never remote/ssh ones.
- Escape hatch `HARK_WATCH_INCLUDE_SELF=1` disables exclusion; excluded target
  is surfaced on `watch.armed` for observability.
- Covered by new unit + watch-integration tests plus the existing suite.

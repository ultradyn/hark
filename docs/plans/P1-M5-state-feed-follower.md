# P1.M5 — Unify the State Feed Follower

**Status:** design locked for E1 (implementation follows E2–E4)  
**Date:** 2026-07-15  
**Backlog:** `P1.M5` · architecture review candidate 5 (Worth exploring)  
**Out of scope:** P1.M6 ListenSessionPolicy (other agent)

## Goal

One **deep** JSONL follower for all state files:

- **Small external interface** — multi-path follow, composite cursor, poll for records.
- **Large implementation** — partial-line buffer, inode/device rotation, truncation, per-source seq.
- **Thin adapters** — `hark monitor` (kinds + singleflight lock + presentation) and dashboard `MultiTailer` (envelope sources + SSE cursor).

## Problem (current)

Two followers with duplicated / uneven hardening:

| Path | Module | Hardening |
|------|--------|-----------|
| `hark monitor` | `monitor_feed.follow_state_files` | Truncation only; no partial-line buffer; no inode rotation |
| Dashboard | `dashboard.tailer.SourceTailer` / `MultiTailer` | Partial buffer + inode + composite cursor |

Dual **compaction**:

| Edge | Function |
|------|----------|
| Watch write (`--for-monitor`) | `events.monitor_profile` |
| Monitor read | `compact_mode_a_event` → `monitor_profile` again for agent.* |

Orchestrators can see double-shaped compact lines; dashboard wants full JSONL.

## Solution

```text
  producers (watch, ambient, system, usage, delivery)
       │  append JSONL (prefer full events)
       ▼
  ┌─────────────────────────────────────┐
  │  StateFeedFollower (deep core)      │
  │  SourceFollower × N                 │
  │  buffer · inode · truncate · cursor │
  └──────────────┬──────────────────────┘
                 │ FeedRecord stream
       ┌─────────┴──────────┐
       ▼                    ▼
  monitor adapter      dashboard MultiTailer
  kinds + lock +       sources + SSE envelope
  present_for_monitor
```

Package: `src/hark/state_feed/`.

---

## External interface

```python
@dataclass
class FeedRecord:
    source: str          # logical envelope source (watch, ambient, …)
    cursor_key: str      # composite cursor key (may differ, e.g. bound vs delivery)
    seq: int             # 1-based line index in current file incarnation
    payload: dict[str, Any]

class SourceFollower:
    def seek_to(self, seq: int) -> None: ...
    def start_at_end(self) -> None: ...
    def poll(self) -> Iterator[FeedRecord]: ...
    def close(self) -> None: ...

class StateFeedFollower:
    """Multi-path follower with one composite cursor token."""
    def composite_cursor(self) -> str: ...
    def start_live(self) -> None: ...
    def start_from(self, cursor: str | None, *, default_tail: int = 0) -> None: ...
    def poll(self) -> Iterator[FeedRecord]: ...
    def close(self) -> None: ...

def parse_cursor(cursor: str | None) -> dict[str, int]: ...
def format_cursor(positions: dict[str, int] | list[tuple[str, int]]) -> str: ...

def present_for_monitor(event: dict[str, Any]) -> dict[str, Any]:
    """Single HEP presentation profile for harness Monitors (agent + ambient + tts)."""
```

### Adapter responsibilities

| Adapter | Owns | Does **not** own |
|---------|------|------------------|
| **Core StateFeedFollower** | Partial buffer, inode/rotation, truncation, multi-path poll, cursor tokens | Kind filtering, flock, NDJSON print, dashboard envelopes |
| **`hark monitor`** | `MODE_A_WAKE_KINDS`, singleflight `MonitorFeedLock`, replay, `present_for_monitor` on emit, poll sleep loop | Tailer mechanics |
| **Dashboard MultiTailer** | Default source map (watch/ambient/system/usage/delivery split), envelope transforms, `read_page` sort/limit, SSE resume via same cursor format | Kind filtering, monitor lock |

### Cursor token (E2.T002)

**Format (dashboard-compatible):** legacy `key:seq` inputs remain accepted;
emitted positions are `key:seq@incarnation~checkpoint~byte_offset`.

- Keys = `cursor_key` per source (not always envelope source).
- `parse_cursor` is lenient (skips bad parts).
- Resume validates the opaque incarnation and rolling raw-line checkpoint,
  then seeks to the byte offset. A mismatch replays the replacement from seq 0.
- SSE `id:` lines continue to use the backward-compatible composite cursor.

---

## Compact timing (E1.T002) — LOCKED

| Decision | **Presentation edge once** (read/emit for consumers that need compact) |
|----------|------------------------------------------------------------------------|
| Prefer | JSONL stores **full** events; `present_for_monitor` only when emitting to a harness Monitor |
| Single function | `present_for_monitor` unifies `monitor_profile` (agent/watch/target) + ambient/tts compact branches |
| No double | Monitor adapter must not re-enter a second profile stack; `compact_mode_a_event` becomes an alias of `present_for_monitor` |
| Watch `--for-monitor` stdout | Remains a presentation edge for that process’s stdout (not a second pass over the same monitor-adapter pipeline) |
| Idempotent | Re-presenting an already-compact line must not crash; agent fields stay stable |

Writers that historically wrote compact into JSONL remain tolerated; the design target is full-on-disk + present-on-read for Mode A monitors.

---

## Module layout

```text
src/hark/state_feed/
  __init__.py
  record.py       # FeedRecord
  cursor.py       # parse/format
  source.py       # SourceFollower
  follower.py     # StateFeedFollower
  present.py      # present_for_monitor
```

| Facade | After |
|--------|--------|
| `dashboard.tailer.SourceTailer` | Alias / thin wrap of `SourceFollower` |
| `dashboard.tailer.MultiTailer` | Thin adapter over `StateFeedFollower` |
| `monitor_feed.follow_state_files` | Loop over `StateFeedFollower` + kinds + emit |
| `monitor_feed.compact_mode_a_event` | Alias → `present_for_monitor` |

---

## Invariants

1. Complete JSONL lines only (partial trailing line buffered).  
2. Rotation = inode/dev change **or** size shrink → reopen from start.  
3. Composite cursor stable for SSE resume.  
4. Monitor singleflight (B102) preserved.  
5. Mode A wake kinds set unchanged.  
6. HEP schema not bumped.

## Non-goals

1. No ListenSessionPolicy (M6).  
2. No HEP wire version change.  
3. No merging producers (watch/ambient stay separate writers).  
4. No rewriting dashboard WebUI contract beyond shared follower.

## Acceptance criteria

| # | Criterion |
|---|-----------|
| AC1 | Deep `StateFeedFollower` with hardened source follow |
| AC2 | Monitor adapter uses core; singleflight preserved |
| AC3 | Dashboard MultiTailer uses same core (no second follow impl) |
| AC4 | One presentation function; no designed double profile |
| AC5 | Cursor multi-source; SSE-compatible format |
| AC6 | Rotation/partial tests shared / green |
| AC7 | ARCHITECTURE feed topology docs |

## Implementation order

1. E1 — this plan (interface + compact timing).  
2. E2 — core follower + cursor.  
3. E3 — monitor + dashboard adapters + present unify.  
4. E4 — port tests + docs.

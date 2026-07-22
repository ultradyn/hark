# harkd — optional always-on daemon (experimental)

> **Status:** experimental scaffold / **not required for handsfree v1**.  
> Handsfree (`hark` CLI + skill + Monitor + `hark start` / `./scripts/run-mode-a.sh`) is the supported product path.  
> Python v0 of `harkd` defines process ownership, shared state, and **no silent double-send** with the skill path.

Related: [ARCHITECTURE.md](ARCHITECTURE.md) · [SPEC.md](SPEC.md) §3 · [NAMING.md](NAMING.md) · [IMPLEMENTATION.md](IMPLEMENTATION.md)

---

## 1. What handsfree does today

**Handsfree** is a human-or-agent supervisory loop **outside** Herdr:

| Piece | Role |
|-------|------|
| `hark monitor --for-monitor` | Unified HEP feed of `blocked` / `done` + ambient wake events (required Monitor; `hark watch` alone misses ambient) |
| `hark ambient` | Optional local wake (`hey hark` / …) → cloud STT prompt path |
| `hark context` / `ask` / `answer` / `keys` / `tts` / `listen` | Tools the orchestrator (or human) invokes |
| Skill `hark` / `handsfree` | Judgment: false done, menus, summaries, when to dig in |
| `hark start` / `stop` / `restart` | Preferred: start/stop watch + ambient, pidfile, graceful stop |
| `./scripts/run-mode-a.sh` | Shell equivalent of `hark start` / `hark stop` |

**Delivery owner in handsfree:** the orchestrator (or human) calls `hark answer` / `keys` / `reply`. The library (`DeliveryStore`, fingerprints, pane revision) enforces race-safe sends. The agent **MUST NOT** invent target IDs.

**Process ownership for always-on workers:**

```text
~/.local/state/hark/mode-a.pids   # PIDs of watch/ambient started by run-mode-a
~/.local/state/hark/busy.lock     # active recording (graceful stop waits)
~/.local/state/hark/mic.lock      # exclusive mic lease (fcntl)
~/.local/state/hark/ambient.pause # listen/ask asks ambient to yield mic
```

Logs and shared JSONL live under the same XDG state dir (see §3).

---

## 2. What harkd would own later (Mode B)

**Mode B / `harkd`** is an optional always-on process that runs the **same safety library** without a supervisory agent:

| Concern | Future harkd ownership |
|---------|------------------------|
| Always-on ambient + watch | Single supervised instance (no orphan workers) |
| Single-instance | `harkd.pid` under state dir |
| Dialogue / priority queue | Full interaction FSM (blocked → TTS → listen → confirm → deliver) |
| Delivery | **Sole** auto-delivery owner when running (see §4) |
| Control plane | status / stop / reload; later: Unix socket or local RPC |

**v0 Python scaffold (this repo):** process lifecycle only — `start` (foreground), `status`, `stop`. It may optionally spawn the same ambient/watch workers the launcher uses (`--workers`). It does **not** implement the Mode B dialogue FSM or auto-answer.

---

## 3. Shared state (handsfree tools ↔ harkd)

Both modes **MUST** use the same XDG layout (overridable via `XDG_*`):

| Path | Purpose |
|------|---------|
| `~/.config/hark/` | Config (`config.toml`) |
| `~/.local/state/hark/` | Runtime state (authoritative for coordination) |
| `~/.cache/hark/` | Cache (TTS snippets, etc.) |

### State files (coordination + delivery)

| File under state dir | Owner / use |
|----------------------|-------------|
| `harkd.pid` | Single-instance pidfile for `harkd` |
| `mode-a.pids` | Handsfree launcher (or harkd `--workers`) ambient/watch PIDs |
| `monitor.pid` | Singleflight lock for `hark monitor` feed consumer (B102) |
| `busy.lock` | Recording in progress (lifecycle / stop grace) |
| `mic.lock` | Exclusive mic (`MicLease`) |
| `ambient.pause` | Cooperative yield from ambient to listen/ask |
| `events.jsonl` | Bound events (`DeliveryStore`) |
| `deliveries.jsonl` | Delivery outcomes / idempotency |
| `system.jsonl` | Unified syslog |
| `watch.jsonl` / `ambient.jsonl` | Worker logs when redirected; ambient also dual-writes HEP wake events to `ambient.jsonl` even if stdout is elsewhere (B104) |
| `shutdown_reason` | stop \| restart (spoken cue) |

**Invariant:** library code reads/writes these paths via `hark.paths.state_dir()` so CLI tools, handsfree scripts, and `harkd` share one namespace.

---

## 4. Boundary: handsfree CLI vs daemon

```text
                    ┌─────────────────────────────┐
                    │   Shared hark library       │
                    │  delivery · mic · HEP · STT │
                    └─────────────┬───────────────┘
                                  │
           ┌──────────────────────┼──────────────────────┐
           ▼                      │                      ▼
   Handsfree (v1 product)         │              harkd (optional)
   · agent + skill + Monitor      │              · always-on process
   · hark answer is delivery      │              · future: auto voice loop
   · run-mode-a.sh workers        │              · harkd.pid single-instance
           │                      │                      │
           └────────── MUST NOT both auto-deliver ───────┘
```

| | Handsfree | harkd |
|--|--------|-------|
| **v1 required?** | Yes | No |
| **Worker start** | `hark start` (or `run-mode-a.sh` / manual ambient+watch) | `hark daemon start [--workers]` |
| **Judgment** | Orchestrator / skill | Future FSM in-process |
| **Delivery** | Explicit `hark answer` | Future auto; same `DeliveryStore` |
| **Single-instance** | `mode-a.pids` + script restart policy | `harkd.pid` |

### Coexistence rules (v0)

1. **At most one** of: live `harkd` **or** handsfree workers from `run-mode-a.sh`.  
2. `hark daemon start` **MUST refuse** if another live `harkd.pid` exists.  
3. `hark daemon start` **MUST refuse** if `mode-a.pids` lists live PIDs (handsfree already owns ambient/watch).  
4. `./scripts/run-mode-a.sh` **MUST refuse** if a live `harkd.pid` exists (avoid killing/replacing daemon workers silently).  
5. One-shot CLI (`tts`, `listen`, `ask`, `answer`) remains available either way; mic coordination uses `mic.lock` / `ambient.pause` as today.

---

## 5. No silent double-send

Double-send is prevented by **one delivery owner** plus library idempotency:

1. **Single delivery owner**  
   - Handsfree: only the orchestrator (or human) calls `hark answer` / bound keys.  
   - Mode B (future): only `harkd` auto-delivers; the skill **MUST NOT** also `answer` the same events while harkd owns delivery.  
   - v0: harkd does **not** auto-deliver — so it cannot race handsfree on send.

2. **Shared `DeliveryStore`** (`events.jsonl` + `deliveries.jsonl`)  
   - Bound `answer` atomically acquires a durable per-event owner before live
     validation, then fsyncs `acquired` → `validating` → `sending` before the
     Herdr write and finally `delivered`, `rejected`, or `uncertain`.
   - A concurrent answer receives the current stable outcome and cannot send.
     An abandoned pre-send owner may be recovered; an abandoned `sending`
     owner becomes `uncertain` and is never retried automatically.
   - External `skip`, expiry, and target-invalidation actions may fence an
     `acquired`/`validating` owner, but are refused after `sending` begins and
     cannot replace an existing terminal outcome.
   - Every transition fsyncs `deliveries.jsonl`; creation also fsyncs its parent
     directory so the first durable ownership record survives a cold crash.
   - Fingerprint and pane-revision checks remain mandatory after acquisition.
   - Both modes **MUST** use this store; no parallel “shadow” delivery path.

3. **Process exclusivity for always-on loops**  
   - Pidfile / refuse rules above prevent two ambient loops and two watches that could surface the same block twice and encourage two answers.  
   - `busy.lock` / `mic.lock` prevent overlapping capture, not delivery itself.

4. **Explicit refusal beats silent takeover**  
   - Never clear the other mode’s pidfile and start competing workers without user intent.

---

## 6. CLI (Python v0)

```bash
# Preferred entrypoints (equivalent):
uv run hark daemon start          # foreground supervisor (pidfile)
uv run hark daemon status         # harkd + workers + locks (JSON with --json)
uv run hark daemon stop           # SIGTERM via harkd.pid

uv run harkd start|status|stop    # same via console script

# Optional: supervise the same ambient/watch workers
uv run hark daemon start --workers
uv run hark daemon start --workers --no-ambient
uv run hark daemon start --workers --session default
```

Handsfree remains:

```bash
uv run hark start                 # ambient + watch --for-monitor (idempotent)
uv run hark stop                  # SIGTERM then SIGKILL after grace
uv run hark restart
uv run hark start --status        # running state only
# shell equivalent:
./scripts/run-mode-a.sh
./scripts/run-mode-a.sh --stop
uv run hark watch --for-monitor
uv run hark ambient
```

---

## 7. Non-goals (Python-only v0)

- Full Mode B dialogue FSM / priority queue / auto-TTS of blocked questions  
- systemd unit, D-Bus, or production socket API  
- Replacing the handsfree skill or Monitor requirement for v1  
- Cross-host daemon; multi-user instance arbitration  
- Silently stopping handsfree workers to “take over”

---

## 8. Implementation map

| Piece | Location |
|-------|----------|
| Spec (this doc) | `docs/HARKD.md` |
| Daemon logic | `src/hark/daemon.py` |
| CLI | `hark daemon …` / console script `harkd` |
| Handsfree workers | `hark start` / `stop` / `restart` (`src/hark/workers.py`); `scripts/run-mode-a.sh` |
| Shared paths | `src/hark/paths.py` → `state_dir()` |
| Delivery | `src/hark/delivery.py` |
| Mic / busy | `src/hark/audio/capture.py`, `lifecycle.py`, `mic_coord.py` |

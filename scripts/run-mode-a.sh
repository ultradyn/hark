#!/usr/bin/env bash
# Run Hark Mode A properly: Herdr watch + ambient wake (hey hark).
#
#   ./scripts/run-mode-a.sh
#   ./scripts/run-mode-a.sh --no-ambient
#   ./scripts/run-mode-a.sh --stop          # graceful: wait for active recording
#   ./scripts/run-mode-a.sh --stop --force  # SIGKILL if still up after grace
#
# Logs:
#   ~/.local/state/hark/watch.jsonl
#   ~/.local/state/hark/ambient.jsonl
#   ~/.local/state/hark/system.jsonl
# PIDs:
#   ~/.local/state/hark/mode-a.pids
# Refuses start if experimental harkd is live (~/.local/state/hark/harkd.pid).
# Busy marker (while user is recording):
#   ~/.local/state/hark/busy.lock
#
# Single-instance: before start, previous Mode A workers (pidfile + orphans)
# are stopped; the pidfile is always rewritten from scratch with only live PIDs.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/hark"
mkdir -p "$STATE"
PIDFILE="$STATE/mode-a.pids"
HARKD_PIDFILE="$STATE/harkd.pid"
BUSY="$STATE/busy.lock"
WATCH_LOG="$STATE/watch.jsonl"
AMBIENT_LOG="$STATE/ambient.jsonl"
SYSTEM_LOG="$STATE/system.jsonl"

DO_WATCH=1
DO_AMBIENT=1
FORCE=0
SESSION="${HARK_SESSION:-default}"
# Max wait for an in-flight recording (seconds)
STOP_GRACE="${HARK_STOP_GRACE_S:-120}"

# reason: stop | restart — written so ambient can TTS the right line
stage_shutdown_reason() {
  local reason="${1:-stop}"
  echo "$reason" >"$STATE/shutdown_reason"
  export HARK_SHUTDOWN_REASON="$reason"
}

pid_alive() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

# True if /proc/PID is a Mode A worker: `hark ambient` or `hark watch`
# (uv wrapper or python entrypoint). Avoids pgrep -f self-match and does not
# match this script, shells that only mention ambient logs, or pgrep/pkill.
is_mode_a_worker() {
  local pid="${1:-}"
  local role="${2:-}" # optional: ambient | watch
  local cmdfile="/proc/${pid}/cmdline"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [[ -r "$cmdfile" ]] || return 1

  local -a args=()
  mapfile -d '' -t args <"$cmdfile" || true
  ((${#args[@]} > 0)) || return 1

  local exe base a has_hark=0 role_found=""
  exe="${args[0]}"
  base="${exe##*/}"
  case "$base" in
    pgrep | pkill | pidof | kill | killall) return 1 ;;
  esac

  for a in "${args[@]}"; do
    case "$a" in
      *run-mode-a*) return 1 ;;
    esac
    if [[ "$a" == "hark" || "$a" == */hark ]]; then
      has_hark=1
    fi
    if [[ "$a" == "ambient" || "$a" == "watch" ]]; then
      role_found="$a"
    fi
  done

  [[ $has_hark -eq 1 && -n "$role_found" ]] || return 1
  if [[ -n "$role" && "$role_found" != "$role" ]]; then
    return 1
  fi
  return 0
}

# Unique live PIDs: pidfile entries that still exist + /proc scan for workers.
# Does not use pgrep -f (self-match / loose substring pitfalls).
collect_mode_a_pids() {
  {
    local line pid cmdfile
    if [[ -f "$PIDFILE" ]]; then
      while IFS= read -r line || [[ -n "$line" ]]; do
        # trim whitespace
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        [[ -z "$line" ]] && continue
        if pid_alive "$line"; then
          printf '%s\n' "$line"
        fi
      done <"$PIDFILE"
    fi
    for cmdfile in /proc/[0-9]*/cmdline; do
      [[ -e "$cmdfile" ]] || continue
      pid="${cmdfile%/cmdline}"
      pid="${pid#/proc/}"
      [[ "$pid" == "$$" ]] && continue
      if is_mode_a_worker "$pid"; then
        printf '%s\n' "$pid"
      fi
    done
  } | sort -n -u
}

# Rewrite pidfile from scratch with only live PIDs; remove when empty.
write_pidfile() {
  local -a live=()
  local p
  for p in "$@"; do
    if pid_alive "$p"; then
      live+=("$p")
    fi
  done
  if ((${#live[@]} == 0)); then
    rm -f "$PIDFILE"
    return 0
  fi
  printf '%s\n' "${live[@]}" >"$PIDFILE"
}

signal_pids() {
  local sig="$1"
  shift
  local p
  for p in "$@"; do
    kill "-${sig}" "$p" 2>/dev/null || true
  done
}

graceful_stop() {
  local force="$1"
  local reason="${2:-stop}"
  stage_shutdown_reason "$reason"

  local -a pids=()
  mapfile -t pids < <(collect_mode_a_pids) || true
  # Drop empty line if collect printed nothing
  if ((${#pids[@]} == 1)) && [[ -z "${pids[0]}" ]]; then
    pids=()
  fi

  if ((${#pids[@]} == 0)); then
    echo "no Mode A processes running"
    rm -f "$PIDFILE"
    return 0
  fi

  echo "sending SIGTERM (graceful, reason=$reason) to: ${pids[*]}"
  signal_pids TERM "${pids[@]}"

  # If recording, wait until busy.lock clears (or processes exit).
  # Re-scan each tick so orphaned python children of a dead uv parent are tracked.
  local waited=0
  local -a still=()
  while [[ $waited -lt $STOP_GRACE ]]; do
    mapfile -t still < <(collect_mode_a_pids) || true
    if ((${#still[@]} == 1)) && [[ -z "${still[0]:-}" ]]; then
      still=()
    fi

    if ((${#still[@]} == 0)); then
      echo "all Mode A processes exited cleanly (${waited}s)"
      rm -f "$PIDFILE" "$BUSY"
      return 0
    fi

    # Keep pidfile honest while waiting (no stale dead entries)
    write_pidfile "${still[@]}"

    if [[ -f "$BUSY" ]] && [[ $((waited % 5)) -eq 0 ]]; then
      echo "waiting for active recording to finish… (${waited}s / ${STOP_GRACE}s)"
      cat "$BUSY" 2>/dev/null || true
    fi
    sleep 1
    waited=$((waited + 1))
  done

  mapfile -t still < <(collect_mode_a_pids) || true
  if ((${#still[@]} == 1)) && [[ -z "${still[0]:-}" ]]; then
    still=()
  fi

  if [[ "$force" -eq 1 ]]; then
    if ((${#still[@]} > 0)); then
      echo "force-killing remaining processes: ${still[*]}"
      signal_pids KILL "${still[@]}"
    else
      echo "force-killing remaining processes: none"
    fi
    rm -f "$PIDFILE" "$BUSY"
    return 0
  fi

  echo "warning: still running after ${STOP_GRACE}s; use --force to SIGKILL" >&2
  write_pidfile "${still[@]}"
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-watch) DO_WATCH=0; shift ;;
    --no-ambient) DO_AMBIENT=0; shift ;;
    --session) SESSION="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --stop)
      shift
      # allow --stop --force
      while [[ $# -gt 0 ]]; do
        case "$1" in
          --force) FORCE=1; shift ;;
          *) break ;;
        esac
      done
      graceful_stop "$FORCE" "stop"
      exit $?
      ;;
    -h | --help)
      sed -n '2,20p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown: $1" >&2; exit 1 ;;
  esac
done

# Refuse to race experimental harkd (docs/HARKD.md): single always-on owner.
if [[ -f "$HARKD_PIDFILE" ]]; then
  harkd_pid="$(tr -d '[:space:]' <"$HARKD_PIDFILE" 2>/dev/null || true)"
  if [[ "$harkd_pid" =~ ^[0-9]+$ ]] && kill -0 "$harkd_pid" 2>/dev/null; then
    echo "error: harkd is running (pid $harkd_pid via $HARKD_PIDFILE)" >&2
    echo "  stop it first: uv run hark daemon stop" >&2
    echo "  (Mode A and harkd must not both own ambient/watch — see docs/HARKD.md)" >&2
    exit 1
  fi
  # stale pidfile
  rm -f "$HARKD_PIDFILE"
fi

cd "$ROOT"
HARK=(uv run hark)

# Always replace previous Mode A (pidfile and/or orphan ambient/watch workers)
# so partial restarts cannot leave duplicate ambients.
prev_count=0
if [[ -f "$PIDFILE" ]]; then
  prev_count=1
fi
mapfile -t _prev < <(collect_mode_a_pids) || true
if ((${#_prev[@]} == 1)) && [[ -z "${_prev[0]}" ]]; then
  _prev=()
fi
if ((${#_prev[@]} > 0)); then
  prev_count=${#_prev[@]}
fi

if [[ $prev_count -gt 0 ]]; then
  echo "restarting previous Mode A (graceful, ${#_prev[@]} pid(s))…"
  graceful_stop 0 "restart" || graceful_stop 1 "restart"
  # Allow ambient to finish shutdown TTS after recording
  sleep 0.5
fi

# Fresh pidfile: only live processes from this start will be recorded
rm -f "$PIDFILE"

started=()

if [[ "$DO_WATCH" -eq 1 ]]; then
  echo "starting watch → $WATCH_LOG (session=$SESSION)"
  nohup "${HARK[@]}" watch --session "$SESSION" --for-monitor --statuses blocked,done \
    >>"$WATCH_LOG" 2>&1 &
  started+=("$!")
fi

if [[ "$DO_AMBIENT" -eq 1 ]]; then
  echo "starting ambient loop → $AMBIENT_LOG"
  # List configured wake/trigger phrases (custom via config [ambient])
  PHRASE_LINE="$(
    cd "$ROOT" && python3 -c '
from hark.config import load_config
ps = load_config().ambient.activation_phrases
print("  say: " + " / ".join(ps[:10]) + (" …" if len(ps) > 10 else ""))
' 2>/dev/null || echo "  say: hey hark / hey herald (or custom trigger_phrases)"
  )"
  echo "$PHRASE_LINE"
  nohup "${HARK[@]}" ambient \
    >>"$AMBIENT_LOG" 2>&1 &
  started+=("$!")
fi

sleep 1

# Prefer full discovery (uv + python children) so the pidfile is complete;
# fall back to $! list if scan is empty (race before exec).
mapfile -t live < <(collect_mode_a_pids) || true
if ((${#live[@]} == 1)) && [[ -z "${live[0]}" ]]; then
  live=()
fi
if ((${#live[@]} > 0)); then
  write_pidfile "${live[@]}"
elif ((${#started[@]} > 0)); then
  write_pidfile "${started[@]}"
else
  rm -f "$PIDFILE"
fi

if [[ -f "$PIDFILE" ]]; then
  echo "PIDs: $(tr '\n' ' ' <"$PIDFILE")"
else
  echo "PIDs: (none — nothing started or already exited)"
fi
echo "tail logs:"
echo "  uv run hark logs -f"
echo "  tail -f $SYSTEM_LOG"
echo "  tail -f $AMBIENT_LOG"
echo "stop:  $0 --stop          # waits for recording to finish"
echo "       $0 --stop --force  # hard kill after grace"
if [[ -f "$AMBIENT_LOG" ]]; then
  echo "--- ambient (recent) ---"
  tail -n 3 "$AMBIENT_LOG" 2>/dev/null || true
fi

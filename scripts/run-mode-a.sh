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
# Busy marker (while user is recording):
#   ~/.local/state/hark/busy.lock

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/hark"
mkdir -p "$STATE"
PIDFILE="$STATE/mode-a.pids"
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
  echo "$reason" > "$STATE/shutdown_reason"
  export HARK_SHUTDOWN_REASON="$reason"
}

graceful_stop() {
  local force="$1"
  local reason="${2:-stop}"
  stage_shutdown_reason "$reason"

  if [[ ! -f "$PIDFILE" ]]; then
    echo "no pidfile at $PIDFILE"
    pkill -TERM -f 'hark watch --session' 2>/dev/null || true
    pkill -TERM -f 'hark ambient' 2>/dev/null || true
    return 0
  fi

  mapfile -t PIDS < "$PIDFILE" || true
  if [[ ${#PIDS[@]} -eq 0 ]]; then
    rm -f "$PIDFILE"
    return 0
  fi

  echo "sending SIGTERM (graceful, reason=$reason) to: ${PIDS[*]}"
  for pid in "${PIDS[@]}"; do
    # Pass reason via process environment is already set for children only;
    # ambient reads $STATE/shutdown_reason on exit.
    kill -TERM "$pid" 2>/dev/null || true
  done

  # If recording, wait until busy.lock clears (or processes exit)
  local waited=0
  while [[ $waited -lt $STOP_GRACE ]]; do
    local any_alive=0
    for pid in "${PIDS[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        any_alive=1
        break
      fi
    done
    if [[ $any_alive -eq 0 ]]; then
      echo "all Mode A processes exited cleanly (${waited}s)"
      rm -f "$PIDFILE" "$BUSY"
      return 0
    fi
    if [[ -f "$BUSY" ]]; then
      if [[ $((waited % 5)) -eq 0 ]]; then
        echo "waiting for active recording to finish… (${waited}s / ${STOP_GRACE}s)"
        cat "$BUSY" 2>/dev/null || true
      fi
    fi
    sleep 1
    waited=$((waited + 1))
  done

  if [[ "$force" -eq 1 ]]; then
    echo "force-killing remaining processes"
    for pid in "${PIDS[@]}"; do
      kill -KILL "$pid" 2>/dev/null || true
    done
    rm -f "$PIDFILE" "$BUSY"
    return 0
  fi

  echo "warning: still running after ${STOP_GRACE}s; use --force to SIGKILL" >&2
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
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown: $1" >&2; exit 1 ;;
  esac
done

cd "$ROOT"
HARK=(uv run hark)

# stop previous mode-a gracefully before restart (distinct TTS: "Hark restarting.")
if [[ -f "$PIDFILE" ]]; then
  echo "restarting previous Mode A (graceful)…"
  graceful_stop 0 "restart" || graceful_stop 1 "restart"
  # Allow ambient to finish shutdown TTS after recording
  sleep 0.5
fi

: > "$PIDFILE"

if [[ "$DO_WATCH" -eq 1 ]]; then
  echo "starting watch → $WATCH_LOG (session=$SESSION)"
  nohup "${HARK[@]}" watch --session "$SESSION" --for-monitor --statuses blocked,done \
    >>"$WATCH_LOG" 2>&1 &
  echo $! >>"$PIDFILE"
fi

if [[ "$DO_AMBIENT" -eq 1 ]]; then
  echo "starting ambient loop → $AMBIENT_LOG"
  echo "  say: hey hark / hey herald / hello herald"
  nohup "${HARK[@]}" ambient \
    >>"$AMBIENT_LOG" 2>&1 &
  echo $! >>"$PIDFILE"
fi

sleep 1
echo "PIDs: $(tr '\n' ' ' < "$PIDFILE")"
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

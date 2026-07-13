#!/usr/bin/env bash
# Run Hark Mode A properly: Herdr watch + ambient wake (hey hark).
#
#   ./scripts/run-mode-a.sh
#   ./scripts/run-mode-a.sh --no-ambient
#   ./scripts/run-mode-a.sh --no-watch
#
# Logs:
#   ~/.local/state/hark/watch.jsonl
#   ~/.local/state/hark/ambient.jsonl
# PIDs:
#   ~/.local/state/hark/mode-a.pids

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${XDG_STATE_HOME:-$HOME/.local/state}/hark"
mkdir -p "$STATE"
PIDFILE="$STATE/mode-a.pids"
WATCH_LOG="$STATE/watch.jsonl"
AMBIENT_LOG="$STATE/ambient.jsonl"

DO_WATCH=1
DO_AMBIENT=1
SESSION="${HARK_SESSION:-default}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-watch) DO_WATCH=0; shift ;;
    --no-ambient) DO_AMBIENT=0; shift ;;
    --session) SESSION="$2"; shift 2 ;;
    --stop)
      if [[ -f "$PIDFILE" ]]; then
        while read -r pid; do
          kill "$pid" 2>/dev/null || true
        done < "$PIDFILE"
        rm -f "$PIDFILE"
        echo "stopped Mode A processes"
      else
        echo "no pidfile at $PIDFILE"
        # best-effort
        pkill -f 'hark watch --session' 2>/dev/null || true
        pkill -f 'hark ambient' 2>/dev/null || true
      fi
      exit 0
      ;;
    -h|--help)
      sed -n '2,14p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown: $1" >&2; exit 1 ;;
  esac
done

cd "$ROOT"
HARK=(uv run hark)

# stop previous mode-a from this script
if [[ -f "$PIDFILE" ]]; then
  echo "stopping previous Mode A…"
  while read -r pid; do
    kill "$pid" 2>/dev/null || true
  done < "$PIDFILE" || true
  rm -f "$PIDFILE"
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
  echo "  say: hey hark / hey herald"
  nohup "${HARK[@]}" ambient \
    >>"$AMBIENT_LOG" 2>&1 &
  echo $! >>"$PIDFILE"
fi

sleep 1
echo "PIDs: $(tr '\n' ' ' < "$PIDFILE")"
echo "tail logs:"
echo "  tail -f $WATCH_LOG"
echo "  tail -f $AMBIENT_LOG"
echo "stop:  $0 --stop"

# show last lines if any
if [[ -f "$AMBIENT_LOG" ]]; then
  echo "--- ambient (recent) ---"
  tail -n 3 "$AMBIENT_LOG" 2>/dev/null || true
fi

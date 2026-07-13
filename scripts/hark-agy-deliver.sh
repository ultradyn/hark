#!/usr/bin/env bash
# Mode A wake sidecar for Google Antigravity (agy) via agentapi.
#
# Prerequisite:
#   1. Mode A workers running (./scripts/run-mode-a.sh)
#   2. agy-env registered: hark agentapi register
#      (needs ANTIGRAVITY_LS_ADDRESS + ANTIGRAVITY_CONVERSATION_ID)
#
# Usage:
#   ./scripts/hark-agy-deliver.sh
#   ./scripts/hark-agy-deliver.sh --dry-run
#   ./scripts/hark-agy-deliver.sh --replay 5
#
# See docs/AGY.md and docs/plans/B049-agy-agentapi.md

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if command -v uv >/dev/null 2>&1 && [[ -f "$ROOT/pyproject.toml" ]]; then
  HARK=(uv run hark)
elif command -v hark >/dev/null 2>&1; then
  HARK=(hark)
else
  echo "error: need uv run hark (checkout) or hark on PATH" >&2
  exit 1
fi

EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h | --help)
      sed -n '2,16p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *)
      EXTRA+=("$1")
      shift
      ;;
  esac
done

echo "hark-agy-deliver: status" >&2
"${HARK[@]}" agentapi status || {
  echo "error: register first: hark agentapi register" >&2
  exit 1
}

exec "${HARK[@]}" agentapi deliver --follow-monitor "${EXTRA[@]}"

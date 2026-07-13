#!/bin/sh
set -eu
out="${1:-herdr-api.schema.json}"
command -v herdr >/dev/null 2>&1 || { echo "herdr not found" >&2; exit 1; }
herdr api schema --output "$out"
printf 'captured %s\n' "$out"
herdr status || true

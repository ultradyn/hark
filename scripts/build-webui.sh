#!/usr/bin/env bash
# Build the dashboard webui and stage it into the Python package so the wheel
# ships a working `hark serve` (src/hark/dashboard/webui_dist/ is gitignored;
# run this before `uv build` / releases — see RELEASE.md).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WEBUI="$ROOT/webui"
DEST="$ROOT/src/hark/dashboard/webui_dist"

command -v npm >/dev/null || { echo "npm is required" >&2; exit 1; }

cd "$WEBUI"
[ -d node_modules ] || npm ci
npm run build

rm -rf "$DEST"
cp -r "$WEBUI/dist" "$DEST"
echo "staged webui -> ${DEST#"$ROOT"/} ($(du -sh "$DEST" | cut -f1))"

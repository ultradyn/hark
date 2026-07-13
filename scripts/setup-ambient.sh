#!/usr/bin/env bash
# One-shot ambient setup: vosk package + model + optional config enable.
#
#   ./scripts/setup-ambient.sh
#   ./scripts/setup-ambient.sh --no-config   # model only
#   ./scripts/setup-ambient.sh --method curl

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
METHOD="auto"
WRITE_CONFIG=1
CONFIG_PATH="${HARK_CONFIG:-$HOME/.config/hark/config.toml}"
MODEL_DIR="${HARK_VOSK_MODEL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/hark/models}"
MODEL_PATH="${MODEL_DIR%/}/vosk-model-small-en-us-0.15"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --method) METHOD="$2"; shift 2 ;;
    --no-config) WRITE_CONFIG=0; shift ;;
    --config) CONFIG_PATH="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \?//'
      exit 0
      ;;
    *) echo "unknown: $1" >&2; exit 1 ;;
  esac
done

echo "==> install Python wake extra (vosk)"
if command -v uv >/dev/null 2>&1; then
  (cd "$ROOT" && uv sync --extra wake --extra dev)
else
  (cd "$ROOT" && pip install 'vosk>=0.3.45')
fi

echo "==> download vosk model"
bash "$ROOT/scripts/download-vosk-model.sh" --dir "$MODEL_DIR" --method "$METHOD"

if [[ ! -d "$MODEL_PATH" ]]; then
  echo "error: model missing at $MODEL_PATH" >&2
  exit 1
fi

if [[ "$WRITE_CONFIG" -eq 1 ]]; then
  mkdir -p "$(dirname "$CONFIG_PATH")"
  if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "==> writing new config $CONFIG_PATH"
    if command -v uv >/dev/null 2>&1; then
      (cd "$ROOT" && uv run hark config init 2>/dev/null || true)
    fi
  fi
  if [[ -f "$CONFIG_PATH" ]]; then
    echo "==> enable ambient in $CONFIG_PATH"
    python3 - "$CONFIG_PATH" "$MODEL_PATH" <<'PY'
import re, sys
from pathlib import Path
path, model = Path(sys.argv[1]), sys.argv[2]
text = path.read_text(encoding="utf-8")
if "[ambient]" not in text:
    text += f"""

[ambient]
enabled = true
activation_phrases = ["hey hark", "hey herald", "okay hark", "ok hark"]
engine = "vosk"
model_path = "{model}"
snippet_s = 2.5
timeout_s = 300
# surface_timeouts = true  # set false to quiet continuous ambient.timeout
"""
else:
    def set_key(block_name, key, value, text):
        # crude in-section replace / insert
        m = re.search(rf"(\[{re.escape(block_name)}\])(.*?)(?=\n\[|\Z)", text, re.S)
        if not m:
            return text
        head, body = m.group(1), m.group(2)
        if re.search(rf"(?m)^{re.escape(key)}\s*=", body):
            body = re.sub(
                rf"(?m)^{re.escape(key)}\s*=.*$",
                f'{key} = {value}',
                body,
            )
        else:
            body = body.rstrip() + f"\n{key} = {value}\n"
        return text[: m.start()] + head + body + text[m.end() :]

    text = set_key("ambient", "enabled", "true", text)
    text = set_key("ambient", "engine", '"vosk"', text)
    text = set_key("ambient", "model_path", f'"{model}"', text)
path.write_text(text, encoding="utf-8")
print(f"updated {path}")
print(f"  ambient.enabled = true")
print(f"  ambient.model_path = {model}")
PY
  else
    echo "warn: no config at $CONFIG_PATH — set manually:"
    echo "  [ambient]"
    echo "  enabled = true"
    echo "  engine = \"vosk\""
    echo "  model_path = \"$MODEL_PATH\""
  fi
fi

echo ""
echo "Done. Smoke test:"
echo "  cd $ROOT && uv run hark doctor"
echo "  uv run hark ambient --timeout 60"
echo "  # say: hey hark"
echo ""
echo "MODEL_PATH=$MODEL_PATH"

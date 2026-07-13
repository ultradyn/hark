#!/usr/bin/env bash
# Download vosk-model-small-en-us-0.15 for Hark ambient wake.
#
# Methods (in order, skip with --method):
#   1. hf          — Hugging Face CLI (`hf download`)
#   2. curl        — direct HTTPS
#   3. wget        — direct HTTPS
#   4. browser     — open URL in default browser + wait for manual drop
#
# Usage:
#   ./scripts/download-vosk-model.sh
#   ./scripts/download-vosk-model.sh --dir ~/.local/share/hark/models
#   ./scripts/download-vosk-model.sh --method curl
#   HARK_VOSK_MODEL_DIR=... ./scripts/download-vosk-model.sh

set -euo pipefail

MODEL_NAME="vosk-model-small-en-us-0.15"
ZIP_NAME="${MODEL_NAME}.zip"

# Official + mirrors (first success wins within each method)
ALPHACEPHEI_URL="https://alphacephei.com/vosk/models/${ZIP_NAME}"
HF_REPO_GRIMSO="grimso/vosk-models"
HF_FILE_GRIMSO="${ZIP_NAME}"
HF_URL_GRIMSO="https://huggingface.co/grimso/vosk-models/resolve/main/${ZIP_NAME}"
HF_REPO_RHASSPY="rhasspy/vosk-models"
HF_FILE_RHASSPY="en/${ZIP_NAME}"
HF_URL_RHASSPY="https://huggingface.co/rhasspy/vosk-models/resolve/main/en/${ZIP_NAME}"

DEFAULT_DIR="${HARK_VOSK_MODEL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/hark/models}"
OUT_DIR="$DEFAULT_DIR"
METHOD="auto" # auto | hf | curl | wget | browser
FORCE=0

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) OUT_DIR="$2"; shift 2 ;;
    --method) METHOD="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

MODEL_DIR="${OUT_DIR%/}/${MODEL_NAME}"
ZIP_PATH="${OUT_DIR%/}/${ZIP_NAME}"
TMP_DIR="${OUT_DIR%/}/.tmp-vosk-$$"

have() { command -v "$1" >/dev/null 2>&1; }

is_valid_model() {
  local d="$1"
  [[ -d "$d" ]] && [[ -f "$d/am/final.mdl" || -f "$d/conf/model.conf" || -d "$d/graph" || -d "$d/ivector" || -f "$d/README" ]]
}

extract_zip() {
  local zip="$1"
  local dest_parent="$2"
  mkdir -p "$dest_parent"
  if have unzip; then
    unzip -qo "$zip" -d "$dest_parent"
  elif have bsdtar; then
    bsdtar -xf "$zip" -C "$dest_parent"
  elif have python3; then
    python3 - "$zip" "$dest_parent" <<'PY'
import sys, zipfile
from pathlib import Path
z, dest = Path(sys.argv[1]), Path(sys.argv[2])
with zipfile.ZipFile(z) as zf:
    zf.extractall(dest)
PY
  else
    echo "error: need unzip, bsdtar, or python3 to extract" >&2
    return 1
  fi
}

finalize_from_extract() {
  local parent="$1"
  # zip usually contains a top-level MODEL_NAME/
  if is_valid_model "${parent}/${MODEL_NAME}"; then
    mkdir -p "$OUT_DIR"
    rm -rf "$MODEL_DIR"
    mv "${parent}/${MODEL_NAME}" "$MODEL_DIR"
    return 0
  fi
  # sometimes files land flat in parent
  if is_valid_model "$parent"; then
    mkdir -p "$OUT_DIR"
    rm -rf "$MODEL_DIR"
    mv "$parent" "$MODEL_DIR"
    return 0
  fi
  # nested find
  local found
  found="$(find "$parent" -type d -name "$MODEL_NAME" 2>/dev/null | head -1 || true)"
  if [[ -n "$found" ]] && is_valid_model "$found"; then
    mkdir -p "$OUT_DIR"
    rm -rf "$MODEL_DIR"
    mv "$found" "$MODEL_DIR"
    return 0
  fi
  echo "error: extracted archive but could not find valid model tree" >&2
  return 1
}

download_curl() {
  local url="$1"
  local dest="$2"
  echo "curl: $url"
  curl -fL --retry 3 --retry-delay 2 -o "$dest" "$url"
}

download_wget() {
  local url="$1"
  local dest="$2"
  echo "wget: $url"
  wget -O "$dest" "$url"
}

try_hf() {
  local cli=""
  if have hf; then cli=hf
  elif have huggingface-cli; then cli=huggingface-cli
  else
    echo "hf: not installed (skip)"
    return 1
  fi

  mkdir -p "$TMP_DIR"
  # Prefer grimso single-file repo; fall back to rhasspy path
  if "$cli" download "$HF_REPO_GRIMSO" "$HF_FILE_GRIMSO" --local-dir "$TMP_DIR" 2>/dev/null; then
    :
  elif "$cli" download "$HF_REPO_RHASSPY" "$HF_FILE_RHASSPY" --local-dir "$TMP_DIR" 2>/dev/null; then
    :
  else
    echo "hf: download failed" >&2
    return 1
  fi

  local zip
  zip="$(find "$TMP_DIR" -name "$ZIP_NAME" -type f | head -1 || true)"
  if [[ -z "$zip" ]]; then
    echo "hf: zip not found under $TMP_DIR" >&2
    return 1
  fi
  extract_zip "$zip" "$TMP_DIR/extract"
  finalize_from_extract "$TMP_DIR/extract"
}

try_url() {
  local tool="$1"
  shift
  local urls=("$@")
  mkdir -p "$TMP_DIR"
  local url
  for url in "${urls[@]}"; do
    rm -f "$ZIP_PATH"
    if [[ "$tool" == curl ]]; then
      download_curl "$url" "$ZIP_PATH" || continue
    else
      download_wget "$url" "$ZIP_PATH" || continue
    fi
    if [[ -s "$ZIP_PATH" ]]; then
      extract_zip "$ZIP_PATH" "$TMP_DIR/extract"
      finalize_from_extract "$TMP_DIR/extract"
      rm -f "$ZIP_PATH"
      return 0
    fi
  done
  return 1
}

try_browser() {
  local url="$ALPHACEPHEI_URL"
  echo ""
  echo "browser fallback: open this URL and save the zip, then place it here:"
  echo "  $url"
  echo "  drop path: $ZIP_PATH"
  echo ""
  if have xdg-open; then
    xdg-open "$url" >/dev/null 2>&1 || true
  elif have open; then
    open "$url" || true
  else
    echo "(no xdg-open/open — paste URL into a browser yourself)"
  fi
  echo "Waiting for $ZIP_PATH (Ctrl+C to abort)…"
  local i=0
  while [[ $i -lt 600 ]]; do
    if [[ -s "$ZIP_PATH" ]]; then
      sleep 1
      mkdir -p "$TMP_DIR"
      extract_zip "$ZIP_PATH" "$TMP_DIR/extract"
      finalize_from_extract "$TMP_DIR/extract"
      return 0
    fi
    sleep 1
    i=$((i + 1))
  done
  echo "timeout waiting for manual download" >&2
  return 1
}

cleanup() { rm -rf "$TMP_DIR" 2>/dev/null || true; }
trap cleanup EXIT

if is_valid_model "$MODEL_DIR" && [[ "$FORCE" -eq 0 ]]; then
  echo "already installed: $MODEL_DIR"
  echo "MODEL_PATH=$MODEL_DIR"
  exit 0
fi

mkdir -p "$OUT_DIR"
echo "installing $MODEL_NAME → $MODEL_DIR"
echo "method=$METHOD"

URLS=("$ALPHACEPHEI_URL" "$HF_URL_GRIMSO" "$HF_URL_RHASSPY")

ok=0
case "$METHOD" in
  auto)
    try_hf && ok=1 || true
    if [[ "$ok" -eq 0 ]] && have curl; then try_url curl "${URLS[@]}" && ok=1 || true; fi
    if [[ "$ok" -eq 0 ]] && have wget; then try_url wget "${URLS[@]}" && ok=1 || true; fi
    if [[ "$ok" -eq 0 ]]; then try_browser && ok=1 || true; fi
    ;;
  hf) try_hf && ok=1 ;;
  curl) have curl || { echo "curl missing" >&2; exit 1; }; try_url curl "${URLS[@]}" && ok=1 ;;
  wget) have wget || { echo "wget missing" >&2; exit 1; }; try_url wget "${URLS[@]}" && ok=1 ;;
  browser) try_browser && ok=1 ;;
  *) echo "unknown method: $METHOD" >&2; exit 1 ;;
esac

if [[ "$ok" -ne 1 ]] || ! is_valid_model "$MODEL_DIR"; then
  echo "FAILED to install $MODEL_NAME" >&2
  echo "Manual:" >&2
  echo "  1. Download $ALPHACEPHEI_URL" >&2
  echo "  2. unzip into $OUT_DIR so you have $MODEL_DIR" >&2
  exit 1
fi

echo "OK: $MODEL_DIR"
echo "MODEL_PATH=$MODEL_DIR"
# size hint
du -sh "$MODEL_DIR" 2>/dev/null || true

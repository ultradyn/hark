#!/usr/bin/env bash
# Download sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01 (English open-vocab KWS).
#
# Methods (in order for auto, skip with --method):
#   1. curl  — GitHub release tarball
#   2. wget  — same
#   3. browser — open URL + wait for manual drop
#
# Usage:
#   ./scripts/download-sherpa-kws-model.sh
#   ./scripts/download-sherpa-kws-model.sh --dir ~/.local/share/hark/models
#   ./scripts/download-sherpa-kws-model.sh --method curl
#   HARK_SHERPA_KWS_MODEL_DIR=... ./scripts/download-sherpa-kws-model.sh
#
# After install, set in config.toml:
#   [ambient]
#   engine = "sherpa_kws"
#   # model_path auto-detected under XDG data home when present

set -euo pipefail

MODEL_NAME="sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
TAR_NAME="${MODEL_NAME}.tar.bz2"
# Official release (int8 + fp32 onnx in one tree)
GITHUB_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/${TAR_NAME}"

DEFAULT_DIR="${HARK_SHERPA_KWS_MODEL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/hark/models}"
OUT_DIR="$DEFAULT_DIR"
METHOD="auto" # auto | curl | wget | browser
FORCE=0

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \?//'
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
TAR_PATH="${OUT_DIR%/}/${TAR_NAME}"
TMP_DIR="${OUT_DIR%/}/.tmp-sherpa-kws-$$"

have() { command -v "$1" >/dev/null 2>&1; }

is_valid_model() {
  local d="$1"
  [[ -d "$d" ]] || return 1
  [[ -f "$d/tokens.txt" ]] || return 1
  [[ -f "$d/bpe.model" ]] || return 1
  # Prefer int8 encoder; accept fp32 if that is all that is present
  [[ -f "$d/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx" \
     || -f "$d/encoder-epoch-12-avg-2-chunk-16-left-64.onnx" ]] || return 1
  [[ -f "$d/decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx" \
     || -f "$d/decoder-epoch-12-avg-2-chunk-16-left-64.onnx" ]] || return 1
  [[ -f "$d/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx" \
     || -f "$d/joiner-epoch-12-avg-2-chunk-16-left-64.onnx" ]] || return 1
  return 0
}

extract_tar() {
  local tar="$1"
  local dest_parent="$2"
  mkdir -p "$dest_parent"
  if have tar; then
    tar -xjf "$tar" -C "$dest_parent"
  elif have bsdtar; then
    bsdtar -xf "$tar" -C "$dest_parent"
  else
    echo "error: need tar or bsdtar to extract" >&2
    return 1
  fi
}

finalize_from_extract() {
  local parent="$1"
  if is_valid_model "${parent}/${MODEL_NAME}"; then
    mkdir -p "$OUT_DIR"
    rm -rf "$MODEL_DIR"
    mv "${parent}/${MODEL_NAME}" "$MODEL_DIR"
    return 0
  fi
  if is_valid_model "$parent"; then
    mkdir -p "$OUT_DIR"
    rm -rf "$MODEL_DIR"
    mv "$parent" "$MODEL_DIR"
    return 0
  fi
  local found
  found="$(find "$parent" -type d -name "$MODEL_NAME" 2>/dev/null | head -1 || true)"
  if [[ -n "$found" ]] && is_valid_model "$found"; then
    mkdir -p "$OUT_DIR"
    rm -rf "$MODEL_DIR"
    mv "$found" "$MODEL_DIR"
    return 0
  fi
  echo "error: extracted archive but could not find valid KWS model tree" >&2
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

try_url() {
  local tool="$1"
  local url="$2"
  mkdir -p "$TMP_DIR" "$OUT_DIR"
  rm -f "$TAR_PATH"
  if [[ "$tool" == curl ]]; then
    download_curl "$url" "$TAR_PATH" || return 1
  else
    download_wget "$url" "$TAR_PATH" || return 1
  fi
  if [[ -s "$TAR_PATH" ]]; then
    extract_tar "$TAR_PATH" "$TMP_DIR/extract"
    finalize_from_extract "$TMP_DIR/extract"
    rm -f "$TAR_PATH"
    return 0
  fi
  return 1
}

try_browser() {
  local url="$GITHUB_URL"
  echo ""
  echo "browser fallback: open this URL and save the tarball, then place it here:"
  echo "  $url"
  echo "  drop path: $TAR_PATH"
  echo ""
  if have xdg-open; then
    xdg-open "$url" >/dev/null 2>&1 || true
  elif have open; then
    open "$url" || true
  else
    echo "(no xdg-open/open — paste URL into a browser yourself)"
  fi
  echo "Waiting for $TAR_PATH (Ctrl+C to abort)…"
  local i=0
  while [[ $i -lt 600 ]]; do
    if [[ -s "$TAR_PATH" ]]; then
      sleep 1
      mkdir -p "$TMP_DIR"
      extract_tar "$TAR_PATH" "$TMP_DIR/extract"
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

ok=0
case "$METHOD" in
  auto)
    if have curl; then try_url curl "$GITHUB_URL" && ok=1 || true; fi
    if [[ "$ok" -eq 0 ]] && have wget; then try_url wget "$GITHUB_URL" && ok=1 || true; fi
    if [[ "$ok" -eq 0 ]]; then try_browser && ok=1 || true; fi
    ;;
  curl) have curl || { echo "curl missing" >&2; exit 1; }; try_url curl "$GITHUB_URL" && ok=1 ;;
  wget) have wget || { echo "wget missing" >&2; exit 1; }; try_url wget "$GITHUB_URL" && ok=1 ;;
  browser) try_browser && ok=1 ;;
  *) echo "unknown method: $METHOD" >&2; exit 1 ;;
esac

if [[ "$ok" -ne 1 ]] || ! is_valid_model "$MODEL_DIR"; then
  echo "FAILED to install $MODEL_NAME" >&2
  echo "Manual:" >&2
  echo "  1. Download $GITHUB_URL" >&2
  echo "  2. tar -xjf into $OUT_DIR so you have $MODEL_DIR" >&2
  echo "  3. uv sync --extra wake-sherpa   # Python bindings" >&2
  exit 1
fi

echo "OK: $MODEL_DIR"
echo "MODEL_PATH=$MODEL_DIR"
du -sh "$MODEL_DIR" 2>/dev/null || true
echo ""
echo "Next:"
echo "  uv sync --extra wake-sherpa"
echo "  # config.toml:"
echo "  [ambient]"
echo "  engine = \"sherpa_kws\""
echo "  # model_path = \"$MODEL_DIR\"  # optional if auto-detected"

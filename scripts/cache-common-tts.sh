#!/usr/bin/env bash
# Pre-render common TTS phrases into assets/tts/<voice>/ for offline reuse.
#
#   ./scripts/cache-common-tts.sh
#   ./scripts/cache-common-tts.sh --voice eve
#   ./scripts/cache-common-tts.sh --voice ara

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VOICE="${HARK_TTS_VOICE:-eve}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --voice) VOICE="$2"; shift 2 ;;
    -h|--help) sed -n '2,8p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown: $1" >&2; exit 1 ;;
  esac
done

cd "$ROOT"
echo "Caching common phrases for voice=$VOICE …"
uv run python - <<PY
from hark.audio.cues import COMMON_PHRASES, store_cached_tts, tts_cache_path
from hark.config import load_config
from hark.providers.resolve import resolve_tts

voice = ${VOICE@Q}
cfg = load_config()
tts = resolve_tts(cfg.tts.provider, voice=voice, language=cfg.tts.language or "en")
for phrase in COMMON_PHRASES:
    path = tts_cache_path(voice, phrase)
    if path.is_file() and path.stat().st_size > 64:
        print(f"  skip  {path.relative_to(path.parents[2])}")
        continue
    print(f"  synth {phrase!r}")
    result = tts.synthesize(phrase, voice=voice)
    out = store_cached_tts(voice, phrase, result.audio)
    print(f"  wrote {out}")
print("done")
PY

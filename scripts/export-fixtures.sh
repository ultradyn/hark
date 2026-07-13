#!/usr/bin/env bash
# Export live Hark state into repo fixtures for Python/Rust parity tests.
# Does not overwrite fixtures/text/* goldens (hand-curated).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE="${HARK_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/hark}"
HEP="$ROOT/fixtures/events/hep"
SYSLOG="$ROOT/fixtures/events/syslog"
USAGE="$ROOT/fixtures/usage"
VOICE_LIVE="$ROOT/fixtures/voice/wake/live"
WITH_WAKE=0

for arg in "$@"; do
  case "$arg" in
    --with-wake) WITH_WAKE=1 ;;
    -h|--help)
      cat <<EOF
Usage: $(basename "$0") [--with-wake]

Exports curated HEP/syslog/usage samples from:
  $STATE

  --with-wake   Also copy today's debug wake snips into fixtures/voice/wake/live/
                and refresh fixtures/voice/wake/cases.jsonl from sidecars.

Environment:
  HARK_STATE_DIR   Override state root (default: ~/.local/state/hark)
EOF
      exit 0
      ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

mkdir -p "$HEP" "$SYSLOG" "$USAGE" "$VOICE_LIVE"

python3 - "$STATE" "$HEP" "$SYSLOG" "$USAGE" <<'PY'
import json
import sys
from pathlib import Path

state, hep_dir, syslog_dir, usage_dir = map(Path, sys.argv[1:5])
hep_dir.mkdir(parents=True, exist_ok=True)
syslog_dir.mkdir(parents=True, exist_ok=True)
usage_dir.mkdir(parents=True, exist_ok=True)

def load_jsonl(path: Path):
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("==>"):
            continue
        if not line.startswith("{"):
            i = line.find("{")
            if i < 0:
                continue
            line = line[i:]
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out

# HEP watch: first of each kind
watch = load_jsonl(state / "watch.jsonl")
by_kind = {}
for o in watch:
    k = o.get("kind")
    if k and k not in by_kind and o.get("schema") == "hark.event.v1":
        by_kind[k] = o
for k, o in sorted(by_kind.items()):
    path = hep_dir / f"{k.replace('.', '_')}.json"
    path.write_text(json.dumps(o, indent=2) + "\n")
    print(f"wrote {path.relative_to(path.parents[2])}")

# ambient.prompt HEP samples
prompts = []
for name in ("ambient.jsonl", "ambient-prompts.jsonl"):
    for o in load_jsonl(state / name):
        if o.get("kind") == "ambient.prompt" and o.get("schema") == "hark.event.v1":
            prompts.append(o)
seen = set()
unique = []
for o in prompts:
    eid = o.get("event_id")
    if eid in seen:
        continue
    seen.add(eid)
    unique.append(o)
if unique:
    (hep_dir / "ambient_prompt_samples.jsonl").write_text(
        "\n".join(json.dumps(o, separators=(",", ":")) for o in unique) + "\n"
    )
    (hep_dir / "ambient_prompt_first.json").write_text(
        json.dumps(unique[0], indent=2) + "\n"
    )
    if len(unique) > 1:
        (hep_dir / "ambient_prompt_second.json").write_text(
            json.dumps(unique[1], indent=2) + "\n"
        )
    print(f"wrote {len(unique)} ambient.prompt HEP samples")

# syslog interesting sample
events = load_jsonl(state / "system.jsonl")
interesting = [e for e in events if e.get("event") not in ("ambient.debug", None)]
(syslog_dir / "system_interesting_sample.jsonl").write_text(
    "\n".join(json.dumps(e, separators=(",", ":")) for e in interesting[:60]) + "\n"
)
# wake → prompt chain
seq = []
capturing = False
for e in events:
    if e.get("event") == "ambient.wake" and not capturing:
        capturing = True
        seq = [e]
        continue
    if capturing:
        seq.append(e)
        if e.get("event") == "ambient.prompt":
            break
if seq:
    (syslog_dir / "wake_to_prompt_sequence.jsonl").write_text(
        "\n".join(json.dumps(e, separators=(",", ":")) for e in seq) + "\n"
    )
    print(f"wrote wake→prompt sequence ({len(seq)} events)")

usage = load_jsonl(state / "usage.jsonl")
if usage:
    (usage_dir / "sample.jsonl").write_text(
        "\n".join(json.dumps(u, separators=(",", ":")) for u in usage[:30]) + "\n"
    )
    print(f"wrote usage sample ({min(30, len(usage))} lines)")
PY

if [[ "$WITH_WAKE" -eq 1 ]]; then
  DAY="$(date +%Y-%m-%d)"
  SRC="$STATE/debug/wake/$DAY"
  if [[ ! -d "$SRC" ]]; then
    # fall back to newest day dir
    SRC="$(ls -1d "$STATE"/debug/wake/*/ 2>/dev/null | sort | tail -1 || true)"
  fi
  if [[ -z "${SRC:-}" || ! -d "$SRC" ]]; then
    echo "no wake snips under $STATE/debug/wake" >&2
  else
    echo "copying wake snips from $SRC"
    python3 - "$SRC" "$VOICE_LIVE" "$ROOT/fixtures/voice/wake/cases.jsonl" <<'PY'
import json
import re
import shutil
import sys
from pathlib import Path

src, dest, cases_path = map(Path, sys.argv[1:4])
dest.mkdir(parents=True, exist_ok=True)

def slug(text: str, matched: bool) -> str:
    t = re.sub(r"[^a-z0-9]+", "-", (text or "empty").lower()).strip("-")[:40] or "empty"
    return f"{t}-{'hit' if matched else 'miss'}"

cases = []
for meta_path in sorted(src.glob("*.json")):
    meta = json.loads(meta_path.read_text())
    wav_src = meta_path.with_suffix(".wav")
    if not wav_src.is_file():
        continue
    name = slug(meta.get("text") or "", bool(meta.get("matched")))
    # avoid overwrite collisions
    stem = name
    n = 2
    while (dest / f"{stem}.wav").exists() and (dest / f"{stem}.json").read_text() != "":
        # if same content text, skip; else disambiguate
        existing = json.loads((dest / f"{stem}.json").read_text())
        if existing.get("text") == meta.get("text") and existing.get("matched") == meta.get("matched"):
            stem = None
            break
        stem = f"{name}-{n}"
        n += 1
    if stem is None:
        continue
    wav_dst = dest / f"{stem}.wav"
    meta_dst = dest / f"{stem}.json"
    shutil.copy2(wav_src, wav_dst)
    out = dict(meta)
    out["wav"] = f"fixtures/voice/wake/live/{stem}.wav"
    out["fixture_id"] = stem
    out["source"] = f"export:{src.name}"
    meta_dst.write_text(json.dumps(out, indent=2) + "\n")
    cases.append({
        "id": stem,
        "wav": f"live/{stem}.wav",
        "meta": f"live/{stem}.json",
        "vosk_text": meta.get("text"),
        "expect_match": bool(meta.get("matched")),
        "expect_phrase_contains": (
            "herald" if meta.get("phrase") and "herald" in str(meta.get("phrase"))
            else ("hark" if meta.get("matched") else None)
        ),
        "notes": f"Exported from {src}; phrase={meta.get('phrase')!r}",
    })
    print(f"  {stem}")

if cases:
    # merge with existing cases by id
    existing = {}
    if cases_path.is_file():
        for line in cases_path.read_text().splitlines():
            if line.strip():
                o = json.loads(line)
                existing[o["id"]] = o
    for c in cases:
        # drop null expect_phrase_contains for cleaner json
        if c.get("expect_phrase_contains") is None:
            c.pop("expect_phrase_contains", None)
        existing[c["id"]] = c
    cases_path.write_text(
        "\n".join(json.dumps(existing[k], separators=(",", ":")) for k in sorted(existing)) + "\n"
    )
    print(f"updated {cases_path} ({len(existing)} cases)")
PY
  fi
fi

# Refresh MANIFEST hashes
python3 - "$ROOT" <<'PY'
import hashlib
import json
import sys
import time
from pathlib import Path

root = Path(sys.argv[1])
fix = root / "fixtures"
entries = []
for path in sorted(fix.rglob("*")):
    if not path.is_file():
        continue
    if path.name == "MANIFEST.json":
        continue
    rel = path.relative_to(fix).as_posix()
    data = path.read_bytes()
    entries.append({
        "path": rel,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    })
manifest = {
    "schema": "hark.fixtures.manifest.v1",
    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "count": len(entries),
    "files": entries,
}
(fix / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
print(f"MANIFEST.json: {len(entries)} files")
PY

echo "export complete → $ROOT/fixtures"

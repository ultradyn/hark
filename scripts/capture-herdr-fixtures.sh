#!/usr/bin/env bash
# Capture live Herdr agent-list (+ optional hark watch HEP) into fixtures/herdr/
# with path redaction. Safe to re-run; overwrites generated JSON/JSONL.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/fixtures/herdr"
mkdir -p "$OUT"

if ! command -v herdr >/dev/null 2>&1; then
  echo "herdr not found on PATH" >&2
  exit 1
fi

RAW="$(mktemp)"
trap 'rm -f "$RAW"' EXIT

echo "capturing herdr agent list..."
herdr agent list >"$RAW"

python3 - "$RAW" "$OUT" <<'PY'
import json, re, sys
from datetime import datetime, timezone
from pathlib import Path

raw_path, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
raw = json.loads(raw_path.read_text(encoding="utf-8"))
HOME_RE = re.compile(r"/home/[^/]+")

def redact(o):
    if isinstance(o, dict):
        return {k: redact(v) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x) for x in o]
    if isinstance(o, str):
        s = HOME_RE.sub("/home/operator", o)
        if len(s) > 80 and re.search(r"(sk-|api[_-]?key|token|secret|bearer)", s, re.I):
            return "[REDACTED]"
        return s
    return o

data = redact(raw)
agents = (data.get("result") or data).get("agents") or []
for a in agents:
    if isinstance(a, dict) and "revision" in a:
        try:
            a["revision"] = int(a.get("revision") or 0)
        except (TypeError, ValueError):
            a["revision"] = 0

def wrap(agents_list):
    return {
        "id": "cli:agent:list",
        "result": {"agents": agents_list, "type": "agent_list"},
        "_fixture": {
            "source": "live herdr agent list (redacted)",
            "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    }

out_dir.mkdir(parents=True, exist_ok=True)
(out_dir / "agent-list-mixed.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
(out_dir / "agent-list-empty.json").write_text(json.dumps(wrap([]), indent=2) + "\n", encoding="utf-8")
blocked = [a for a in agents if isinstance(a, dict) and a.get("agent_status") == "blocked"]
working = [a for a in agents if isinstance(a, dict) and a.get("agent_status") == "working"]
idle = [a for a in agents if isinstance(a, dict) and a.get("agent_status") == "idle"][:3]
(out_dir / "agent-list-blocked.json").write_text(json.dumps(wrap(blocked), indent=2) + "\n", encoding="utf-8")
(out_dir / "agent-list-working.json").write_text(json.dumps(wrap(working), indent=2) + "\n", encoding="utf-8")
(out_dir / "agent-list-idle-sample.json").write_text(json.dumps(wrap(idle), indent=2) + "\n", encoding="utf-8")
print(f"wrote agent lists: total={len(agents)} blocked={len(blocked)} working={len(working)}")
if not blocked:
    print("warning: no blocked agents in live capture", file=sys.stderr)
if not working:
    print("warning: no working agents in live capture", file=sys.stderr)
PY

# HEP watch stream sample (optional)
WATCH="${XDG_STATE_HOME:-$HOME/.local/state}/hark/watch.jsonl"
if [[ -f "$WATCH" ]]; then
  python3 - "$WATCH" "$OUT" <<'PY'
import json, re, sys
from datetime import datetime, timezone
from pathlib import Path
src, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
HOME_RE = re.compile(r"/home/[^/]+")

def redact(o):
    if isinstance(o, dict):
        return {k: redact(v) for k, v in o.items()}
    if isinstance(o, list):
        return [redact(x) for x in o]
    if isinstance(o, str):
        s = HOME_RE.sub("/home/operator", o)
        if len(s) > 400:
            s = s[:400] + "…[truncated]"
        return s
    return o

wanted = {"agent.blocked", "agent.completed", "agent.state_changed", "watch.armed", "watch.heartbeat"}
picked, counts = [], {}
for line in reversed(src.read_text(encoding="utf-8").splitlines()):
    try:
        o = json.loads(line)
    except json.JSONDecodeError:
        continue
    k = o.get("kind")
    if k not in wanted:
        continue
    n = counts.get(k, 0)
    if n >= (2 if k != "watch.heartbeat" else 1):
        continue
    counts[k] = n + 1
    picked.append(redact(o))
    if sum(counts.values()) >= 10:
        break
picked.reverse()
meta = {
    "kind": "fixture.meta",
    "source": "watch.jsonl redacted",
    "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
lines = [json.dumps(meta, separators=(",", ":"))]
lines += [json.dumps(o, separators=(",", ":")) for o in picked]
text = "\n".join(lines) + "\n"
(out_dir / "watch-stream-blocked.jsonl").write_text(text, encoding="utf-8")
(out_dir / "watch-stream-hep.jsonl").write_text(text, encoding="utf-8")
print(f"wrote watch HEP samples: {len(picked)} events")
PY
else
  echo "no $WATCH — skipping HEP watch stream capture" >&2
fi

# Keep stable synthetic wire envelopes if missing
WIRE="$OUT/watch-stream-wire.jsonl"
if [[ ! -f "$WIRE" ]]; then
  cat >"$WIRE" <<'EOF'
{"id":"ev1","method":"events.notification","params":{"event":{"type":"pane.agent_status_changed","pane_id":"w1:p6","agent":"agy","agent_status":"blocked","workspace_id":"w1","tab_id":"w1:t1"}}}
{"id":"ev2","method":"events.notification","params":{"event":{"type":"pane.agent_status_changed","pane_id":"w2:p1","agent":"grok","agent_status":"working","workspace_id":"w2","tab_id":"w2:t1"}}}
{"type":"pane.agent_status_changed","pane_id":"w2:p1","agent":"grok","agent_status":"idle"}
{"params":{"event":{"type":"pane.closed","pane_id":"w9:p1"}}}
EOF
  echo "wrote default watch-stream-wire.jsonl"
fi

echo "done → $OUT"
ls -la "$OUT"

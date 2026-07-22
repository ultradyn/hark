# Example — handsfree with Hark

## Operator

> Load hark. I’m reimaging a box. Keep Herdr agents unblocked by voice (local + workbox).

## Agent

```text
1. uv run hark doctor
2. Monitor(persistent, command="hark monitor --for-monitor")
```

The Monitor tool is native in Claude Code / Grok. On Pi use [`pi-monitor`](https://github.com/clankercode/pi-monitor) (`pi install npm:pi-monitor`); on OpenCode use [`opencode-monitor-bg`](https://github.com/clankercode/opencode-monitor-bg); on Antigravity (`agy`) use agentapi inject.

## Blocked event (monitor line)

```json
{
  "schema": "hark.event.v1",
  "kind": "agent.blocked",
  "event_id": "19f5b4dc27a4eacb1c13d843a61",
  "observed_at": "2026-07-13T11:47:30.554Z",
  "session_id": "default",
  "agent": "agy",
  "pane_id": "w1:p6",
  "status_to": "blocked",
  "question": "\nDo you want to proceed?\n> 1. Yes\n  2. Yes, and always allow in this conversation for commands that start with 'python3 tools/ci/codegen-c2c-skills.py'\n  3. Yes, and always allow for commands that start with 'python3 tools/ci/codegen-c2c…",
  "risk": "R2",
  "fingerprint": "blake2b:6e4fda9a686a5829013036219f3019be",
  "instructions": "Use the hark skill; do not invent an answer. hark context default/w1:p6"
}
```

(Real line lifted from `fixtures/herdr/watch-stream-hep.jsonl`.)

## Handle

```bash
hark context default/w1:p6 --lines 40
# 1. Yes  2. No — permission-like

hark ask --confirm always "Work agy asks: proceed with the command? Yes or no."
# human: "Yes"

hark answer 19f5b4dc27a4eacb1c13d843a61 --keys 1 enter
# if free text policy: hark answer 19f5b4dc27a4eacb1c13d843a61 --text "yes"
```

## Done (judge)

```bash
hark context local/w2:p1 --lines 40
# still running bg jobs → stay quiet
```

## Tear down

Stop Monitor when back at the keyboard.  

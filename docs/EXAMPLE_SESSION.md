# Example — Mode A with Hark

## Operator

> Load hark. I’m reimaging a box. Keep Herdr agents unblocked by voice (local + workbox).

## Agent

```text
1. uv run hark doctor
2. Monitor(persistent, command="hark watch --for-monitor --statuses blocked,done")
```

## Blocked event (monitor line)

```json
{
  "schema": "hark.event.v1",
  "kind": "agent.blocked",
  "event_id": "01JXYZ",
  "session_id": "work",
  "pane_id": "w1:p6",
  "agent": "agy",
  "question": "Do you want to proceed?",
  "risk": "R2",
  "instructions": "Use the hark skill; do not invent an answer."
}
```

## Handle

```bash
hark context work/w1:p6 --lines 40
# 1. Yes  2. No — permission-like

hark ask --confirm always "Work agy asks: proceed with the command? Yes or no."
# human: "Yes"

hark answer 01JXYZ --keys 1 enter
# if free text policy: hark answer 01JXYZ --text "yes"
```

## Done (judge)

```bash
hark context local/w2:p1 --lines 40
# still running bg jobs → stay quiet
```

## Tear down

Stop Monitor when back at the keyboard.  

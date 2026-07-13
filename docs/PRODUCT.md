# Product goals — Hark

## Problem

Coding agents in Herdr stop for human input. The operator is often away from the keyboard (machine setup, shop work) but can hear and speak. Multiple agents and multiple Herdr sessions (local + remote) make polling terminals impractical.

## Goal

**Hark** — when your agents need a word.

Software (`hark` / optional `harkd`) + skill (`hark`) so a human can unblock and supervise Herdr agents entirely by voice, with cloud STT/TTS and **race-safe delivery**.

## Primary stories

1. Blocked agent → spoken question → spoken answer → safe deliver → continue.  
2. Permission prompt → always confirm → keys or text.  
3. Many blocked agents → priority queue; one answer window at a time.  
4. Multi-session (laptop + workbox) → one feed, tagged targets.  
5. False Herdr `done` → Mode A peeks context → only announce if real.  
6. Other models implement from SPEC alone.  

## Non-goals (v1)

- Local neural STT/TTS  
- Playwright consumer-dictation as primary  
- Biometric speaker ID  
- Full-duplex barge-in  
- Autonomous approval without the human  
- Public network exposure of control plane  

## Success metrics

| Metric | Target |
|--------|--------|
| Blocked → TTS start | < 2 s median (ex-network) |
| End-of-speech → transcript | < 1.5 s after provider final |
| Idle watch overhead | low CPU/RSS (see SPEC) |
| False ambient send | near zero |
| Stale double-send | zero (library enforcement) |
| Skill usability | Mode A from skill alone |

## Brand

Tagline: **When your agents need a word.**  
Full branding notes: [NAMING.md](NAMING.md), original verse in prior `HARK-README-INTRO.md`.  

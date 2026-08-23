---
name: handsfree
description: >
  Alias of the hark skill (identical handsfree voice bridge). Voice Q&A with you,
  or watch Herdr agents and speak their blocked questions. After load, communicate
  primarily via TTS; cloud STT/TTS; reply with text or menu keys.
  Use when operator says handsfree, hark, or voice bridge. Requires `hark` CLI.
---

# Handsfree — alias for Hark

> Alias of [`hark`](../hark/SKILL.md). Same product, CLI, TTS mode, loop.

Follow **all** of [`../hark/SKILL.md`](../hark/SKILL.md). It is the only source of truth — this file adds nothing.

Do not re-derive the startup from memory. The parts agents most often get wrong:

- **Start** — infer scope/mode/role/voice and announce in one TTS line. No interview, no questions.
- **Daemon by exception** — `hark ask` / `hark tts` need no workers. Arm `hark start` + one Monitor only for `herdr` scope or wake-word input.
- **Voice follows phase** — vocal while planning, quiet while executing except blocker / decision / done.
- **B141** — emit the question verbatim in visible chat *before* the blocking call.
- **CLI missing?** [`../hark/POST_INSTALL.md`](../hark/POST_INSTALL.md) — `npx skills` does not install Python.
- Never pipe interactive `hark` to `| tail` (B109). Safe targeting: prefer `hark answer <event_id>`.
- Dogfood: every friction → `bl bug`, never a silent workaround.

Do not invent answers. Speak, listen, deliver.

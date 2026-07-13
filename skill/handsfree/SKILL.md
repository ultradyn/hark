---
name: handsfree
description: >
  Alias of the hark skill (identical handsfree voice bridge). After load, communicate
  primarily via TTS; watch blocked/done, speak questions, cloud STT/TTS, reply with
  text or menu keys. Use when operator says handsfree, hark, or voice bridge.
  Requires `hark` CLI and Herdr ≥ 0.7.1.
---

# Handsfree — alias for Hark

> **This skill is an alias of [`hark`](../hark/SKILL.md).** Same product, CLI (`hark`), TTS mode, and loop. Install either or both names.

Follow **all** instructions in `skill/hark/SKILL.md`, including:

- **TTS mode** after skill start (prefer `hark tts --listen` / `hark ask` for operator communication)
- Voice-first setup (sessions, preferences)
- Question → listen → act loop
- **Required unified Monitor** on skill start: **one** `hark monitor --for-monitor` (`persistent: true`). Do not arm a second if one is already live (CLI singleflight refuses). Ambient-only or watch-only is incomplete.
- **Antigravity (`agy`):** experimental — no native Monitor; use `hark agentapi register` + `hark agentapi deliver --follow-monitor` (see `docs/AGY.md` and the **Antigravity** section in `skill/hark/SKILL.md`).
- Radio **partials**: **must** `hark listen-end` when partial clearly ends with a done signal (`over`, `okay hark send`, `that's all`, …); **must** `hark listen-end --cancel` when unrelated conversation / TTS bleed is being forwarded; remind operators to say over / okay hark send
- Safe targeting / confirm rules
- **Dogfooding:** every friction → session todo + `bl bug`; fix now or later, never silent forget

Do not invent answers. Speak, listen, deliver.

---
name: hark
description: >
  Compatibility shim. Prefer skill/hark/ or skill/handsfree/. Hands-free voice
  bridge for Herdr agents. Requires `hark` CLI and Herdr ≥ 0.7.1.
---

# Compatibility entry

This file exists so tools that expect `skill/SKILL.md` still find **Hark**.

**Canonical skills:**

| Path | Name |
|------|------|
| [`hark/SKILL.md`](hark/SKILL.md) | **hark** (primary) |
| [`handsfree/SKILL.md`](handsfree/SKILL.md) | **handsfree** (alias) |

Load either. Full Mode A instructions live in those files (identical loop; CLI is always `hark`).

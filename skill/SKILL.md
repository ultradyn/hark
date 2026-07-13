---
name: hark-shim
description: >
  Compatibility shim only — not for install. Prefer skill/hark/ or
  skill/handsfree/ (or monorepo skills/ for npx skills). Hands-free voice
  bridge for Herdr agents. Requires `hark` CLI and Herdr ≥ 0.7.1.
metadata:
  internal: true
---

# Compatibility entry

This file exists so tools that expect `skill/SKILL.md` still find **Hark**.

**Do not install this shim** (`npx skills` marks it internal). Use the real skills:

| Path | Name |
|------|------|
| [`hark/SKILL.md`](hark/SKILL.md) | **hark** (primary) |
| [`handsfree/SKILL.md`](handsfree/SKILL.md) | **handsfree** (alias) |
| [`../skills/`](../skills/) | Same skills for `npx skills add` discovery |

Load either. Full Mode A instructions live in those files (identical loop; CLI is always `hark`).

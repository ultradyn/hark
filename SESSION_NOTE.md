# Session note — Hark

**Canonical path:** `/home/xertrov/src/grok/hark`  
**Date:** 2026-07-13

## Dogfooding rule

Any problem while operating Hark is a chance to improve it. Always:

1. Add a **todo** for the issue in the agent session list.  
2. File a durable **`bl bug`** when it should outlive the session.  
3. Fix immediately if small; otherwise continue current work and return later.

Skill text: `skill/hark/SKILL.md` § Dogfooding.

---

## Pre-compact handoff (2026-07-13 ~22:50)

### User directive (binding) — B030 OG image

**B030 OG social image must be made with `~/.llm-general/skills/`**, not hand-rolled SVG alone:

| Skill | Role |
|-------|------|
| **`og-social-previews`** | HTML card design + meta tags + e2e workflow |
| **`headless-browser-screenshots`** | Render 1200×630 via `scripts/screenshot.mjs` |
| **`visual-review-and-fix`** | Vision + geometry PASS before ship |

Pattern: design `site/og-image.html` → screenshot → `site/og.png` → VR&F → absolute `og:*` / `twitter:*`.

- Starters: `~/.llm-general/skills/og-social-previews/templates/`
- Reference (study, don't copy brand): `~/src/c2c/docs/assets/og-image.html`

### B030 current state (needs skill redo)

- Meta tags + image already on master: `6e93c23` + `266c00b bl done B030`
- Live tags point at `https://hark.xk.io/og.png` (1200×630)
- Assets today: `site/og.svg` + rasterized `site/og.png` — **not** the skill pipeline (no HTML card source, no headless-browser-screenshots, no VR&F loop)
- **Follow-up after compact:** rebuild card via skill stack; keep/adjust meta; HTML as source of truth

### bl-fix-many (B036–B039, B041; skip B040)

Worktrees: `~/.cache/hark-worktrees/{B036,B037,B038,B039,B041}`  
Parent master ~ahead origin 62+

| ID | Status at compact | Notes |
|----|-------------------|--------|
| B030 | merged; **redo image** | skill stack required |
| B036 | in progress | worktree dirty: config_watch, ambient, lifecycle |
| B037 | **ready merge** | `fix/B037` @ `e6be3f3` — `radio_partial_silence_s` |
| B038 | **ready merge** | `fix/B038` @ `683907e` — `stt.request`/`stt.response` |
| B039 | in progress | worktree dirty: soft end + fixtures + docs |
| B041 | likely ready | `fix/B041` @ `64c020d` — `hark watch-logs` |
| B040 | skip | already done earlier |

Subagent IDs:
- B037: `019f5b84-6132-79a0-b1da-82c101c91c8a`
- B038: `019f5b84-6133-70c3-ad02-9d79af6492c7`
- B036: `019f5b84-6132-79a0-b1da-82b323fdb87e`
- B039: `019f5b84-6133-70c3-ad02-9d81808ead02`
- B041: `019f5b84-6135-7a23-9ade-b8895041451f`

### Post-compact checklist

1. Collect B036 / B039 / B041 agent results  
2. Review + merge each `fix/B0xx` → master; `bl done`  
3. **Redo B030 image** with og-social-previews stack  
4. Re-arm `hark monitor --for-monitor` if needed  
5. Leave agy alone  
6. Optional: `~/.cache/hark/tts` for TTS cache  

### Product context (recent)

Wake names/phrases + learn; radio end_mode; surface_timeouts; lead silence 117ms; unified monitor; always commit.

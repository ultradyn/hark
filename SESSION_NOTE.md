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

### Post-compact completed (2026-07-13)

| ID | Result |
|----|--------|
| **B030 OG redo** | `site/og-image.html` → skill render → `site/og.png` (1200×630); meta tags kept; `og.svg` removed; docs in `site/README.md` + `67da03e` |
| B036 | merged + `bl done` — config.toml mtime watch |
| B037 | merged + `bl done` — `radio_partial_silence_s` |
| B038 | merged + `bl done` — `stt.request`/`stt.response` |
| B039 | merged + `bl done` — soft end default on (`send it`, sentence-final `over`) |
| B041 | merged + `bl done` — `hark watch-logs` |

Skill stack used for OG: `og-social-previews` + `headless-browser-screenshots` (+ visual crop-zoom review).

Optional later: re-arm `hark monitor --for-monitor`; `~/.cache/hark/tts`; leave agy alone.

# Hark static site

Marketing / docs landing for [ultradyn/hark](https://github.com/ultradyn/hark).

## Design system

| File | Role |
|------|------|
| `css/tokens.css` | Colors, type, space, radii — **edit first** |
| `css/base.css` | Reset, body, atmosphere |
| `css/components.css` | Buttons, cards, terminal, flow, nav, verse |
| `css/layout.css` | Hero, grids, sections |
| `js/main.js` | Nav scroll (+ optional `#wave` canvas) |
| `index.html` | Marketing SPA; hero RHS = SVG architecture diagram |
| `setup.html` | Dedicated first-run setup checklist (doctor → setup → session-profile → start) |
| `llms.txt` | LLM-oriented doc index (includes Setup) |

Change brand colors or type scale in **tokens only**; components consume `var(--…)`.

**Product links:** wrap partner names (e.g. Herdr) in
`<a class="product-link" href="https://herdr.dev/">Herdr</a>` so they inherit local
text color and underline cleanly in body, eyebrow, and verse.

### Typography (B042)

| Role | Family | Use |
|------|--------|-----|
| **Display** | [Fraunces](https://fonts.google.com/specimen/Fraunces) | Wordmark, headings — soft optical serif |
| **Body** | [Source Sans 3](https://fonts.google.com/specimen/Source+Sans+3) | UI, lede, longform |
| **Mono** | [JetBrains Mono](https://fonts.google.com/specimen/JetBrains+Mono) | Terminal, code, chips, URLs |

Loaded from Google Fonts with `preconnect` + `display=swap` in `index.html` (and
`og-image.html` for social renders). Tokens keep system fallbacks if the network
is blocked. Edit stacks in `css/tokens.css`.

**Performance:** static SVG diagram (no canvas loop), minimal CSS/JS; three
webfont families only. Prefer editing tokens over adding dependencies.

### Compatible-agent logos (B048 / B052)

Local monochrome SVGs live in **`assets/logos/`** (dark-bg fills, no CDN).
Marks are simplified official brand glyphs (not generic placeholders):

| File | Agent | Source |
|------|--------|--------|
| `claude.svg` | Claude Code | Anthropic Claude aster |
| `grok.svg` | Grok Build | grok.com singularity mark |
| `antigravity.svg` | Antigravity | Google Antigravity arch |
| `pi.svg` | Pi | pi.dev block P+i mark |
| `opencode.svg` | OpenCode | opencode.ai/brand O mark |
| `codex.svg` | Codex / others | OpenAI Codex mark |

Homepage `#supports` strip order matches the primary table rows:
**Claude Code → Grok Build → Antigravity → Pi → OpenCode → Codex**.
Marketing UI uses product names only (no CLI binary aliases).

**Adding a logo**

1. Drop a square SVG in `assets/logos/` (viewBox as provided by brand; 24×24
   common). Use light fills (`#eef1f7` / muted accents) so marks read on
   `--bg-void`.
2. Wire it in `index.html` — strip item + table row (`alt` on strip images;
   decorative `alt=""` + adjacent name in the table). Keep strip order aligned
   with the table.
3. Prefer local files over hotlinked brand CDNs (broken/offline-proof).
4. Keep names short for mobile; the table wraps in `.supports-table-wrap` for
   horizontal scroll when needed.

## Open Graph / Twitter card

Source of truth: **`og-image.html`** (fixed 1200×630). Rendered PNG: **`og.png`**.

Uses the skill stack under `~/.llm-general/skills/`:

1. **`og-social-previews`** — card design + meta workflow  
2. **`headless-browser-screenshots`** — Playwright render  
3. **`visual-review-and-fix`** — vision/geometry polish before ship  

```bash
# From repo root
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
node "$HOME/.llm-general/skills/headless-browser-screenshots/scripts/screenshot.mjs" \
  --url site/og-image.html \
  --out site/og.png \
  --width 1200 --height 630
```

`index.html` points `og:image` / `twitter:image` at `https://hark.xk.io/og.png`.
Do not screenshot the live homepage for social previews — edit the HTML card and re-render.

## Local preview

```bash
cd site && python3 -m http.server 8765
# open http://127.0.0.1:8765
```

## Deploy

GitHub Actions (`.github/workflows/pages.yml`) publishes the site on **version tags only**
(`v*`, same cadence as npm releases). The artifact is:

- contents of `site/`
- plus root **`install.sh`** → served as **https://hark.xk.io/install.sh**

So the bash one-liner always matches the tagged release tree. Manual redeploy:
Actions → “Deploy site to GitHub Pages” → Run workflow.

Enable Pages → Source: **GitHub Actions** in repo settings if needed.

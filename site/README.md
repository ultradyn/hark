# Hark static site

Marketing / docs landing for [clankercode/hark](https://github.com/clankercode/hark).

## Design system

| File | Role |
|------|------|
| `css/tokens.css` | Colors, type, space, radii — **edit first** |
| `css/base.css` | Reset, body, atmosphere |
| `css/components.css` | Buttons, cards, terminal, flow, nav, verse |
| `css/layout.css` | Hero, grids, sections |
| `js/main.js` | Nav scroll (+ optional `#wave` canvas) |
| `index.html` | Single-page composition; hero RHS = SVG architecture diagram |

Change brand colors or type scale in **tokens only**; components consume `var(--…)`.

**Product links:** wrap partner names (e.g. Herdr) in
`<a class="product-link" href="https://herdr.dev/">Herdr</a>` so they inherit local
text color and underline cleanly in body, eyebrow, and verse.

**Performance:** system font stacks only (no Google Fonts), static SVG diagram (no
canvas loop), minimal CSS/JS. Prefer editing tokens over adding dependencies.

## Local preview

```bash
cd site && python3 -m http.server 8765
# open http://127.0.0.1:8765
```

## Deploy

GitHub Actions (`.github/workflows/pages.yml`) publishes `site/` to GitHub Pages.
Enable Pages → Source: **GitHub Actions** in repo settings if needed.

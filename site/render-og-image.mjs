// Thin project-local OG renderer — shells out to headless-browser-screenshots.
//
//   node site/render-og-image.mjs
//
// Writes site/og.png (1200×630) from sibling og-image.html.
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { homedir } from "node:os";

const here = dirname(fileURLToPath(import.meta.url));
const html = resolve(here, "og-image.html");
const out = resolve(here, "og.png");
const skillScript = resolve(
  homedir(),
  ".llm-general/skills/headless-browser-screenshots/scripts/screenshot.mjs"
);

const env = {
  ...process.env,
  PLAYWRIGHT_BROWSERS_PATH:
    process.env.PLAYWRIGHT_BROWSERS_PATH || `${homedir()}/.cache/ms-playwright`,
};

const r = spawnSync(
  process.execPath,
  [
    skillScript,
    "--url",
    html,
    "--out",
    out,
    "--width",
    "1200",
    "--height",
    "630",
    "--delay",
    "200",
  ],
  { stdio: "inherit", env }
);

process.exit(r.status ?? 1);

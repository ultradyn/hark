#!/usr/bin/env node
/**
 * Copy monorepo skill/{hark,handsfree}/SKILL.md into package skills/.
 *
 * Source of truth: monorepo skill/ (also exposed as skills/ for npx skills).
 * Runs on prepack / CI. When monorepo skill/ is absent (e.g. published tarball
 * extract), keeps existing packaged skills/ and exits 0.
 *
 * Set HARK_SYNC_REQUIRED=1 to fail if monorepo skill/ is missing.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkgRoot = path.resolve(here, "..");
const monorepoRoot = path.resolve(pkgRoot, "../..");
const src = path.join(monorepoRoot, "skill");
const dest = path.join(pkgRoot, "skills");
const required = process.env.HARK_SYNC_REQUIRED === "1";
const names = ["hark", "handsfree"];

function fail(msg) {
  console.error(`sync-skills: ${msg}`);
  process.exit(1);
}

if (!fs.existsSync(path.join(src, "hark", "SKILL.md"))) {
  if (required) {
    fail(`monorepo skill/ not found at ${src} (HARK_SYNC_REQUIRED=1)`);
  }
  console.log("sync-skills: monorepo skill/ not found — keeping packaged skills/");
  process.exit(0);
}

// Extra markdown docs shipped beside SKILL.md (setup / local STT).
const extraDocs = ["SETUP.md", "WAKE_STT.md"];

for (const name of names) {
  const from = path.join(src, name, "SKILL.md");
  if (!fs.existsSync(from)) fail(`missing ${from}`);
  const text = fs.readFileSync(from, "utf8");
  if (!text.startsWith("---")) fail(`${name}: SKILL.md missing YAML frontmatter`);
  if (!new RegExp(`^name:\\s*${name}\\s*$`, "m").test(text)) {
    fail(`${name}: frontmatter name must be "${name}"`);
  }
  if (!/^description:\s*>?/m.test(text)) {
    fail(`${name}: frontmatter missing description`);
  }
  const toDir = path.join(dest, name);
  fs.mkdirSync(toDir, { recursive: true });
  fs.copyFileSync(from, path.join(toDir, "SKILL.md"));
  console.log(`sync-skills: ${name}`);
  // Package mirrors for hark skill docs (handsfree may omit extras)
  for (const doc of extraDocs) {
    const docFrom = path.join(src, name, doc);
    if (fs.existsSync(docFrom)) {
      fs.copyFileSync(docFrom, path.join(toDir, doc));
      console.log(`sync-skills: ${name}/${doc}`);
    }
  }
}

// Sanity: packaged layout must be discoverable by npx skills (skills/*/SKILL.md).
for (const name of names) {
  const p = path.join(dest, name, "SKILL.md");
  if (!fs.existsSync(p)) fail(`after sync missing ${p}`);
}
console.log("sync-skills: ok");

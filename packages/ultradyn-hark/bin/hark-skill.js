#!/usr/bin/env node
/**
 * Print install paths for hark / handsfree skills and optional install hints.
 * Skills live under this package's skills/ directory.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkgRoot = path.resolve(here, "..");
const skills = path.join(pkgRoot, "skills");

const args = process.argv.slice(2);
const cmd = args[0] || "path";

function listSkills() {
  return fs
    .readdirSync(skills, { withFileTypes: true })
    .filter((d) => d.isDirectory())
    .map((d) => d.name)
    .filter((n) => fs.existsSync(path.join(skills, n, "SKILL.md")));
}

if (cmd === "path" || cmd === "paths") {
  for (const n of listSkills()) {
    console.log(path.join(skills, n));
  }
  process.exit(0);
}

if (cmd === "list") {
  for (const n of listSkills()) console.log(n);
  process.exit(0);
}

if (cmd === "help" || cmd === "-h" || cmd === "--help") {
  console.log(`@ultradyn/hark — agent skills for Hark (handsfree Mode A)

Usage:
  hark-skill path          Print absolute paths to skill dirs
  hark-skill list          List skill names (hark, handsfree)
  hark-skill help

Install skills into agents (recommended):
  npx skills add ultradyn/hark -g -y
  # or from this package after npm i -g @ultradyn/hark:
  npx skills add "$(hark-skill path | head -1)/.." -g -y

CLI (separate Python package):
  See https://github.com/ultradyn/hark and https://hark.xk.io
`);
  process.exit(0);
}

console.error(`unknown command: ${cmd} (try: hark-skill help)`);
process.exit(2);

#!/usr/bin/env node
/** Copy skill/ from monorepo root into package skills/ when available. */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkgRoot = path.resolve(here, "..");
const monorepoRoot = path.resolve(pkgRoot, "../..");
const src = path.join(monorepoRoot, "skill");
const dest = path.join(pkgRoot, "skills");

if (!fs.existsSync(path.join(src, "hark", "SKILL.md"))) {
  console.log("sync-skills: monorepo skill/ not found — keeping packaged skills/");
  process.exit(0);
}

for (const name of ["hark", "handsfree"]) {
  const from = path.join(src, name, "SKILL.md");
  const toDir = path.join(dest, name);
  fs.mkdirSync(toDir, { recursive: true });
  fs.copyFileSync(from, path.join(toDir, "SKILL.md"));
  console.log(`sync-skills: ${name}`);
}

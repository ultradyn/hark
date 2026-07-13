# Changelog

All notable changes to **Hark** and the **`@ultradyn/hark`** npm skills package.

Format: sections headed `## X.Y.Z` match git tags `vX.Y.Z` and the npm package version in `packages/ultradyn-hark/package.json`.

## Unreleased

- Optional TTS/listen overlap pre-arm (`audio.overlap_prearm`, `overlap_discard_ms`): start
  capture near TTS end while discarding audio until TTS ends + residual (B004). Half-duplex
  remains the default.

## 0.1.3

- Skills discovery: monorepo `skills/` symlinks for `npx skills`; internal skill shim.
- Harden package skill sync (`HARK_SYNC_REQUIRED`, frontmatter checks).
- CI: `npm-package.yml` validates pack + skills list on skill/package changes.
- Near-miss wake monitor events (B019), custom wake SIGHUP reload (B020).
- Homepage install picker bash/npm/pnpm/bun (B023).
- Repo transfer prep tooling (B024).

## 0.1.2

- Automated release via GitHub Actions `release.yml` (OIDC trusted publishing, no NPM_TOKEN).
- Package validation gate (skills + `npm pack --dry-run`) before publish.
- Site install picker (bash/npm/pnpm/bun) on hark.xk.io (docs surface; not in tarball).

## 0.1.1

- Polish npm package README for npmjs.com (skills vs CLI, install via npm/pnpm/bun, `install.sh`).
- Release pipeline: GitHub Actions `release.yml` with OIDC trusted publishing (no `NPM_TOKEN`).

## 0.1.0

- Initial `@ultradyn/hark` skills package (`hark` + `handsfree` skills, `hark-skill` bin).
- `npx skills add clankercode/hark` documented as the recommended skill install path.

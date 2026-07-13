# Changelog

All notable changes to **Hark** and the **`@ultradyn/hark`** npm skills package.

Format: sections headed `## X.Y.Z` match git tags `vX.Y.Z` and the npm package version in `packages/ultradyn-hark/package.json`.

## 0.1.1

- Polish npm package README for npmjs.com (skills vs CLI, install via npm/pnpm/bun, `install.sh`).
- Release pipeline: GitHub Actions `release.yml` with OIDC trusted publishing (no `NPM_TOKEN`).

## 0.1.0

- Initial `@ultradyn/hark` skills package (`hark` + `handsfree` skills, `hark-skill` bin).
- `npx skills add clankercode/hark` documented as the recommended skill install path.

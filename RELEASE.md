# Release process — Hark / `@ultradyn/hark`

Releases are automated by CI. Pushing a `vX.Y.Z` tag triggers
[`.github/workflows/release.yml`](.github/workflows/release.yml), which:

1. Syncs monorepo `skill/` into `packages/ultradyn-hark/skills/`
2. Verifies the tag matches `packages/ultradyn-hark/package.json` `version`
3. Runs the Python test gate (`pytest`)
4. Publishes **`@ultradyn/hark`** to npm via **OIDC trusted publishing**
   (no `NPM_TOKEN`; build provenance attached automatically)
5. Creates a GitHub Release with auto-generated notes

The **npm package version** lives in `packages/ultradyn-hark/package.json`.
The Python CLI version is in root `pyproject.toml` and may differ until
aligned deliberately.

Site deploys separately via [`.github/workflows/pages.yml`](.github/workflows/pages.yml)
on pushes that touch `site/`.

---

## Cutting a release

From a **clean** `master` (or the integration branch you release from):

```bash
# 1. Bump the npm package only (no git tag from npm — monorepo root tags)
./scripts/release-npm.sh 0.1.2          # explicit version
# or: ./scripts/release-npm.sh patch    # patch | minor | major

# 2. Push commit + tag (the tag push is what publishes)
git push origin master
git push origin "v$(node -p "require('./packages/ultradyn-hark/package.json').version")"
```

`scripts/release-npm.sh`:

- Requires a clean working tree and branch `master`
- Bumps `packages/ultradyn-hark/package.json` via `npm version … --no-git-tag-version`
- Commits `chore(npm): release @ultradyn/hark X.Y.Z`
- Creates annotated tag `vX.Y.Z`
- Does **not** push (you push so CI runs once)

Equivalent by hand:

```bash
cd packages/ultradyn-hark
npm version 0.1.2 --no-git-tag-version
cd ../..
git add packages/ultradyn-hark/package.json
git commit -m "chore(npm): release @ultradyn/hark 0.1.2"
git tag -a v0.1.2 -m "v0.1.2"
git push origin master
git push origin v0.1.2
```

### After the tag is on GitHub (agents)

**Required:** run the **`/watch-gh-populate-release`** skill (or load
`watch-gh-populate-release`) once the version tag has been pushed.

That skill:

1. Watches `release.yml` until success (`gh run watch`)
2. Verifies the GitHub Release exists
3. Populates / refreshes release notes from `CHANGELOG.md` (and package context)

Do **not** claim the release is finished while the workflow is `in_progress` or
failed. On failure: fix, re-run if needed
(`gh workflow run release.yml -f tag=vX.Y.Z`), watch again.

Quick manual watch:

```bash
gh run list --workflow=release.yml --limit 5
gh run watch <run-id>
npm view @ultradyn/hark version
gh release view vX.Y.Z
```

---

## One-time setup: npm trusted publisher

Trusted publishing must be configured once on npm so the registry trusts this
repo’s **release** workflow (no long-lived token):

1. Open <https://www.npmjs.com/package/@ultradyn/hark/access>
   (package → **Settings** → **Trusted Publishing**).
2. Add a **GitHub Actions** publisher:
   - Organization / user: **must match the actual GitHub remote that runs this
     workflow** — still `clankercode` until the org transfer completes; switch
     (or add) `ultradyn` after `ultradyn/hark` is the live remote. Public
     install/docs URLs may already say `ultradyn/hark`; OIDC does **not**
     follow those links — it binds to the repo that executed Actions.
   - Repository: `hark`
   - Workflow filename: **`release.yml`** (must match exactly)
   - Environment: *(leave blank unless you add a GitHub Environment)*

Operator status: trusted publisher for `@ultradyn/hark` via `release.yml` is
expected to be configured against the **current** GitHub owner. If publish
fails with OIDC / provenance errors, re-check org/repo/workflow against
`git remote -v` / the Actions run URL — not against marketing URLs alone.
See [`docs/REPO_TRANSFER.md`](docs/REPO_TRANSFER.md).

---

## Changelog

Keep a top-level [`CHANGELOG.md`](CHANGELOG.md) with sections like:

```markdown
## 0.1.2

- …
```

The post-release skill prefers the `## X.Y.Z` section for GitHub Release notes.
Add the section **before** tagging when you have user-facing notes.

---

## Notes

- Tag name must equal the npm package version (`v0.1.2` ↔ `0.1.2`); CI fails
  if they disagree.
- This package is skills + `hark-skill` bin only — no TypeScript build step.
  `prepack` / the workflow syncs skills from monorepo `skill/`.
- If publish fails because the version already exists on npm, bump and tag again.
- Do **not** use `NPM_TOKEN` for routine publishes; OIDC trusted publishing is
  the supported path. The old token-based `npm-publish.yml` was removed in favor
  of `release.yml`.
- After moving the GitHub remote to `ultradyn/hark`, update the trusted publisher
  org/repo fields on npm and any links in this doc (see B024 / `docs/REPO_TRANSFER.md`
  if present).

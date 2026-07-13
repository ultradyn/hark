# Repository transfer: `clankercode/hark` → `ultradyn/hark`

**Status (prep only):** code is transfer-ready. The GitHub org move is a **human/operator** action. Do **not** flip production install URLs until `ultradyn/hark` exists and redirects work.

Related backlog: **B024**.

## Why this doc

GitHub will host the canonical repo at `ultradyn/hark`. Until the transfer completes, public one-liners and docs must keep pointing at `clankercode/hark` so `curl | bash` install does not break.

This repo includes:

| Artifact | Role |
| --- | --- |
| `install.sh` `HARK_GITHUB_REPO` / `GITHUB_REPO` | Override clone + raw URL base without rewriting the file (`HARK_GITHUB_REPO=ultradyn/hark`) |
| `scripts/rewrite-github-urls.sh` | Bulk `clankercode/hark` → `ultradyn/hark` across docs/site/npm (dry-run default) |
| This file | Operator checklist |

---

## Operator checklist (do in order)

### 1. Preflight

- [ ] Confirm you have admin on `clankercode/hark` and permission to create/own under `ultradyn`.
- [ ] Note current default branch (`master`), Pages custom domain `hark.xk.io`, and any secrets (npm OIDC / `NPM_TOKEN`, Pages).
- [ ] Ensure CI is green on the branch you will transfer (usually `master`).
- [ ] Optional: announce a short maintenance window (redirects are usually automatic; Pages may need re-check).

### 2. GitHub transfer

- [ ] GitHub UI: **Settings → General → Danger Zone → Transfer ownership** on `clankercode/hark` → org/user `ultradyn`, keep name `hark`.
  - Or: create `ultradyn/hark` and migrate (prefer official transfer so stars, issues, and redirects follow).
- [ ] Wait until `https://github.com/ultradyn/hark` loads.
- [ ] Verify **redirect**: `https://github.com/clankercode/hark` → `ultradyn/hark` (GitHub maintains redirects for renamed/transferred repos).
- [ ] Verify raw URLs still resolve via old path *or* new path:
  - `https://raw.githubusercontent.com/ultradyn/hark/master/install.sh`
  - (redirect behavior for raw.githubusercontent.com can lag; prefer updating docs after transfer).

### 3. Local remotes (each clone / worktree)

Do **not** rewrite other people's machines. On each checkout you control:

```bash
git remote -v
git remote set-url origin https://github.com/ultradyn/hark.git
# or SSH:
# git remote set-url origin git@github.com:ultradyn/hark.git
git fetch origin
git remote -v
```

Optional: keep a named remote during transition:

```bash
git remote rename origin old-clanker
git remote add origin https://github.com/ultradyn/hark.git
```

### 4. Rewrite in-repo URLs (after transfer is live)

From a clean branch off updated `master`:

```bash
# Preview
./scripts/rewrite-github-urls.sh

# Apply
./scripts/rewrite-github-urls.sh --apply

git diff
git add -A
git commit -m "chore: point GitHub URLs at ultradyn/hark after transfer"
git push origin HEAD
```

What the script touches (when present):

- `README.md`, `install.sh`
- `site/README.md`, `site/index.html`
- `packages/ultradyn-hark/{package.json,README.md,bin/hark-skill.js,skills/*/SKILL.md}`
- `skill/**/SKILL.md`, other `docs/**/*.md` (**not** this file)
- `.github/workflows/*.yml`, `SESSION_NOTE.md`

Excluded on purpose: `docs/REPO_TRANSFER.md` (keeps old→new narrative) and `scripts/rewrite-github-urls.sh`.

Also sets `install.sh` default:

```bash
GITHUB_REPO="${HARK_GITHUB_REPO:-ultradyn/hark}"
```

and keeps both `clankercode/hark` and `ultradyn/hark` in the installer’s known-origin allowlist so pre-transfer clones still auto-fetch.

**Do not run `--apply` on production-facing default branch before the GitHub repo exists.**

### 5. GitHub Pages / custom domain

- [ ] **Settings → Pages**: source still GitHub Actions (workflow `.github/workflows/pages.yml`).
- [ ] Confirm custom domain `hark.xk.io` (see `site/CNAME`) is still attached after transfer.
- [ ] DNS (if needed): GitHub Pages A/AAAA or CNAME records unchanged if domain stayed on the same Pages site.
- [ ] Force a deploy: push a no-op under `site/` or **Actions → Deploy site to GitHub Pages → Run workflow**.
- [ ] Verify `https://hark.xk.io` and HTTPS certificate.

### 6. Actions / secrets

- [ ] Re-check org/repo Actions permissions after transfer.
- [ ] npm publish workflow (`.github/workflows/npm-publish.yml`): re-validate trusted publisher / `NPM_TOKEN` for `ultradyn/hark`.
- [ ] Re-run a `workflow_dispatch` smoke if secrets were recreated.

### 7. npm package `@ultradyn/hark`

- [ ] After rewrite, `packages/ultradyn-hark/package.json` `repository` / `bugs` URLs should be `ultradyn/hark`.
- [ ] Publish a patch when you want registry metadata updated (tag `v*`, or manual workflow):
  - `repository.url`: `git+https://github.com/ultradyn/hark.git`
  - `bugs.url`: `https://github.com/ultradyn/hark/issues`
- [ ] Confirm npm package page links to the new repo.

### 8. Skills / docs messaging

- [ ] `npx skills add ultradyn/hark -g -y` (new path) — confirm after transfer.
- [ ] Old path `npx skills add clankercode/hark` may work via GitHub redirect; document preferred path as `ultradyn/hark`.
- [ ] Site + README install one-liner should use `raw.githubusercontent.com/ultradyn/hark/...` only after rewrite commit is on default branch.

### 9. Tags / release notes

- [ ] Existing tags move with the transfer; confirm `git ls-remote --tags origin`.
- [ ] Optional release note: “Canonical GitHub path is now `ultradyn/hark`; `clankercode/hark` redirects.”
- [ ] If install pins by tag (`HARK_REF=v…`), re-test one-liner against new raw URL.

### 10. Redirect note for old path

GitHub’s built-in redirect covers:

- `github.com/clankercode/hark` → `ultradyn/hark`
- Clone URLs in many clients

Still update:

- Marketing site, README, npm `repository` fields
- Any external wikis, social bios, package READMEs

Optional banner (site or README) for one release cycle:

> Repo moved to [`ultradyn/hark`](https://github.com/ultradyn/hark). Old `clankercode/hark` links redirect on GitHub.

---

## Immediate workaround (before rewrite lands)

Clone or install against the new owner without waiting for a docs commit:

```bash
HARK_GITHUB_REPO=ultradyn/hark bash install.sh
# or from curl once raw URL serves the new tree:
curl -fsSL https://raw.githubusercontent.com/ultradyn/hark/master/install.sh \
  | HARK_GITHUB_REPO=ultradyn/hark bash
```

Until default branch points `GITHUB_REPO` at `ultradyn/hark`, the env override is the supported escape hatch.

---

## What this prep commit deliberately does **not** do

- Does **not** transfer the GitHub repository.
- Does **not** change live README/site/npm install URLs to `ultradyn/hark` while `clankercode/hark` is still the only public tree.
- Does **not** change the operator’s global `git remote` outside this worktree.

## Verification commands (post-transfer)

```bash
gh repo view ultradyn/hark
gh repo view clankercode/hark   # should redirect / show moved
curl -fsI https://raw.githubusercontent.com/ultradyn/hark/master/install.sh | head
curl -fsSL https://hark.xk.io/ | head
./scripts/rewrite-github-urls.sh          # expect no remaining FROM matches after apply+commit
rg -n 'clankercode/hark' || true          # should be empty (or only historical notes)
```

# Repository transfer: `clankercode/hark` → `ultradyn/hark`

**Status:** in-repo install/docs/site/npm URLs were flipped to `ultradyn/hark` (**B025**). The GitHub org move itself is still a **human/operator** action — if `ultradyn/hark` is not live yet, raw/GitHub pages may 404 until transfer or until you override with `HARK_GITHUB_REPO=clankercode/hark`.

Related backlog: **B024** (prep + tooling), **B025** (URL flip).

## Why this doc

Canonical public path is **`ultradyn/hark`**. The physical GitHub repository may still live under `clankercode/hark` until transfer; GitHub redirects usually cover clone/browse once transfer completes. npm **Trusted Publisher** OIDC must track the **actual** Actions remote (see RELEASE.md), not only marketing URLs.

This repo includes:

| Artifact | Role |
| --- | --- |
| `install.sh` `HARK_GITHUB_REPO` / `GITHUB_REPO` | Default `ultradyn/hark`; override without rewrite (`HARK_GITHUB_REPO=clankercode/hark` during transition) |
| `scripts/rewrite-github-urls.sh` | Bulk `clankercode/hark` → `ultradyn/hark` across docs/site/npm (dry-run default; already applied for B025) |
| This file | Operator checklist (keeps both old and new paths on purpose) |

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

### 4. Rewrite in-repo URLs

**B025 applied** the rewrite so live docs/install/site/npm metadata default to `ultradyn/hark`. Re-run only if a new path regression lands:

```bash
# Preview
./scripts/rewrite-github-urls.sh

# Apply
./scripts/rewrite-github-urls.sh --apply

git diff
git add -A
git commit -m "chore: point GitHub URLs at ultradyn/hark"
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

Hand-fixes still needed after the script (bare owner strings, not `owner/repo`):

- `site/index.html` `data-repo-owner`
- `site/js/main.js` default `repoOwner`
- `RELEASE.md` / workflow comments for **Trusted Publisher** (actual remote ≠ docs URL)

### 5. GitHub Pages / custom domain

- [ ] **Settings → Pages**: source still GitHub Actions (workflow `.github/workflows/pages.yml`).
- [ ] Confirm custom domain `hark.xk.io` (see `site/CNAME`) is still attached after transfer.
- [ ] DNS (if needed): GitHub Pages A/AAAA or CNAME records unchanged if domain stayed on the same Pages site.
- [ ] Force a deploy: push a no-op under `site/` or **Actions → Deploy site to GitHub Pages → Run workflow**.
- [ ] Verify `https://hark.xk.io` and HTTPS certificate.

### 6. Actions / secrets / npm Trusted Publisher

- [ ] Re-check org/repo Actions permissions after transfer.
- [ ] npm Trusted Publisher (`.github/workflows/release.yml`): OIDC **must** list the org/user that **actually hosts** the workflow run. Before transfer that is often still `clankercode`; after transfer update to `ultradyn`. Do not assume docs URLs imply the publisher binding.
- [ ] Re-run a `workflow_dispatch` smoke if secrets/publisher were recreated.

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

## Immediate workaround (if `ultradyn/hark` is not live yet)

In-repo defaults already say `ultradyn/hark`. If the GitHub tree is still only under `clankercode`, override for install/clone:

```bash
HARK_GITHUB_REPO=clankercode/hark bash install.sh
# or:
curl -fsSL https://raw.githubusercontent.com/clankercode/hark/master/install.sh \
  | HARK_GITHUB_REPO=clankercode/hark bash
```

---

## What transfer prep / URL flip deliberately does **not** do

- Does **not** transfer the GitHub repository (operator UI action).
- Does **not** change the operator’s global `git remote` outside this worktree.
- Does **not** auto-update npm Trusted Publisher org (must match actual remote; see RELEASE.md).

## Verification commands (post-transfer)

```bash
gh repo view ultradyn/hark
gh repo view clankercode/hark   # should redirect / show moved
curl -fsI https://raw.githubusercontent.com/ultradyn/hark/master/install.sh | head
curl -fsSL https://hark.xk.io/ | head
./scripts/rewrite-github-urls.sh          # expect no remaining FROM matches after apply+commit
rg -n 'clankercode/hark' || true          # should be empty (or only historical notes)
```

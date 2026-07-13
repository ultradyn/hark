#!/usr/bin/env bash
# Bump packages/ultradyn-hark, commit, and create vX.Y.Z tag (no push).
# Pushing the tag triggers .github/workflows/release.yml (OIDC npm publish).
# See RELEASE.md.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PKG_DIR="$ROOT/packages/ultradyn-hark"
VERSION_ARG="${1:-}"

if [[ -z "$VERSION_ARG" ]]; then
  echo "usage: $0 <version|patch|minor|major>" >&2
  echo "  example: $0 0.1.2" >&2
  echo "  example: $0 patch" >&2
  exit 1
fi

cd "$ROOT"

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "working tree is not clean — commit or stash first" >&2
  exit 1
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$branch" != "master" ]]; then
  echo "releases are cut from master, not '$branch'" >&2
  exit 1
fi

if [[ ! -f "$PKG_DIR/package.json" ]]; then
  echo "missing $PKG_DIR/package.json" >&2
  exit 1
fi

cd "$PKG_DIR"
# Monorepo: bump package.json only; tag from repo root.
npm version "$VERSION_ARG" --no-git-tag-version
pkg="$(node -p "require('./package.json').version")"
cd "$ROOT"

tag="v${pkg}"
if git rev-parse "$tag" >/dev/null 2>&1; then
  echo "tag $tag already exists" >&2
  exit 1
fi

git add packages/ultradyn-hark/package.json
# package-lock may not exist; ignore if absent
if [[ -f packages/ultradyn-hark/package-lock.json ]]; then
  git add packages/ultradyn-hark/package-lock.json
fi

git commit -m "chore(npm): release @ultradyn/hark ${pkg}"
git tag -a "$tag" -m "$tag"

echo
echo "Created commit + tag $tag (@ultradyn/hark@${pkg})"
echo "Push to publish:"
echo "  git push origin master"
echo "  git push origin $tag"
echo
echo "Then run /watch-gh-populate-release (see RELEASE.md)."

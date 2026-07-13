#!/usr/bin/env bash
# Rewrite GitHub owner/repo path after org transfer (clankercode/hark → ultradyn/hark).
#
# Dry-run by default (prints planned changes). Apply with --apply.
#
# Usage:
#   ./scripts/rewrite-github-urls.sh              # dry-run
#   ./scripts/rewrite-github-urls.sh --apply      # write changes
#   ./scripts/rewrite-github-urls.sh --from OLD --to NEW [--apply]
#
# After GitHub transfer completes, run with --apply, review, and commit.
# See docs/REPO_TRANSFER.md.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FROM_REPO="clankercode/hark"
TO_REPO="ultradyn/hark"
APPLY=0

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Replace GitHub owner/repo references across tracked docs and packaging files.

  --apply           Write changes (default is dry-run)
  --from OWNER/REPO Source path (default: ${FROM_REPO})
  --to OWNER/REPO   Target path (default: ${TO_REPO})
  -h, --help        Show this help

Dry-run prints each file and a unified diff (if any). Exit 0 always on dry-run
when tools are present; --apply exits non-zero if no files would change.

Target globs (relative to repo root):
  README.md
  install.sh
  site/README.md
  site/index.html
  packages/ultradyn-hark/package.json
  packages/ultradyn-hark/README.md
  packages/ultradyn-hark/bin/hark-skill.js
  packages/ultradyn-hark/skills/**/SKILL.md   (if present)
  skill/**/SKILL.md
  docs/**/*.md
  .github/workflows/*.yml
  SESSION_NOTE.md
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --from) FROM_REPO="$2"; shift 2 ;;
    --to) TO_REPO="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
done

if [[ "$FROM_REPO" == "$TO_REPO" ]]; then
  echo "error: --from and --to are identical ($FROM_REPO)" >&2
  exit 1
fi

# Collect existing target files (globs expanded safely).
# Intentionally excluded:
#   docs/REPO_TRANSFER.md  — historical checklist must keep both old and new paths
#   scripts/rewrite-github-urls.sh — must keep default --from value
mapfile -t TARGETS < <(
  cd "$ROOT" || exit 1
  # shellcheck disable=SC2086
  for pattern in \
    README.md \
    install.sh \
    site/README.md \
    site/index.html \
    packages/ultradyn-hark/package.json \
    packages/ultradyn-hark/README.md \
    packages/ultradyn-hark/bin/hark-skill.js \
    packages/ultradyn-hark/skills/*/SKILL.md \
    skill/*/SKILL.md \
    skill/SKILL.md \
    docs/*.md \
    .github/workflows/*.yml \
    SESSION_NOTE.md
  do
    # Unquoted pattern for glob; skip unmatched globs.
    for f in $pattern; do
      [[ -f "$f" ]] || continue
      case "$f" in
        docs/REPO_TRANSFER.md) continue ;;
      esac
      printf '%s\n' "$f"
    done
  done | sort -u
)

if [[ ${#TARGETS[@]} -eq 0 ]]; then
  echo "error: no target files found under $ROOT" >&2
  exit 1
fi

echo "==> rewrite GitHub URLs"
echo "    root:  $ROOT"
echo "    from:  $FROM_REPO"
echo "    to:    $TO_REPO"
echo "    mode:  $([[ "$APPLY" -eq 1 ]] && echo APPLY || echo dry-run)"
echo "    files: ${#TARGETS[@]}"
echo

changed=0
skipped=0
for rel in "${TARGETS[@]}"; do
  path="$ROOT/$rel"
  if ! grep -qF "$FROM_REPO" "$path" 2>/dev/null; then
    skipped=$((skipped + 1))
    continue
  fi

  tmp="$(mktemp)"
  # Portable in-place substitute (no sed -i differences).
  # Also flip install.sh default HARK_GITHUB_REPO when rewriting to new owner.
  # Capture python exit under set -e (exit 2 = no net change after dual-allowlist restore).
  set +e
  python3 - "$path" "$tmp" "$FROM_REPO" "$TO_REPO" <<'PY'
import sys
from pathlib import Path

src, dst, old, new = sys.argv[1:5]
text = Path(src).read_text(encoding="utf-8")
out = text.replace(old, new)

# install.sh: flip default GITHUB_REPO, but keep BOTH old and new origin allowlists
# so existing clones with the pre-transfer remote still auto-fetch.
if Path(src).name == "install.sh":
    repl = f'GITHUB_REPO="${{HARK_GITHUB_REPO:-{new}}}"'
    for a in (
        f'GITHUB_REPO="${{HARK_GITHUB_REPO:-{old}}}"',
        f'GITHUB_REPO="${{HARK_GITHUB_REPO:-{new}}}"',
    ):
        if a in out:
            out = out.replace(a, repl, 1)
            break

    # Restore explicit dual allowlist if present (global replace collapses old→new).
    dual_https = f'''KNOWN_ORIGIN_HTTPS=(
  "https://github.com/${{GITHUB_REPO}}.git"
  "https://github.com/${{GITHUB_REPO}}"
  "https://github.com/{old}.git"
  "https://github.com/{old}"
  "https://github.com/{new}.git"
  "https://github.com/{new}"
)'''
    dual_ssh = f'''KNOWN_ORIGIN_SSH=(
  "git@github.com:${{GITHUB_REPO}}.git"
  "git@github.com:{old}.git"
  "git@github.com:{new}.git"
)'''
    import re
    out = re.sub(
        r"KNOWN_ORIGIN_HTTPS=\([\s\S]*?\)",
        dual_https,
        out,
        count=1,
    )
    out = re.sub(
        r"KNOWN_ORIGIN_SSH=\([\s\S]*?\)",
        dual_ssh,
        out,
        count=1,
    )

Path(dst).write_text(out, encoding="utf-8")
sys.exit(0 if out != text else 2)
PY
  py_rc=$?
  set -e
  if [[ $py_rc -eq 2 ]]; then
    rm -f "$tmp"
    skipped=$((skipped + 1))
    continue
  fi
  if [[ $py_rc -ne 0 ]]; then
    rm -f "$tmp"
    echo "error: rewrite failed for $rel" >&2
    exit 1
  fi

  if ! cmp -s "$path" "$tmp"; then
    changed=$((changed + 1))
    echo "--- $rel"
    if command -v diff >/dev/null 2>&1; then
      diff -u "$path" "$tmp" | head -n 80 || true
      echo
    else
      echo "  (would update; diff not available)"
    fi
    if [[ "$APPLY" -eq 1 ]]; then
      cat "$tmp" >"$path"
      echo "  wrote $rel"
    fi
  else
    skipped=$((skipped + 1))
  fi
  rm -f "$tmp"
done

echo
echo "==> summary: would_change/changed=$changed  unchanged=$skipped  total=${#TARGETS[@]}"
if [[ "$APPLY" -eq 0 ]]; then
  echo "    dry-run only. Re-run with --apply to write."
  echo "    Then: git diff && git add -A && git commit"
else
  if [[ "$changed" -eq 0 ]]; then
    echo "    no files modified (already rewritten or no matches)."
    exit 1
  fi
  echo "    applied. Review with git diff, then commit."
fi

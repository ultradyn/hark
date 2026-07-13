#!/usr/bin/env bash
# Hark one-line installer (CLI + agent skills)
#
# Recommended (inspect first, then run):
#   curl -fsSL https://raw.githubusercontent.com/clankercode/hark/master/install.sh -o /tmp/hark-install.sh
#   less /tmp/hark-install.sh
#   bash /tmp/hark-install.sh
#
# One-liner (HTTPS only; trust the repo):
#   curl -fsSL https://raw.githubusercontent.com/clankercode/hark/master/install.sh | bash
#
# Options / env:
#   --ref REF          Git branch/tag/commit (default: master; env HARK_REF)
#   --dir DIR          Clone/checkout directory (default: XDG data hark/src; env HARK_HOME)
#   --skills-dir DIR   Where to install skills (default: ~/.claude/skills; env HARK_SKILLS_DIR)
#   --prefix DIR       Install prefix for pip path (default: ~/.local; env PREFIX)
#   --destdir DIR      Staging root prepended to prefix (env DESTDIR)
#   --method uv|pip    Prefer uv tool install or pip --user/prefix (default: auto)
#   --with-wake        Also install optional ambient wake extra (vosk)
#   --no-skills        Skip copying skill files
#   --no-cli           Skip CLI install (skills only)
#   --force            Reinstall CLI even if present
#   -h, --help         Show help
#
# Safe defaults: no sudo, no silent destructive ops, HTTPS git only, idempotent.

set -euo pipefail

REPO_HTTPS="https://github.com/clankercode/hark.git"
RAW_BASE="https://raw.githubusercontent.com/clankercode/hark"

REF="${HARK_REF:-master}"
PREFIX="${PREFIX:-${HOME}/.local}"
DESTDIR="${DESTDIR:-}"
METHOD="auto" # auto | uv | pip
WITH_WAKE=0
INSTALL_SKILLS=1
INSTALL_CLI=1
FORCE=0

XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
DEFAULT_SRC="${XDG_DATA}/hark/src"
SRC_DIR="${HARK_HOME:-$DEFAULT_SRC}"
SKILLS_DIR="${HARK_SKILLS_DIR:-$HOME/.claude/skills}"

# When this script lives inside a checkout, prefer that tree.
# Piped installs (`curl … | bash`) have no useful source path — skip local detect.
LOCAL_ROOT=""
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [[ -n "$SCRIPT_PATH" && "$SCRIPT_PATH" != "bash" && "$SCRIPT_PATH" != "-" && "$SCRIPT_PATH" != "/dev/stdin" && "$SCRIPT_PATH" != /dev/fd/* ]]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd 2>/dev/null)" || SCRIPT_DIR=""
  if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/src/hark" && -d "$SCRIPT_DIR/skill" ]]; then
    LOCAL_ROOT="$SCRIPT_DIR"
  fi
fi

usage() {
  cat <<'EOF'
Hark one-line installer (CLI + agent skills)

  curl -fsSL https://raw.githubusercontent.com/clankercode/hark/master/install.sh | bash

Options / env:
  --ref REF          Git branch/tag/commit (default: master; env HARK_REF)
  --dir DIR          Clone/checkout directory (default: XDG data hark/src; env HARK_HOME)
  --skills-dir DIR   Skills destination (default: ~/.claude/skills; env HARK_SKILLS_DIR)
  --prefix DIR       Install prefix for pip path (default: ~/.local; env PREFIX)
  --destdir DIR      Staging root prepended to prefix (env DESTDIR)
  --method uv|pip    Prefer uv tool install or pip (default: auto)
  --with-wake        Also install optional ambient wake extra (vosk)
  --no-skills        Skip copying skill files
  --no-cli           Skip CLI install (skills only)
  --force            Reinstall CLI even if present
  -h, --help         Show help

Safe defaults: no sudo, HTTPS git only, idempotent.
EOF
  exit "${1:-0}"
}

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'warn: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1
  Install it, then re-run this script."
}

# Reject insecure git URLs (no git://, no plain http).
assert_https_git_url() {
  case "$1" in
    https://*) ;;
    *) die "refusing non-HTTPS git URL: $1" ;;
  esac
}

python_ok() {
  need_cmd python3
  python3 - <<'PY' || die "Python 3.11+ required (found $(python3 --version 2>&1))"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

resolve_method() {
  case "$METHOD" in
    uv|pip) ;;
    auto)
      if command -v uv >/dev/null 2>&1; then
        METHOD="uv"
      else
        # Prefer uv (install if curl available); else pip
        if command -v curl >/dev/null 2>&1; then
          METHOD="uv"
        else
          METHOD="pip"
        fi
      fi
      ;;
    *) die "unknown --method: $METHOD (use uv or pip)" ;;
  esac
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  need_cmd curl
  log "uv not found; installing via official HTTPS installer (user-local, no sudo)"
  # Official Astral installer — user may re-run shell to refresh PATH
  curl -fsSL https://astral.sh/uv/install.sh | sh
  # Common install locations
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  command -v uv >/dev/null 2>&1 || die "uv install finished but 'uv' not on PATH.
  Add ~/.local/bin to PATH and re-run."
}

staged_prefix() {
  # DESTDIR + PREFIX for packaging-style installs
  printf '%s%s' "${DESTDIR}" "${PREFIX}"
}

is_git_checkout() {
  # Regular clone (.git dir) or worktree/submodule (.git file)
  [[ -e "$1/.git" ]] && git -C "$1" rev-parse --git-dir >/dev/null 2>&1
}

ensure_repo() {
  if [[ -n "$LOCAL_ROOT" ]]; then
    # Running from a local checkout: use it unless --dir / HARK_HOME was overridden.
    if [[ "$SRC_DIR" == "$DEFAULT_SRC" ]]; then
      SRC_DIR="$LOCAL_ROOT"
      log "using local checkout: $SRC_DIR"
      return 0
    fi
  fi

  # Existing tree with package sources (clone, worktree, or unpacked tarball)
  if [[ -f "$SRC_DIR/pyproject.toml" && -d "$SRC_DIR/src/hark" ]]; then
    log "using existing sources: $SRC_DIR"
    if is_git_checkout "$SRC_DIR"; then
      # Best-effort update when we can reach origin (non-fatal if offline / no remote)
      if git -C "$SRC_DIR" remote get-url origin >/dev/null 2>&1; then
        local origin
        origin="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)"
        case "$origin" in
          https://github.com/clankercode/hark.git|https://github.com/clankercode/hark|git@github.com:clankercode/hark.git)
            log "fetching updates for ref $REF (best-effort)"
            git -C "$SRC_DIR" fetch --tags --force origin "$REF" 2>/dev/null || warn "git fetch failed (offline?); using current tree"
            ;;
          *)
            warn "origin is '${origin:-none}' — not auto-fetching; using tree as-is"
            ;;
        esac
      fi
    fi
    return 0
  fi

  assert_https_git_url "$REPO_HTTPS"
  need_cmd git

  if is_git_checkout "$SRC_DIR"; then
    log "updating existing clone: $SRC_DIR (ref $REF)"
    # Prefer HTTPS remote for safety of future fetches from this script
    local origin
    origin="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)"
    case "$origin" in
      https://github.com/clankercode/hark.git|https://github.com/clankercode/hark)
        ;;
      git@github.com:clankercode/hark.git)
        warn "origin uses SSH; leaving remote unchanged"
        ;;
      *)
        if [[ -n "$origin" ]]; then
          warn "origin is '$origin' (expected clankercode/hark); continuing with fetch of $REF"
        fi
        ;;
    esac
    git -C "$SRC_DIR" fetch --tags --force origin "$REF"
    if git -C "$SRC_DIR" rev-parse --verify "refs/remotes/origin/$REF" >/dev/null 2>&1; then
      git -C "$SRC_DIR" checkout -q -B "hark-install/$REF" "origin/$REF"
    elif git -C "$SRC_DIR" rev-parse --verify "$REF" >/dev/null 2>&1; then
      git -C "$SRC_DIR" checkout -q "$REF"
    else
      git -C "$SRC_DIR" fetch origin "$REF" && git -C "$SRC_DIR" checkout -q FETCH_HEAD
    fi
  else
    if [[ -e "$SRC_DIR" && ! -d "$SRC_DIR" ]]; then
      die "HARK_HOME path exists and is not a directory: $SRC_DIR"
    fi
    if [[ -d "$SRC_DIR" && -n "$(ls -A "$SRC_DIR" 2>/dev/null || true)" ]]; then
      die "directory not empty and not a hark source tree: $SRC_DIR
  Set HARK_HOME / --dir to a fresh path, or remove it."
    fi
    log "cloning $REPO_HTTPS ($REF) → $SRC_DIR"
    mkdir -p "$(dirname "$SRC_DIR")"
    if ! git clone --depth 1 --branch "$REF" "$REPO_HTTPS" "$SRC_DIR" 2>/dev/null; then
      # REF may be a commit SHA (not a branch/tag name for --branch)
      rm -rf "$SRC_DIR"
      git clone "$REPO_HTTPS" "$SRC_DIR"
      git -C "$SRC_DIR" checkout "$REF"
    fi
  fi

  [[ -f "$SRC_DIR/pyproject.toml" ]] || die "checkout missing pyproject.toml: $SRC_DIR"
  [[ -d "$SRC_DIR/src/hark" ]] || die "checkout missing src/hark: $SRC_DIR"
}

install_cli_uv() {
  ensure_uv
  local -a cmd=(uv tool install --force)
  # Editable: re-run installer (or git pull in HARK_HOME) picks up source changes
  cmd+=(--editable)
  if [[ "$WITH_WAKE" -eq 1 ]]; then
    cmd+=(--with 'vosk>=0.3.45')
  fi
  if [[ "$FORCE" -eq 1 ]]; then
    uv tool uninstall hark >/dev/null 2>&1 || true
  fi
  log "installing hark CLI with uv tool (from $SRC_DIR)"
  # PACKAGE may be a local path or git+https URL
  cmd+=("$SRC_DIR")
  "${cmd[@]}"
  export PATH="${HOME}/.local/bin:${PATH}"
}

install_cli_pip() {
  need_cmd python3
  local target
  target="$(staged_prefix)"
  log "installing hark CLI with pip (prefix=$target)"
  local pip_args=(install --upgrade)
  if [[ -n "$DESTDIR" || "$PREFIX" != "${HOME}/.local" ]]; then
    pip_args+=(--prefix="$target")
  else
    pip_args+=(--user)
  fi
  if [[ "$WITH_WAKE" -eq 1 ]]; then
    python3 -m pip "${pip_args[@]}" "${SRC_DIR}[wake]"
  else
    python3 -m pip "${pip_args[@]}" "$SRC_DIR"
  fi
  export PATH="${HOME}/.local/bin:${PATH}"
}

install_cli() {
  [[ "$INSTALL_CLI" -eq 1 ]] || { log "skipping CLI (--no-cli)"; return 0; }
  resolve_method
  # uv tool install is user-local only; staged DESTDIR/custom PREFIX → pip
  if [[ -n "$DESTDIR" || ( "$PREFIX" != "${HOME}/.local" && "$METHOD" == "uv" ) ]]; then
    if [[ "$METHOD" == "uv" ]]; then
      warn "DESTDIR/PREFIX set — using pip for staged prefix install"
      METHOD="pip"
    fi
  fi
  case "$METHOD" in
    uv) install_cli_uv ;;
    pip) install_cli_pip ;;
  esac

  if command -v hark >/dev/null 2>&1; then
    log "hark on PATH: $(command -v hark)"
    hark --help >/dev/null 2>&1 || warn "hark --help failed; check installation"
  else
    warn "hark not found on PATH yet.
  Ensure $(staged_prefix)/bin or ~/.local/bin is on your PATH, then re-open the shell."
  fi
}

# Copy skill trees idempotently (rsync-like via mkdir + cp -R).
# Never deletes unrelated skills; only overwrites hark / handsfree skill dirs.
install_skills() {
  [[ "$INSTALL_SKILLS" -eq 1 ]] || { log "skipping skills (--no-skills)"; return 0; }

  local skill_src="$SRC_DIR/skill"
  [[ -d "$skill_src/hark" ]] || die "missing skill tree: $skill_src/hark"

  local dest_root="$SKILLS_DIR"
  if [[ -n "$DESTDIR" ]]; then
    dest_root="${DESTDIR}${SKILLS_DIR}"
  fi

  log "installing agent skills → $dest_root"
  mkdir -p "$dest_root"

  copy_skill() {
    local name="$1"
    local from="$skill_src/$name"
    local to="$dest_root/$name"
    [[ -d "$from" ]] || return 0
    mkdir -p "$to"
    # Copy contents; overwrite files in place (idempotent update)
    if command -v rsync >/dev/null 2>&1; then
      rsync -a --delete "$from/" "$to/"
    else
      # Portable fallback: replace directory contents carefully
      rm -rf "${to}.tmp-hark-install"
      mkdir -p "${to}.tmp-hark-install"
      cp -a "$from/." "${to}.tmp-hark-install/"
      rm -rf "$to"
      mv "${to}.tmp-hark-install" "$to"
    fi
    [[ -f "$to/SKILL.md" ]] || die "skill install failed (no SKILL.md): $to"
    log "  skill: $name → $to"
  }

  copy_skill hark
  copy_skill handsfree
}

print_next_steps() {
  cat <<EOF

────────────────────────────────────────
Hark install complete.

Repo / source:  $SRC_DIR
Skills:         $SKILLS_DIR/{hark,handsfree}
CLI method:     $METHOD

Next steps:
  1. Ensure CLI is on PATH (usually ~/.local/bin):
       export PATH="\$HOME/.local/bin:\$PATH"
  2. Health check:
       hark doctor
  3. Optional config:
       hark config init
  4. In Claude Code / compatible agents, run the skill:
       /hark
     (alias: /handsfree)

Ambient wake (optional, local Vosk model):
  cd "$SRC_DIR" && ./scripts/setup-ambient.sh

Re-run this installer anytime to update (idempotent).
Pin a release:  HARK_REF=v0.1.0 bash install.sh
  or:           curl -fsSL ${RAW_BASE}/master/install.sh | HARK_REF=master bash
────────────────────────────────────────
EOF
}

# ── args ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --dir) SRC_DIR="$2"; shift 2 ;;
    --skills-dir) SKILLS_DIR="$2"; shift 2 ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    --destdir) DESTDIR="$2"; shift 2 ;;
    --method) METHOD="$2"; shift 2 ;;
    --with-wake) WITH_WAKE=1; shift ;;
    --no-skills) INSTALL_SKILLS=0; shift ;;
    --no-cli) INSTALL_CLI=0; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done

# ── preflight ───────────────────────────────────────────────────────
log "Hark installer (ref=$REF)"
need_cmd bash
python_ok
# curl only required when we need remote uv install or when not local; git when cloning
if [[ -z "$LOCAL_ROOT" || "$SRC_DIR" != "$LOCAL_ROOT" ]]; then
  need_cmd git
fi

ensure_repo
install_cli
install_skills
print_next_steps

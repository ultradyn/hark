"""Detect stale non-editable PATH/tool installs vs a local source tree (B100).

Dogfood often uses ``uv tool install`` → ``~/.local/bin/hark``. A *non-editable*
install freezes a copy under the tool venv's site-packages. After ``git pull``
(or a newer worktree) the PATH CLI can lag master (missing ``start``/``stop``,
arm-cue fixes, …) while ``uv run hark`` from the checkout has the new commands.

This module is fail-soft and side-effect free. Doctor surfaces warnings only;
it never reinstalls packages.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from hark import __version__

# Marker modules compared between installed package and source tree.
# cli.py is the usual lag surface (new subcommands); speech.py was B078.
_COMPARE_RELPATHS = (
    "cli.py",
    "speech.py",
    "__init__.py",
)

# Top-level subcommands expected in a current dogfood checkout (soft).
# Used only when a source tree is available and we can parse its cli.py —
# not as a hard failure for release installs that intentionally omit nothing.
_ADD_PARSER_RE = re.compile(
    r"""\.add_parser\(\s*['"]([a-z0-9][-a-z0-9]*)['"]""",
    re.IGNORECASE,
)

# Nested add_parser under daemon/session/… still match; we only care about
# names that are registered as *top-level* choices. Heuristic: collect all
# add_parser names from cli.py + workers.add_lifecycle_parsers sources.
_LIFECYCLE_CMDS = frozenset({"start", "stop", "restart"})

# Dogfood-notable top-level commands (directional "source has, install lacks").
_NOTABLE_CMDS = frozenset(
    {
        "start",
        "stop",
        "restart",
        "setup",
        "webui",
        "dashboard",
        "serve",
        "wake-enroll",
    }
)

# argparse "invalid choice" line often lists choices; also help usage line.
_USAGE_CHOICES_RE = re.compile(
    r"\{([a-z0-9][-a-z0-9|,]*)\}",
    re.IGNORECASE,
)


def package_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("hark")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    return __version__


def package_root() -> Path:
    """Directory containing the loaded ``hark`` package (…/hark/)."""
    import hark as _hark

    return Path(_hark.__file__).resolve().parent


def _read_direct_url() -> dict[str, Any] | None:
    try:
        from importlib.metadata import PackageNotFoundError, distribution

        try:
            dist = distribution("hark")
        except PackageNotFoundError:
            return None
        raw = dist.read_text("direct_url.json")
        if not raw:
            return None
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _file_url_to_path(url: str) -> Path | None:
    """Resolve ``file://`` URLs (including percent-encoded spaces)."""
    if not url or not str(url).startswith("file:"):
        return None
    try:
        parsed = urlparse(url)
        # urlparse gives path with possible %20; unquote to real filesystem path
        path = unquote(parsed.path or "")
        if not path:
            return None
        # Windows file:///C:/… — rare here; keep POSIX-focused
        return Path(path).resolve()
    except (OSError, ValueError, TypeError):
        return None


def is_hark_source_tree(root: Path | None) -> bool:
    """True when *root* looks like a hark monorepo / checkout."""
    if root is None:
        return False
    try:
        r = root.resolve()
    except OSError:
        return False
    if not r.is_dir():
        return False
    pyproject = r / "pyproject.toml"
    src_pkg = r / "src" / "hark"
    if not pyproject.is_file() or not (src_pkg / "__init__.py").is_file():
        return False
    try:
        text = pyproject.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    # Loose match — avoid requiring a full TOML parse.
    return re.search(r'(?m)^name\s*=\s*["\']hark["\']', text) is not None


def discover_source_trees(
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    direct_url: dict[str, Any] | None = None,
) -> list[Path]:
    """Ordered candidate source checkouts (deduped, existing only)."""
    env = env if env is not None else os.environ
    out: list[Path] = []
    seen: set[Path] = set()

    def add(p: Path | None) -> None:
        if p is None:
            return
        try:
            r = p.resolve()
        except OSError:
            return
        if r in seen or not is_hark_source_tree(r):
            return
        seen.add(r)
        out.append(r)

    for key in ("HARK_HOME", "HARK_REPO"):
        raw = (env.get(key) or "").strip()
        if raw:
            add(Path(raw).expanduser())

    du = direct_url if direct_url is not None else _read_direct_url()
    if du:
        url = du.get("url")
        if isinstance(url, str):
            add(_file_url_to_path(url))

    # Walk cwd upward for a checkout (dogfood worktree).
    start = (cwd or Path.cwd()).resolve()
    cur: Path | None = start
    for _ in range(12):
        if cur is None:
            break
        add(cur)
        if cur.parent == cur:
            break
        cur = cur.parent

    # Default installer location (~/.local/share/hark/src)
    xdg_data = env.get("XDG_DATA_HOME")
    if xdg_data:
        add(Path(xdg_data) / "hark" / "src")
    else:
        add(Path.home() / ".local" / "share" / "hark" / "src")

    return out


def _sha256_file(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _parse_add_parser_names(text: str) -> set[str]:
    return set(_ADD_PARSER_RE.findall(text))


def _source_cli_commands(source_root: Path) -> set[str]:
    """Top-level-ish CLI names discoverable from checkout sources."""
    names: set[str] = set()
    for rel in ("src/hark/cli.py", "src/hark/workers.py"):
        p = source_root / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names |= _parse_add_parser_names(text)
    return names


def _installed_cli_commands() -> set[str]:
    """Commands registered by the *running* package's argparse."""
    try:
        from hark.cli import build_parser

        parser = build_parser()
        # argparse stores subparsers actions; find the required cmd group
        for action in parser._actions:  # noqa: SLF001 — intentional introspect
            if getattr(action, "dest", None) == "cmd" and hasattr(action, "choices"):
                choices = action.choices
                if isinstance(choices, dict):
                    return set(choices.keys())
        return set()
    except Exception:
        return set()


def git_describe(source_root: Path) -> str | None:
    """Best-effort ``git describe --always --dirty`` for *source_root*."""
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run(
            [git, "-C", str(source_root), "describe", "--always", "--dirty"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def git_head(source_root: Path) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        proc = subprocess.run(
            [git, "-C", str(source_root), "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


def classify_install(
    *,
    pkg_root: Path | None = None,
    direct_url: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify how the running ``hark`` package is installed."""
    root = (pkg_root or package_root()).resolve()
    du = direct_url if direct_url is not None else _read_direct_url()
    editable = False
    source_url: str | None = None
    source_path: Path | None = None
    if du:
        url = du.get("url")
        if isinstance(url, str):
            source_url = url
            source_path = _file_url_to_path(url)
        di = du.get("dir_info")
        if isinstance(di, dict) and di.get("editable") is True:
            editable = True
    # Also treat package living under src/hark of a checkout as editable/dev
    if root.name == "hark" and root.parent.name == "src":
        candidate = root.parent.parent
        if is_hark_source_tree(candidate):
            editable = True
            if source_path is None:
                source_path = candidate

    in_uv_tools = "uv/tools/hark" in str(root).replace("\\", "/")
    in_site = "site-packages" in root.parts or "dist-packages" in root.parts

    if editable:
        mode = "editable"
    elif in_uv_tools or (in_site and source_url and source_url.startswith("file:")):
        mode = "tool-copy"  # non-editable uv tool / local path wheel
    elif in_site:
        mode = "site-packages"
    else:
        mode = "unknown"

    return {
        "mode": mode,
        "editable": editable,
        "package_root": str(root),
        "package_version": package_version(),
        "hark_version": __version__,
        "python": sys.executable,
        "direct_url": du,
        "source_url": source_url,
        "install_source_path": str(source_path) if source_path else None,
        "in_uv_tools": in_uv_tools,
    }


def _notable_missing(source_cmds: set[str], installed_cmds: set[str]) -> list[str]:
    return sorted((source_cmds - installed_cmds) & _NOTABLE_CMDS)


def compare_to_source(
    source_root: Path,
    *,
    pkg_root: Path | None = None,
    installed_commands: set[str] | None = None,
    with_git: bool = True,
) -> dict[str, Any]:
    """Compare a package tree / command set to a source checkout.

    *stale* / *behind* is **directional**: only when the source has notable
    top-level commands the install lacks (or install is missing marker files).
    Content hash mismatches alone are ``different`` (divergent), not "behind".
    """
    pkg = (pkg_root or package_root()).resolve()
    src_pkg = (source_root / "src" / "hark").resolve()
    file_diffs: list[dict[str, Any]] = []
    missing_in_install: list[str] = []
    missing_in_source: list[str] = []

    for rel in _COMPARE_RELPATHS:
        ip = pkg / rel
        sp = src_pkg / rel
        if not sp.is_file():
            missing_in_source.append(rel)
            continue
        if not ip.is_file():
            missing_in_install.append(rel)
            file_diffs.append({"path": rel, "reason": "missing_in_install"})
            continue
        ih = _sha256_file(ip)
        sh = _sha256_file(sp)
        if ih and sh and ih != sh:
            file_diffs.append(
                {
                    "path": rel,
                    "reason": "content_mismatch",
                    "installed_sha256": ih[:12],
                    "source_sha256": sh[:12],
                }
            )

    installed_cmds = (
        set(installed_commands)
        if installed_commands is not None
        else _installed_cli_commands()
    )
    source_cmds = _source_cli_commands(source_root)
    source_only = sorted(source_cmds - installed_cmds)
    notable_missing = _notable_missing(source_cmds, installed_cmds)
    # Directional lag only (B100): source has start/stop/… install does not.
    # Hash-only divergence is not "behind" (could be newer install / other tree).
    behind = bool(notable_missing) or bool(missing_in_install)
    different = bool(file_diffs) and not behind

    return {
        "source_root": str(source_root.resolve()),
        "source_package": str(src_pkg),
        "git_describe": git_describe(source_root) if with_git else None,
        "git_head": git_head(source_root) if with_git else None,
        "file_diffs": file_diffs,
        "missing_in_install": missing_in_install,
        "missing_in_source": missing_in_source,
        "installed_commands": sorted(installed_cmds),
        "source_only_commands": source_only,
        "missing_commands": notable_missing,
        "behind": behind,
        "different": different,
    }


def reinstall_hint(source_root: Path | None = None) -> str:
    """One-line fix for a stale non-editable dogfood install."""
    if source_root is not None and is_hark_source_tree(source_root):
        return f"cd {shlex.quote(str(source_root))} && uv tool install -e . --force"
    return "cd <hark-checkout> && uv tool install -e . --force"


def _resolve_script_target(script: Path) -> Path | None:
    """Best-effort real path for a console script (follow symlink)."""
    try:
        return script.expanduser().resolve()
    except OSError:
        return None


def _package_root_from_script(script: Path) -> Path | None:
    """Infer site-packages/hark from a uv-tool / venv console script shebang."""
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Typical: #!/…/uv/tools/hark/bin/python3
    first = text.splitlines()[0] if text else ""
    if first.startswith("#!"):
        py = Path(first[2:].strip())
        # …/bin/python → …/lib/pythonX.Y/site-packages/hark
        bin_dir = py.parent
        root = bin_dir.parent
        for candidate in (
            root / "lib",
        ):
            if not candidate.is_dir():
                continue
            for sp in candidate.glob("python*/site-packages/hark"):
                if (sp / "__init__.py").is_file():
                    return sp.resolve()
            for sp in candidate.glob("python*/dist-packages/hark"):
                if (sp / "__init__.py").is_file():
                    return sp.resolve()
    # Editable / PATH that is `python -m` wrapper living next to package — skip
    return None


def _script_same_as_running(script: Path, pkg: Path | None) -> bool:
    """True when *script* is this process's console entry (not a foreign tool)."""
    try:
        if pkg is not None and pkg.resolve() == package_root().resolve():
            return True
    except Exception:
        pass
    try:
        # uv/venv: sys.executable may be a symlink into the store; compare
        # *unresolved* bin dirs and also sys.prefix/bin.
        exe = Path(sys.executable)
        script_bin = script.expanduser()
        if not script_bin.is_absolute():
            script_bin = script_bin.resolve()
        candidates = {
            exe.parent,
            Path(sys.prefix) / "bin",
            Path(sys.base_prefix) / "bin",
        }
        # Do not resolve() script parent vs cpython store — compare as-is strings
        sp = script if script.is_absolute() else script.resolve()
        if sp.parent in candidates or sp.parent.resolve() in {
            c.resolve() for c in candidates if c.exists()
        }:
            return True
        # Same file as sys.prefix/bin/hark
        prefix_hark = Path(sys.prefix) / "bin" / "hark"
        if prefix_hark.is_file() and sp.resolve() == prefix_hark.resolve():
            return True
    except Exception:
        pass
    return False


def _probe_one_hark(script: Path, *, timeout: float = 2.5) -> dict[str, Any]:
    resolved = _resolve_script_target(script)
    pkg = _package_root_from_script(script) if script.is_file() else None
    commands: set[str] = set()
    help_err: str | None = None
    version_out: str | None = None
    try:
        proc = subprocess.run(
            [str(script), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
        for m in _USAGE_CHOICES_RE.finditer(blob):
            for part in m.group(1).split(","):
                name = part.strip()
                if name:
                    commands.add(name)
        if proc.returncode not in (0, 1, 2) and not commands:
            help_err = f"exit {proc.returncode}"
    except (OSError, subprocess.TimeoutExpired) as exc:
        help_err = f"{type(exc).__name__}: {exc}"
    try:
        proc_v = subprocess.run(
            [str(script), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        version_out = (proc_v.stdout or proc_v.stderr or "").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {
        "ok": True,
        "path": str(script),
        "resolved": str(resolved) if resolved else None,
        "package_root": str(pkg) if pkg else None,
        "commands": sorted(commands),
        "version": version_out,
        "same_as_running": _script_same_as_running(script, pkg),
        "error": help_err,
    }


def path_hark_probe(
    *,
    which: str | None = None,
    timeout: float = 2.5,
    also_local_bin: bool = True,
) -> dict[str, Any]:
    """Inspect the ``hark`` executable on PATH (may differ from this process).

    Fail-soft: never raises. Used so ``uv run hark doctor`` can still warn that
    ``~/.local/bin/hark`` is a stale tool copy (B100 dogfood).

    When ``uv run`` puts the project venv first on PATH, ``which hark`` is the
    *fresh* entry — we still probe ``~/.local/bin/hark`` if it exists and
    differs, because that is what agents invoke outside the venv.
    """
    path = which if which is not None else shutil.which("hark")
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    if also_local_bin and which is None:
        local = Path.home() / ".local" / "bin" / "hark"
        if local.is_file():
            try:
                if not candidates or local.resolve() != candidates[0].resolve():
                    candidates.append(local)
            except OSError:
                candidates.append(local)
    if not candidates:
        return {"ok": False, "path": None, "error": "hark not on PATH"}

    probes = [_probe_one_hark(c, timeout=timeout) for c in candidates]
    # Prefer a probe that is *not* this process and is a tool/site-packages copy
    # for lag detection; still report primary which in "path".
    primary = probes[0]
    foreign = next(
        (
            p
            for p in probes
            if not p.get("same_as_running")
            and (
                p.get("package_root")
                or p.get("commands") is not None
            )
        ),
        None,
    )
    chosen = foreign or primary
    chosen = dict(chosen)
    chosen["all_paths"] = [p.get("path") for p in probes]
    chosen["probes"] = probes
    # which_hark display: primary PATH hit
    chosen["which"] = primary.get("path")
    chosen["path"] = chosen.get("path")  # foreign or primary
    return chosen


def install_status(
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    pkg_root: Path | None = None,
    direct_url: dict[str, Any] | None = None,
    compare: bool = True,
    probe_path_hark: bool = True,
    path_hark_which: str | None = None,
) -> dict[str, Any]:
    """Full install freshness snapshot for doctor / JSON.

    Soft-only: never raises; ``ok`` is True unless comparison failed hard
    (missing package). ``stale`` / ``warnings`` carry the dogfood signal.

    Also probes the **PATH** ``hark`` binary (may differ from this process) so
    ``uv run hark doctor`` still warns about a lagging ``~/.local/bin/hark``.
    """
    warnings: list[str] = []
    hints: list[str] = []
    try:
        info = classify_install(pkg_root=pkg_root, direct_url=direct_url)
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "ok": False,
            "status": "error",
            "stale": False,
            "error": str(exc),
            "warnings": [f"install check failed: {exc}"],
            "hints": [],
        }

    sources = discover_source_trees(
        cwd=cwd, env=env, direct_url=info.get("direct_url") or direct_url
    )
    # Prefer install_source_path if still a valid tree
    preferred: Path | None = None
    if info.get("install_source_path"):
        p = Path(str(info["install_source_path"]))
        if is_hark_source_tree(p):
            preferred = p.resolve()
    if preferred is None and sources:
        preferred = sources[0]

    comparison: dict[str, Any] | None = None
    path_probe: dict[str, Any] | None = None
    path_comparison: dict[str, Any] | None = None
    stale = False
    status = "ok"

    if compare and preferred is not None:
        try:
            comparison = compare_to_source(preferred, pkg_root=pkg_root)
        except Exception as exc:  # pragma: no cover
            warnings.append(f"source compare failed: {exc}")
            comparison = {"error": str(exc), "behind": False, "different": False}

    if probe_path_hark:
        try:
            path_probe = path_hark_probe(which=path_hark_which)
        except Exception as exc:  # pragma: no cover
            path_probe = {"ok": False, "error": str(exc)}

    # Local dogfood context: file:// install source and/or a discovered checkout.
    # Pure PyPI / release tool installs without a nearby tree stay quiet.
    local_dogfood = preferred is not None or (
        isinstance(info.get("source_url"), str)
        and str(info["source_url"]).startswith("file:")
    )

    def _mark_stale(cmp: dict[str, Any], *, subject: str) -> None:
        nonlocal stale, status
        stale = True
        status = "stale"
        missing = cmp.get("missing_commands") or []
        diffs = cmp.get("file_diffs") or []
        parts: list[str] = []
        if missing:
            parts.append("missing commands: " + ", ".join(missing))
        if diffs and missing:
            # only mention file diffs as supporting evidence when already lagging
            paths = ", ".join(d.get("path", "?") for d in diffs[:5])
            parts.append(f"files differ ({paths})")
        detail = "; ".join(parts) if parts else "source tree is ahead"
        src = cmp.get("source_root") or preferred
        git_d = cmp.get("git_describe")
        git_bit = f" git={git_d}" if git_d else ""
        warnings.append(
            f"{subject} is behind local source at {src}{git_bit} ({detail})"
        )
        hints.append(reinstall_hint(Path(str(src)) if src else None))
        if not info.get("editable"):
            hints.append(
                "dogfood: prefer editable install so git pulls update the CLI"
            )

    # 1) Running package behind source (directional)
    if comparison and comparison.get("behind"):
        _mark_stale(comparison, subject="running hark package")

    # 2) PATH / ~/.local/bin hark behind source (even when this process is uv run)
    path_candidates: list[dict[str, Any]] = []
    if path_probe and path_probe.get("ok"):
        # Prefer multi-probe list when present
        path_candidates = list(path_probe.get("probes") or [path_probe])

    for ph in path_candidates:
        if not ph.get("ok") or ph.get("same_as_running"):
            continue
        path_pkg = ph.get("package_root")
        path_cmds = set(ph.get("commands") or [])
        try:
            if path_pkg:
                path_comparison = compare_to_source(
                    preferred,  # type: ignore[arg-type]
                    pkg_root=Path(str(path_pkg)),
                    installed_commands=path_cmds or None,
                ) if preferred is not None else None
            elif path_cmds and preferred is not None:
                missing = _notable_missing(
                    _source_cli_commands(preferred), path_cmds
                )
                path_comparison = {
                    "source_root": str(preferred),
                    "missing_commands": missing,
                    "file_diffs": [],
                    "behind": bool(missing),
                    "different": False,
                    "git_describe": git_describe(preferred),
                }
            else:
                path_comparison = None
        except Exception as exc:  # pragma: no cover
            warnings.append(f"PATH hark compare failed: {exc}")
            path_comparison = None

        if path_comparison and path_comparison.get("behind"):
            was_running_ok = not stale and info.get("editable")
            subj = f"PATH hark ({ph.get('path')})"
            _mark_stale(path_comparison, subject=subj)
            # Running process may be fine (uv run / editable); only PATH lags.
            if was_running_ok:
                status = "path-stale"
            # keep lagging binary for doctor display; preserve original which
            which_saved = (path_probe or {}).get("which") or (
                path_candidates[0].get("path") if path_candidates else None
            )
            path_probe = dict(ph)
            path_probe["probes"] = path_candidates
            path_probe["which"] = which_saved
            path_probe["all_paths"] = [
                p.get("path") for p in path_candidates if p.get("path")
            ]
            break

    if not stale and comparison and comparison.get("different"):
        # Hash mismatch without missing commands — neutral, not "behind"
        status = "different"
        paths = ", ".join(
            d.get("path", "?") for d in (comparison.get("file_diffs") or [])[:5]
        )
        warnings.append(
            f"running package files differ from source "
            f"({paths}) — not directional; check checkout vs install"
        )
        if preferred is not None:
            hints.append(reinstall_hint(preferred))

    if (
        not stale
        and status not in ("different",)
        and local_dogfood
        and info.get("mode") in ("tool-copy", "site-packages")
        and not info.get("editable")
    ):
        # Non-editable tool install next to a local tree — even when currently
        # matching, the next git pull will lag until reinstall.
        status = "frozen"
        warnings.append(
            "hark is a non-editable tool/site-packages copy — "
            "git pulls in the source tree will not update PATH until reinstall"
        )
        hints.append(reinstall_hint(preferred))
    elif not stale and info.get("editable") and status == "ok":
        status = "editable"
        # If cwd is a *different* checkout than the editable source, soft warn
        if preferred is not None and sources:
            editable_src = preferred
            for s in sources:
                if s.resolve() != editable_src.resolve():
                    try:
                        other = compare_to_source(
                            s, pkg_root=pkg_root, with_git=False
                        )
                    except Exception:
                        continue
                    if other.get("behind") and other.get("missing_commands"):
                        warnings.append(
                            f"editable install tracks {editable_src}; cwd tree "
                            f"{s} has extra commands "
                            f"({', '.join(other['missing_commands'])}) — "
                            f"use uv run hark or reinstall -e from that tree"
                        )
                        hints.append(reinstall_hint(s))
                        status = "checkout-mismatch"
                        break
    elif (
        not stale
        and status == "ok"
        and not info.get("editable")
        and info.get("mode") in ("tool-copy", "site-packages")
    ):
        # Release install, no local tree — report mode only, no dogfood nag.
        status = "ok"

    # Dedupe hints/warnings while preserving order
    def _uniq(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for it in items:
            if it not in seen:
                seen.add(it)
                out.append(it)
        return out

    warnings = _uniq(warnings)
    hints = _uniq(hints)

    which_hark = (
        (path_probe or {}).get("which")
        or (path_probe or {}).get("path")
        or shutil.which("hark")
    )
    report: dict[str, Any] = {
        "ok": True,
        "status": status,
        "stale": stale,
        "mode": info.get("mode"),
        "editable": bool(info.get("editable")),
        "package_version": info.get("package_version"),
        "hark_version": info.get("hark_version"),
        "package_root": info.get("package_root"),
        "python": info.get("python"),
        "which_hark": which_hark,
        "path_hark": path_probe,
        "path_comparison": path_comparison,
        "source_url": info.get("source_url"),
        "install_source_path": info.get("install_source_path"),
        "source_trees": [str(s) for s in sources],
        "compared_source": str(preferred) if preferred else None,
        "comparison": comparison,
        "warnings": warnings,
        "hints": hints,
    }
    return report


def install_status_for_api(**kwargs: Any) -> dict[str, Any]:
    """Dict for doctor JSON (fail-soft)."""
    try:
        return install_status(**kwargs)
    except Exception as exc:  # pragma: no cover
        return {
            "ok": False,
            "status": "error",
            "stale": False,
            "error": str(exc),
            "warnings": [f"install check failed: {exc}"],
            "hints": [],
        }

"""GitHub release self-update check (B088).

Fetches the latest release tag for the hark repo, compares to the installed
version, and caches the result under the XDG state dir with a 24h TTL.

Fail-soft: offline / API errors never raise to callers. Never downloads or
installs packages — notice only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TextIO

from hark.paths import state_dir

DEFAULT_REPO = "ultradyn/hark"
CACHE_FILENAME = "update_check.json"
TTL_SECONDS = 24 * 60 * 60
DEFAULT_TIMEOUT_S = 2.5
GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
USER_AGENT = "hark-update-check"


@dataclass(frozen=True)
class UpdateStatus:
    """Snapshot of the last known release check."""

    current_version: str
    latest_version: str | None = None
    update_available: bool = False
    html_url: str | None = None
    tag_name: str | None = None
    checked_at: float | None = None  # unix epoch seconds
    from_cache: bool = False
    stale: bool = False
    disabled: bool = False
    error: str | None = None
    repo: str = DEFAULT_REPO

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # ISO-ish for APIs that prefer strings
        if self.checked_at is not None:
            d["checked_at_iso"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.checked_at)
            )
        else:
            d["checked_at_iso"] = None
        return d


def package_version() -> str:
    """Installed package version (importlib.metadata, else hark.__version__)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("hark")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    from hark import __version__

    return __version__


def cache_path() -> Path:
    return state_dir() / CACHE_FILENAME


def normalize_version(raw: str | None) -> str:
    """Strip leading ``v`` / whitespace for comparison and display."""
    if not raw:
        return ""
    s = str(raw).strip()
    if s[:1] in ("v", "V") and len(s) > 1 and s[1].isdigit():
        s = s[1:]
    return s


def version_tuple(raw: str | None) -> tuple[int, ...]:
    """Numeric version parts for ordering (``v1.2.3-rc1`` → ``(1, 2, 3, 1)``)."""
    s = normalize_version(raw)
    if not s:
        return (0,)
    parts = [int(p) for p in re.findall(r"\d+", s)]
    return tuple(parts) if parts else (0,)


def is_newer(latest: str | None, current: str | None) -> bool:
    """True when *latest* is strictly greater than *current*."""
    if not latest or not current:
        return False
    return version_tuple(latest) > version_tuple(current)


def _env_enabled(default: bool = True) -> bool:
    raw = os.environ.get("HARK_UPDATE_CHECK")
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "disabled")


def _env_repo(default: str = DEFAULT_REPO) -> str:
    raw = os.environ.get("HARK_UPDATE_REPO")
    if raw and raw.strip():
        return raw.strip().lstrip("/")
    return default


def _read_cache(path: Path | None = None) -> dict[str, Any] | None:
    p = path or cache_path()
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None


def _write_cache(payload: dict[str, Any], path: Path | None = None) -> None:
    p = path or cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(p)
    except OSError:
        # fail soft — cache is best-effort
        pass


def _status_from_cache(
    cache: dict[str, Any],
    *,
    current: str,
    now: float,
    ttl_s: float,
    repo: str,
) -> UpdateStatus:
    checked_at = cache.get("checked_at")
    try:
        checked_f = float(checked_at) if checked_at is not None else None
    except (TypeError, ValueError):
        checked_f = None
    stale = True
    if checked_f is not None:
        stale = (now - checked_f) > float(ttl_s)
    latest = cache.get("latest_version") or cache.get("tag_name")
    latest_s = normalize_version(str(latest)) if latest else None
    return UpdateStatus(
        current_version=current,
        latest_version=latest_s,
        update_available=is_newer(latest_s, current),
        html_url=str(cache["html_url"]) if cache.get("html_url") else None,
        tag_name=str(cache["tag_name"]) if cache.get("tag_name") else None,
        checked_at=checked_f,
        from_cache=True,
        stale=stale,
        disabled=False,
        error=cache.get("error") if isinstance(cache.get("error"), str) else None,
        repo=str(cache.get("repo") or repo),
    )


def fetch_latest_release(
    repo: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    client: Any | None = None,
) -> dict[str, Any]:
    """HTTP GET GitHub releases/latest. Returns parsed fields or raises."""
    import httpx

    url = GITHUB_API.format(repo=repo)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if client is not None:
        resp = client.get(url, headers=headers, timeout=timeout)
    else:
        with httpx.Client(timeout=timeout) as c:
            resp = c.get(url, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError("unexpected GitHub release payload")
    tag = body.get("tag_name") or body.get("name")
    if not tag:
        raise ValueError("release missing tag_name")
    return {
        "tag_name": str(tag),
        "latest_version": normalize_version(str(tag)),
        "html_url": body.get("html_url") or f"https://github.com/{repo}/releases",
        "name": body.get("name"),
    }


def check_for_update(
    *,
    enabled: bool = True,
    repo: str | None = None,
    current: str | None = None,
    force: bool = False,
    ttl_s: float = TTL_SECONDS,
    timeout: float = DEFAULT_TIMEOUT_S,
    now: float | None = None,
    cache_file: Path | None = None,
    client: Any | None = None,
) -> UpdateStatus:
    """Return update status, using cache when fresh (TTL) or after a fetch.

    *force* skips the fresh-cache short-circuit and always hits the network
    (still fail-soft). Network failures fall back to a stale cache when present.
    """
    enabled = _env_enabled(enabled)
    repo_s = _env_repo(repo or DEFAULT_REPO)
    current_s = normalize_version(current or package_version()) or "0"
    now_f = float(time.time() if now is None else now)

    if not enabled:
        return UpdateStatus(
            current_version=current_s,
            disabled=True,
            repo=repo_s,
        )

    cached = _read_cache(cache_file)
    if cached is not None and not force:
        # Different repo override → treat as miss (do not trust foreign cache)
        cached_repo = str(cached.get("repo") or DEFAULT_REPO)
        if cached_repo == repo_s:
            status = _status_from_cache(
                cached, current=current_s, now=now_f, ttl_s=ttl_s, repo=repo_s
            )
            if not status.stale:
                # re-evaluate update_available against *current* (install may have
                # been upgraded since the cache was written)
                return status
        else:
            cached = None  # drop for fallback path if fetch fails

    err: str | None = None
    try:
        fetched = fetch_latest_release(repo_s, timeout=timeout, client=client)
        payload = {
            "checked_at": now_f,
            "repo": repo_s,
            "tag_name": fetched["tag_name"],
            "latest_version": fetched["latest_version"],
            "html_url": fetched.get("html_url"),
            "name": fetched.get("name"),
            "error": None,
        }
        _write_cache(payload, cache_file)
        latest = str(fetched["latest_version"])
        return UpdateStatus(
            current_version=current_s,
            latest_version=latest,
            update_available=is_newer(latest, current_s),
            html_url=str(fetched["html_url"]) if fetched.get("html_url") else None,
            tag_name=str(fetched["tag_name"]),
            checked_at=now_f,
            from_cache=False,
            stale=False,
            disabled=False,
            error=None,
            repo=repo_s,
        )
    except Exception as exc:  # noqa: BLE001 — fail soft offline / API errors
        err = f"{type(exc).__name__}: {exc}"

    if cached is not None:
        status = _status_from_cache(
            cached, current=current_s, now=now_f, ttl_s=ttl_s, repo=repo_s
        )
        return UpdateStatus(
            current_version=status.current_version,
            latest_version=status.latest_version,
            update_available=status.update_available,
            html_url=status.html_url,
            tag_name=status.tag_name,
            checked_at=status.checked_at,
            from_cache=True,
            stale=True,
            disabled=False,
            error=err,
            repo=status.repo,
        )

    return UpdateStatus(
        current_version=current_s,
        update_available=False,
        from_cache=False,
        stale=False,
        disabled=False,
        error=err,
        repo=repo_s,
        checked_at=None,
    )


def format_update_notice(status: UpdateStatus) -> str | None:
    """Human one-liner, or None when nothing should be printed."""
    if status.disabled or not status.update_available:
        return None
    cur = status.current_version
    lat = status.latest_version or "?"
    url = status.html_url or f"https://github.com/{status.repo}/releases"
    return f"hark: update available: {cur} → {lat}  ({url})"


def maybe_print_update_notice(
    *,
    enabled: bool = True,
    repo: str | None = None,
    current: str | None = None,
    force: bool = False,
    file: TextIO | None = None,
    **kwargs: Any,
) -> UpdateStatus:
    """Run check_for_update and print a notice to *file* (default stderr)."""
    status = check_for_update(
        enabled=enabled, repo=repo, current=current, force=force, **kwargs
    )
    msg = format_update_notice(status)
    if msg:
        print(msg, file=file or sys.stderr)
    return status


def update_status_for_api(
    *,
    enabled: bool = True,
    repo: str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Dict for dashboard health / doctor (cache-first, fail soft)."""
    status = check_for_update(enabled=enabled, repo=repo, **kwargs)
    return status.to_dict()

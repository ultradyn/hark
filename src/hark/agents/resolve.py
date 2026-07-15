"""Resolve coding-agent CLI argv (aliases + ad-hoc + reject list).

Prefer short aliases when they are safe PATH binaries (``cc``, ``cx``, ``gk``,
``cr``), else fall back to the canonical command. Never shell out through
interactive fish/zsh functions — only ``PATH`` executables or config overrides.

Pitfalls (see ``docs/plans/I005-voice-herdr-agent-control.md``):

- ``cc`` often resolves to **gcc** — rejected.
- ``cr`` may be **CodeRabbit**, not cursor-agent — rejected when detected.
- Fish functions for ``cc``/``cx`` are invisible to ``herdr agent start``.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence


class ResolveFailureReason(str, Enum):
    """Machine-readable reason that CLI resolution failed."""

    OTHER = "other"
    UNKNOWN_AGENT = "unknown_agent"
    EMPTY_COMMAND = "empty_command"
    INVALID_OVERRIDE = "invalid_override"
    NO_SAFE_EXECUTABLE = "no_safe_executable"
    INVALID_ADHOC_COMMAND = "invalid_adhoc_command"


class ResolveError(ValueError):
    """Could not resolve a coding-agent CLI."""

    def __init__(
        self,
        message: str,
        *,
        reason: ResolveFailureReason = ResolveFailureReason.OTHER,
    ) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ResolvedCli:
    """Result of argv resolution."""

    agent_key: str
    argv: list[str]
    source: str  # alias | canonical | override | adhoc
    command: str  # first argv token (path or basename)


@dataclass(frozen=True)
class AgentSpec:
    key: str
    canonical: str
    aliases: tuple[str, ...] = ()
    # Spoken / alternate names that map to this key
    names: tuple[str, ...] = ()


# Built-in catalog: preferred aliases first, then canonical.
AGENT_CATALOG: tuple[AgentSpec, ...] = (
    AgentSpec(
        key="claude",
        canonical="claude",
        aliases=("cc",),
        names=("claude", "claude code", "claude-code"),
    ),
    AgentSpec(
        key="codex",
        canonical="codex",
        aliases=("cx",),
        names=("codex", "openai codex"),
    ),
    AgentSpec(
        key="grok",
        canonical="grok",
        aliases=("gk",),
        names=("grok", "grok build", "grok-build"),
    ),
    AgentSpec(
        key="cursor-agent",
        canonical="cursor-agent",
        aliases=("cr",),
        names=("cursor", "cursor-agent", "cursor agent"),
    ),
    AgentSpec(
        key="opencode",
        canonical="opencode",
        aliases=(),
        names=("opencode", "open code"),
    ),
    AgentSpec(
        key="pi",
        canonical="pi",
        aliases=(),
        names=("pi",),
    ),
    AgentSpec(
        key="agy",
        canonical="agy",
        aliases=(),
        names=("agy", "antigravity"),
    ),
)

_KEY_BY_TOKEN: dict[str, str] = {}
for _spec in AGENT_CATALOG:
    _KEY_BY_TOKEN[_spec.key.lower()] = _spec.key
    _KEY_BY_TOKEN[_spec.canonical.lower()] = _spec.key
    for _a in _spec.aliases:
        _KEY_BY_TOKEN[_a.lower()] = _spec.key
    for _n in _spec.names:
        _KEY_BY_TOKEN[_n.lower()] = _spec.key


def _which(cmd: str, path: str | None = None) -> str | None:
    """Locate ``cmd`` on PATH. Uses env swap so monkeypatched ``which(name)`` still works."""
    if path is None:
        return shutil.which(cmd)
    old = os.environ.get("PATH")
    try:
        os.environ["PATH"] = path
        return shutil.which(cmd)
    finally:
        if old is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = old


def _is_rejected(cmd: str, resolved_path: str) -> bool:
    """Return True if this PATH hit must not be used as a coding CLI."""
    cmd_base = Path(cmd).name.lower()
    try:
        real = Path(resolved_path).resolve()
        real_s = str(real).lower()
        real_name = real.name.lower()
    except OSError:
        real_s = resolved_path.lower()
        real_name = Path(resolved_path).name.lower()

    # gcc toolchain masquerading as `cc` (common on Linux: /usr/bin/cc → gcc)
    if cmd_base == "cc":
        if real_name == "gcc" or real_name.startswith("gcc-"):
            return True
        if real_name == "cc" and "claude" not in real_s:
            # System cc is almost always the C compiler (not a user shim under ~/.local)
            if real_s.startswith("/usr/bin/") or real_s.startswith("/bin/"):
                return True

    # CodeRabbit often installs as `cr` — not cursor-agent
    if cmd_base == "cr":
        if "coderabbit" in real_s or real_name == "coderabbit":
            return True
        # Ambiguous bare `cr` without cursor in path → reject
        if "cursor" not in real_s:
            return True

    return False


def is_safe_executable(cmd: str, *, path: str | None = None) -> str | None:
    """Return absolute path if cmd is on PATH and not rejected, else None."""
    found = _which(cmd, path=path)
    if not found:
        return None
    if _is_rejected(cmd, found):
        return None
    return found


def _is_regular_executable(path: str) -> bool:
    """Return whether ``path`` names a regular executable file."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode) and os.access(path, os.X_OK)
    except (OSError, ValueError):
        return False


def _resolve_override_executable(
    agent_key: str, command: str, *, path: str | None = None
) -> str:
    """Validate and resolve an override command without bypassing safety policy."""
    is_path = (
        Path(command).is_absolute()
        or command.startswith(".")
        or os.sep in command
        or (os.altsep is not None and os.altsep in command)
    )
    try:
        resolved = command if is_path else _which(command, path=path)
    except (OSError, ValueError):
        resolved = None
    if resolved is None:
        raise ResolveError(
            f"override command not found on PATH for agent {agent_key!r}: {command!r}",
            reason=ResolveFailureReason.INVALID_OVERRIDE,
        )
    try:
        selected = (
            resolved
            if os.path.isabs(resolved)
            else os.path.join(os.getcwd(), resolved)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResolveError(
            f"override for agent {agent_key!r} is not a regular executable: "
            f"{command!r}",
            reason=ResolveFailureReason.INVALID_OVERRIDE,
        ) from exc
    if not _is_regular_executable(selected):
        raise ResolveError(
            f"override for agent {agent_key!r} is not a regular executable: "
            f"{command!r}",
            reason=ResolveFailureReason.INVALID_OVERRIDE,
        )
    try:
        target = str(Path(selected).resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResolveError(
            f"override for agent {agent_key!r} is not a regular executable: "
            f"{command!r}",
            reason=ResolveFailureReason.INVALID_OVERRIDE,
        ) from exc
    if _is_rejected(command, target):
        raise ResolveError(
            f"unsafe override for agent {agent_key!r} rejected: {command!r}",
            reason=ResolveFailureReason.INVALID_OVERRIDE,
        )
    return selected


def _spec_for_key(key: str) -> AgentSpec | None:
    key_l = key.strip().lower()
    mapped = _KEY_BY_TOKEN.get(key_l)
    if not mapped:
        return None
    for spec in AGENT_CATALOG:
        if spec.key == mapped:
            return spec
    return None


def _longest_catalog_prefix(parts: Sequence[str]) -> tuple[str, list[str]]:
    """Split argv at the longest leading token recognized by the catalog."""
    for end in range(len(parts), 0, -1):
        candidate = " ".join(parts[:end])
        if _spec_for_key(candidate) is not None:
            return candidate, list(parts[end:])
    return parts[0], list(parts[1:])


def resolve_agent_argv(
    name_or_alias: str,
    *,
    extra_args: Sequence[str] = (),
    overrides: Mapping[str, str | Sequence[str]] | None = None,
    prefer_aliases: bool = True,
    path: str | None = None,
) -> ResolvedCli:
    """Resolve a known agent name/alias to argv.

    ``overrides`` maps agent key (or alias) → command string or argv prefix.
    When set for this agent, override wins (source=``override``).
    """
    token = (name_or_alias or "").strip()
    if not token:
        raise ResolveError(
            "empty agent name",
            reason=ResolveFailureReason.EMPTY_COMMAND,
        )

    spec = _spec_for_key(token)
    if spec is None:
        raise ResolveError(
            f"unknown agent {token!r}; use a catalog name "
            f"({', '.join(s.key for s in AGENT_CATALOG)}) or ad-hoc argv",
            reason=ResolveFailureReason.UNKNOWN_AGENT,
        )

    # Overrides (config or caller)
    if overrides:
        override_key = next(
            (
                key
                for key in dict.fromkeys((spec.key, token, *spec.aliases))
                if key in overrides
            ),
            None,
        )
        if override_key is not None:
            ovr = overrides[override_key]
            if isinstance(ovr, str):
                try:
                    prefix = _split_simple(ovr)
                except ValueError as exc:
                    raise ResolveError(
                        f"malformed override for agent {spec.key!r}: {exc}",
                        reason=ResolveFailureReason.INVALID_OVERRIDE,
                    ) from exc
            else:
                prefix = [str(x) for x in ovr]
            if not prefix or not prefix[0]:
                raise ResolveError(
                    f"empty override for agent {spec.key!r}",
                    reason=ResolveFailureReason.INVALID_OVERRIDE,
                )
            first = prefix[0]
            executable = _resolve_override_executable(spec.key, first, path=path)
            prefix = [executable, *prefix[1:]]
            argv = [*prefix, *[str(a) for a in extra_args]]
            return ResolvedCli(
                agent_key=spec.key,
                argv=argv,
                source="override",
                command=argv[0],
            )

    candidates: list[tuple[str, str]] = []  # (cmd, source)
    if prefer_aliases:
        for alias in spec.aliases:
            candidates.append((alias, "alias"))
    candidates.append((spec.canonical, "canonical"))
    if not prefer_aliases:
        for alias in spec.aliases:
            candidates.append((alias, "alias"))

    seen: set[str] = set()
    for cmd, source in candidates:
        if cmd in seen:
            continue
        seen.add(cmd)
        found = is_safe_executable(cmd, path=path)
        if found:
            argv = [found, *[str(a) for a in extra_args]]
            return ResolvedCli(
                agent_key=spec.key,
                argv=argv,
                source=source,
                command=found,
            )

    tried = ", ".join(c for c, _ in candidates)
    raise ResolveError(
        f"no safe executable for token {token!r} (catalog agent {spec.key!r}; "
        f"tried: {tried}). Install the CLI, add a PATH shim for the alias, or "
        "set [agents] override.",
        reason=ResolveFailureReason.NO_SAFE_EXECUTABLE,
    )


def resolve_adhoc_argv(
    command: str | Sequence[str],
    *,
    extra_args: Sequence[str] = (),
    path: str | None = None,
    require_on_path: bool = True,
    require_safe_executable: bool = False,
) -> ResolvedCli:
    """Resolve free-form ad-hoc argv (no catalog alias magic)."""
    if isinstance(command, str):
        try:
            prefix = _split_simple(command)
        except ValueError as exc:
            raise ResolveError(
                f"malformed ad-hoc command: {exc}",
                reason=ResolveFailureReason.INVALID_ADHOC_COMMAND,
            ) from exc
    else:
        prefix = [str(x) for x in command]
    if not prefix or not prefix[0]:
        raise ResolveError(
            "empty ad-hoc command",
            reason=ResolveFailureReason.EMPTY_COMMAND,
        )

    first = prefix[0]
    if require_safe_executable:
        found = _which(first, path=path)
        if not found:
            candidate = Path(first)
            if candidate.exists() or candidate.is_symlink():
                message = (
                    "implicit ad-hoc command is not a regular executable file: "
                    f"{first!r}"
                )
            else:
                message = f"ad-hoc command not found on PATH: {first!r}"
            raise ResolveError(
                message,
                reason=ResolveFailureReason.INVALID_ADHOC_COMMAND,
            )
        if not _is_regular_executable(found):
            raise ResolveError(
                f"implicit ad-hoc command is not a regular executable file: {first!r}",
                reason=ResolveFailureReason.INVALID_ADHOC_COMMAND,
            )
        if _is_rejected(first, found):
            raise ResolveError(
                f"implicit ad-hoc command rejected as unsafe: {first!r}",
                reason=ResolveFailureReason.INVALID_ADHOC_COMMAND,
            )
        prefix = [found, *prefix[1:]]
    elif require_on_path and not first.startswith("/") and not first.startswith("."):
        found = _which(first, path=path)
        if not found:
            raise ResolveError(
                f"ad-hoc command not found on PATH: {first!r}",
                reason=ResolveFailureReason.INVALID_ADHOC_COMMAND,
            )
        prefix = [found, *prefix[1:]]
    elif first.startswith("/") or first.startswith("."):
        if not Path(first).exists() and require_on_path:
            # still allow if which finds it
            found = _which(first, path=path)
            if not found:
                raise ResolveError(
                    f"ad-hoc path not found: {first!r}",
                    reason=ResolveFailureReason.INVALID_ADHOC_COMMAND,
                )
            prefix = [found, *prefix[1:]]

    argv = [*prefix, *[str(a) for a in extra_args]]
    return ResolvedCli(
        agent_key="adhoc",
        argv=argv,
        source="adhoc",
        command=argv[0],
    )


def resolve_flexible(
    name_or_command: str,
    *,
    extra_args: Sequence[str] = (),
    overrides: Mapping[str, str | Sequence[str]] | None = None,
    prefer_aliases: bool = True,
    path: str | None = None,
    adhoc: bool = False,
) -> ResolvedCli:
    """Resolve a catalog command, or safely fall back for an unknown command head."""
    if adhoc:
        return resolve_adhoc_argv(
            name_or_command, extra_args=extra_args, path=path
        )
    try:
        command_parts = _split_simple(name_or_command)
    except ValueError:
        # Preserve resolve_adhoc_argv's structured malformed-command error.
        return resolve_adhoc_argv(
            name_or_command,
            extra_args=extra_args,
            path=path,
            require_safe_executable=True,
        )
    if not command_parts:
        # Preserve resolve_agent_argv's structured empty-command error.
        return resolve_agent_argv(
            name_or_command,
            extra_args=extra_args,
            overrides=overrides,
            prefer_aliases=prefer_aliases,
            path=path,
        )
    command_head, embedded_args = _longest_catalog_prefix(command_parts)
    try:
        return resolve_agent_argv(
            command_head,
            extra_args=[*embedded_args, *extra_args],
            overrides=overrides,
            prefer_aliases=prefer_aliases,
            path=path,
        )
    except ResolveError as exc:
        if exc.reason is not ResolveFailureReason.UNKNOWN_AGENT:
            raise
        # Only a genuinely unknown catalog token gets implicit ad-hoc policy.
        # resolve_adhoc_argv retains authority over its PATH/input validation.
        return resolve_adhoc_argv(
            name_or_command,
            extra_args=extra_args,
            path=path,
            require_safe_executable=True,
        )


def _split_simple(s: str) -> list[str]:
    """Minimal shell-ish split (no expansions). Empty → []."""
    s = s.strip()
    if not s:
        return []
    return shlex.split(s, posix=True)


def catalog_status(
    *,
    overrides: Mapping[str, str | Sequence[str]] | None = None,
    prefer_aliases: bool = True,
    path: str | None = None,
) -> list[dict[str, Any]]:
    """For doctor: per-catalog agent resolve result (soft)."""
    rows: list[dict[str, Any]] = []
    for spec in AGENT_CATALOG:
        try:
            resolved = resolve_agent_argv(
                spec.key,
                overrides=overrides,
                prefer_aliases=prefer_aliases,
                path=path,
            )
            rows.append(
                {
                    "agent": spec.key,
                    "ok": True,
                    "argv0": resolved.command,
                    "source": resolved.source,
                    "aliases": list(spec.aliases),
                    "canonical": spec.canonical,
                }
            )
        except ResolveError as exc:
            rows.append(
                {
                    "agent": spec.key,
                    "ok": False,
                    "error": str(exc),
                    "aliases": list(spec.aliases),
                    "canonical": spec.canonical,
                }
            )
    return rows

"""Coding-agent CLI resolution and spawn helpers (I005)."""

from hark.agents.resolve import (
    AGENT_CATALOG,
    ResolvedCli,
    ResolveError,
    resolve_agent_argv,
    resolve_adhoc_argv,
)

__all__ = [
    "AGENT_CATALOG",
    "ResolvedCli",
    "ResolveError",
    "resolve_agent_argv",
    "resolve_adhoc_argv",
]

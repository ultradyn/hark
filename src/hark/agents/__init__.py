"""Coding-agent CLI resolution and spawn helpers (I005)."""

from hark.agents.resolve import (
    AGENT_CATALOG,
    ResolvedCli,
    ResolveError,
    ResolveFailureReason,
    resolve_agent_argv,
    resolve_adhoc_argv,
    resolve_flexible,
)

__all__ = [
    "AGENT_CATALOG",
    "ResolvedCli",
    "ResolveError",
    "ResolveFailureReason",
    "resolve_agent_argv",
    "resolve_adhoc_argv",
    "resolve_flexible",
]

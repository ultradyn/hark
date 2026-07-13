"""Detect when hark itself runs inside a Herdr-managed pane.

Herdr exports ``HERDR_ENV``/``HERDR_PANE_ID``/``HERDR_SOCKET_PATH`` (and an
optional ``HERDR_SESSION``) into the panes it manages (see ``docs/HERDR.md``).
When ``hark watch`` runs inside such a pane, hark's own pane shows up in
``herdr agent list``; without exclusion, watch forwards events about — and reads
the pane of — hark's own session, creating a feedback loop.

``detect_self`` resolves the running pane's identity from the environment so
watch can filter it out before edge-detection and reaction.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from hark.herdr.client import AgentInfo

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() in _TRUTHY


def _resolve(path: str | os.PathLike[str] | None) -> str | None:
    """Canonical filesystem path for socket comparison (symlinks resolved)."""
    if not path:
        return None
    try:
        return os.path.realpath(os.path.expanduser(str(path)))
    except OSError:
        return os.path.abspath(os.path.expanduser(str(path)))


@dataclass(frozen=True)
class SelfIdentity:
    """The herdr pane hark is currently running inside."""

    pane_id: str
    socket_path: str | None = None
    session: str | None = None

    @property
    def target(self) -> str:
        return f"{self.session or 'local'}/{self.pane_id}"

    def matches_agent(
        self,
        agent: "AgentInfo",
        *,
        session_socket: str | os.PathLike[str] | None,
        session_is_remote: bool,
    ) -> bool:
        """Is *agent* hark's own pane?

        Pane id must match. When both socket paths are known they must resolve to
        the same file. When either socket is unknown, trust the pane match only
        for local (non-remote) sessions — hark cannot run inside a remote herdr,
        so a remote pane sharing an id must never be excluded.
        """
        if not self.pane_id or agent.pane_id != self.pane_id:
            return False
        self_sock = _resolve(self.socket_path)
        other_sock = _resolve(session_socket)
        if self_sock is not None and other_sock is not None:
            return self_sock == other_sock
        # One side unknown: pane id alone is only trustworthy for local sessions.
        return not session_is_remote


def detect_self(env: Mapping[str, str] | None = None) -> SelfIdentity | None:
    """Identify hark's own herdr pane from the environment, if any.

    Returns ``None`` when hark is not running inside a herdr pane, or when
    ``HARK_WATCH_INCLUDE_SELF`` is set truthy (escape hatch to disable
    self-exclusion for debugging).
    """
    env = os.environ if env is None else env
    if _is_truthy(env.get("HARK_WATCH_INCLUDE_SELF")):
        return None
    if not _is_truthy(env.get("HERDR_ENV")):
        return None
    pane_id = (env.get("HERDR_PANE_ID") or "").strip()
    if not pane_id:
        return None
    socket_path = (env.get("HERDR_SOCKET_PATH") or "").strip() or None
    session = (env.get("HERDR_SESSION") or "").strip() or None
    return SelfIdentity(pane_id=pane_id, socket_path=socket_path, session=session)

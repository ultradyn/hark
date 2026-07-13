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
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from hark.herdr.client import AgentInfo

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _is_truthy(value: object | None) -> bool:
    return isinstance(value, str) and value.strip().lower() in _TRUTHY


def _resolve(path: str | os.PathLike[str] | None) -> str | None:
    """Canonical filesystem path for socket comparison (symlinks resolved)."""
    if not path:
        return None
    try:
        return os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SelfIdentity:
    """The herdr pane hark is currently running inside."""

    pane_id: str
    socket_path: str | None = None
    session: str | None = None

    @property
    def target(self) -> str:
        return f"{self.session or 'local'}/{self.pane_id}"

    def matches_pane(
        self,
        pane_id: str,
        *,
        session_socket: str | os.PathLike[str] | None,
        session_is_remote: bool,
    ) -> bool:
        """Is *pane_id* hark's own pane on this local herdr server?

        Pane id and canonical socket path must both match. Missing or malformed
        socket paths deliberately fail closed: pane ids are only unique within a
        herdr server, so they cannot identify self across configured sessions.
        Remote/tunnelled sessions are never self, even if their local tunnel
        path happens to match malformed input.
        """
        if session_is_remote or not self.pane_id or pane_id != self.pane_id:
            return False
        self_sock = _resolve(self.socket_path)
        other_sock = _resolve(session_socket)
        return self_sock is not None and self_sock == other_sock

    def matches_agent(
        self,
        agent: "AgentInfo",
        *,
        session_socket: str | os.PathLike[str] | None,
        session_is_remote: bool,
    ) -> bool:
        """Is *agent* hark's own pane?"""
        return self.matches_pane(
            agent.pane_id,
            session_socket=session_socket,
            session_is_remote=session_is_remote,
        )


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
    pane_id_value = env.get("HERDR_PANE_ID")
    pane_id = pane_id_value.strip() if isinstance(pane_id_value, str) else ""
    if not pane_id:
        return None
    socket_value = env.get("HERDR_SOCKET_PATH")
    socket_path = socket_value.strip() if isinstance(socket_value, str) else ""
    if not _resolve(socket_path):
        return None
    session_value = env.get("HERDR_SESSION")
    session = session_value.strip() if isinstance(session_value, str) else ""
    return SelfIdentity(pane_id=pane_id, socket_path=socket_path, session=session)

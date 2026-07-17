"""Shared configured-session access for every Herdr-facing Hark surface."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Callable, Protocol

from hark.config import HarkConfig, SessionConfig
from hark.herdr.client import HerdrClient, HerdrError


class TunnelAdapter(Protocol):
    local_socket: object

    def stop(self) -> None: ...


class TunnelFactory(Protocol):
    def __call__(
        self,
        session_id: str,
        ssh: str,
        *,
        remote_socket: str | None = None,
    ) -> TunnelAdapter: ...


_ACTIVE_ACCESS: ContextVar[HerdrSessionAccess | None] = ContextVar(
    "hark_active_herdr_access", default=None
)


def _error_text(exc: BaseException) -> str:
    try:
        return str(exc)[:400]
    except BaseException:
        return type(exc).__name__


class HerdrSessionAccess:
    """Resolve configured sessions and own any SSH-tunnel leases.

    A scope caches one client per selected session. Configured transport metadata
    is retained when the effective socket changes to a Hark-managed tunnel or a
    local named-session socket.
    """

    def __init__(
        self,
        cfg: HarkConfig,
        *,
        client_factory: Callable[[SessionConfig], HerdrClient] = HerdrClient,
        tunnel_factory: TunnelFactory | None = None,
    ) -> None:
        if tunnel_factory is None:
            from hark.herdr.tunnel import ensure_tunnel

            tunnel_factory = ensure_tunnel
        self.cfg = cfg
        self._client_factory = client_factory
        self._tunnel_factory = tunnel_factory
        self._clients: dict[str, HerdrClient] = {}
        self._named_clients: dict[tuple[str, str], HerdrClient] = {}
        self._tunnels: list[tuple[str, TunnelAdapter]] = []
        self._token: Token[HerdrSessionAccess | None] | None = None

    def _configured_session(self, session_id: str) -> SessionConfig:
        session = self.cfg.session_by_id(session_id)
        if session is not None:
            return session
        # HarkConfig() is a common lightweight local-only library value. Do not
        # extend this compatibility case to unknown names in a real config.
        if not self.cfg.sessions and session_id == "local":
            return SessionConfig(id="local")
        raise HerdrError(f"unknown configured Herdr session {session_id!r}")

    def _acquire_tunnel(
        self,
        configured: SessionConfig,
        *,
        remote_socket: str | None,
        identity: str,
    ) -> TunnelAdapter:
        assert configured.ssh is not None
        try:
            tunnel = self._tunnel_factory(
                configured.id,
                configured.ssh,
                remote_socket=remote_socket,
            )
        except Exception as exc:
            raise HerdrError(
                f"Herdr session {identity!r} tunnel failed: {_error_text(exc)}"
            ) from exc
        self._tunnels.append((identity, tunnel))
        return tunnel

    def client(self, session_id: str) -> HerdrClient:
        """Return a client for exactly one configured session."""
        cached = self._clients.get(session_id)
        if cached is not None:
            return cached

        configured = self._configured_session(session_id)
        effective = configured
        if configured.ssh and not configured.socket:
            tunnel = self._acquire_tunnel(
                configured,
                remote_socket=configured.remote_socket,
                identity=configured.id,
            )
            effective = replace(configured, socket=str(tunnel.local_socket))

        client = self._client_factory(effective)
        self._clients[session_id] = client
        return client

    def named_client(
        self,
        configured_session_id: str,
        named_session_id: str,
        *,
        start: bool = True,
    ) -> HerdrClient:
        """Select a named Herdr server without losing configured transport."""
        key = (configured_session_id, named_session_id)
        cached = self._named_clients.get(key)
        if cached is not None:
            return cached

        name = (named_session_id or "").strip()
        path = PurePosixPath(name)
        if not name or path.name != name or name in {".", ".."}:
            raise HerdrError(f"invalid Herdr session name {named_session_id!r}")

        configured = self._configured_session(configured_session_id)
        if configured.ssh:
            # Hark never starts a remote Herdr server. An explicit socket is an
            # authoritative externally-managed forward. Otherwise tunnel the
            # configured destination, deriving Herdr's conventional named path
            # only when no destination was configured.
            effective = replace(configured, id=name)
            if not configured.socket:
                remote_socket = configured.remote_socket or (
                    f"~/.config/herdr/sessions/{name}/herdr.sock"
                )
                tunnel = self._acquire_tunnel(
                    configured,
                    remote_socket=remote_socket,
                    identity=name,
                )
                effective = replace(
                    effective,
                    socket=str(tunnel.local_socket),
                    remote_socket=remote_socket,
                )
            client = self._client_factory(effective)
            health = client.health()
            ok = health.get("ok", False) if isinstance(health, dict) else health.ok
            if not ok:
                error = (
                    health.get("error")
                    if isinstance(health, dict)
                    else getattr(health, "error", None)
                )
                raise HerdrError(
                    f"remote named Herdr session {name!r} is unavailable; "
                    "Hark does not start remote Herdr servers"
                    + (f": {error}" if error else "")
                )
        else:
            base = self.client(configured_session_id)
            info = base.ensure_session(name, start=start)
            if not info.socket_path:
                raise HerdrError(
                    f"Herdr named session {name!r} did not report its socket"
                )
            client = self._client_factory(
                replace(configured, id=name, socket=str(info.socket_path))
            )

        self._named_clients[key] = client
        return client

    def __enter__(self) -> HerdrSessionAccess:
        if self._token is not None:
            raise RuntimeError("HerdrSessionAccess cannot re-enter the same scope")
        self._token = _ACTIVE_ACCESS.set(self)
        return self

    def close(self) -> None:
        failures: list[str] = []
        for identity, tunnel in reversed(self._tunnels):
            try:
                tunnel.stop()
            except Exception as exc:
                failures.append(f"{identity}: {_error_text(exc)}")
        self._tunnels.clear()
        self._clients.clear()
        self._named_clients.clear()
        if failures:
            raise HerdrError("failed to close Herdr tunnel(s): " + "; ".join(failures))

    def __exit__(self, exc_type, exc, tb) -> bool:
        token = self._token
        self._token = None
        if token is not None:
            _ACTIVE_ACCESS.reset(token)
        try:
            self.close()
        except Exception:
            if exc is None:
                raise
        return False


def active_client(cfg: HarkConfig, session_id: str) -> HerdrClient:
    """Resolve a client only through the active shared access scope."""
    access = _ACTIVE_ACCESS.get()
    if access is None or access.cfg is not cfg:
        raise RuntimeError("Herdr client requested outside a session access scope")
    return access.client(session_id)


def active_named_client(
    cfg: HarkConfig,
    configured_session_id: str,
    named_session_id: str,
    *,
    start: bool = True,
) -> HerdrClient:
    access = _ACTIVE_ACCESS.get()
    if access is None or access.cfg is not cfg:
        raise RuntimeError("Herdr client requested outside a session access scope")
    return access.named_client(
        configured_session_id,
        named_session_id,
        start=start,
    )

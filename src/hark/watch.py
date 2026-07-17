"""Multi-session watch → HEP events (poll and/or socket)."""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, Callable, TextIO

from hark.config import HarkConfig
from hark.delivery import DeliveryStore
from hark.events import (
    DEFAULT_PANE_CAPTURE_LINES,
    DEFAULT_PANE_CAPTURE_MAX_CHARS,
    make_watch_armed,
    make_watch_error,
    make_watch_heartbeat,
    make_target_invalidated,
    monitor_profile,
)
from hark.exitcodes import HERDR
from hark.herdr.access import HerdrSessionAccess
from hark.herdr.client import AgentInfo, HerdrClient, HerdrError
from hark.herdr.socket_client import is_expected_disconnect
from hark.herdr.tunnel import ensure_tunnel
from hark.pane_understanding.classify import EdgeTracker, PaneClassifier
from hark.pane_understanding.types import ClassifyPolicy, PaneObservation
from hark.self_detect import SelfIdentity, detect_self

# Min gap between watch.error for expected socket drops (Broken pipe, reset, …).
_EXPECTED_DISCONNECT_ERROR_INTERVAL_S = 60.0
_SOCKET_RECONNECT_BACKOFF_MIN_S = 0.25
_SOCKET_RECONNECT_BACKOFF_MAX_S = 10.0
# If a subscribe session lived this long, treat the next drop as healthy→quick reconnect.
_SOCKET_HEALTHY_SESSION_S = 2.0


class _WatchErrorLimiter:
    """Rate-limit repeated expected-disconnect watch.error emissions."""

    def __init__(self, interval_s: float = _EXPECTED_DISCONNECT_ERROR_INTERVAL_S) -> None:
        self.interval_s = interval_s
        self._last_emit = 0.0
        self._suppressed = 0

    def allow(self) -> tuple[bool, int]:
        """Return (should_emit, suppressed_since_last_emit)."""
        now = time.monotonic()
        if now - self._last_emit >= self.interval_s:
            suppressed = self._suppressed
            self._suppressed = 0
            self._last_emit = now
            return True, suppressed
        self._suppressed += 1
        return False, 0



def _observations_for_agents(
    agents: list[AgentInfo],
    *,
    read_pane: Callable[[AgentInfo], str | None] | None,
) -> list[PaneObservation]:
    """I/O → PaneObservation list for PaneClassifier (no policy here)."""
    out: list[PaneObservation] = []
    for agent in agents:
        text_body = read_pane(agent) if read_pane is not None else None
        out.append(
            PaneObservation(
                session_id=agent.session_id,
                pane_id=agent.pane_id,
                status=agent.status,
                pane_text=text_body,
                agent=agent.agent,
                revision=int(agent.revision or 0),
                raw_agent=agent,
            )
        )
    return out


def _classify_and_emit(
    classifier: PaneClassifier,
    agents: list[AgentInfo],
    *,
    interest: set[str],
    detect_false_done: bool,
    read_pane: Callable[[AgentInfo], str | None] | None,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    """Watch seam: build observations, classify, emit HEPs."""
    obs = _observations_for_agents(agents, read_pane=read_pane)
    for event in classifier.process_observations(
        obs,
        interest=interest,
        detect_false_done=detect_false_done,
    ):
        emit(event)


# Re-export for tests that import EdgeTracker from hark.watch.
__all__ = ["EdgeTracker", "PaneClassifier", "run_watch"]


def _filter_self(
    agents: list[AgentInfo],
    self_ident: SelfIdentity | None,
    client: HerdrClient,
) -> list[AgentInfo]:
    """Drop hark's own pane so watch never forwards or reacts to itself.

    When hark runs inside a herdr pane, that pane appears in ``agent list``.
    Filtering it here (before edge-detection and pane reads) prevents both the
    event feedback loop and self-pane reads.
    """
    if self_ident is None:
        return agents
    session_socket = getattr(client, "socket_path", None)
    session_is_remote = bool(getattr(client.session, "ssh", None))
    return [
        agent
        for agent in agents
        if not self_ident.matches_agent(
            agent,
            session_socket=session_socket,
            session_is_remote=session_is_remote,
        )
    ]


def run_watch(
    cfg: HarkConfig,
    *,
    session_ids: list[str] | None = None,
    statuses: list[str] | None = None,
    for_monitor: bool = False,
    transport: str | None = None,
    once: bool = False,
    out: TextIO | None = None,
    read_questions: bool = True,
    register_events: bool = True,
) -> int:
    with HerdrSessionAccess(
        cfg,
        client_factory=HerdrClient,
        tunnel_factory=ensure_tunnel,
    ) as access:
        return _run_watch_scoped(
            cfg,
            access=access,
            session_ids=session_ids,
            statuses=statuses,
            for_monitor=for_monitor,
            transport=transport,
            once=once,
            out=out,
            read_questions=read_questions,
            register_events=register_events,
        )


def _run_watch_scoped(
    cfg: HarkConfig,
    *,
    access: HerdrSessionAccess,
    session_ids: list[str] | None = None,
    statuses: list[str] | None = None,
    for_monitor: bool = False,
    transport: str | None = None,
    once: bool = False,
    out: TextIO | None = None,
    read_questions: bool = True,
    register_events: bool = True,
) -> int:
    out = out or sys.stdout
    transport = (transport or cfg.watch.transport or "auto").lower()
    interest = set(statuses or cfg.watch.statuses)
    selected_ids = list(session_ids or [session.id for session in cfg.sessions])
    if not selected_ids and not session_ids:
        selected_ids = ["local"]

    clients: list[HerdrClient] = []
    for session_id in selected_ids:
        try:
            clients.append(access.client(session_id))
        except HerdrError as exc:
            _emit(
                make_watch_error(session_id, str(exc)),
                for_monitor=for_monitor,
                out=out,
            )
    if not clients:
        return HERDR

    pane_capture = bool(getattr(cfg.watch, "pane_capture", True))
    pane_capture_lines = int(
        getattr(cfg.watch, "pane_capture_lines", DEFAULT_PANE_CAPTURE_LINES)
    )
    pane_capture_max_chars = int(
        getattr(cfg.watch, "pane_capture_max_chars", DEFAULT_PANE_CAPTURE_MAX_CHARS)
    )
    # Always read a full viewport-ish block: menus are at the bottom, but
    # Grok Tasks/subagent chrome is near the top (B096). Attachment of
    # pane_capture to HEP is controlled separately by ``pane_capture``.
    read_lines = max(40, pane_capture_lines)

    classifier = PaneClassifier(
        ClassifyPolicy(
            interest=frozenset(interest),
            detect_false_done=bool(getattr(cfg.watch, "detect_false_done", True)),
            pane_capture=pane_capture,
            pane_capture_lines=pane_capture_lines,
            pane_capture_max_chars=pane_capture_max_chars,
        )
    )
    self_ident = detect_self()
    store = DeliveryStore() if register_events else None
    poll_s = max(0.2, cfg.watch.poll_ms / 1000.0)
    heartbeat_s = max(5.0, cfg.watch.heartbeat_s)
    last_heartbeat = time.monotonic()

    detect_false_done = bool(getattr(cfg.watch, "detect_false_done", True))

    def emit(event: dict[str, Any]) -> None:
        question = event.get("question")
        fingerprint = (
            question.get("fingerprint") if isinstance(question, dict) else None
        )
        if (
            store
            and event.get("kind")
            in ("agent.blocked", "agent.question_changed", "agent.needs_input")
            and isinstance(fingerprint, str)
            and fingerprint.strip()
        ):
            try:
                store.register_from_hep(event)
            except Exception:
                pass
        _emit(event, for_monitor=for_monitor, out=out)

    emit(
        make_watch_armed(
            [c.session.id for c in clients],
            transport=transport if transport != "auto" else "poll",
            statuses=sorted(interest),
            self_target=self_ident.target if self_ident else None,
        )
    )

    def read_pane(agent: AgentInfo) -> str | None:
        """Herdr pane read for PaneObservation.pane_text (I/O only).

        Full bounded body: menus at bottom; Tasks/subagent chrome often at top
        (B096). Classifier splits excerpts + optional pane_capture.
        """
        if not read_questions:
            return None
        try:
            client = next(c for c in clients if c.session.id == agent.session_id)
            text = client.read_pane(agent.pane_id, lines=read_lines)
            if not text or not str(text).strip():
                return None
            return text
        except (HerdrError, StopIteration):
            return None

    # Socket path for single local session when requested
    use_socket = transport == "socket" or (
        transport == "auto" and len(clients) == 1 and clients[0].socket_exists()
    )
    fallback_limiter = _WatchErrorLimiter()
    if use_socket and transport != "poll" and not once:
        try:
            return _watch_socket(
                clients[0],
                classifier=classifier,
                interest=interest,
                emit=emit,
                heartbeat_s=heartbeat_s,
                sessions=[c.session.id for c in clients],
                read_pane=read_pane if read_questions else None,
                store=store,
                detect_false_done=detect_false_done,
                self_ident=self_ident,
            )
        except Exception as exc:
            # Expected disconnects should normally reconnect inside _watch_socket.
            # If we still land here (or process only tried once), rate-limit spam.
            _emit_watch_error(
                emit,
                clients[0].session.id,
                f"socket watch failed, poll: {exc}",
                exc=exc,
                limiter=fallback_limiter,
            )

    try:
        while True:
            for client in clients:
                try:
                    agents = client.list_agents()
                except HerdrError as exc:
                    emit(make_watch_error(client.session.id, str(exc)))
                    continue
                agents = _filter_self(agents, self_ident, client)
                _classify_and_emit(
                    classifier,
                    agents,
                    interest=interest,
                    detect_false_done=detect_false_done,
                    read_pane=read_pane if read_questions else None,
                    emit=emit,
                )
            if once:
                return 0
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_s:
                emit(make_watch_heartbeat([c.session.id for c in clients]))
                last_heartbeat = now
            time.sleep(poll_s)
    except KeyboardInterrupt:
        return 0


def _emit(event: dict[str, Any], *, for_monitor: bool, out: TextIO) -> None:
    payload = monitor_profile(event) if for_monitor else event
    out.write(json.dumps(payload, separators=(",", ":")) + "\n")
    out.flush()


def _emit_watch_error(
    emit: Callable[[dict[str, Any]], None],
    session_id: str,
    message: str,
    *,
    exc: BaseException | None = None,
    limiter: _WatchErrorLimiter | None = None,
) -> None:
    """Emit watch.error; rate-limit when *exc* is an expected disconnect."""
    if exc is not None and is_expected_disconnect(exc) and limiter is not None:
        allowed, suppressed = limiter.allow()
        if not allowed:
            return
        if suppressed:
            message = f"{message} ({suppressed} similar suppressed)"
    emit(make_watch_error(session_id, message))


def _watch_socket(
    client: HerdrClient,
    *,
    classifier: PaneClassifier,
    interest: set[str],
    emit: Callable[[dict[str, Any]], None],
    heartbeat_s: float,
    sessions: list[str],
    read_pane: Callable[[AgentInfo], str | None] | None,
    store: DeliveryStore | None,
    detect_false_done: bool = True,
    self_ident: SelfIdentity | None = None,
) -> int:
    """Hybrid: socket events trigger refresh + poll edges; heartbeat thread.

    Reconnects quietly on expected peer drops (Broken pipe, connection reset,
    EPIPE, brief socket absence). Real protocol failures propagate so
    ``run_watch`` can fall back to poll with a visible watch.error.
    """
    from hark.herdr.socket_client import run_subscribe_loop

    stop = threading.Event()
    limiter = _WatchErrorLimiter()
    backoff_s = _SOCKET_RECONNECT_BACKOFF_MIN_S

    def heartbeat() -> None:
        while not stop.wait(heartbeat_s):
            emit(make_watch_heartbeat(sessions))

    def reconcile() -> None:
        try:
            agents = client.list_agents()
        except HerdrError as exc:
            # list_agents failures are operational; always surface (not disconnect spam).
            emit(make_watch_error(client.session.id, str(exc)))
            return
        agents = _filter_self(agents, self_ident, client)
        _classify_and_emit(
            classifier,
            agents,
            interest=interest,
            detect_false_done=detect_false_done,
            read_pane=read_pane,
            emit=emit,
        )

    def on_wire(raw: dict[str, Any]) -> None:
        if _handle_lifecycle_event(
            raw,
            client=client,
            store=store,
            emit=emit,
            self_ident=self_ident,
        ):
            return
        try:
            agents = client.list_agents()
        except HerdrError as exc:
            emit(make_watch_error(client.session.id, str(exc)))
            return
        agents = _filter_self(agents, self_ident, client)
        _classify_and_emit(
            classifier,
            agents,
            interest=interest,
            detect_false_done=detect_false_done,
            read_pane=read_pane,
            emit=emit,
        )

    # initial reconcile
    reconcile()

    t = threading.Thread(target=heartbeat, daemon=True)
    t.start()
    try:
        while True:
            started = time.monotonic()
            try:
                run_subscribe_loop(client.socket_path, on_wire)
                return 0
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                if not is_expected_disconnect(exc):
                    # Protocol/API failure → outer poll fallback with full error.
                    raise
                ran_for = time.monotonic() - started
                _emit_watch_error(
                    emit,
                    client.session.id,
                    f"socket disconnected, reconnecting: {exc}",
                    exc=exc,
                    limiter=limiter,
                )
                # Edge-detect via CLI while socket is down.
                reconcile()
                if ran_for >= _SOCKET_HEALTHY_SESSION_S:
                    backoff_s = _SOCKET_RECONNECT_BACKOFF_MIN_S
                time.sleep(backoff_s)
                if ran_for < _SOCKET_HEALTHY_SESSION_S:
                    backoff_s = min(
                        _SOCKET_RECONNECT_BACKOFF_MAX_S,
                        backoff_s * 2,
                    )
    finally:
        stop.set()
    return 0


_LIFECYCLE_TYPES = frozenset({"pane.closed", "pane.exited", "pane.moved"})


def _handle_lifecycle_event(
    raw: dict[str, Any],
    *,
    client: HerdrClient,
    store: DeliveryStore | None,
    emit: Callable[[dict[str, Any]], None],
    self_ident: SelfIdentity | None = None,
) -> bool:
    """Invalidate bound answers from a socket lifecycle event, if present.

    Stays in watch (P1.M3 E3.T002): delivery invalidation is I/O-side, not
    Pane Understanding. Classifier is not notified; closed panes simply stop
    appearing in list_agents observations.
    """
    containers: list[dict[str, Any]] = []
    todo: list[dict[str, Any]] = [raw]
    event_type: str | None = None
    while todo:
        value = todo.pop()
        containers.append(value)
        for key, child in value.items():
            if key in ("type", "method", "event", "name") and isinstance(child, str):
                if child in _LIFECYCLE_TYPES:
                    event_type = child
            if key in ("params", "data", "result", "event", "payload", "pane", "target") and isinstance(child, dict):
                todo.append(child)
    if event_type is None:
        return False
    pane_id = next(
        (str(container["pane_id"]) for container in containers if container.get("pane_id")),
        None,
    )
    if pane_id is None:
        pane_id = next(
            (str(container["id"]) for container in containers if container.get("id")),
            None,
        )
    if pane_id is None:
        return False
    if self_ident is not None and self_ident.matches_pane(
        pane_id,
        session_socket=getattr(client, "socket_path", None),
        session_is_remote=bool(getattr(client.session, "ssh", None)),
    ):
        # Do not forward lifecycle changes from hark's own pane either.
        return True
    invalidated = (
        store.invalidate_target(client.session.id, pane_id, reason=event_type)
        if store is not None
        else []
    )
    emit(
        make_target_invalidated(
            client.session.id,
            pane_id,
            reason=event_type,
            event_ids=[event.event_id for event in invalidated],
        )
    )
    return True

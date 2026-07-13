"""Multi-session watch → HEP events (poll and/or socket)."""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Any, Callable, TextIO

from hark.config import HarkConfig, SessionConfig
from hark.delivery import DeliveryStore
from hark.events import (
    make_agent_status_event,
    make_watch_armed,
    make_watch_error,
    make_watch_heartbeat,
    make_target_invalidated,
    monitor_profile,
)
from hark.fingerprint import question_fingerprint
from hark.herdr.client import AgentInfo, HerdrClient, HerdrError
from hark.herdr.tunnel import Tunnel, ensure_tunnel


class EdgeTracker:
    def __init__(self) -> None:
        self._status: dict[tuple[str, str], str] = {}
        self._dedupe: set[tuple[str, str, str, str]] = set()

    def process(
        self,
        agents: list[AgentInfo],
        *,
        interest: set[str],
        question_for: Callable[[AgentInfo], str | None] | None = None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for agent in agents:
            key = (agent.session_id, agent.pane_id)
            prev = self._status.get(key)
            cur = agent.status
            if prev == cur:
                continue
            self._status[key] = cur

            if prev is None:
                if cur != "blocked" or "blocked" not in interest:
                    continue
            elif cur not in interest and prev not in interest:
                continue

            q_text = None
            if cur == "blocked" and question_for:
                q_text = question_for(agent)
            fp = question_fingerprint(q_text or "", None) if q_text else ""
            dkey = (agent.session_id, agent.pane_id, cur, fp)
            if cur == "blocked" and fp and dkey in self._dedupe:
                continue
            if cur == "blocked" and fp:
                self._dedupe.add(dkey)

            if cur in interest or (prev is not None and prev in interest):
                events.append(
                    make_agent_status_event(
                        agent,
                        from_status=prev,
                        to_status=cur,
                        question_text=q_text,
                    )
                )
        return events


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
    out = out or sys.stdout
    transport = (transport or cfg.watch.transport or "auto").lower()
    interest = set(statuses or cfg.watch.statuses)
    sessions = cfg.sessions
    if session_ids:
        want = set(session_ids)
        sessions = [s for s in cfg.sessions if s.id in want]
        if not sessions:
            sessions = [SessionConfig(id=sid) for sid in session_ids]

    tunnels: list[Tunnel] = []
    clients: list[HerdrClient] = []
    for s in sessions:
        if s.ssh:
            try:
                t = ensure_tunnel(s.id, s.ssh, remote_socket=s.remote_socket)
                tunnels.append(t)
                s = SessionConfig(
                    id=s.id,
                    socket=str(t.local_socket),
                    ssh=s.ssh,
                    label=s.label,
                )
            except Exception as exc:
                _emit(
                    make_watch_error(s.id, f"tunnel: {exc}"),
                    for_monitor=for_monitor,
                    out=out,
                )
                continue
        clients.append(HerdrClient(s))

    tracker = EdgeTracker()
    store = DeliveryStore() if register_events else None
    poll_s = max(0.2, cfg.watch.poll_ms / 1000.0)
    heartbeat_s = max(5.0, cfg.watch.heartbeat_s)
    last_heartbeat = time.monotonic()

    def emit(event: dict[str, Any]) -> None:
        question = event.get("question")
        fingerprint = (
            question.get("fingerprint") if isinstance(question, dict) else None
        )
        if (
            store
            and event.get("kind") in ("agent.blocked", "agent.question_changed")
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
        )
    )

    def question_for(agent: AgentInfo) -> str | None:
        if not read_questions:
            return None
        try:
            client = next(c for c in clients if c.session.id == agent.session_id)
            text = client.read_pane(agent.pane_id, lines=40)
            from hark.events import extract_question_excerpt

            return extract_question_excerpt(text) or None
        except (HerdrError, StopIteration):
            return None

    # Socket path for single local session when requested
    use_socket = transport == "socket" or (
        transport == "auto" and len(clients) == 1 and clients[0].socket_exists()
    )
    if use_socket and transport != "poll" and not once:
        try:
            return _watch_socket(
                clients[0],
                tracker=tracker,
                interest=interest,
                emit=emit,
                heartbeat_s=heartbeat_s,
                sessions=[c.session.id for c in clients],
                question_for=question_for if read_questions else None,
                store=store,
            )
        except Exception as exc:
            emit(make_watch_error(clients[0].session.id, f"socket watch failed, poll: {exc}"))

    try:
        while True:
            for client in clients:
                try:
                    agents = client.list_agents()
                except HerdrError as exc:
                    emit(make_watch_error(client.session.id, str(exc)))
                    continue
                for event in tracker.process(
                    agents, interest=interest, question_for=question_for
                ):
                    emit(event)
            if once:
                return 0
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat_s:
                emit(make_watch_heartbeat([c.session.id for c in clients]))
                last_heartbeat = now
            time.sleep(poll_s)
    except KeyboardInterrupt:
        return 0
    finally:
        for t in tunnels:
            t.stop()


def _emit(event: dict[str, Any], *, for_monitor: bool, out: TextIO) -> None:
    payload = monitor_profile(event) if for_monitor else event
    out.write(json.dumps(payload, separators=(",", ":")) + "\n")
    out.flush()


def _watch_socket(
    client: HerdrClient,
    *,
    tracker: EdgeTracker,
    interest: set[str],
    emit: Callable[[dict[str, Any]], None],
    heartbeat_s: float,
    sessions: list[str],
    question_for: Callable[[AgentInfo], str | None] | None,
    store: DeliveryStore | None,
) -> int:
    """Hybrid: socket events trigger refresh + poll edges; heartbeat thread."""
    from hark.herdr.socket_client import run_subscribe_loop

    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(heartbeat_s):
            emit(make_watch_heartbeat(sessions))

    def on_wire(raw: dict[str, Any]) -> None:
        if _handle_lifecycle_event(raw, client=client, store=store, emit=emit):
            return
        try:
            agents = client.list_agents()
        except HerdrError as exc:
            emit(make_watch_error(client.session.id, str(exc)))
            return
        for event in tracker.process(
            agents, interest=interest, question_for=question_for
        ):
            emit(event)

    # initial reconcile
    try:
        agents = client.list_agents()
        for event in tracker.process(
            agents, interest=interest, question_for=question_for
        ):
            emit(event)
    except HerdrError as exc:
        emit(make_watch_error(client.session.id, str(exc)))

    t = threading.Thread(target=heartbeat, daemon=True)
    t.start()
    try:
        run_subscribe_loop(client.socket_path, on_wire)
    except KeyboardInterrupt:
        return 0
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
) -> bool:
    """Invalidate bound answers from a socket lifecycle event, if present."""
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

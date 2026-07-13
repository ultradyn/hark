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
    is_idle_like_status,
    looks_like_pending_question,
    make_agent_needs_input,
    make_agent_question_changed,
    make_agent_status_event,
    make_watch_armed,
    make_watch_error,
    make_watch_heartbeat,
    make_target_invalidated,
    monitor_profile,
)
from hark.fingerprint import question_fingerprint
from hark.herdr.client import AgentInfo, HerdrClient, HerdrError
from hark.herdr.socket_client import is_expected_disconnect
from hark.herdr.tunnel import Tunnel, ensure_tunnel

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


class EdgeTracker:
    """Edge-detect agent status changes; surface false-done menus as needs_input."""

    def __init__(self) -> None:
        self._status: dict[tuple[str, str], str] = {}
        self._dedupe: set[tuple[str, str, str, str]] = set()
        # Last question fingerprint while awaiting input (blocked or false-done).
        self._last_fp: dict[tuple[str, str], str] = {}
        # Status value for which we already ran a false-done pane inspect.
        self._false_done_scanned: dict[tuple[str, str], str] = {}

    def process(
        self,
        agents: list[AgentInfo],
        *,
        interest: set[str],
        question_for: Callable[[AgentInfo], str | None] | None = None,
        detect_false_done: bool = True,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for agent in agents:
            key = (agent.session_id, agent.pane_id)
            prev = self._status.get(key)
            cur = agent.status

            # Same status: optional question_changed while still blocked, or
            # re-scan idle-like for a newly appeared menu (rare but cheap).
            if prev == cur:
                events.extend(
                    self._same_status_events(
                        agent,
                        key=key,
                        cur=cur,
                        interest=interest,
                        question_for=question_for,
                        detect_false_done=detect_false_done,
                    )
                )
                continue

            self._status[key] = cur
            first_seen = prev is None

            # First observation: only surface blocked (or false-done on idle-like).
            if first_seen:
                if cur == "blocked" and "blocked" in interest:
                    events.extend(
                        self._emit_blocked(
                            agent, key=key, prev=prev, cur=cur, question_for=question_for
                        )
                    )
                elif (
                    detect_false_done
                    and is_idle_like_status(cur)
                    and question_for
                    and self._watch_cares_about_input(interest)
                ):
                    events.extend(
                        self._maybe_false_done(
                            agent,
                            key=key,
                            prev=prev,
                            cur=cur,
                            question_for=question_for,
                            also_completed=cur == "done" and "done" in interest,
                        )
                    )
                continue

            # Leaving interest entirely (e.g. working→working never hits here).
            if cur not in interest and prev not in interest:
                # Still catch false done when status becomes idle-like even if
                # "done" was not in interest but blocked was (Mode A often has both).
                if (
                    detect_false_done
                    and is_idle_like_status(cur)
                    and question_for
                    and self._watch_cares_about_input(interest)
                ):
                    events.extend(
                        self._maybe_false_done(
                            agent,
                            key=key,
                            prev=prev,
                            cur=cur,
                            question_for=question_for,
                            also_completed=False,
                        )
                    )
                continue

            if cur == "blocked":
                events.extend(
                    self._emit_blocked(
                        agent, key=key, prev=prev, cur=cur, question_for=question_for
                    )
                )
                continue

            if (
                detect_false_done
                and is_idle_like_status(cur)
                and question_for
                and self._watch_cares_about_input(interest)
            ):
                false_done_events = self._maybe_false_done(
                    agent,
                    key=key,
                    prev=prev,
                    cur=cur,
                    question_for=question_for,
                    also_completed=(
                        cur in interest or prev in interest
                    )
                    and (cur == "done" or "done" in interest or prev in interest),
                )
                if false_done_events:
                    events.extend(false_done_events)
                    continue

            if cur in interest or prev in interest:
                events.append(
                    make_agent_status_event(
                        agent,
                        from_status=prev,
                        to_status=cur,
                        question_text=None,
                    )
                )
        return events

    @staticmethod
    def _watch_cares_about_input(interest: set[str]) -> bool:
        return bool(interest & {"blocked", "done", "idle"})

    def _emit_blocked(
        self,
        agent: AgentInfo,
        *,
        key: tuple[str, str],
        prev: str | None,
        cur: str,
        question_for: Callable[[AgentInfo], str | None] | None,
    ) -> list[dict[str, Any]]:
        q_text = question_for(agent) if question_for else None
        fp = question_fingerprint(q_text or "", None) if q_text else ""
        dkey = (agent.session_id, agent.pane_id, cur, fp)
        if fp and dkey in self._dedupe:
            return []
        if fp:
            self._dedupe.add(dkey)
            self._last_fp[key] = fp
        return [
            make_agent_status_event(
                agent,
                from_status=prev,
                to_status=cur,
                question_text=q_text,
            )
        ]

    def _maybe_false_done(
        self,
        agent: AgentInfo,
        *,
        key: tuple[str, str],
        prev: str | None,
        cur: str,
        question_for: Callable[[AgentInfo], str | None],
        also_completed: bool,
    ) -> list[dict[str, Any]]:
        self._false_done_scanned[key] = cur
        q_text = question_for(agent)

        def _completed_only(q: str | None = None) -> list[dict[str, Any]]:
            if not also_completed:
                return []
            return [
                make_agent_status_event(
                    agent, from_status=prev, to_status=cur, question_text=q
                )
            ]

        if not q_text:
            return _completed_only()

        hit = looks_like_pending_question(q_text)
        if not hit:
            return _completed_only()

        fp = question_fingerprint(q_text, list(hit.choices) or None)
        dkey = (agent.session_id, agent.pane_id, "needs_input", fp)
        if fp and dkey in self._dedupe:
            return _completed_only(q_text)
        if fp:
            self._dedupe.add(dkey)
            self._last_fp[key] = fp

        out: list[dict[str, Any]] = [
            make_agent_needs_input(
                agent,
                from_status=prev,
                to_status=cur,
                question_text=q_text,
                hit=hit,
            )
        ]
        if also_completed and cur == "done":
            completed = make_agent_status_event(
                agent,
                from_status=prev,
                to_status=cur,
                question_text=q_text,
            )
            # Lower priority so needs_input wins attention in sorted UIs.
            completed["priority"] = min(int(completed.get("priority") or 50), 40)
            completed["false_done"] = True
            out.append(completed)
        elif also_completed and cur != "done":
            out.extend(_completed_only(q_text))
        return out

    def _same_status_events(
        self,
        agent: AgentInfo,
        *,
        key: tuple[str, str],
        cur: str,
        interest: set[str],
        question_for: Callable[[AgentInfo], str | None] | None,
        detect_false_done: bool,
    ) -> list[dict[str, Any]]:
        """While status is unchanged: question_changed (blocked) or late false-done.

        Idle-like re-scan only once per status epoch (avoids pane-read spam).
        """
        if not question_for:
            return []

        # Re-block heuristic: still blocked, question text changed.
        if cur == "blocked" and "blocked" in interest:
            q_text = question_for(agent)
            if not q_text:
                return []
            fp = question_fingerprint(q_text, None)
            prev_fp = self._last_fp.get(key)
            if not fp or fp == prev_fp:
                if fp:
                    self._last_fp[key] = fp
                return []
            self._last_fp[key] = fp
            dkey = (agent.session_id, agent.pane_id, "question_changed", fp)
            if dkey in self._dedupe:
                return []
            self._dedupe.add(dkey)
            return [
                make_agent_question_changed(
                    agent, to_status=cur, question_text=q_text
                )
            ]

        # One late re-check after status settled on done/idle (menu may paint
        # after the status edge). Skip if transition path already inspected.
        if (
            detect_false_done
            and is_idle_like_status(cur)
            and self._watch_cares_about_input(interest)
            and self._false_done_scanned.get(key) != cur
        ):
            self._false_done_scanned[key] = cur
            q_text = question_for(agent)
            if not q_text:
                return []
            hit = looks_like_pending_question(q_text)
            if not hit:
                return []
            fp = question_fingerprint(q_text, list(hit.choices) or None)
            dkey = (agent.session_id, agent.pane_id, "needs_input", fp)
            if not fp or dkey in self._dedupe:
                return []
            self._dedupe.add(dkey)
            self._last_fp[key] = fp
            return [
                make_agent_needs_input(
                    agent,
                    from_status=cur,
                    to_status=cur,
                    question_text=q_text,
                    hit=hit,
                )
            ]
        return []


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
    fallback_limiter = _WatchErrorLimiter()
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
                detect_false_done=detect_false_done,
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
                for event in tracker.process(
                    agents,
                    interest=interest,
                    question_for=question_for,
                    detect_false_done=detect_false_done,
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
    tracker: EdgeTracker,
    interest: set[str],
    emit: Callable[[dict[str, Any]], None],
    heartbeat_s: float,
    sessions: list[str],
    question_for: Callable[[AgentInfo], str | None] | None,
    store: DeliveryStore | None,
    detect_false_done: bool = True,
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
        for event in tracker.process(
            agents,
            interest=interest,
            question_for=question_for,
            detect_false_done=detect_false_done,
        ):
            emit(event)

    def on_wire(raw: dict[str, Any]) -> None:
        if _handle_lifecycle_event(raw, client=client, store=store, emit=emit):
            return
        try:
            agents = client.list_agents()
        except HerdrError as exc:
            emit(make_watch_error(client.session.id, str(exc)))
            return
        for event in tracker.process(
            agents,
            interest=interest,
            question_for=question_for,
            detect_false_done=detect_false_done,
        ):
            emit(event)

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

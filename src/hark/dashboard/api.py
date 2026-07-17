"""Snapshot + action builders for hark.dashboard.v1 (transport-free).

Everything here returns plain dicts shaped by schemas/dashboard-v1/; the HTTP
layer (server.py) only serializes. Reuses library surfaces (doctor, config,
herdr client, DeliveryStore, UsageStore) so the Rust port re-implements
serialization, not logic.
"""

from __future__ import annotations

import functools
import io
import json
from pathlib import Path
from typing import Any

from hark.answering import answer_bound_event
from hark.config import HarkConfig, config_to_dict
from hark.delivery import DeliveryStore
from hark.events import new_event_id, utc_now_iso
from hark.herdr.access import HerdrSessionAccess, active_client
from hark.herdr.client import HerdrClient, HerdrError
from hark.herdr.tunnel import ensure_tunnel
from hark.paths import state_dir

SCHEMA = "hark.dashboard.v1"


def _with_herdr_access(func):
    @functools.wraps(func)
    def wrapped(cfg: HarkConfig, *args, **kwargs):
        with HerdrSessionAccess(
            cfg,
            client_factory=HerdrClient,
            tunnel_factory=ensure_tunnel,
        ):
            return func(cfg, *args, **kwargs)

    return wrapped


def _client_for(cfg: HarkConfig, session_id: str) -> HerdrClient:
    return active_client(cfg, session_id)


def health_snapshot(cfg: HarkConfig, server_meta: dict[str, Any]) -> dict[str, Any]:
    from hark.doctor import run_doctor

    buf = io.StringIO()
    run_doctor(cfg, as_json=True, out=buf, err=io.StringIO())
    doctor = json.loads(buf.getvalue() or "{}")
    # Prefer top-level update (also mirrored under doctor.update from B088)
    update = doctor.get("update") if isinstance(doctor.get("update"), dict) else {}
    if not update:
        try:
            from hark.update_check import update_status_for_api

            update = update_status_for_api(
                enabled=bool(getattr(cfg.update, "enabled", True)),
                repo=getattr(cfg.update, "repo", None),
            )
        except Exception as exc:  # pragma: no cover — fail soft
            update = {"error": str(exc), "update_available": False}
    return {
        "schema": SCHEMA,
        "ok": bool(doctor.get("ok", False)),
        "server": server_meta,
        "doctor": doctor,
        "pipeline": pipeline_state(),
        "update": update,
    }


def pipeline_state() -> dict[str, Any]:
    """Live voice-pipeline coordination state (additive health field, B064)."""
    from hark.daemon import collect_status

    status = collect_status().to_dict()
    root = state_dir()
    status["ambient_pause"] = (root / "ambient.pause").is_file()
    # announcements currently held for a conference: latest status per queue id
    queue = root / "announce_hold_queue.jsonl"
    latest: dict[str, str] = {}
    if queue.is_file():
        for line in queue.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("id"):
                latest[str(rec["id"])] = str(rec.get("status") or "")
    status["announce_hold_queued"] = sum(1 for s in latest.values() if s == "held")
    return status


def config_snapshot(cfg: HarkConfig) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "ok": True,
        "redacted": True,
        "config": config_to_dict(cfg),
    }


@_with_herdr_access
def herdr_sessions_snapshot(cfg: HarkConfig) -> dict[str, Any]:
    store = DeliveryStore()
    pending_by_pane: dict[tuple[str, str], str] = {}
    for ev in store.pending_events():
        key = (str(ev.get("session_id") or ""), str(ev.get("pane_id") or ""))
        pending_by_pane[key] = str(ev.get("event_id"))

    sessions: list[dict[str, Any]] = []
    all_ok = True
    for session in cfg.sessions:
        try:
            client = _client_for(cfg, session.id)
            health = client.health()
        except HerdrError as exc:
            sessions.append(
                {
                    "session_id": session.id,
                    "ok": False,
                    "label": session.label,
                    "version": None,
                    "protocol": None,
                    "socket": None,
                    "ssh": session.ssh,
                    "agent_count": 0,
                    "error": str(exc)[:400],
                    "agents": [],
                }
            )
            all_ok = False
            continue
        agents: list[dict[str, Any]] = []
        if health.ok:
            try:
                infos = client.list_agents()
            except HerdrError:
                infos = []
            for a in infos:
                agents.append(
                    {
                        "session_id": a.session_id or session.id,
                        "pane_id": a.pane_id,
                        "agent": a.agent,
                        "status": a.status,
                        "revision": a.revision,
                        "workspace_id": a.workspace_id,
                        "tab_id": a.tab_id,
                        "terminal_id": a.terminal_id,
                        "cwd": a.cwd,
                        "focused": a.focused,
                        "friendly_name": a.raw.get("friendly_name")
                        or a.raw.get("name"),
                        "pending_event_id": pending_by_pane.get(
                            (a.session_id or session.id, a.pane_id)
                        ),
                    }
                )
        else:
            all_ok = False
        sessions.append(
            {
                "session_id": session.id,
                "ok": health.ok,
                "label": session.label,
                "version": health.version,
                "protocol": health.protocol,
                "socket": health.socket,
                "ssh": session.ssh,
                "agent_count": health.agent_count,
                "error": health.error,
                "agents": agents,
            }
        )
    return {"schema": SCHEMA, "ok": all_ok, "sessions": sessions}


@_with_herdr_access
def context_snapshot(
    cfg: HarkConfig, session_id: str, pane_id: str, *, lines: int = 60
) -> dict[str, Any]:
    client = _client_for(cfg, session_id)
    text = client.read_pane(pane_id, lines=lines)
    live = client.get_agent(pane_id)

    pending_question: dict[str, Any] | None = None
    store = DeliveryStore()
    for ev in store.pending_events():
        if ev.get("session_id") == session_id and ev.get("pane_id") == pane_id:
            event_id = str(ev.get("event_id"))
            hep = find_hep_event(event_id)
            question = (hep or {}).get("question") or {}
            pending_question = {
                "event_id": event_id,
                "text": ev.get("question_text") or question.get("text"),
                "choices": question.get("choices"),
                "fingerprint": ev.get("question_fingerprint"),
                "risk": ev.get("risk"),
            }
    return {
        "schema": SCHEMA,
        "ok": True,
        "session_id": session_id,
        "pane_id": pane_id,
        "lines": lines,
        "revision": live.revision if live else None,
        "text": text,
        "pending_question": pending_question,
    }


def deliveries_snapshot(*, recent_limit: int = 100) -> dict[str, Any]:
    store = DeliveryStore()
    pending = []
    for ev in store.pending_events():
        pending.append(
            {
                k: ev.get(k)
                for k in (
                    "event_id",
                    "session_id",
                    "pane_id",
                    "pane_revision",
                    "question_fingerprint",
                    "question_text",
                    "risk",
                    "status",
                    "created_at",
                )
            }
        )
    recent: list[dict[str, Any]] = []
    deliveries = state_dir() / "deliveries.jsonl"
    if deliveries.is_file():
        lines = deliveries.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-recent_limit:]:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("event_id"):
                recent.append(rec)
    return {"schema": SCHEMA, "ok": True, "pending": pending, "recent": recent}


def usage_snapshot(*, near_miss_limit: int = 20) -> dict[str, Any]:
    from hark.usage import UsageStore

    summary = UsageStore().summary()
    near_misses: list[dict[str, Any]] = []
    ambient = state_dir() / "ambient.jsonl"
    if ambient.is_file():
        for line in ambient.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("kind") == "ambient.wake_near_miss":
                near_misses.append(
                    {
                        "count": obj.get("count"),
                        "attempts": obj.get("attempts") or [],
                        "observed_at": obj.get("observed_at"),
                    }
                )
    return {
        "schema": SCHEMA,
        "ok": True,
        "summary": summary,
        "near_misses": near_misses[-near_miss_limit:],
    }


def find_hep_event(event_id: str, *, state: Path | None = None) -> dict[str, Any] | None:
    """Register-on-demand lookup: newest HEP record with this event_id."""
    state = state or state_dir()
    found: dict[str, Any] | None = None
    for name in ("watch.jsonl", "ambient.jsonl"):
        path = state / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("event_id") == event_id:
                found = obj
    return found


@_with_herdr_access
def answer_action(cfg: HarkConfig, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    event_id = str(body.get("event_id") or "")
    text = body.get("text")
    keys = body.get("keys")
    if not event_id:
        return 400, _err("bad_request", "event_id required")
    if not isinstance(text, (str, type(None))) or not isinstance(keys, (list, type(None))):
        return 400, _err("bad_request", "text must be string, keys a list")

    result = answer_bound_event(
        event_id,
        text=text or None,
        keys=[str(k) for k in keys] if keys else None,
        client_for=lambda sid: _client_for(cfg, sid),
        register_fallback=find_hep_event,
    )
    payload = result.to_payload()
    if result.status in ("rejected", "in_progress"):
        status = {
            "bad_request": 400,
            "unknown_event": 404,
        }.get(result.reason or "", 409)
        return status, payload
    return 200, payload


def prompt_action(body: dict[str, Any], *, state: Path | None = None) -> tuple[int, dict[str, Any]]:
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return 400, _err("bad_request", "text required")
    state = state or state_dir()
    event = {
        "schema": "hark.event.v1",
        "kind": "ambient.prompt",
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "phrase": "dashboard",
        "text": text.strip(),
        "wake_backend": "dashboard",
        "session_id": body.get("session_id"),
        "partial": False,
        "final": True,
        "stream_id": f"dash-{new_event_id()[:12]}",
        "instructions": (
            "FINAL operator prompt (typed/dictated via dashboard). "
            "You may now respond/act. Not bound to a pane — use judgment; "
            "do not invent answers."
        ),
    }
    state.mkdir(parents=True, exist_ok=True)
    with (state / "ambient.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, separators=(",", ":")) + "\n")
    return 200, {"ok": True, "event_id": event["event_id"]}


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}

"""Active listen session control — agent can finish/cancel mid-recording.

Radio mode waits for product-scoped end phrases. Operators often say something
looser ("that's all", "how do I stop?", "okay send it"). Partials carry HOLD
warnings *and* CLI hints so the orchestrator may finalize via:

  hark listen-end --stream-id <id>
  hark listen-end --stream-id <id> --cancel
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Literal

from hark.paths import state_dir
from hark.syslog import log as syslog

Action = Literal["finish", "cancel"]


def listen_control_dir() -> Path:
    return state_dir() / "listen"


def active_path() -> Path:
    return listen_control_dir() / "active.json"


def command_path(stream_id: str | None = None) -> Path:
    if stream_id:
        return listen_control_dir() / f"{stream_id}.cmd"
    return listen_control_dir() / "command"


def voice_activity_path() -> Path:
    """Operator speech energy marker for streaming TTS quiet gate (B105)."""
    return listen_control_dir() / "voice.json"


def register_active_listen(
    stream_id: str,
    *,
    mode: str = "radio",
    streaming: bool = False,
    streaming_ack_min_quiet_s: float | None = None,
) -> Path:
    """Mark a listen session active so agents can target it.

    ``streaming`` / ``streaming_ack_min_quiet_s`` come from ListenSessionPolicy
    at the open seam (P1.M6) so TTS quiet-gate does not re-read ambient TOML.
    """
    d = listen_control_dir()
    d.mkdir(parents=True, exist_ok=True)
    # clear stale command for this stream
    command_path(stream_id).unlink(missing_ok=True)
    command_path(None).unlink(missing_ok=True)
    payload: dict[str, Any] = {
        "stream_id": stream_id,
        "mode": mode,
        "streaming": bool(streaming),
        "pid": os.getpid(),
        "started_at": time.time(),
        "end_cmd": f"hark listen-end --stream-id {stream_id}",
        "cancel_cmd": f"hark listen-end --stream-id {stream_id} --cancel",
    }
    if streaming_ack_min_quiet_s is not None:
        payload["streaming_ack_min_quiet_s"] = float(streaming_ack_min_quiet_s)
    path = active_path()
    try:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        # Fresh capture: clear prior voice activity so quiet is measured from start.
        clear_voice_activity()
        return path
    except BaseException:
        # Publication succeeded but ownership was not returned to the caller.
        # Remove only this exact marker; a concurrent newer listen wins.
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            same_owner = all(
                current.get(field) == payload[field]
                for field in ("stream_id", "pid", "started_at")
            )
            if same_owner:
                path.unlink(missing_ok=True)
        except BaseException:
            pass
        raise


def clear_active_listen(stream_id: str | None = None) -> None:
    try:
        active = active_path()
        if active.is_file():
            if stream_id is None:
                active.unlink(missing_ok=True)
                clear_voice_activity()
            else:
                try:
                    data = json.loads(active.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    data = {}
                if data.get("stream_id") == stream_id or not data.get("stream_id"):
                    active.unlink(missing_ok=True)
                    clear_voice_activity()
        if stream_id:
            command_path(stream_id).unlink(missing_ok=True)
        command_path(None).unlink(missing_ok=True)
    except OSError:
        pass


def clear_voice_activity() -> None:
    try:
        voice_activity_path().unlink(missing_ok=True)
    except OSError:
        pass


def touch_voice_activity(*, stream_id: str | None = None) -> None:
    """Record that operator speech energy was heard (B105 streaming quiet gate).

    Safe to call frequently; writers should throttle (≈10 Hz) in the capture loop.
    """
    d = listen_control_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        now = time.time()
        payload: dict[str, Any] = {
            "last_voice_at": now,
            "updated_at": now,
        }
        if stream_id:
            payload["stream_id"] = stream_id
        else:
            active = read_active()
            if active and active.get("stream_id"):
                payload["stream_id"] = active["stream_id"]
        voice_activity_path().write_text(
            json.dumps(payload, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def read_voice_activity() -> dict[str, Any] | None:
    path = voice_activity_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def operator_quiet_s(*, now: float | None = None) -> float | None:
    """Seconds of operator quiet while a listen capture is active (B105).

    Returns ``None`` when no active listen. Prefers ``voice.json`` last speech
    energy time; if speech has not opened yet, uses ``active.started_at`` so
    pre-open silence counts as quiet.
    """
    active = read_active()
    if not active:
        return None
    t = float(now if now is not None else time.time())
    voice = read_voice_activity()
    sid = str(active.get("stream_id") or "") or None
    last: float | None = None
    if voice:
        vsid = str(voice.get("stream_id") or "") or None
        if vsid is None or sid is None or vsid == sid:
            raw = voice.get("last_voice_at")
            try:
                last = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                last = None
    if last is None:
        try:
            last = float(active.get("started_at") or t)
        except (TypeError, ValueError):
            last = t
    return max(0.0, t - last)


def read_active() -> dict[str, Any] | None:
    path = active_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def request_listen_action(
    action: Action,
    *,
    stream_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Agent/CLI: request finish or cancel of the active (or named) listen."""
    if action not in ("finish", "cancel"):
        raise ValueError(f"invalid action: {action}")
    active = read_active()
    sid = stream_id or (active or {}).get("stream_id")
    if not sid and not active:
        return {"ok": False, "error": "no active listen session"}
    if stream_id and active and active.get("stream_id") and active["stream_id"] != stream_id:
        return {
            "ok": False,
            "error": f"stream_id mismatch (active={active['stream_id']})",
            "active": active,
        }
    target = sid or "unknown"
    d = listen_control_dir()
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "action": action,
        "stream_id": target,
        "reason": reason,
        "requested_at": time.time(),
        "pid": os.getpid(),
    }
    # Write both specific and generic command files
    for path in (command_path(target if sid else None), command_path(None)):
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    syslog(
        "listen.control_request",
        component="listen",
        level="info",
        action=action,
        stream_id=target,
        reason=reason,
    )
    return {"ok": True, "action": action, "stream_id": target}


def poll_listen_action(stream_id: str | None = None) -> Action | None:
    """Listen loop: non-destructive until consumed via consume_listen_action."""
    paths = []
    if stream_id:
        paths.append(command_path(stream_id))
    paths.append(command_path(None))
    for path in paths:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if stream_id and data.get("stream_id") not in (None, stream_id, "unknown"):
            continue
        action = data.get("action")
        if action in ("finish", "cancel"):
            return action  # type: ignore[return-value]
    return None


def consume_listen_action(stream_id: str | None = None) -> Action | None:
    """Read and clear pending action."""
    action = poll_listen_action(stream_id)
    if action is None:
        return None
    if stream_id:
        command_path(stream_id).unlink(missing_ok=True)
    command_path(None).unlink(missing_ok=True)
    return action


def agent_control_block(stream_id: str) -> dict[str, str]:
    """Embed in partial events so the orchestrator knows how to end capture."""
    return {
        "end_recording": f"hark listen-end --stream-id {stream_id}",
        "cancel_recording": f"hark listen-end --stream-id {stream_id} --cancel",
        "hint": (
            "MUST: if the operator clearly finished (utterance ends with over, "
            "okay over, okay hark send, that's all, send it, stop recording, "
            "message done, or similar) and this stream is still active, run "
            "end_recording (finish, not cancel). Prefer cancel_recording only "
            "if they abort. Do NOT end mid-clause: 'over the weekend', "
            "'send it to staging', 'that's all I know about X'."
        ),
    }

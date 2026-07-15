"""Handsfree session profile from the structured startup interview (B125).

Stores operator intent for this handsfree run under state:

  ``~/.local/state/hark/session_profile.json``

Fields:

  * **scope** — ``session_local`` (no Herdr event forward) | ``herdr`` (watch agents)
  * **autonomy** — how chatty/proactive the agent should be
  * **role** — free-text purpose of this session
  * **mode** — listen/ambient product mode (auto_end | radio | conversation)

``apply_mode_to_config`` writes ``[ambient].streaming`` / ``[listen].end_mode``.
``should_start_watch`` drives ``hark start`` / workers so session-local runs
do not spawn Herdr watch (no blocked/done noise on the monitor path).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from hark.paths import default_config_path, state_dir

Scope = Literal["session_local", "herdr"]
Autonomy = Literal["silent", "blocked_only", "proactive", "babysit"]
Mode = Literal["auto_end", "radio", "conversation"]

PROFILE_NAME = "session_profile.json"
SCHEMA_VERSION = 1

SCOPES: tuple[str, ...] = ("session_local", "herdr")
AUTONOMIES: tuple[str, ...] = ("silent", "blocked_only", "proactive", "babysit")
MODES: tuple[str, ...] = ("auto_end", "radio", "conversation")

# Spoken / synonym → canonical
_SCOPE_ALIASES: dict[str, Scope] = {
    "session_local": "session_local",
    "session-local": "session_local",
    "local": "session_local",
    "session": "session_local",
    "this chat": "session_local",
    "orchestrator": "session_local",
    "ambient only": "session_local",
    "no herdr": "session_local",
    "herdr": "herdr",
    "agents": "herdr",
    "watch herdr": "herdr",
    "both": "herdr",
    "all": "herdr",
    "forward herdr": "herdr",
}
_AUTONOMY_ALIASES: dict[str, Autonomy] = {
    "silent": "silent",
    "quiet": "silent",
    "mostly silent": "silent",
    "blocked_only": "blocked_only",
    "blocked only": "blocked_only",
    "blocked": "blocked_only",
    "when blocked": "blocked_only",
    "proactive": "proactive",
    "status": "proactive",
    "babysit": "babysit",
    "hands on": "babysit",
    "hands-on": "babysit",
    "active": "babysit",
}
_MODE_ALIASES: dict[str, Mode] = {
    "auto_end": "auto_end",
    "auto-end": "auto_end",
    "auto end": "auto_end",
    "silence": "auto_end",
    "smart turn": "auto_end",
    "trigger + auto-end": "auto_end",
    "radio": "radio",
    "hold": "radio",
    "trigger + radio": "radio",
    "conversation": "conversation",
    "conversational": "conversation",
    "streaming": "conversation",
    "always on": "conversation",
}


@dataclass
class SessionProfile:
    """Operator intent for one handsfree skill session."""

    scope: Scope = "herdr"
    autonomy: Autonomy = "blocked_only"
    role: str = ""
    mode: Mode = "radio"
    schema_version: int = SCHEMA_VERSION
    updated_at: str = ""
    # Extra free-form notes from follow-ups (optional)
    notes: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def profile_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / PROFILE_NAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_key(raw: str) -> str:
    return re.sub(r"\s+", " ", (raw or "").strip().lower())


def normalize_scope(raw: str | None, default: Scope = "herdr") -> Scope:
    if raw is None or not str(raw).strip():
        return default
    key = _norm_key(str(raw))
    if key in _SCOPE_ALIASES:
        return _SCOPE_ALIASES[key]
    # substring heuristics
    if "session" in key and "local" in key:
        return "session_local"
    if "herdr" in key or "agent" in key:
        return "herdr"
    if key in ("local only", "just ambient", "this session"):
        return "session_local"
    raise ValueError(
        f"unknown scope {raw!r}; expected one of {', '.join(SCOPES)} "
        f"(or aliases: local / herdr)"
    )


def normalize_autonomy(raw: str | None, default: Autonomy = "blocked_only") -> Autonomy:
    if raw is None or not str(raw).strip():
        return default
    key = _norm_key(str(raw))
    if key in _AUTONOMY_ALIASES:
        return _AUTONOMY_ALIASES[key]
    if "silent" in key or "quiet" in key:
        return "silent"
    if "babysit" in key or "hands" in key:
        return "babysit"
    if "proactive" in key or "status" in key:
        return "proactive"
    if "block" in key:
        return "blocked_only"
    raise ValueError(
        f"unknown autonomy {raw!r}; expected one of {', '.join(AUTONOMIES)}"
    )


def normalize_mode(raw: str | None, default: Mode = "radio") -> Mode:
    if raw is None or not str(raw).strip():
        return default
    key = _norm_key(str(raw))
    if key in _MODE_ALIASES:
        return _MODE_ALIASES[key]
    if "conversation" in key or "stream" in key:
        return "conversation"
    if "radio" in key or "hold" in key or "over" in key:
        return "radio"
    if "auto" in key or "silence" in key or "smart" in key:
        return "auto_end"
    raise ValueError(
        f"unknown mode {raw!r}; expected one of {', '.join(MODES)}"
    )


def load_profile(path: Path | None = None) -> SessionProfile | None:
    p = path or profile_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return SessionProfile(
            scope=normalize_scope(str(data.get("scope") or "herdr")),
            autonomy=normalize_autonomy(str(data.get("autonomy") or "blocked_only")),
            role=str(data.get("role") or "").strip(),
            mode=normalize_mode(str(data.get("mode") or "radio")),
            schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            updated_at=str(data.get("updated_at") or ""),
            notes=dict(data.get("notes") or {})
            if isinstance(data.get("notes"), dict)
            else {},
        )
    except (TypeError, ValueError):
        return None


def save_profile(profile: SessionProfile, path: Path | None = None) -> Path:
    p = path or profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not profile.updated_at:
        profile.updated_at = _utc_now()
    else:
        profile.updated_at = _utc_now()
    profile.schema_version = SCHEMA_VERSION
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(profile.as_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(p)
    return p


def set_profile(
    *,
    scope: str | None = None,
    autonomy: str | None = None,
    role: str | None = None,
    mode: str | None = None,
    notes: dict[str, Any] | None = None,
    path: Path | None = None,
    merge: bool = True,
) -> SessionProfile:
    """Create or update the session profile and persist it."""
    prev = load_profile(path) if merge else None
    prof = prev or SessionProfile()
    if scope is not None:
        prof.scope = normalize_scope(scope)
    if autonomy is not None:
        prof.autonomy = normalize_autonomy(autonomy)
    if role is not None:
        prof.role = str(role).strip()
    if mode is not None:
        prof.mode = normalize_mode(mode)
    if notes:
        prof.notes = {**prof.notes, **notes}
    save_profile(prof, path)
    return prof


def should_start_watch(profile: SessionProfile | None = None) -> bool:
    """False when session-local only (no Herdr event forward)."""
    prof = profile if profile is not None else load_profile()
    if prof is None:
        return True  # default product path: watch Herdr
    return prof.scope != "session_local"


def mode_config_patch(mode: Mode) -> dict[str, Any]:
    """Map interview mode → config.toml ambient/listen keys."""
    if mode == "conversation":
        return {
            "ambient.streaming": True,
            "listen.end_mode": "radio",  # turns use quiet; product end optional
        }
    if mode == "auto_end":
        return {
            "ambient.streaming": False,
            "listen.end_mode": "silence",
        }
    # radio HOLD
    return {
        "ambient.streaming": False,
        "listen.end_mode": "radio",
    }


def apply_mode_to_config(
    mode: Mode | str,
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Write streaming / end_mode for the chosen mode into config.toml.

    Best-effort TOML patch (same style as setup_flow): replace or insert keys
    under ``[ambient]`` / ``[listen]``. Returns a report dict.
    """
    mode_n = normalize_mode(str(mode))
    patch = mode_config_patch(mode_n)
    path = config_path or default_config_path()
    report: dict[str, Any] = {
        "ok": True,
        "mode": mode_n,
        "path": str(path),
        "patch": patch,
        "created": False,
    }
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        from hark.config import write_default_config

        write_default_config(path=path, force=False)
        report["created"] = True

    text = path.read_text(encoding="utf-8")
    streaming = bool(patch["ambient.streaming"])
    end_mode = str(patch["listen.end_mode"])

    text2 = _upsert_toml_key(text, "ambient", "streaming", "true" if streaming else "false")
    text2 = _upsert_toml_key(text2, "listen", "end_mode", f'"{end_mode}"')
    if text2 != text:
        path.write_text(text2, encoding="utf-8")
        report["wrote"] = True
    else:
        report["wrote"] = False
    return report


def _upsert_toml_key(text: str, section: str, key: str, value_lit: str) -> str:
    """Insert or replace ``key = value`` under ``[section]`` (simple TOML)."""
    sec_re = re.compile(rf"(?m)^\[{re.escape(section)}\]\s*$")
    m = sec_re.search(text)
    key_re = re.compile(rf"(?m)^(\s*{re.escape(key)}\s*=\s*).*$")
    if m:
        # Find end of section
        start = m.end()
        next_sec = re.search(r"(?m)^\[", text[start:])
        end = start + next_sec.start() if next_sec else len(text)
        body = text[start:end]
        if key_re.search(body):
            new_body = key_re.sub(rf"\1{value_lit}", body, count=1)
        else:
            insert = f"{key} = {value_lit}\n"
            # after section header newline
            new_body = "\n" + insert + body.lstrip("\n") if not body.startswith("\n") else body
            if key_re.search(body):
                pass
            else:
                # Prefer append after first line of section
                new_body = body
                if not new_body.startswith("\n"):
                    new_body = "\n" + new_body
                new_body = new_body.replace("\n", f"\n{key} = {value_lit}\n", 1)
        return text[:start] + new_body + text[end:]
    # No section — append
    block = f"\n[{section}]\n{key} = {value_lit}\n"
    if text and not text.endswith("\n"):
        text += "\n"
    return text + block


def autonomy_instructions(autonomy: Autonomy) -> str:
    """Skill-facing behavior summary for the agent after interview."""
    if autonomy == "silent":
        return (
            "Stay mostly silent. TTS only for hard blocks / direct ambient asks. "
            "No proactive status, no queue rollups unless operator asks."
        )
    if autonomy == "proactive":
        return (
            "Speak short status when useful (queue count, completions that matter). "
            "Still confirm R2/R3. Prefer brief acks over long monologues."
        )
    if autonomy == "babysit":
        return (
            "Hands-on: announce blocks promptly, offer next steps, surface false-done "
            "risks, keep the operator informed. Still never invent pane answers."
        )
    # blocked_only
    return (
        "Speak when an agent is blocked / needs_input or ambient asks you something. "
        "Skip routine done noise unless false-done looks likely."
    )


def mode_ready_tts(mode: Mode, *, wake_label: str = "hey iris") -> str:
    """One-line ready cue matching the chosen mode."""
    if mode == "conversation":
        return (
            f"Hark is ready in conversation mode. Say {wake_label} once, "
            "then keep talking — no re-wake between turns."
        )
    if mode == "auto_end":
        return (
            f"Hark is ready. Say {wake_label} when you need me. "
            "I'll auto-end when you pause."
        )
    return (
        f"Hark is ready. Say {wake_label} when you need me. "
        "In radio mode, finish with over or okay hark send."
    )


def cmd_session_profile(args: Any) -> int:
    """CLI entry for ``hark session-profile``."""
    import sys

    from hark.exitcodes import OK, USAGE

    sub = getattr(args, "session_profile_cmd", None) or getattr(args, "sp_cmd", None)
    as_json = bool(getattr(args, "json", False))
    out = sys.stdout

    if sub in (None, "show"):
        prof = load_profile()
        if prof is None:
            if as_json:
                out.write(json.dumps({"ok": True, "profile": None}) + "\n")
            else:
                out.write("no session profile set (defaults: herdr + radio + blocked_only)\n")
            return OK
        if as_json:
            out.write(json.dumps({"ok": True, "profile": prof.as_dict()}, indent=2) + "\n")
        else:
            out.write(
                f"scope={prof.scope}  autonomy={prof.autonomy}  mode={prof.mode}\n"
                f"role={prof.role or '(none)'}\n"
                f"updated_at={prof.updated_at}\n"
                f"start_watch={should_start_watch(prof)}\n"
                f"autonomy_hint: {autonomy_instructions(prof.autonomy)}\n"
            )
        return OK

    if sub == "set":
        try:
            prof = set_profile(
                scope=getattr(args, "scope", None),
                autonomy=getattr(args, "autonomy", None),
                role=getattr(args, "role", None),
                mode=getattr(args, "mode", None),
            )
        except ValueError as exc:
            sys.stderr.write(f"hark session-profile: {exc}\n")
            return USAGE
        apply = bool(getattr(args, "apply", False))
        report: dict[str, Any] = {"ok": True, "profile": prof.as_dict()}
        if apply or bool(getattr(args, "apply_mode", False)):
            report["config"] = apply_mode_to_config(prof.mode)
        if as_json:
            out.write(json.dumps(report, indent=2) + "\n")
        else:
            out.write(
                f"saved session profile: scope={prof.scope} mode={prof.mode} "
                f"autonomy={prof.autonomy}\n"
            )
            if "config" in report:
                out.write(f"applied mode → config: {report['config']}\n")
            out.write(
                f"hark start will "
                f"{'include' if should_start_watch(prof) else 'skip'} Herdr watch\n"
            )
        return OK

    if sub == "apply":
        prof = load_profile()
        if prof is None:
            sys.stderr.write("hark session-profile: nothing to apply (run set first)\n")
            return USAGE
        report = apply_mode_to_config(prof.mode)
        if as_json:
            out.write(
                json.dumps(
                    {"ok": True, "profile": prof.as_dict(), "config": report},
                    indent=2,
                )
                + "\n"
            )
        else:
            out.write(f"applied mode={prof.mode} → {report}\n")
        return OK

    if sub == "clear":
        p = profile_path()
        if p.is_file():
            p.unlink()
        if as_json:
            out.write(json.dumps({"ok": True, "cleared": True}) + "\n")
        else:
            out.write("session profile cleared\n")
        return OK

    sys.stderr.write(f"hark session-profile: unknown subcommand {sub!r}\n")
    return USAGE

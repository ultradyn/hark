"""Guided first-run setup — ``hark setup`` (B070).

Writes/updates ``~/.config/hark/config.toml`` and
``~/.local/state/hark/setup-complete.json`` with ``hark_version`` so later
runs only re-prompt when the setup schema grows.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from hark import __version__
from hark.config import (
    DEFAULT_CONFIG_TOML,
    default_config_path,
    default_sherpa_kws_model_path,
    default_vosk_model_path,
    load_config,
)
from hark.exitcodes import OK
from hark.paths import state_dir
from hark.wake import is_sherpa_kws_model_dir

# Bump when setup gains new required questions (re-prompt only new ones).
SETUP_SCHEMA_VERSION = 1

SETUP_COMPLETE_NAME = "setup-complete.json"


@dataclass
class SetupAnswers:
    persona: str = "feminine"  # feminine | masculine | custom
    wake_names: list[str] = field(
        default_factory=lambda: ["iris", "mercury", "hark", "herald"]
    )
    tts_voice: str = "eve"
    tts_provider: str = "xai"
    wake_engine: str = "vosk"  # vosk | sherpa_kws | defer
    sessions: list[dict[str, str]] = field(
        default_factory=lambda: [{"id": "local"}]
    )
    notes: str = ""


def setup_complete_path() -> Path:
    return state_dir() / SETUP_COMPLETE_NAME


def load_setup_complete(path: Path | None = None) -> dict[str, Any] | None:
    p = path or setup_complete_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _sessions_answers_ok(sessions: Any) -> bool:
    """True when setup answers include at least one session with an id (B116)."""
    if not isinstance(sessions, list) or not sessions:
        return False
    for s in sessions:
        if isinstance(s, dict) and str(s.get("id") or "").strip():
            return True
    return False


def setup_needs_run(
    current: dict[str, Any] | None = None,
    *,
    hark_version: str | None = None,
) -> tuple[bool, list[str]]:
    """Return (needs_full_or_partial, missing_question_keys).

    Re-prompt when flag missing, schema older than SETUP_SCHEMA_VERSION,
    required answer keys absent, or ``answers.sessions`` empty / invalid (B116).
    """
    cur = current if current is not None else load_setup_complete()
    if cur is None:
        return True, ["all"]
    schema = int(cur.get("setup_schema_version") or 0)
    missing: list[str] = []
    if schema < SETUP_SCHEMA_VERSION:
        # Future: map schema deltas to only new keys. For now schema 1 is full.
        missing.append("schema_upgrade")
    answers = cur.get("answers") if isinstance(cur.get("answers"), dict) else {}
    for key in ("persona", "wake_engine", "tts_voice", "wake_names", "sessions"):
        if key not in answers:
            missing.append(key)
    # Empty or id-less sessions → treat as not configured (B116)
    if "sessions" not in missing and not _sessions_answers_ok(answers.get("sessions")):
        missing.append("sessions")
    # Optional: flag hark_version for operators; do not force re-run on every bump
    _ = hark_version or __version__
    return (len(missing) > 0), missing


def write_setup_complete(
    answers: SetupAnswers,
    *,
    path: Path | None = None,
    hark_version: str | None = None,
) -> Path:
    dest = path or setup_complete_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hark_version": hark_version or __version__,
        "setup_schema_version": SETUP_SCHEMA_VERSION,
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "answers": {
            "persona": answers.persona,
            "wake_names": list(answers.wake_names),
            "tts_voice": answers.tts_voice,
            "tts_provider": answers.tts_provider,
            "wake_engine": answers.wake_engine,
            "sessions": list(answers.sessions),
            "notes": answers.notes,
        },
    }
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def persona_defaults(persona: str) -> tuple[list[str], str]:
    """Return (wake_names, tts_voice) for feminine / masculine / custom."""
    p = (persona or "feminine").strip().lower()
    if p in ("masculine", "male", "m", "mercury"):
        # Mercury persona + product aliases; TTS leo
        return ["mercury", "iris", "hark", "herald"], "leo"
    if p in ("custom", "other"):
        return ["iris", "mercury", "hark", "herald"], "eve"
    # feminine default: Iris + eve
    return ["iris", "mercury", "hark", "herald"], "eve"


def _set_toml_key(text: str, section: str, key: str, value: str) -> str:
    """Set key=value inside [section], inserting section if needed."""
    sec_re = re.compile(
        rf"(^|\n)(\[{re.escape(section)}\])(.*?)(?=\n\[|\Z)",
        re.S,
    )
    m = sec_re.search(text)
    if not m:
        block = f"\n[{section}]\n{key} = {value}\n"
        return text.rstrip() + "\n" + block
    head, body = m.group(2), m.group(3)
    if re.search(rf"(?m)^{re.escape(key)}\s*=", body):
        body = re.sub(
            rf"(?m)^{re.escape(key)}\s*=\s*.*$",
            f"{key} = {value}",
            body,
        )
    else:
        body = body.rstrip() + f"\n{key} = {value}\n"
    start, end = m.start(2), m.end(3)
    return text[:start] + head + body + text[end:]


def _set_toml_list(text: str, section: str, key: str, items: list[str]) -> str:
    inner = ", ".join(json.dumps(x) for x in items)
    return _set_toml_key(text, section, key, f"[{inner}]")


def _ensure_session_blocks(text: str, sessions: list[dict[str, str]]) -> str:
    """Replace or append [[herdr.sessions]] blocks from setup answers."""
    # Drop existing session tables (simple: strip all [[herdr.sessions]] … until next [[ or [ that is not herdr.sessions)
    cleaned = re.sub(
        r"\n*\[\[herdr\.sessions\]\][^\[]*(?=(\n\[|\Z))",
        "\n",
        text,
        flags=re.S,
    )
    blocks: list[str] = []
    for s in sessions:
        sid = s.get("id") or "local"
        lines = [f'id = {json.dumps(sid)}']
        if s.get("ssh"):
            lines.append(f'ssh = {json.dumps(s["ssh"])}')
        if s.get("label"):
            lines.append(f'label = {json.dumps(s["label"])}')
        blocks.append("[[herdr.sessions]]\n" + "\n".join(lines) + "\n")
    return cleaned.rstrip() + "\n\n" + "\n".join(blocks)


def apply_answers_to_config(
    answers: SetupAnswers,
    *,
    config_path: Path | None = None,
    create_if_missing: bool = True,
) -> Path:
    """Write setup answers into config.toml (merge)."""
    path = config_path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        text = path.read_text(encoding="utf-8")
    elif create_if_missing:
        text = DEFAULT_CONFIG_TOML
    else:
        raise FileNotFoundError(path)

    # TTS voice
    text = _set_toml_key(text, "tts", "voice", json.dumps(answers.tts_voice))
    if answers.tts_provider:
        text = _set_toml_key(
            text, "tts", "provider", json.dumps(answers.tts_provider)
        )

    # Ambient names + engine
    text = _set_toml_list(text, "ambient", "names", list(answers.wake_names))
    text = _set_toml_key(text, "ambient", "wake_mode", json.dumps("names"))
    eng = answers.wake_engine
    if eng == "defer":
        eng = "vosk"
    text = _set_toml_key(text, "ambient", "engine", json.dumps(eng))
    if eng == "sherpa_kws":
        mp = default_sherpa_kws_model_path()
        if is_sherpa_kws_model_dir(mp):
            text = _set_toml_key(
                text, "ambient", "model_path", json.dumps(str(mp))
            )
    elif eng == "vosk":
        mp = default_vosk_model_path()
        if mp.is_dir():
            text = _set_toml_key(
                text, "ambient", "model_path", json.dumps(str(mp))
            )

    text = _ensure_session_blocks(text, answers.sessions)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    return path


def _prompt(prompt: str, default: str = "", *, inp: TextIO, out: TextIO) -> str:
    suffix = f" [{default}]" if default else ""
    out.write(f"{prompt}{suffix}: ")
    out.flush()
    line = inp.readline()
    if not line:
        return default
    s = line.strip()
    return s if s else default


def _repo_root() -> Path:
    # src/hark/setup_flow.py → repo root
    return Path(__file__).resolve().parents[2]


def try_download_sherpa_model(
    *,
    out: TextIO,
    err: TextIO,
) -> bool:
    script = _repo_root() / "scripts" / "download-sherpa-kws-model.sh"
    if not script.is_file():
        err.write(f"download script missing: {script}\n")
        return False
    out.write(f"Running {script} …\n")
    out.flush()
    try:
        r = subprocess.run(
            ["bash", str(script)],
            check=False,
        )
    except OSError as exc:
        err.write(f"download failed: {exc}\n")
        return False
    return r.returncode == 0 and is_sherpa_kws_model_dir(
        default_sherpa_kws_model_path()
    )


def try_list_tts_samples() -> list[dict[str, str]]:
    """List bundled TTS sample paths when present (graceful if incomplete).

    B076 multi-provider auth may expand catalog later; here we only surface
    on-disk samples under assets/tts/samples/ without requiring API keys.
    """
    root = _repo_root() / "assets" / "tts" / "samples"
    if not root.is_dir():
        return []
    out: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.mp3")):
        rel = path.relative_to(root)
        parts = rel.parts
        provider = parts[0] if len(parts) > 1 else "unknown"
        voice = path.stem
        out.append(
            {
                "provider": provider,
                "voice": voice,
                "path": str(path),
            }
        )
    return out


def run_setup(
    *,
    non_interactive: bool = False,
    persona: str | None = None,
    wake_engine: str | None = None,
    voice: str | None = None,
    names: str | None = None,
    sessions: str | None = None,
    skip_doctor: bool = False,
    skip_download: bool = False,
    force: bool = False,
    config_path: Path | None = None,
    inp: TextIO | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """CLI entry for ``hark setup``. Returns process exit code."""
    inp = inp or sys.stdin
    out = out or sys.stdout
    err = err or sys.stderr

    existing = load_setup_complete()
    needs, missing = setup_needs_run(existing)
    if existing and not needs and not force:
        out.write(
            f"Setup already complete (hark_version="
            f"{existing.get('hark_version')}, schema="
            f"{existing.get('setup_schema_version')}). "
            f"Use --force to re-run.\n"
        )
        return OK

    answers = SetupAnswers()
    if existing and isinstance(existing.get("answers"), dict):
        prev = existing["answers"]
        answers.persona = str(prev.get("persona") or answers.persona)
        answers.tts_voice = str(prev.get("tts_voice") or answers.tts_voice)
        answers.wake_engine = str(prev.get("wake_engine") or answers.wake_engine)
        if isinstance(prev.get("wake_names"), list):
            answers.wake_names = [str(x) for x in prev["wake_names"]]
        if isinstance(prev.get("sessions"), list):
            answers.sessions = [
                {str(k): str(v) for k, v in s.items()}
                for s in prev["sessions"]
                if isinstance(s, dict)
            ]

    # --- 1. Doctor ---
    if not skip_doctor:
        out.write("==> hark doctor\n")
        try:
            from hark.doctor import run_doctor

            run_doctor(load_config(config_path), as_json=False, out=out, err=err)
        except Exception as exc:
            err.write(f"doctor warning: {exc}\n")

    # --- 2–6 questions (flags or interactive) ---
    if non_interactive or not (inp.isatty() if hasattr(inp, "isatty") else False):
        # Flag / default path
        answers.persona = (persona or answers.persona or "feminine").lower()
        names_list, default_voice = persona_defaults(answers.persona)
        if names:
            answers.wake_names = [n.strip() for n in names.split(",") if n.strip()]
        else:
            answers.wake_names = names_list
        answers.tts_voice = voice or default_voice
        answers.wake_engine = (wake_engine or answers.wake_engine or "vosk").lower()
        if sessions:
            # "local" or "local,work=ssh:host"
            sess_out: list[dict[str, str]] = []
            for part in sessions.split(","):
                part = part.strip()
                if not part:
                    continue
                if "=" in part:
                    sid, rest = part.split("=", 1)
                    entry: dict[str, str] = {"id": sid.strip()}
                    if rest.startswith("ssh:"):
                        entry["ssh"] = rest[4:]
                    sess_out.append(entry)
                else:
                    sess_out.append({"id": part})
            answers.sessions = sess_out or [{"id": "local"}]
    else:
        out.write("\nHark guided setup (see skill/hark/SETUP.md)\n\n")

        # Sessions (always first preference — B116; required for handsfree)
        out.write(
            "Herdr sessions: which servers should Hark watch?\n"
            "  local — this machine only\n"
            "  ssh   — one remote host (SSH tunnel to Herdr socket)\n"
            "  mix   — local + remote together\n"
        )
        sess_choice = _prompt(
            "Herdr sessions: local / ssh / mix",
            "local",
            inp=inp,
            out=out,
        ).lower()
        if sess_choice in ("ssh", "remote"):
            host = _prompt("SSH host (user@host or alias)", "workbox", inp=inp, out=out)
            answers.sessions = [{"id": "work", "ssh": host, "label": host}]
        elif sess_choice in ("mix", "both"):
            host = _prompt("SSH host for remote session", "workbox", inp=inp, out=out)
            answers.sessions = [
                {"id": "local"},
                {"id": "work", "ssh": host, "label": host},
            ]
        else:
            answers.sessions = [{"id": "local"}]

        # Persona
        answers.persona = _prompt(
            "Persona: feminine (Iris+eve) / masculine (Mercury+leo) / custom",
            persona or "feminine",
            inp=inp,
            out=out,
        ).lower()
        if answers.persona.startswith("m"):
            answers.persona = "masculine"
        elif answers.persona.startswith("c"):
            answers.persona = "custom"
        else:
            answers.persona = "feminine"
        names_list, default_voice = persona_defaults(answers.persona)
        if answers.persona == "custom":
            custom = _prompt(
                "Custom primary wake name",
                "iris",
                inp=inp,
                out=out,
            ).strip().lower()
            answers.wake_names = [custom, "hark", "herald"]
            answers.tts_voice = _prompt(
                "TTS voice id",
                voice or "eve",
                inp=inp,
                out=out,
            )
        else:
            answers.wake_names = names_list
            answers.tts_voice = voice or default_voice

        samples = try_list_tts_samples()
        if samples:
            out.write(
                f"  ({len(samples)} bundled TTS sample(s) under assets/tts/samples; "
                "play with your audio player if desired)\n"
            )
        else:
            out.write(
                "  (no bundled multi-provider samples, or auth incomplete — "
                "voice id still applied; B076 expands catalog)\n"
            )
        answers.tts_voice = _prompt(
            "TTS voice",
            answers.tts_voice,
            inp=inp,
            out=out,
        )

        # Wake engine
        sherpa_ready = is_sherpa_kws_model_dir(default_sherpa_kws_model_path())
        rec = "sherpa_kws" if sherpa_ready else "vosk"
        out.write(
            "Wake backend: vosk (default, small ASR + aliases) | "
            "sherpa_kws (open-vocab KWS, recommended when model installed) | "
            "defer (keep vosk)\n"
        )
        if sherpa_ready:
            out.write("  Sherpa KWS model: found\n")
        else:
            out.write(
                "  Sherpa KWS model: not found "
                f"({default_sherpa_kws_model_path()})\n"
            )
        answers.wake_engine = _prompt(
            "Wake engine",
            wake_engine or rec,
            inp=inp,
            out=out,
        ).lower()
        if answers.wake_engine in ("sherpa", "kws", "s"):
            answers.wake_engine = "sherpa_kws"
        if answers.wake_engine in ("d", "later", "skip"):
            answers.wake_engine = "defer"

    # Normalize engine
    if answers.wake_engine in ("sherpa", "kws"):
        answers.wake_engine = "sherpa_kws"
    if answers.wake_engine not in ("vosk", "sherpa_kws", "defer"):
        err.write(f"unknown wake engine {answers.wake_engine!r}; using vosk\n")
        answers.wake_engine = "vosk"

    # Download sherpa if selected
    if answers.wake_engine == "sherpa_kws" and not skip_download:
        if not is_sherpa_kws_model_dir(default_sherpa_kws_model_path()):
            out.write("Downloading Sherpa KWS model…\n")
            if not try_download_sherpa_model(out=out, err=err):
                err.write(
                    "Sherpa model download failed — falling back to vosk. "
                    "Install later with ./scripts/download-sherpa-kws-model.sh\n"
                )
                answers.wake_engine = "vosk"
                answers.notes = "sherpa_download_failed_fallback_vosk"

    # Apply config + flag
    cfg_path = apply_answers_to_config(answers, config_path=config_path)
    flag_path = write_setup_complete(answers)
    out.write(f"Wrote config: {cfg_path}\n")
    out.write(f"Wrote setup flag: {flag_path}\n")
    out.write(
        f"  persona={answers.persona} voice={answers.tts_voice} "
        f"engine={answers.wake_engine} names={answers.wake_names}\n"
    )
    sess_bits = []
    for s in answers.sessions:
        sid = s.get("id") or "?"
        if s.get("ssh"):
            sess_bits.append(f"{sid}=ssh:{s['ssh']}")
        else:
            sess_bits.append(sid)
    out.write(f"  sessions: {', '.join(sess_bits) or '(none)'}\n")
    out.write(
        "Next: confirm wake (say hey iris / hey mercury), then arm handsfree "
        "(see skill/hark/SETUP.md). Optional enroll: `hark wake-enroll` (beep-paced samples; I006).\n"
    )
    return OK


def cmd_setup(args: Any) -> int:
    """argparse adapter."""
    return run_setup(
        non_interactive=bool(getattr(args, "yes", False)),
        persona=getattr(args, "persona", None),
        wake_engine=getattr(args, "wake_engine", None),
        voice=getattr(args, "voice", None),
        names=getattr(args, "names", None),
        sessions=getattr(args, "sessions", None),
        skip_doctor=bool(getattr(args, "skip_doctor", False)),
        skip_download=bool(getattr(args, "skip_download", False)),
        force=bool(getattr(args, "force", False)),
        config_path=(
            Path(args.config_path) if getattr(args, "config_path", None) else None
        ),
    )

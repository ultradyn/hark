"""hark doctor — diagnostics (redacted)."""

from __future__ import annotations

import json
import shutil
import sys
from typing import Any, TextIO

from hark import __version__
from hark.config import HarkConfig, load_config
from hark.exitcodes import HERDR, OK
from hark.herdr.client import HerdrClient
from hark.paths import (
    cache_dir,
    config_dir,
    default_config_path,
    grok_auth_path,
    state_dir,
)
from hark.providers.auth import all_provider_status


def run_doctor(
    cfg: HarkConfig | None = None,
    *,
    as_json: bool = False,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    cfg = cfg or load_config()

    report: dict[str, Any] = {
        "hark_version": __version__,
        "config_path": str(cfg.path) if cfg.path else str(default_config_path()),
        "config_exists": bool(cfg.path and cfg.path.is_file()),
        "config_warnings": list(cfg.warnings),
        "paths": {
            "config_dir": str(config_dir()),
            "state_dir": str(state_dir()),
            "cache_dir": str(cache_dir()),
            "grok_auth": str(grok_auth_path()),
            "grok_auth_exists": grok_auth_path().is_file(),
        },
        "listen": {
            "end_mode": cfg.listen.end_mode,
            "max_listen_s": cfg.listen.max_listen_s,
            "end_phrase_count": len(cfg.listen.end_phrases),
            "strip_phrase": cfg.listen.strip_phrase,
            "soft_end_phrases_enabled": bool(
                getattr(cfg.listen, "soft_end_phrases_enabled", True)
            ),
            "soft_end_phrase_count": len(
                getattr(cfg.listen, "soft_end_phrases", []) or []
            ),
        },
        "ambient": {
            "enabled": cfg.ambient.enabled,
            "engine": cfg.ambient.engine,
            "wake_mode": getattr(cfg.ambient, "wake_mode", "names"),
            "names": list(getattr(cfg.ambient, "names", []) or []),
            "activation_count": len(cfg.ambient.activation_phrases),
            "learn_from_near_misses": bool(
                getattr(cfg.ambient, "learn_from_near_misses", True)
            ),
            "model_path": cfg.ambient.model_path,
            "model_ok": bool(
                cfg.ambient.model_path
                and __import__("pathlib").Path(cfg.ambient.model_path).is_dir()
            ),
            "snippet_s": cfg.ambient.snippet_s,
        },
        "herdr_bin": shutil.which("herdr"),
        "sessions": [],
        "providers": [],
        "ok": True,
        "herdr_ok": True,
    }

    for session in cfg.sessions:
        client = HerdrClient(session)
        health = client.health()
        entry = {
            "session_id": health.session_id,
            "ok": health.ok,
            "version": health.version,
            "protocol": health.protocol,
            "socket": health.socket,
            "agent_count": health.agent_count,
            "error": health.error,
        }
        report["sessions"].append(entry)
        if not health.ok:
            report["herdr_ok"] = False
            report["ok"] = False

    for auth in all_provider_status():
        report["providers"].append(
            {
                "name": auth.name,
                "available": auth.available,
                "source": auth.source,
                "detail": auth.detail,
            }
        )

    # Primary speech path
    xai = next((p for p in report["providers"] if p["name"] == "xai"), None)
    if xai and not xai["available"]:
        # not fatal for doctor of herdr path, but mark speech degraded
        report["speech_ok"] = False
        report["speech_hint"] = "run grok login or set XAI_API_KEY"
    else:
        report["speech_ok"] = True

    if as_json:
        out.write(json.dumps(report, indent=2) + "\n")
    else:
        _print_human(report, out=out)

    if not report["herdr_ok"]:
        return HERDR
    return OK


def _print_human(report: dict[str, Any], *, out: TextIO) -> None:
    print(f"hark doctor  v{report['hark_version']}", file=out)
    print(f"  config: {report['config_path']}"
          f" ({'found' if report['config_exists'] else 'defaults — run: hark config init'})",
          file=out)
    for w in report.get("config_warnings") or []:
        print(f"  warn: {w}", file=out)
    print(f"  state:  {report['paths']['state_dir']}", file=out)
    print(f"  herdr:  {report['herdr_bin'] or 'NOT FOUND'}", file=out)
    print("  sessions:", file=out)
    for s in report["sessions"]:
        if s["ok"]:
            print(
                f"    ✓ {s['session_id']}: herdr {s['version']} "
                f"protocol~{s['protocol']} agents={s['agent_count']} "
                f"sock={s['socket']}",
                file=out,
            )
        else:
            print(f"    ✗ {s['session_id']}: {s['error']} (sock={s['socket']})", file=out)
    print("  providers:", file=out)
    for p in report["providers"]:
        mark = "✓" if p["available"] else "·"
        src = f" [{p['source']}]" if p["source"] else ""
        print(f"    {mark} {p['name']}{src}: {p['detail']}", file=out)
    listen = report.get("listen") or {}
    print(
        f"  listen: end_mode={listen.get('end_mode', '?')} "
        f"max={listen.get('max_listen_s', '?')}s "
        f"(radio = keep open until end phrase)",
        file=out,
    )
    ambient = report.get("ambient") or {}
    model = ambient.get("model_path") or "(no model_path)"
    model_ok = ambient.get("model_ok")
    print(
        f"  ambient: enabled={ambient.get('enabled')} "
        f"engine={ambient.get('engine')} "
        f"phrases={ambient.get('activation_count', 0)} "
        f"model={'ok' if model_ok else 'MISSING'} "
        f"({model})",
        file=out,
    )
    if report.get("speech_ok"):
        print("  speech: ready (xAI or fallback keys)", file=out)
    else:
        print(f"  speech: not ready — {report.get('speech_hint')}", file=out)
    print(
        "  overall: "
        + ("OK" if report["ok"] and report.get("speech_ok", True) else "DEGRADED"),
        file=out,
    )

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
    codex_auth_path,
    config_dir,
    default_config_path,
    grok_auth_path,
    legacy_minimax_path,
    mmx_config_path,
    opencode_auth_path,
    pi_agent_auth_path,
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
            "codex_auth": str(codex_auth_path()),
            "codex_auth_exists": codex_auth_path().is_file(),
            "opencode_auth": str(opencode_auth_path()),
            "opencode_auth_exists": opencode_auth_path().is_file(),
            "pi_agent_auth": str(pi_agent_auth_path()),
            "pi_agent_auth_exists": pi_agent_auth_path().is_file(),
            "mmx_config": str(mmx_config_path()),
            "mmx_config_exists": mmx_config_path().is_file(),
            "legacy_minimax": str(legacy_minimax_path()),
            "legacy_minimax_exists": legacy_minimax_path().exists(),
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

    # Media ducking readiness (B047 / I002) — soft warnings only; never hard-fail
    report["media_duck"] = _media_duck_report(cfg)

    # Dashboard / hark serve posture (B066) — misconfig that would refuse
    # startup is an error; TLS/ffmpeg gaps are advisory
    report["dashboard"] = _dashboard_report(cfg)
    if report["dashboard"]["errors"]:
        report["ok"] = False

    # Coding CLI resolve readiness (I005 / B059) — soft only
    report["coding_clis"] = _coding_clis_report(cfg)

    if as_json:
        out.write(json.dumps(report, indent=2) + "\n")
    else:
        _print_human(report, out=out)

    if not report["herdr_ok"]:
        return HERDR
    return OK


def _coding_clis_report(cfg: HarkConfig) -> dict[str, Any]:
    """Which catalog coding CLIs resolve (soft; missing agents are OK)."""
    from hark.agents.resolve import catalog_status

    agents_cfg = getattr(cfg, "agents", None)
    prefer = True
    overrides = None
    if agents_cfg is not None:
        prefer = bool(getattr(agents_cfg, "prefer_aliases", True))
        overrides = dict(getattr(agents_cfg, "cli", {}) or {}) or None
    rows = catalog_status(overrides=overrides, prefer_aliases=prefer)
    ok_n = sum(1 for r in rows if r.get("ok"))
    return {
        "status": "ready" if ok_n else "empty",
        "resolved": ok_n,
        "total": len(rows),
        "prefer_aliases": prefer,
        "agents": rows,
    }


def _media_duck_report(cfg: HarkConfig) -> dict[str, Any]:
    """Probe pactl/playerctl for ducking; degraded status is advisory only."""
    audio = cfg.audio
    pactl_bin = shutil.which("pactl")
    playerctl_bin = shutil.which("playerctl")
    duck_on = bool(
        audio.duck_media_during_tts
        or audio.duck_media_during_stt
        or audio.pause_media_during_tts
        or audio.pause_media_during_stt
    )
    pause_on = bool(audio.pause_media_during_tts or audio.pause_media_during_stt)
    mpris_wanted = bool(pause_on or audio.media_check_mpris)
    warnings: list[str] = []
    if duck_on and not pactl_bin:
        warnings.append(
            "pactl missing — media volume ducking unavailable (fail-open; TTS/STT still run)"
        )
    if mpris_wanted and not playerctl_bin:
        warnings.append(
            "playerctl missing — MPRIS detect/pause path degraded "
            "(volume duck still works if pactl is present)"
        )
    if duck_on and pactl_bin:
        status = "ready"
    elif duck_on and not pactl_bin:
        status = "degraded"
    else:
        status = "disabled"
    return {
        "status": status,
        "pactl": pactl_bin,
        "pactl_ok": bool(pactl_bin),
        "playerctl": playerctl_bin,
        "playerctl_ok": bool(playerctl_bin),
        "duck_media_during_tts": bool(audio.duck_media_during_tts),
        "pause_media_during_tts": bool(audio.pause_media_during_tts),
        "duck_media_during_stt": bool(audio.duck_media_during_stt),
        "pause_media_during_stt": bool(audio.pause_media_during_stt),
        "duck_level": float(audio.duck_level),
        "media_check_mpris": bool(audio.media_check_mpris),
        "warnings": warnings,
    }


LOCALHOST_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _dashboard_report(cfg: HarkConfig) -> dict[str, Any]:
    """`hark serve` readiness: bind/token/TLS posture (docs/DASHBOARD.md)."""
    dash = cfg.dashboard
    is_local = dash.host in LOCALHOST_HOSTS
    warnings: list[str] = []
    errors: list[str] = []
    if not is_local and not dash.token:
        errors.append(
            f"[dashboard].host = {dash.host!r} without a token — "
            "hark serve will refuse to start (set token; hark serve --print-token)"
        )
    if not is_local and not dash.tls_terminated:
        warnings.append(
            "remote bind without TLS: browsers block PWA install, notifications "
            "and mic capture on plain http — use `tailscale serve` and set "
            "[dashboard].tls_terminated = true"
        )
    if dash.require_token and not dash.token:
        errors.append("[dashboard].require_token is set but no token configured")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        warnings.append(
            "ffmpeg missing — browser-mic dictation unavailable "
            "(host mic + wav uploads still work)"
        )
    status = "error" if errors else ("warn" if warnings else "ok")
    return {
        "status": status,
        "host": dash.host,
        "port": dash.port,
        "localhost": is_local,
        "token_configured": bool(dash.token),
        "require_token": dash.require_token,
        "tls_terminated": dash.tls_terminated,
        "ffmpeg_ok": bool(ffmpeg),
        "warnings": warnings,
        "errors": errors,
    }


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
    md = report.get("media_duck") or {}
    if md:
        pactl_s = "ok" if md.get("pactl_ok") else "MISSING"
        playerctl_s = "ok" if md.get("playerctl_ok") else "missing"
        print(
            f"  media duck: {md.get('status', '?')} "
            f"(pactl={pactl_s} playerctl={playerctl_s} "
            f"level={md.get('duck_level', '?')} "
            f"tts={_on_off(md.get('duck_media_during_tts'))} "
            f"stt={_on_off(md.get('duck_media_during_stt'))} "
            f"pause_stt={_on_off(md.get('pause_media_during_stt'))})",
            file=out,
        )
        for w in md.get("warnings") or []:
            print(f"  warn: {w}", file=out)
    dash = report.get("dashboard") or {}
    if dash:
        print(
            f"  dashboard: {dash.get('status', '?')} "
            f"(bind={dash.get('host')}:{dash.get('port')} "
            f"token={_on_off(dash.get('token_configured'))} "
            f"tls={_on_off(dash.get('tls_terminated'))} "
            f"ffmpeg={'ok' if dash.get('ffmpeg_ok') else 'missing'})",
            file=out,
        )
        for e in dash.get("errors") or []:
            print(f"  ERROR: {e}", file=out)
        for w in dash.get("warnings") or []:
            print(f"  warn: {w}", file=out)
    clis = report.get("coding_clis") or {}
    if clis:
        print(
            f"  coding CLIs: {clis.get('resolved', 0)}/{clis.get('total', 0)} resolved "
            f"(prefer_aliases={clis.get('prefer_aliases')})",
            file=out,
        )
        for row in clis.get("agents") or []:
            if row.get("ok"):
                print(
                    f"    ✓ {row.get('agent')}: {row.get('argv0')} "
                    f"[{row.get('source')}]",
                    file=out,
                )
            else:
                print(f"    · {row.get('agent')}: missing", file=out)
    print(
        "  overall: "
        + ("OK" if report["ok"] and report.get("speech_ok", True) else "DEGRADED"),
        file=out,
    )


def _on_off(value: Any) -> str:
    return "on" if value else "off"

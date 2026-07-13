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


def _ambient_readiness(cfg: HarkConfig) -> dict[str, Any]:
    """Engine-aware ambient/wake model readiness for doctor (B070)."""
    from pathlib import Path

    eng = (cfg.ambient.engine or "vosk").strip().lower()
    model_path = cfg.ambient.model_path
    model_ok = False
    status = "unknown"
    warnings: list[str] = []
    hints: list[str] = []
    package_ok: bool | None = None

    if eng in ("text_probe", "mock", "test", "off", "none", "disabled"):
        model_ok = True
        status = "probe"
        package_ok = True
    elif eng == "vosk":
        try:
            import vosk  # noqa: F401

            package_ok = True
        except ImportError:
            package_ok = False
            warnings.append("vosk package missing (uv sync --extra wake)")
        p = Path(model_path) if model_path else None
        model_ok = bool(p and p.is_dir())
        if model_ok:
            status = "ready" if package_ok else "package_missing"
        else:
            status = "missing_model"
            hints.append("run ./scripts/setup-ambient.sh or download-vosk-model.sh")
    elif eng in ("sherpa_kws", "sherpa", "kws"):
        try:
            import sherpa_onnx  # noqa: F401

            package_ok = True
        except ImportError:
            package_ok = False
            warnings.append(
                "sherpa-onnx package missing (uv sync --extra wake-sherpa)"
            )
        try:
            from hark.wake import is_sherpa_kws_model_dir

            model_ok = is_sherpa_kws_model_dir(model_path)
        except Exception:
            model_ok = bool(
                model_path and Path(model_path).is_dir()
                and (Path(model_path) / "tokens.txt").is_file()
            )
        if model_ok and package_ok:
            status = "ready"
        elif not model_ok:
            status = "missing_model"
            hints.append(
                "run ./scripts/download-sherpa-kws-model.sh "
                "(English GigaSpeech 3.3M int8 KWS)"
            )
        else:
            status = "package_missing"
    else:
        status = "unknown_engine"
        warnings.append(f"unknown ambient.engine={eng!r}")
        p = Path(model_path) if model_path else None
        model_ok = bool(p and p.is_dir())

    return {
        "enabled": cfg.ambient.enabled,
        "engine": cfg.ambient.engine,
        "wake_mode": getattr(cfg.ambient, "wake_mode", "names"),
        "names": list(getattr(cfg.ambient, "names", []) or []),
        "activation_count": len(cfg.ambient.activation_phrases),
        "learn_from_near_misses": bool(
            getattr(cfg.ambient, "learn_from_near_misses", True)
        ),
        "model_path": model_path,
        "model_ok": model_ok,
        "package_ok": package_ok,
        "status": status,
        "warnings": warnings,
        "hints": hints,
        "snippet_s": cfg.ambient.snippet_s,
    }


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
        "ambient": _ambient_readiness(cfg),
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

    # Optional local full-STT (B072) — soft readiness only; cloud remains default
    try:
        from hark.providers.local_stt import local_stt_statuses

        local_model = getattr(cfg.stt, "local_model", "tiny.en") or "tiny.en"
        report["local_stt"] = [
            {
                "name": s.name,
                "available": s.available,
                "detail": s.detail,
                "rtf_note": s.rtf_note,
            }
            for s in local_stt_statuses(model=local_model)
        ]
        report["stt_provider"] = cfg.stt.provider
        report["stt_local_fail_open"] = bool(getattr(cfg.stt, "local_fail_open", True))
    except Exception as exc:  # pragma: no cover - defensive
        report["local_stt"] = []
        report["local_stt_error"] = str(exc)

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

    # GitHub release self-update check (B088) — advisory only
    report["update"] = _update_report(cfg)

    # PATH / uv-tool install freshness vs local source tree (B100) — soft only
    report["install"] = _install_report()

    # TTS play queue (B099) — auto-heal abandoned tickets; soft warn only
    report["tts_play_queue"] = _tts_play_queue_report()

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


def _tts_play_queue_report() -> dict[str, Any]:
    """Heal abandoned TTS play-queue tickets; report status (B099, soft only)."""
    try:
        from hark.audio.playback import heal_tts_play_queue

        status = heal_tts_play_queue(missing_as_abandoned=True)
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "status": "error",
            "ok": False,
            "error": str(exc)[:200],
            "warnings": [f"tts play queue check failed: {exc}"],
        }
    warnings: list[str] = []
    healed_n = int(status.get("healed_count") or 0)
    pending = int(status.get("pending") or 0)
    if healed_n:
        warnings.append(
            f"healed {healed_n} abandoned TTS play ticket(s) "
            f"(serving={status.get('serving')} next={status.get('next')})"
        )
    if pending > 0 and not healed_n:
        # Live holders still waiting — informational, not a failure
        warnings.append(
            f"TTS play queue has {pending} pending ticket(s) "
            f"(serving={status.get('serving')} next={status.get('next')})"
        )
    if healed_n:
        qstatus = "healed"
    elif pending > 0:
        qstatus = "busy"
    else:
        qstatus = "idle"
    return {
        "status": qstatus,
        "ok": bool(status.get("ok", True)),
        "serving": status.get("serving"),
        "next": status.get("next"),
        "pending": pending,
        "healed_count": healed_n,
        "healed": status.get("healed") or [],
        "path": status.get("path"),
        "warnings": warnings,
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


def _update_report(cfg: HarkConfig) -> dict[str, Any]:
    """Cached GitHub release check (B088). Soft; never fails doctor."""
    try:
        from hark.update_check import update_status_for_api

        return update_status_for_api(
            enabled=bool(getattr(cfg.update, "enabled", True)),
            repo=getattr(cfg.update, "repo", None),
        )
    except Exception as exc:  # pragma: no cover — defensive
        return {"error": str(exc), "update_available": False, "disabled": False}


def _install_report() -> dict[str, Any]:
    """PATH/tool install vs local source tree (B100). Soft; never fails doctor."""
    try:
        from hark.install_check import install_status_for_api

        return install_status_for_api()
    except Exception as exc:  # pragma: no cover — defensive
        return {
            "ok": False,
            "status": "error",
            "stale": False,
            "error": str(exc),
            "warnings": [f"install check failed: {exc}"],
            "hints": [],
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
    local_stt = report.get("local_stt") or []
    if local_stt:
        pinned = report.get("stt_provider") or "auto"
        fail_open = report.get("stt_local_fail_open", True)
        print(
            f"  local STT: config provider={pinned} "
            f"fail_open={fail_open} (optional; not ambient wake)",
            file=out,
        )
        for p in local_stt:
            mark = "✓" if p["available"] else "·"
            print(f"    {mark} {p['name']}: {p['detail']}", file=out)
            if p.get("rtf_note"):
                print(f"      RTF: {p['rtf_note']}", file=out)
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
    eng = ambient.get("engine") or "?"
    status = ambient.get("status") or ("ok" if model_ok else "missing_model")
    print(
        f"  ambient: enabled={ambient.get('enabled')} "
        f"engine={eng} "
        f"phrases={ambient.get('activation_count', 0)} "
        f"model={'ok' if model_ok else 'MISSING'} "
        f"status={status} "
        f"({model})",
        file=out,
    )
    for w in ambient.get("warnings") or []:
        print(f"  warn: {w}", file=out)
    for h in ambient.get("hints") or []:
        print(f"  hint: {h}", file=out)
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
    upd = report.get("update") or {}
    if upd:
        if upd.get("disabled"):
            print("  update: check disabled", file=out)
        elif upd.get("update_available"):
            print(
                f"  update: AVAILABLE  {upd.get('current_version')} → "
                f"{upd.get('latest_version')}  "
                f"({upd.get('html_url') or upd.get('repo')})",
                file=out,
            )
        elif upd.get("latest_version"):
            print(
                f"  update: up to date  "
                f"(installed={upd.get('current_version')} "
                f"latest={upd.get('latest_version')}"
                f"{'; stale' if upd.get('stale') else ''})",
                file=out,
            )
        elif upd.get("error"):
            print(f"  update: check failed ({upd.get('error')})", file=out)
        else:
            print("  update: no release data yet", file=out)
    inst = report.get("install") or {}
    if inst:
        mode = inst.get("mode") or "?"
        st = inst.get("status") or "?"
        ver = inst.get("package_version") or inst.get("hark_version") or "?"
        src = inst.get("compared_source") or inst.get("install_source_path") or ""
        src_bit = f" source={src}" if src else ""
        editable = "editable" if inst.get("editable") else "non-editable"
        print(
            f"  install: {st}  ({editable} mode={mode} v{ver}{src_bit})",
            file=out,
        )
        if inst.get("which_hark"):
            print(f"    which: {inst['which_hark']}", file=out)
        ph = inst.get("path_hark") or {}
        lag_path = ph.get("path")
        if (
            lag_path
            and not ph.get("same_as_running")
            and lag_path != inst.get("which_hark")
        ):
            print(
                f"    tool:  {lag_path} "
                f"(≠ this process; cmds={len(ph.get('commands') or [])})",
                file=out,
            )
        elif lag_path and not ph.get("same_as_running") and inst.get("stale"):
            print(
                f"    tool:  {lag_path} "
                f"(≠ this process; cmds={len(ph.get('commands') or [])})",
                file=out,
            )
        cmp = inst.get("path_comparison") or inst.get("comparison") or {}
        if (inst.get("comparison") or {}).get("git_describe"):
            print(f"    git:  {inst['comparison']['git_describe']}", file=out)
        elif cmp.get("git_describe"):
            print(f"    git:  {cmp['git_describe']}", file=out)
        missing = cmp.get("missing_commands") or []
        if not missing and (inst.get("comparison") or {}).get("missing_commands"):
            missing = inst["comparison"]["missing_commands"]
        if missing:
            print(f"    missing cmds: {', '.join(missing)}", file=out)
        for w in inst.get("warnings") or []:
            print(f"  warn: {w}", file=out)
        for h in inst.get("hints") or []:
            print(f"  hint: {h}", file=out)
    tq = report.get("tts_play_queue") or {}
    if tq:
        print(
            f"  tts play queue: {tq.get('status', '?')} "
            f"(serving={tq.get('serving')} next={tq.get('next')} "
            f"pending={tq.get('pending', 0)} "
            f"healed={tq.get('healed_count', 0)})",
            file=out,
        )
        for w in tq.get("warnings") or []:
            print(f"  warn: {w}", file=out)
    print(
        "  overall: "
        + ("OK" if report["ok"] and report.get("speech_ok", True) else "DEGRADED"),
        file=out,
    )


def _on_off(value: Any) -> str:
    return "on" if value else "off"

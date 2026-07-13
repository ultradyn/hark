"""Ambient listen: local wake snippets, then cloud STT for the prompt body."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from typing import Any, TextIO

from hark.audio.capture import MicBusyError, MicLease, record_seconds
from hark.config import HarkConfig, load_config
from hark.debug_snips import purge_old_debug_snips, save_wake_snippet
from hark.events import new_event_id, utc_now_iso
from hark.lifecycle import (
    clear_reload_request,
    clear_shutdown_reason,
    get_shutdown_reason,
    install_signal_handlers,
    reload_requested,
    shutdown_phrase,
    shutdown_requested,
)
from hark.mic_coord import ambient_pause_requested
from hark.partial import make_final_event, new_stream_id
from hark.speech import run_listen, run_tts
from hark.syslog import log as syslog
from hark.wake import (
    DEFAULT_ACTIVATION_PHRASES,
    NearMissAccumulator,
    WakeBackend,
    WakeHit,
    build_wake_backend,
    make_wake_near_miss_event,
    plausible_near_miss,
)


@dataclass
class AmbientResult:
    activated: bool
    phrase: str | None
    text: str | None
    wake_backend: str | None = None
    listen: dict[str, Any] | None = None
    event_id: str | None = None
    stream_id: str | None = None
    partials_emitted: int = 0
    final: bool = True
    partial: bool = False


def _wait_for_wake(
    backend: WakeBackend,
    *,
    snippet_s: float,
    deadline: float,
    out: TextIO | None = None,
    debug_every_s: float = 15.0,
    debug_save: bool = False,
    debug_retention_days: float = 7.0,
    near_miss_acc: NearMissAccumulator | None = None,
    phrases: list[str] | tuple[str, ...] | None = None,
) -> WakeHit | None:
    """Record short windows until activation or deadline. Reuses backend (no reload).

    Mic lease is held **per snippet only**, and ambient yields while
    ``ambient.pause`` is set so listen/ask can take the mic.

    Plausible failed activations are grouped and emitted as
    ``ambient.wake_near_miss`` on *out* (see NearMissAccumulator schedule).
    """
    snippet = max(0.8, min(snippet_s, 2.5))
    last_debug = 0.0
    snips_since_purge = 0
    acc = near_miss_acc
    phrase_list = list(
        phrases
        if phrases is not None
        else getattr(backend, "phrases", None) or DEFAULT_ACTIVATION_PHRASES
    )
    while time.monotonic() < deadline:
        if shutdown_requested():
            return None
        # SIGHUP / config reload: exit wait so the loop can re-read config
        if reload_requested():
            return None
        # Bound listen/ask requested the mic — yield until they clear pause
        if ambient_pause_requested():
            time.sleep(0.05)
            continue
        try:
            # Short exclusive hold so Mode A listen can interleave between snippets
            with MicLease("ambient"):
                if ambient_pause_requested() or shutdown_requested():
                    continue
                pcm = record_seconds(snippet, sample_rate=16000)
        except MicBusyError:
            # Another process holds mic — wait and retry
            time.sleep(0.1)
            continue
        except Exception as exc:
            if out is not None:
                err = {
                    "schema": "hark.event.v1",
                    "kind": "ambient.error",
                    "event_id": new_event_id(),
                    "observed_at": utc_now_iso(),
                    "error": f"record: {exc}",
                }
                out.write(json.dumps(err, separators=(",", ":")) + "\n")
                out.flush()
            time.sleep(0.5)
            continue

        hit = backend.score_snippet(pcm, 16000)
        text = getattr(backend, "last_text", "") or ""
        rms = float(getattr(backend, "last_rms", 0.0) or 0.0)

        # Dev: keep audio+transcript for scored snippets (hits and misses)
        if debug_save and (hit is not None or text):
            save_wake_snippet(
                pcm16=pcm,
                sample_rate=16000,
                text=text or None,
                matched=hit is not None,
                phrase=hit.phrase if hit else None,
                rms=rms,
                backend=getattr(backend, "name", None),
                enabled=True,
            )
            snips_since_purge += 1
            if snips_since_purge >= 20:
                purge_old_debug_snips(retention_days=debug_retention_days)
                snips_since_purge = 0

        if hit is not None:
            if acc is not None:
                acc.reset_pending()
            syslog(
                "ambient.wake",
                component="ambient",
                message=hit.phrase,
                phrase=hit.phrase,
                raw=hit.raw,
                remainder=hit.remainder,
                backend=hit.backend,
                confidence=hit.confidence,
            )
            return hit

        # Plausible failed wake → Mode A monitor (grouped), never spam on noise
        if acc is not None and text:
            miss = plausible_near_miss(text, phrase_list)
            if miss is not None:
                group = acc.add(miss)
                if group is not None:
                    ev = make_wake_near_miss_event(
                        group,
                        total_near_misses=acc.total,
                        group_index=acc.group_index,
                        phrases=phrase_list,
                    )
                    if out is not None:
                        out.write(json.dumps(ev, separators=(",", ":")) + "\n")
                        out.flush()
                    syslog(
                        "ambient.wake_near_miss",
                        component="ambient",
                        level="info",
                        message=f"{len(group)} near-miss(es)",
                        count=len(group),
                        total_near_misses=acc.total,
                        attempts=[m.text for m in group],
                    )

        now = time.monotonic()
        if now - last_debug >= debug_every_s:
            last_debug = now
            dbg = {
                "schema": "hark.event.v1",
                "kind": "ambient.debug",
                "event_id": new_event_id(),
                "observed_at": utc_now_iso(),
                "rms": round(rms, 5),
                "last_text": text or None,
                "scored": getattr(backend, "snippets_scored", None),
                "skipped_quiet": getattr(backend, "snippets_skipped_quiet", None),
            }
            if out is not None:
                out.write(json.dumps(dbg, separators=(",", ":")) + "\n")
                out.flush()
            syslog(
                "ambient.debug",
                component="ambient",
                level="debug",
                message=text or "quiet",
                rms=dbg["rms"],
                last_text=dbg["last_text"],
                scored=dbg["scored"],
                skipped_quiet=dbg["skipped_quiet"],
            )
        time.sleep(0.05)
    return None


def complete_after_wake(
    cfg: HarkConfig,
    hit: WakeHit,
    *,
    announce: bool = True,
    on_partial: Any | None = None,
    out: TextIO | None = None,
) -> AmbientResult:
    """After wake: beep→record→beep (no spoken 'okay' / 'listening').

    In radio end_mode, interim STT is streamed as ambient.partial (HOLD) via
    on_partial / out until the end phrase yields ambient.prompt (final).
    """
    del announce
    event_id = new_event_id()
    stream_id = new_stream_id()

    def _emit_partial(ev: dict[str, Any]) -> None:
        ev = {**ev, "phrase": hit.phrase}
        if out is not None:
            out.write(json.dumps(ev, separators=(",", ":")) + "\n")
            out.flush()
        if on_partial is not None:
            on_partial(ev)
        syslog(
            "ambient.partial",
            component="ambient",
            level="info",
            stream_id=ev.get("stream_id"),
            seq=ev.get("seq"),
            text=(ev.get("text") or "")[:300],
            partial=True,
            final=False,
            warning=ev.get("warning"),
        )

    try:
        listened = run_listen(
            cfg,
            end_mode=cfg.listen.end_mode,
            on_partial=_emit_partial if cfg.listen.end_mode == "radio" else None,
            stream_id=stream_id,
            partial_kind="ambient.partial",
        )
    except Exception as exc:
        return AmbientResult(
            activated=True,
            phrase=hit.phrase,
            text=None,
            wake_backend=hit.backend,
            listen={"error": str(exc)},
            event_id=event_id,
            stream_id=stream_id,
            final=True,
            partial=False,
        )

    if listened.cancelled:
        return AmbientResult(
            activated=True,
            phrase=hit.phrase,
            text=listened.text,
            wake_backend=hit.backend,
            listen={
                "provider": listened.provider,
                "duration_ms": listened.duration_ms,
                "end_phrase": listened.end_phrase,
                "cancelled": True,
            },
            event_id=event_id,
            stream_id=listened.stream_id or stream_id,
            partials_emitted=listened.partials_emitted,
            final=True,
            partial=False,
        )

    return AmbientResult(
        activated=True,
        phrase=hit.phrase,
        text=listened.text,
        wake_backend=hit.backend,
        listen={
            "provider": listened.provider,
            "duration_ms": listened.duration_ms,
            "end_phrase": listened.end_phrase,
            "cancelled": listened.cancelled,
        },
        event_id=event_id,
        stream_id=listened.stream_id or stream_id,
        partials_emitted=listened.partials_emitted,
        final=True,
        partial=False,
    )


def run_ambient(
    cfg: HarkConfig,
    *,
    once: bool = True,
    timeout_s: float | None = None,
    announce: bool = True,
    backend: WakeBackend | None = None,
    out: TextIO | None = None,
    near_miss_acc: NearMissAccumulator | None = None,
) -> AmbientResult:
    """One wake→prompt cycle. Pass backend= to avoid reloading vosk each time."""
    amb = cfg.ambient
    if not amb.enabled and not once:
        return AmbientResult(activated=False, phrase=None, text=None)

    if backend is None:
        if not amb.model_path and amb.engine == "vosk":
            raise RuntimeError(
                "ambient.engine=vosk requires model_path — run ./scripts/setup-ambient.sh"
            )
        backend = build_wake_backend(
            amb.engine,
            phrases=amb.activation_phrases,
            model_path=amb.model_path,
        )

    deadline = time.monotonic() + (timeout_s or amb.timeout_s or 300.0)

    # Lease is taken per snippet inside _wait_for_wake (not for the whole wait)
    hit = _wait_for_wake(
        backend,
        snippet_s=amb.snippet_s,
        deadline=deadline,
        out=out,
        debug_save=bool(amb.debug),
        debug_retention_days=float(amb.debug_retention_days),
        near_miss_acc=near_miss_acc,
        phrases=amb.activation_phrases,
    )

    if hit is None:
        return AmbientResult(
            activated=False,
            phrase=None,
            text=None,
            wake_backend=getattr(backend, "name", None),
        )

    # Mic lease released — cloud listen / TTS may take the mic
    return complete_after_wake(cfg, hit, announce=announce, out=out)


def apply_config_reload(
    cfg: HarkConfig,
    backend: WakeBackend,
) -> tuple[HarkConfig, WakeBackend, dict[str, Any]]:
    """Re-read config from disk and hot-apply ambient wake settings.

    Phrase changes update ``backend.phrases`` in place so vosk keeps its
    loaded model. Engine/model_path changes rebuild the backend.
    """
    path = cfg.path
    new_cfg = load_config(path)
    # Stay armed while the ambient loop is running
    new_cfg.ambient.enabled = True

    phrases = list(new_cfg.ambient.activation_phrases)
    engine_changed = (new_cfg.ambient.engine or "").lower() != (
        cfg.ambient.engine or ""
    ).lower()
    model_changed = new_cfg.ambient.model_path != cfg.ambient.model_path

    info: dict[str, Any] = {
        "phrases": phrases,
        "engine": new_cfg.ambient.engine,
        "model_path": new_cfg.ambient.model_path,
        "rebuilt_backend": False,
        "path": str(path) if path else None,
    }

    if engine_changed or model_changed:
        if not new_cfg.ambient.model_path and new_cfg.ambient.engine == "vosk":
            raise RuntimeError(
                "ambient.engine=vosk requires model_path — run ./scripts/setup-ambient.sh"
            )
        backend = build_wake_backend(
            new_cfg.ambient.engine,
            phrases=phrases,
            model_path=new_cfg.ambient.model_path,
        )
        info["rebuilt_backend"] = True
    elif hasattr(backend, "phrases"):
        backend.phrases = phrases

    return new_cfg, backend, info


def ambient_event_line(result: AmbientResult) -> dict[str, Any]:
    if result.activated and result.text and not (
        result.listen and result.listen.get("cancelled")
    ):
        kind = "ambient.prompt"
    elif result.listen and result.listen.get("cancelled"):
        kind = "ambient.cancelled"
    elif result.activated and result.listen and result.listen.get("error"):
        kind = "ambient.error"
    elif not result.activated:
        kind = "ambient.timeout"
    else:
        kind = "ambient.error"

    base = {
        "schema": "hark.event.v1",
        "kind": kind,
        "event_id": result.event_id or new_event_id(),
        "observed_at": utc_now_iso(),
        "phrase": result.phrase,
        "text": result.text,
        "wake_backend": result.wake_backend,
        "listen": result.listen,
        "partial": False,
        "final": True,
        "stream_id": result.stream_id,
        "partials_emitted": result.partials_emitted,
    }
    if kind == "ambient.prompt":
        base["instructions"] = (
            "FINAL operator prompt for this stream_id (supersedes all ambient.partial "
            "events with the same stream_id). You may now respond/act. "
            "Not bound to a pane — use judgment; do not invent answers."
        )
        base["warning"] = None
    elif kind == "ambient.cancelled":
        base["instructions"] = (
            "Cancelled — ignore prior partials for this stream_id."
        )
    return base


def _emit_reload_event(
    out: TextIO,
    info: dict[str, Any],
    *,
    error: str | None = None,
) -> None:
    if error:
        line = {
            "schema": "hark.event.v1",
            "kind": "ambient.error",
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            "error": f"config reload: {error}",
        }
    else:
        line = {
            "schema": "hark.event.v1",
            "kind": "ambient.reloaded",
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            "phrases": info.get("phrases"),
            "engine": info.get("engine"),
            "model_path": info.get("model_path"),
            "rebuilt_backend": info.get("rebuilt_backend"),
            "path": info.get("path"),
            "instructions": (
                "Config reloaded (SIGHUP). Activation phrases and ambient "
                "settings now match disk. Prefer kill -HUP <pid> after editing "
                "config.toml; full restart still works."
            ),
        }
    out.write(json.dumps(line, separators=(",", ":")) + "\n")
    out.flush()
    syslog(
        "ambient.reloaded" if not error else "ambient.reload_error",
        component="ambient",
        level="info" if not error else "warn",
        phrases=info.get("phrases") if not error else None,
        error=error,
        rebuilt_backend=info.get("rebuilt_backend") if not error else None,
    )


def run_ambient_loop(
    cfg: HarkConfig,
    *,
    out: TextIO | None = None,
    announce: bool = True,
    idle_log_s: float = 60.0,
) -> int:
    """Continuous ambient: load vosk once, wake→prompt→repeat until Ctrl+C/SIGTERM.

    SIGTERM during an active listen does not abort mid-recording: we finish the
    current wake→STT cycle, emit the event, then exit.

    SIGHUP re-reads config (custom activation phrases, etc.) without stopping
    the process. Phrase-only changes hot-update the backend; engine/model
    changes rebuild it.
    """
    out = out or sys.stdout
    cfg.ambient.enabled = True
    install_signal_handlers()

    # Hardware unmute → OS unmute (Wave button / ALSA → Pulse)
    if getattr(cfg.audio, "sync_hw_unmute", True):
        try:
            from hark.audio.mic_mute import start_mute_sync_watcher

            start_mute_sync_watcher(enabled=True)
        except Exception:
            pass

    if not cfg.ambient.model_path and cfg.ambient.engine == "vosk":
        err = {
            "schema": "hark.event.v1",
            "kind": "ambient.error",
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            "error": "no vosk model_path — run ./scripts/setup-ambient.sh",
        }
        out.write(json.dumps(err, separators=(",", ":")) + "\n")
        out.flush()
        return 1

    # Load model once
    backend = build_wake_backend(
        cfg.ambient.engine,
        phrases=cfg.ambient.activation_phrases,
        model_path=cfg.ambient.model_path,
    )
    # Persist near-miss grouping across wake cycles for Mode A monitor
    near_miss_acc = NearMissAccumulator()
    # Eager load vosk so boot TTS happens after model is ready
    try:
        backend.score_snippet(b"\x00\x00" * 1600, 16000)
    except Exception:
        pass

    boot = {
        "schema": "hark.event.v1",
        "kind": "ambient.armed",
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "engine": cfg.ambient.engine,
        "model_path": cfg.ambient.model_path,
        "phrases": list(cfg.ambient.activation_phrases),
        "snippet_s": cfg.ambient.snippet_s,
        "instructions": (
            "Ambient armed. Say an activation phrase (defaults: hey hark / "
            "hey herald; see ambient.activation_phrases / extra_trigger_phrases), "
            "then your prompt. SIGHUP reloads config. Mic mutes during TTS. "
            "Energy-gated vosk (quiet frames skipped)."
        ),
    }
    out.write(json.dumps(boot, separators=(",", ":")) + "\n")
    out.flush()
    syslog(
        "ambient.armed",
        component="ambient",
        engine=cfg.ambient.engine,
        model_path=cfg.ambient.model_path,
        phrases=list(cfg.ambient.activation_phrases),
    )

    if announce:
        try:
            # Lifecycle cues: keep mic unmuted (Wave ring stays white).
            # Skip (do not hold/block ambient boot) if operator is in a call.
            run_tts(
                cfg,
                "Hark ambient is listening. Say hey hark when you need me.",
                play=True,
                mute_mic=False,
                conference_policy="skip",
            )
        except Exception as exc:
            err = {
                "schema": "hark.event.v1",
                "kind": "ambient.error",
                "event_id": new_event_id(),
                "observed_at": utc_now_iso(),
                "error": f"boot tts: {exc}",
            }
            out.write(json.dumps(err, separators=(",", ":")) + "\n")
            out.flush()

    last_idle = time.monotonic()
    try:
        while not shutdown_requested():
            if reload_requested():
                clear_reload_request()
                try:
                    cfg, backend, info = apply_config_reload(cfg, backend)
                    _emit_reload_event(out, info)
                except Exception as exc:
                    _emit_reload_event(out, {}, error=str(exc)[:200])
                continue

            result = run_ambient(
                cfg,
                once=True,
                timeout_s=cfg.ambient.timeout_s,
                announce=announce,
                backend=backend,
                out=out,
                near_miss_acc=near_miss_acc,
            )

            # Wake wait aborted for SIGHUP — apply reload, skip timeout event
            if reload_requested() and not result.activated:
                continue

            # Always emit if we got something useful; skip pure timeouts when shutting down
            if result.activated or not shutdown_requested():
                line = ambient_event_line(result)
                line = {k: v for k, v in line.items() if v is not None}
                out.write(json.dumps(line, separators=(",", ":")) + "\n")
                out.flush()
                syslog(
                    str(line.get("kind") or "ambient.event"),
                    component="ambient",
                    level="info" if result.activated else "debug",
                    message=(result.text or result.phrase or "")[:200],
                    phrase=result.phrase,
                    text=result.text,
                    wake_backend=result.wake_backend,
                    listen=result.listen,
                    event_id=result.event_id,
                )

            if shutdown_requested():
                break
            if result.activated:
                last_idle = time.monotonic()
            elif time.monotonic() - last_idle >= idle_log_s:
                last_idle = time.monotonic()
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass

    reason = get_shutdown_reason()
    phrase = shutdown_phrase(reason)
    # Speak after any in-flight recording finished (we only get here post-cycle)
    if announce:
        try:
            # Shutdown/restart cues: do not mute the mic; skip during conference
            run_tts(
                cfg,
                phrase,
                play=True,
                mute_mic=False,
                conference_policy="skip",
            )
        except Exception as exc:
            syslog(
                "ambient.shutdown_tts_error",
                component="ambient",
                level="warn",
                error=str(exc)[:160],
                reason=reason,
            )

    stop = {
        "schema": "hark.event.v1",
        "kind": "ambient.stopped",
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "graceful": True,
        "reason": reason,
        "phrase": phrase,
    }
    out.write(json.dumps(stop, separators=(",", ":")) + "\n")
    out.flush()
    syslog(
        "ambient.stopped",
        component="ambient",
        graceful=True,
        reason=reason,
        phrase=phrase,
    )
    clear_shutdown_reason()
    return 0

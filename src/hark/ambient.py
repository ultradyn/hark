"""Ambient listen: local wake snippets, then cloud STT for the prompt body."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from typing import Any, TextIO

from hark.audio.capture import MicLease, record_seconds
from hark.config import HarkConfig
from hark.events import new_event_id, utc_now_iso
from hark.speech import run_listen, run_tts
from hark.wake import WakeHit, build_wake_backend


@dataclass
class AmbientResult:
    activated: bool
    phrase: str | None
    text: str | None
    wake_backend: str | None = None
    listen: dict[str, Any] | None = None
    event_id: str | None = None


def run_ambient(
    cfg: HarkConfig,
    *,
    once: bool = True,
    timeout_s: float | None = None,
    announce: bool = True,
) -> AmbientResult:
    """Scan short local snippets until activation, then cloud-listen for prompt.

    Does NOT use cloud STT during the wake scan (except test text_probe).
    """
    amb = cfg.ambient
    if not amb.enabled and not once:
        return AmbientResult(activated=False, phrase=None, text=None)

    if not amb.model_path and amb.engine == "vosk":
        raise RuntimeError(
            "ambient.engine=vosk requires model_path — run ./scripts/setup-ambient.sh"
        )

    phrases = amb.activation_phrases
    backend = build_wake_backend(
        amb.engine,
        phrases=phrases,
        model_path=amb.model_path,
    )
    # once=True without timeout: single cycle with long wait
    deadline = time.monotonic() + (timeout_s or amb.timeout_s or 300.0)
    snippet = max(1.0, amb.snippet_s)
    hit: WakeHit | None = None
    remainder = ""

    with MicLease("ambient"):
        while time.monotonic() < deadline:
            try:
                pcm = record_seconds(snippet, sample_rate=16000)
            except Exception as exc:
                return AmbientResult(
                    activated=False,
                    phrase=None,
                    text=None,
                    wake_backend=backend.name,
                    listen={"error": str(exc)},
                )
            hit = backend.score_snippet(pcm, 16000)
            if hit is None:
                continue
            remainder = hit.remainder
            break
        else:
            return AmbientResult(
                activated=False, phrase=None, text=None, wake_backend=backend.name
            )

    if hit is None:
        return AmbientResult(
            activated=False, phrase=None, text=None, wake_backend=backend.name
        )

    event_id = new_event_id()

    # Prompt already in same utterance after wake phrase
    if remainder and len(remainder.split()) >= 3:
        if announce:
            try:
                run_tts(cfg, "Got it.", play=True)
            except Exception:
                pass
        return AmbientResult(
            activated=True,
            phrase=hit.phrase,
            text=remainder,
            wake_backend=hit.backend,
            listen={"provider": "inline_after_wake", "duration_ms": 0},
            event_id=event_id,
        )

    # Cue then cloud STT (mic unmuted after any TTS)
    if announce:
        try:
            run_tts(cfg, "Listening.", play=True)
        except Exception:
            pass

    try:
        listened = run_listen(cfg, end_mode=cfg.listen.end_mode)
    except Exception as exc:
        return AmbientResult(
            activated=True,
            phrase=hit.phrase,
            text=None,
            wake_backend=hit.backend,
            listen={"error": str(exc)},
            event_id=event_id,
        )

    if listened.cancelled:
        if announce:
            try:
                run_tts(cfg, "Cancelled.", play=True)
            except Exception:
                pass
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
        )

    if announce and listened.text:
        try:
            run_tts(cfg, "Okay.", play=True)
        except Exception:
            pass

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
    )


def ambient_event_line(result: AmbientResult) -> dict[str, Any]:
    """HEP-ish monitor line for Mode A orchestrators."""
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

    return {
        "schema": "hark.event.v1",
        "kind": kind,
        "event_id": result.event_id or new_event_id(),
        "observed_at": utc_now_iso(),
        "phrase": result.phrase,
        "text": result.text,
        "wake_backend": result.wake_backend,
        "listen": result.listen,
        "instructions": (
            "Ambient operator prompt (not bound to a pane). "
            "Use judgment; do not invent answers. "
            "If it targets an agent, use hark status / context / reply."
            if kind == "ambient.prompt"
            else None
        ),
    }


def run_ambient_loop(
    cfg: HarkConfig,
    *,
    out: TextIO | None = None,
    announce: bool = True,
    idle_log_s: float = 60.0,
) -> int:
    """Continuous ambient: wake → prompt → emit JSONL → repeat until Ctrl+C."""
    out = out or sys.stdout
    cfg.ambient.enabled = True
    last_idle = time.monotonic()

    boot = {
        "schema": "hark.event.v1",
        "kind": "ambient.armed",
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "engine": cfg.ambient.engine,
        "model_path": cfg.ambient.model_path,
        "phrases": list(cfg.ambient.activation_phrases),
        "instructions": (
            "Ambient armed. Say hey hark / hey herald, then your prompt. "
            "Mic mutes during TTS replies."
        ),
    }
    out.write(json.dumps(boot, separators=(",", ":")) + "\n")
    out.flush()

    if announce:
        try:
            run_tts(
                cfg,
                "Hark ambient is listening. Say hey hark when you need me.",
                play=True,
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

    try:
        while True:
            # Each cycle waits up to ambient.timeout_s for a wake, then retries
            result = run_ambient(
                cfg,
                once=True,
                timeout_s=cfg.ambient.timeout_s,
                announce=announce,
            )
            line = ambient_event_line(result)
            # Drop null instructions
            line = {k: v for k, v in line.items() if v is not None}
            out.write(json.dumps(line, separators=(",", ":")) + "\n")
            out.flush()

            if not result.activated:
                now = time.monotonic()
                if now - last_idle >= idle_log_s:
                    hb = {
                        "schema": "hark.event.v1",
                        "kind": "ambient.idle",
                        "event_id": new_event_id(),
                        "observed_at": utc_now_iso(),
                    }
                    out.write(json.dumps(hb, separators=(",", ":")) + "\n")
                    out.flush()
                    last_idle = now
            else:
                last_idle = time.monotonic()
            # brief gap so mic/TTS settle
            time.sleep(0.35)
    except KeyboardInterrupt:
        stop = {
            "schema": "hark.event.v1",
            "kind": "ambient.stopped",
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
        }
        out.write(json.dumps(stop, separators=(",", ":")) + "\n")
        out.flush()
        return 0

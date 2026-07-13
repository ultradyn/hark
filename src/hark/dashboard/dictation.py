"""Dictation backend for hark serve (B065).

Two capture modes, no delivery — transcripts go back to the operator for
review; submission is a separate explicit /answer or /prompt call.

- Browser: one-shot ``transcribe_blob`` — MediaRecorder audio (webm/opus,
  mp4/aac, ogg) is transcoded to WAV via ffmpeg (documented optional dep;
  WAV uploads work without it), then run through the configured STT provider.
- Host: ``start_host`` drives the existing ``run_listen`` flow (mic lease,
  ambient pause, radio partials) in a thread; progress is published on the
  stream as ``serve.dictation`` payloads; stop/cancel use the same
  listen-control files as ``hark listen-end``.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Callable

from hark.config import HarkConfig
from hark.syslog import log

WAV_TYPES = ("audio/wav", "audio/x-wav", "audio/wave")
TRANSCODE_TIMEOUT_S = 60


def _err(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def transcode_to_wav(data: bytes) -> bytes:
    """Any browser container -> 16k mono WAV via ffmpeg (raises on failure)."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "wav",
            "-ar",
            "16000",
            "-ac",
            "1",
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        timeout=TRANSCODE_TIMEOUT_S,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-400:] or "ffmpeg failed")
    return proc.stdout


def transcribe_blob(
    cfg: HarkConfig, data: bytes, content_type: str
) -> tuple[int, dict[str, Any]]:
    """POST /api/v1/dictation/transcribe — stateless browser-audio STT."""
    from hark.providers.base import ProviderError
    from hark.providers.resolve import resolve_stt

    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in WAV_TYPES:
        wav = data
    else:
        if shutil.which("ffmpeg") is None:
            return 501, _err(
                "transcode_unavailable",
                f"ffmpeg not found; upload audio/wav (got {ctype or 'unknown'})",
            )
        try:
            wav = transcode_to_wav(data)
        except Exception as exc:
            return 400, _err("bad_audio", f"transcode failed: {exc}")

    started = time.monotonic()
    try:
        stt = resolve_stt(cfg.stt.provider, stt_cfg=cfg.stt)
        result = stt.transcribe(wav)
    except ProviderError as exc:
        return 502, _err("stt_failed", str(exc))
    latency_ms = int((time.monotonic() - started) * 1000)

    try:
        from hark.usage import UsageStore

        UsageStore().record_stt(
            text=result.text,
            provider=result.provider,
            audio_ms=result.duration_ms,
            latency_ms=latency_ms,
            ok=bool(result.text),
            error=None if result.text else "empty transcript",
        )
    except Exception:
        pass
    log(
        "dictation.transcribe",
        component="dashboard",
        provider=result.provider,
        chars=len(result.text),
        latency_ms=latency_ms,
    )
    return 200, {
        "ok": True,
        "text": result.text,
        "provider": result.provider,
        "latency_ms": latency_ms,
        "audio_ms": result.duration_ms,
    }


class HostDictation:
    """Single host-mic capture at a time, driven through run_listen."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.stream_id: str | None = None
        self.state = "idle"

    def _publish(
        self, publish: Callable[[dict[str, Any]], None], state: str, **extra: Any
    ) -> None:
        self.state = state
        publish({"kind": "serve.dictation", "state": state, **extra})

    def start(
        self, cfg: HarkConfig, publish: Callable[[dict[str, Any]], None]
    ) -> tuple[int, dict[str, Any]]:
        from hark.audio.capture import MicBusyError, MicLease
        # resolve BEFORE spawning: a lazy import inside the thread binds
        # non-deterministically (test monkeypatches would race the thread and
        # a lost race silently opens the real microphone)
        from hark.speech import run_listen

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return 409, _err("capture_active", "host dictation already recording")
            # cheap availability probe; run_listen re-acquires (small race is
            # fine — the worker reports mic_busy as a failed state)
            try:
                with MicLease("hark-serve-probe"):
                    pass
            except MicBusyError as exc:
                return 409, _err("mic_busy", str(exc))

            stream_id = f"dash-{uuid.uuid4().hex[:12]}"
            self.stream_id = stream_id

            def worker() -> None:
                try:
                    self._publish(publish, "recording", stream_id=stream_id)
                    result = run_listen(
                        cfg,
                        stream_id=stream_id,
                        on_partial=lambda ev: publish(
                            {
                                "kind": "serve.dictation",
                                "state": "recording",
                                "stream_id": stream_id,
                                "partial": True,
                                "text": ev.get("text"),
                            }
                        ),
                    )
                    if result.cancelled:
                        self._publish(publish, "cancelled", stream_id=stream_id)
                    elif result.text:
                        self._publish(
                            publish,
                            "done",
                            stream_id=stream_id,
                            text=result.text,
                            provider=result.provider,
                        )
                    else:
                        self._publish(
                            publish, "failed", stream_id=stream_id, error="empty transcript"
                        )
                except Exception as exc:
                    from hark.audio.capture import MicBusyError as _MicBusy

                    code = "mic_busy" if isinstance(exc, _MicBusy) else "listen_error"
                    log(
                        "dictation.host_error",
                        component="dashboard",
                        level="error",
                        message=str(exc),
                        code=code,
                    )
                    self._publish(publish, "failed", stream_id=stream_id, error=str(exc)[:300])

            self._thread = threading.Thread(
                target=worker, name="hark-serve-dictation", daemon=True
            )
            self._thread.start()
        return 200, {"ok": True, "state": "recording", "text": None}

    def control(self, action: str) -> tuple[int, dict[str, Any]]:
        from hark.listen_control import request_listen_action

        if self._thread is None or not self._thread.is_alive() or not self.stream_id:
            return 409, _err("no_capture", "no host dictation in progress")
        request_listen_action(
            "cancel" if action == "cancel" else "finish", stream_id=self.stream_id
        )
        return 200, {
            "ok": True,
            "state": "transcribing" if action == "stop" else "cancelled",
            "text": None,
        }

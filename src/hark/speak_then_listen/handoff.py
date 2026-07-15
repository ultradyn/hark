"""Speak-then-listen handoff: half-duplex default or optional overlap pre-arm.

Owns near-end arm, overlap discard window (``audio_ok_after``), and attaching
``tts_info`` to listen errors. Calls :func:`hark.speech.run_tts` (play stack:
conference → mute → duck) and :func:`hark.speech.run_listen` (Answer Window).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from hark.answer_window.result import ListenResult
from hark.config import HarkConfig
from hark.syslog import log as syslog


def attach_tts_info(exc: BaseException, tts_info: dict[str, Any]) -> BaseException:
    """Attach TTS result dict to a listen/provider error (run_ask / CLI)."""
    try:
        setattr(exc, "tts_info", tts_info)
    except Exception:
        pass
    return exc


def speak_and_listen(
    cfg: HarkConfig,
    text: str,
    *,
    provider: str | None = None,
    voice: str | None = None,
    end_mode: str | None = None,
    out: Path | None = None,
    mute_mic: bool | None = None,
    on_partial: Any | None = None,
    partial_kind: str = "ambient.partial",
) -> tuple[dict[str, Any], ListenResult]:
    """TTS then listen with half-duplex default or optional overlap pre-arm.

    Default (``overlap_prearm=false``): half-duplex — capture starts after TTS
    exits the mute context. ``listen_pre_arm_ms`` only signals near-end so the
    sequential listen can skip / tighten the post-TTS guard.

    Optional (``overlap_prearm=true``): start the capture thread near TTS end
    while mute may still be held. Frames are discarded until TTS finishes plus
    ``overlap_discard_ms`` so residual echo is not fed to STT (ADR-009 no
    barge-in).

    Late-binds ``run_tts`` / ``run_listen`` from :mod:`hark.speech` so test
    monkeypatches on ``hark.speech.*`` still apply.
    """
    # Late bind: tests patch hark.speech.run_tts / run_listen.
    from hark import speech as speech_mod

    pre_arm_ms = int(cfg.audio.listen_pre_arm_ms)
    overlap = bool(cfg.audio.overlap_prearm) and pre_arm_ms > 0
    discard_ms = max(0, int(cfg.audio.overlap_discard_ms))
    arm_event = threading.Event()
    # Monotonic time when TTS fully ends (mute released); None while still playing
    handoff: dict[str, float | None] = {"tts_done_at": None}
    listen_box: dict[str, Any] = {}
    listen_thread: threading.Thread | None = None
    listen_lock = threading.Lock()

    def audio_ok_after() -> float | None:
        """Overlap discard deadline: None while TTS playing; else end + discard_ms."""
        done = handoff["tts_done_at"]
        if done is None:
            return None
        return float(done) + discard_ms / 1000.0

    def _listen_worker() -> None:
        try:
            listen_box["result"] = speech_mod.run_listen(
                cfg,
                profile="bound_answer",
                provider=provider,
                end_mode=end_mode,
                last_tts=text,
                already_armed=True,
                post_tts_guard_s=0.0,
                on_partial=on_partial,
                partial_kind=partial_kind,
                audio_ok_after=audio_ok_after,
            )
        except BaseException as exc:  # noqa: BLE001 — surface to joiner
            listen_box["error"] = exc

    def _on_near_end() -> None:
        # Half-duplex: only mark armed so sequential listen uses zero/tight guard.
        # Overlap: also start capture now (thread); discard until TTS ends + residual.
        arm_event.set()
        if not overlap:
            return
        nonlocal listen_thread
        with listen_lock:
            if listen_thread is not None:
                return
            listen_thread = threading.Thread(
                target=_listen_worker,
                name="hark-overlap-listen",
                daemon=True,
            )
            listen_thread.start()
            syslog(
                "listen.overlap_prearm",
                component="stt",
                level="info",
                discard_ms=discard_ms,
                pre_arm_ms=pre_arm_ms,
            )

    # Operator visual quick-reference (B095): print full question as TTS starts.
    # Only this path (ask / tts --listen) — not ambient acks or confirm readbacks.
    speech_mod.maybe_print_tts_question(cfg, text)

    tts_info = speech_mod.run_tts(
        cfg,
        text,
        provider=provider,
        voice=voice,
        play=True,
        out=out,
        mute_mic=cfg.audio.mute_mic_during_tts if mute_mic is None else mute_mic,
        on_near_end=_on_near_end if pre_arm_ms > 0 else None,
        near_end_ms=pre_arm_ms if pre_arm_ms > 0 else 0,
    )
    # Mic unmuted as TTS context exits — allow overlap discard window to close
    handoff["tts_done_at"] = time.monotonic()

    if listen_thread is not None:
        try:
            listen_thread.join()
        except BaseException:
            # The capture may be blocked inside PortAudio on the overlap worker.
            # Abort only this process's registered stream, then give its normal
            # Answer Window finally/context cleanup a bounded chance to finish.
            from hark.audio.capture import cancel_active_capture

            cancel_active_capture()
            listen_thread.join(timeout=2.0)
            raise
        err = listen_box.get("error")
        if err is not None:
            raise attach_tts_info(err, tts_info)
        listened = listen_box["result"]
        assert isinstance(listened, ListenResult)
        speech_mod._tag_meta_command(listened)
        return tts_info, listened

    # Half-duplex path (default): start listen after TTS + optional guard
    try:
        listened = speech_mod.run_listen(
            cfg,
            profile="bound_answer",
            provider=provider,
            end_mode=end_mode,
            last_tts=text,
            post_tts_guard_s=cfg.audio.post_tts_guard_ms / 1000.0,
            already_armed=arm_event.is_set(),
            on_partial=on_partial,
            partial_kind=partial_kind,
            # arm_cue from [audio].answer_arm_cue via bound_answer profile
        )
    except BaseException as exc:
        raise attach_tts_info(exc, tts_info) from exc
    speech_mod._tag_meta_command(listened)
    return tts_info, listened

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
from hark.audio.capture import capture_interrupt_signals
from hark.config import HarkConfig
from hark.syslog import log as syslog

_OVERLAP_CANCEL_JOIN_S = 2.0


def attach_tts_info(exc: BaseException, tts_info: dict[str, Any]) -> BaseException:
    """Attach TTS result dict to a listen/provider error (run_ask / CLI)."""
    try:
        setattr(exc, "tts_info", tts_info)
    except Exception:
        pass
    return exc


@capture_interrupt_signals()
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
    listen_attempt: Any | None = None
    listen_started = False
    listen_lock = threading.Lock()
    listen_callback_started = threading.Event()
    listen_publication_done = threading.Event()
    listen_worker_entered = threading.Event()

    def audio_ok_after() -> float | None:
        """Overlap discard deadline: None while TTS playing; else end + discard_ms."""
        done = handoff["tts_done_at"]
        if done is None:
            return None
        return float(done) + discard_ms / 1000.0

    def _listen_worker() -> None:
        listen_worker_entered.set()
        try:
            from hark.audio.capture import (
                bind_capture_state,
                raise_if_capture_cancelled,
            )

            # Use the attempt captured before Thread.start, rather than the
            # process-global current scope.  The CLI scope may already be
            # unwinding if this daemon was delayed during publication.
            with bind_capture_state(listen_attempt):
                raise_if_capture_cancelled(listen_attempt)
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
        finally:
            from hark.audio.capture import release_capture_attempt

            release_capture_attempt(listen_attempt)

    def _on_near_end() -> None:
        # Half-duplex: only mark armed so sequential listen uses zero/tight guard.
        # Overlap: also start capture now (thread); discard until TTS ends + residual.
        arm_event.set()
        if not overlap:
            return
        nonlocal listen_attempt, listen_started, listen_thread
        listen_callback_started.set()
        try:
            with listen_lock:
                if listen_thread is not None:
                    return
                from hark.audio.capture import register_capture_attempt

                listen_attempt = register_capture_attempt()
                listen_thread = threading.Thread(
                    target=_listen_worker,
                    name="hark-overlap-listen",
                    daemon=True,
                )
                try:
                    listen_thread.start()
                    listen_started = True
                except BaseException:
                    # Thread.start can be interrupted after the OS thread has
                    # launched but before CPython publishes ident/_started.
                    # Retain the handle and attempt for every BaseException;
                    # bounded cleanup distinguishes a truly unstarted object.
                    listen_started = True
                    raise
                syslog(
                    "listen.overlap_prearm",
                    component="stt",
                    level="info",
                    discard_ms=discard_ms,
                    pre_arm_ms=pre_arm_ms,
                )
        finally:
            listen_publication_done.set()

    # Operator visual quick-reference (B095): print full question as TTS starts.
    # Only this path (ask / tts --listen) — not ambient acks or confirm readbacks.
    speech_mod.maybe_print_tts_question(cfg, text)

    def _cancel_overlap_and_wait(primary: BaseException) -> None:
        """Cancel overlap capture and give its daemon a bounded cleanup wait."""
        from hark.audio.capture import cancel_active_capture

        signum = getattr(primary, "signum", None)
        deadline = time.monotonic() + _OVERLAP_CANCEL_JOIN_S

        def _reinforce() -> None:
            while True:
                try:
                    if isinstance(signum, int):
                        cancel_active_capture(signum)
                    else:
                        cancel_active_capture()
                    return
                except BaseException:
                    # A repeated signal can interrupt cleanup itself.  Keep the
                    # original exception authoritative and retry cancellation.
                    continue

        def _log_timeout(message: str) -> None:
            try:
                syslog(
                    "listen.cancel_cleanup_timeout",
                    component="stt",
                    level="warning",
                    timeout_s=_OVERLAP_CANCEL_JOIN_S,
                    message=message,
                )
            except BaseException:
                # Observability cleanup never replaces the delivered signal.
                pass

        _reinforce()
        # Synchronize with the narrow Thread.start publication window without
        # acquiring a lock that a delayed start may hold.  This does not settle
        # the normal late-callback race (B150); it only makes exception cleanup
        # safe when a callback is already publishing a worker.
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log_timeout("overlap worker publication did not settle before exit")
                return
            try:
                if not listen_publication_done.wait(timeout=remaining):
                    _log_timeout(
                        "overlap worker publication did not settle before exit"
                    )
                    return
                break
            except BaseException:
                _reinforce()

        thread = listen_thread
        started = listen_started
        if thread is None or not started:
            return
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log_timeout(
                    "overlap capture daemon did not release before bounded exit"
                )
                return
            try:
                thread.join(timeout=remaining)
                is_alive = getattr(thread, "is_alive", None)
                if not callable(is_alive) or not is_alive():
                    return
            except RuntimeError:
                # Thread.start may have failed before launch, or may have been
                # interrupted after kernel launch but before `_started` became
                # observable.  Wait on our target-entry handshake, bounded by
                # the original cleanup deadline, then retry join if it entered.
                if not listen_worker_entered.wait(timeout=remaining):
                    _log_timeout(
                        "overlap worker launch state did not settle before exit"
                    )
                    return
            except BaseException:
                # Repeated signals reinforce the original cancellation without
                # replacing it or extending the cleanup deadline.
                _reinforce()

    try:
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
            listen_thread.join()
    except BaseException as exc:
        if listen_thread is not None or listen_callback_started.is_set():
            _cancel_overlap_and_wait(exc)
        raise

    if listen_thread is not None:
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

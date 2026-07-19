"""Ask flow: speak-then-listen plus optional confirm turn (same module).

Confirm profile: readback TTS → silence Answer Window (``profile=confirm``) →
:func:`hark.confirm_lexicon.classify_confirm_reply`. R2/R3 always confirm when
risk requires; JSON fields must stay stable for CLI.
"""

from __future__ import annotations

from typing import Any

from hark.audio.playback import TtsPlayTimeout
from hark.config import HarkConfig
from hark.confirm_lexicon import classify_confirm_reply
from hark.exitcodes import ABORT, OK, PROVIDER, TIMEOUT, normalize_failure_exit
from hark.providers.base import ProviderError
from hark.risk import classify_question, confirm_required


def _provider_failure_result(
    exc: ProviderError,
    *,
    tts_info: Any,
    text: str | None = None,
) -> dict[str, Any]:
    result = {
        "ok": False,
        "error": str(exc),
        "exit": normalize_failure_exit(getattr(exc, "code", None), fallback=PROVIDER),
        "tts": tts_info,
    }
    if text is not None:
        result["text"] = text
    return result


def _timeout_failure_result(
    exc: TimeoutError,
    *,
    tts_info: Any,
    text: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Keep ask failures JSON while retaining typed playback diagnostics."""
    result: dict[str, Any] = {
        "ok": False,
        "error": error or str(exc),
        "exit": TIMEOUT,
        "tts": tts_info,
    }
    if text is not None:
        result["text"] = text
    if isinstance(exc, TtsPlayTimeout):
        result["error_type"] = exc.error_type
        result["tts_play_lock"] = exc.as_dict()
    return result


def _run_ask(
    cfg: HarkConfig,
    prompt: str,
    *,
    confirm: str | None = None,
    end_mode: str | None = None,
    provider: str | None = None,
    risk_hint: str | None = None,
) -> dict[str, Any]:
    """Speak prompt (mic muted), then listen ASAP — optional pre-arm before TTS ends.

    Late-binds ``speak_and_listen`` / ``run_tts`` / ``run_listen`` via
    :mod:`hark.speech` so test monkeypatches on ``hark.speech.*`` still apply.
    """
    from hark import speech as speech_mod

    explicit_confirm = confirm is not None
    confirm_mode = confirm if explicit_confirm else cfg.confirm.mode
    try:
        tts_info, listened = speech_mod.speak_and_listen(
            cfg,
            prompt,
            provider=provider,
            end_mode=end_mode,
        )
    except TimeoutError as exc:
        return _timeout_failure_result(
            exc,
            tts_info=getattr(exc, "tts_info", None),
        )
    except ProviderError as exc:
        return _provider_failure_result(
            exc,
            tts_info=getattr(exc, "tts_info", None),
        )

    if listened.cancelled:
        return {
            "ok": False,
            "cancelled": True,
            "text": listened.text,
            "exit": ABORT,
            "end_phrase": listened.end_phrase,
            "tts": tts_info,
        }

    # Meta-command spoken in the answer window: honour it as control, never
    # treat it as an answer or run the confirm flow (B009).
    if listened.meta_command:
        return {
            "ok": True,
            "meta_command": listened.meta_command,
            "text": listened.text,
            "provider": listened.provider,
            "duration_ms": listened.duration_ms,
            "end_mode": listened.end_mode,
            "tts": tts_info,
            "exit": OK,
        }

    risk = risk_hint or classify_question(prompt).risk
    need_confirm = confirm_required(
        risk,
        confirm_mode,
        explicit_override=explicit_confirm,
    )

    if need_confirm:
        # Confirming: readback TTS + silence Answer Window + lexicon (HandoffState.CONFIRMING).
        readback = f"I heard: {listened.text}. Say yes to send, or cancel."
        try:
            speech_mod.run_tts(cfg, readback, provider=provider, play=True)
            conf = speech_mod.run_listen(
                cfg,
                profile="confirm",
                provider=provider,
                end_mode="silence",
                last_tts=readback,
            )
        except TimeoutError as exc:
            return _timeout_failure_result(
                exc,
                error=None if isinstance(exc, TtsPlayTimeout) else "confirm timeout",
                text=listened.text,
                tts_info=tts_info,
            )
        except ProviderError as exc:
            return _provider_failure_result(
                exc,
                text=listened.text,
                tts_info=tts_info,
            )
        except KeyboardInterrupt as exc:
            # The first answer is already durable user context. Preserve it if
            # cancellation arrives during confirmation TTS or capture.
            if getattr(exc, "tts_info", None) is None:
                setattr(exc, "tts_info", tts_info)
            setattr(exc, "answer_text", listened.text)
            raise
        if conf.cancelled:
            return {
                "ok": False,
                "cancelled": True,
                "confirm_reply": conf.text,
                "text": listened.text,
                "end_phrase": conf.end_phrase,
                "exit": ABORT,
                "tts": tts_info,
            }
        decision = classify_confirm_reply(conf.text)
        if decision != "yes":
            return {
                "ok": False,
                "cancelled": True,
                "confirm_reply": conf.text,
                "text": listened.text,
                "end_phrase": conf.end_phrase,
                "exit": ABORT,
                "tts": tts_info,
            }

    return {
        "ok": True,
        "text": listened.text,
        "provider": listened.provider,
        "duration_ms": listened.duration_ms,
        "end_mode": listened.end_mode,
        "end_phrase": listened.end_phrase,
        "risk": risk,
        "tts": tts_info,
        "exit": OK,
    }


def interrupted_ask_result(exc: KeyboardInterrupt) -> dict[str, Any]:
    """Translate an interruption anywhere in the ask signal scope to JSON."""
    signal_name = getattr(exc, "signal_name", None)
    reason = getattr(exc, "reason", None)
    end_phrase = reason or (f"signal:{signal_name}" if signal_name else "interrupt")
    result = {
        "ok": False,
        "cancelled": True,
        "error": "interrupted",
        "text": getattr(exc, "answer_text", ""),
        "end_phrase": end_phrase,
        "signal": signal_name,
        "exit": ABORT,
        "tts": getattr(exc, "tts_info", None),
    }
    if reason is not None:
        result["reason"] = reason
    return result


def run_ask(
    cfg: HarkConfig,
    prompt: str,
    *,
    confirm: str | None = None,
    end_mode: str | None = None,
    provider: str | None = None,
    risk_hint: str | None = None,
) -> dict[str, Any]:
    """Run an ask turn and translate process interruption into cancellation."""
    from hark.audio.capture import capture_interrupt_signals

    try:
        with capture_interrupt_signals():
            return _run_ask(
                cfg,
                prompt,
                confirm=confirm,
                end_mode=end_mode,
                provider=provider,
                risk_hint=risk_hint,
            )
    except KeyboardInterrupt as exc:
        return interrupted_ask_result(exc)

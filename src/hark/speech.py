"""tts / listen / ask orchestration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hark.audio.capture import MicLease, capture_utterance, write_wav_bytes
from hark.audio.playback import play_wav_bytes, write_wav
from hark.config import HarkConfig
from hark.confirm_lexicon import classify_confirm_reply
from hark.exitcodes import ABORT, AUDIO, OK, PROVIDER, TIMEOUT
from hark.listen_end import EndMode, evaluate_radio_transcript, parse_end_mode
from hark.providers.base import ProviderError, Transcript
from hark.providers.resolve import resolve_stt, resolve_tts
from hark.risk import classify_question, confirm_required


@dataclass
class ListenResult:
    text: str
    provider: str
    duration_ms: int
    end_mode: str
    end_phrase: str | None = None
    cancelled: bool = False


def run_tts(
    cfg: HarkConfig,
    text: str,
    *,
    provider: str | None = None,
    voice: str | None = None,
    play: bool = True,
    out: Path | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    limit = max_chars if max_chars is not None else cfg.tts.max_chars
    truncated = False
    if limit and len(text) > limit:
        text = text[:limit]
        truncated = True
    if not text.strip():
        raise ProviderError("empty TTS text")
    tts = resolve_tts(
        provider or cfg.tts.provider,
        voice=voice or cfg.tts.voice,
        language=cfg.tts.language,
    )
    result = tts.synthesize(text, voice=voice or cfg.tts.voice)
    out_path = None
    if out:
        out_path = str(write_wav(out, result.audio))
    if play:
        play_wav_bytes(result.audio)
    return {
        "ok": True,
        "provider": result.provider,
        "voice": result.voice,
        "truncated": truncated,
        "chars": len(text),
        "out": out_path,
        "content_type": result.content_type,
    }


def _echo_overlap(transcript: str, last_tts: str | None) -> bool:
    if not last_tts or not transcript:
        return False
    a = re.sub(r"\W+", " ", transcript.lower()).strip()
    b = re.sub(r"\W+", " ", last_tts.lower()).strip()
    if len(a) < 8 or len(b) < 8:
        return False
    # crude: high containment
    if a in b or b in a:
        return True
    aw, bw = set(a.split()), set(b.split())
    if not aw or not bw:
        return False
    j = len(aw & bw) / max(1, len(aw | bw))
    return j >= 0.7


def run_listen(
    cfg: HarkConfig,
    *,
    provider: str | None = None,
    end_mode: str | None = None,
    max_s: float | None = None,
    last_tts: str | None = None,
    post_tts_guard_s: float | None = None,
) -> ListenResult:
    mode = parse_end_mode(end_mode or cfg.listen.end_mode)
    max_listen = float(max_s if max_s is not None else cfg.listen.max_listen_s)
    guard = (
        post_tts_guard_s
        if post_tts_guard_s is not None
        else cfg.audio.post_tts_guard_ms / 1000.0
    )
    stt = resolve_stt(provider or cfg.stt.provider)
    end_silence = 1.1 if mode is EndMode.SILENCE else 2.5

    with MicLease("listen"):
        if mode is EndMode.SILENCE:
            try:
                cap = capture_utterance(
                    max_s=max_listen,
                    end_silence_s=end_silence,
                    post_tts_guard_s=guard,
                )
            except TimeoutError as exc:
                raise TimeoutError(str(exc)) from exc
            tr = stt.transcribe(cap.wav)
            if _echo_overlap(tr.text, last_tts):
                raise ProviderError("transcript rejected as TTS echo", code=ABORT)
            return ListenResult(
                text=tr.text,
                provider=tr.provider,
                duration_ms=cap.duration_ms,
                end_mode=mode.value,
            )

        # Radio mode: capture segments, re-STT full buffer, accept only on end phrase.
        import time

        pieces: list[bytes] = []
        started = time.monotonic()
        if guard > 0:
            time.sleep(guard)
        while time.monotonic() - started < max_listen:
            remaining = max_listen - (time.monotonic() - started)
            try:
                cap = capture_utterance(
                    max_s=min(remaining, max_listen),
                    end_silence_s=end_silence,
                    initial_timeout_s=min(45.0, remaining),
                    post_tts_guard_s=0,
                )
            except TimeoutError:
                if pieces:
                    # keep waiting for more speech / end phrase
                    continue
                raise
            pieces.append(cap.pcm16)
            wav = write_wav_bytes(b"".join(pieces), cap.sample_rate)
            tr = stt.transcribe(wav)
            if _echo_overlap(tr.text, last_tts):
                pieces.clear()
                continue
            hit = evaluate_radio_transcript(
                tr.text,
                end_phrases=cfg.listen.end_phrases,
                cancel_phrases=cfg.listen.cancel_phrases,
            )
            if hit is None:
                continue
            if hit.kind == "cancel":
                return ListenResult(
                    text=hit.body,
                    provider=tr.provider,
                    duration_ms=int(1000 * (time.monotonic() - started)),
                    end_mode=mode.value,
                    end_phrase=hit.phrase,
                    cancelled=True,
                )
            body = hit.body if cfg.listen.strip_phrase else tr.text
            return ListenResult(
                text=body,
                provider=tr.provider,
                duration_ms=int(1000 * (time.monotonic() - started)),
                end_mode=mode.value,
                end_phrase=hit.phrase,
            )
        raise TimeoutError(f"radio listen exceeded max_listen_s={max_listen}")


def run_ask(
    cfg: HarkConfig,
    prompt: str,
    *,
    confirm: str | None = None,
    end_mode: str | None = None,
    provider: str | None = None,
    risk_hint: str | None = None,
) -> dict[str, Any]:
    confirm_mode = confirm or cfg.confirm.mode
    # Speak prompt
    tts_info = run_tts(cfg, prompt, provider=provider, play=True)
    # Listen
    try:
        listened = run_listen(
            cfg,
            provider=provider,
            end_mode=end_mode,
            last_tts=prompt,
            post_tts_guard_s=cfg.audio.post_tts_guard_ms / 1000.0,
        )
    except TimeoutError as exc:
        return {"ok": False, "error": str(exc), "exit": TIMEOUT}
    except ProviderError as exc:
        return {"ok": False, "error": str(exc), "exit": getattr(exc, "code", PROVIDER)}

    if listened.cancelled:
        return {
            "ok": False,
            "cancelled": True,
            "text": listened.text,
            "exit": ABORT,
            "end_phrase": listened.end_phrase,
        }

    risk = risk_hint or classify_question(prompt).risk
    need_confirm = confirm_required(risk, confirm_mode)
    # also force if confirm=always
    if confirm_mode == "always":
        need_confirm = True
    if confirm_mode == "never" and risk not in ("R2", "R3"):
        need_confirm = False

    if need_confirm:
        readback = f"I heard: {listened.text}. Say yes to send, or cancel."
        run_tts(cfg, readback, provider=provider, play=True)
        try:
            conf = run_listen(
                cfg,
                provider=provider,
                end_mode="silence",
                last_tts=readback,
            )
        except TimeoutError:
            return {"ok": False, "error": "confirm timeout", "exit": TIMEOUT}
        decision = classify_confirm_reply(conf.text)
        if decision != "yes":
            return {
                "ok": False,
                "cancelled": True,
                "confirm_reply": conf.text,
                "text": listened.text,
                "exit": ABORT,
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

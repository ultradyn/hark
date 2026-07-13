"""tts / listen / ask orchestration."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hark.audio.capture import MicLease, capture_utterance, write_wav_bytes
from hark.audio.cues import (
    configure_cues_from_config,
    lookup_cached_tts,
    play_record_start,
    play_record_stop,
    store_cached_tts,
)
from hark.audio.mic_mute import mic_muted_during_tts
from hark.audio.playback import play_wav_bytes, write_wav
from hark.config import HarkConfig
from hark.confirm_lexicon import classify_confirm_reply
from hark.exitcodes import ABORT, OK, PROVIDER, TIMEOUT
from hark.lifecycle import BusySection
from hark.listen_end import EndMode, evaluate_radio_transcript, parse_end_mode
from hark.partial import make_partial_event, new_stream_id
from hark.providers.base import ProviderError
from hark.providers.resolve import resolve_stt, resolve_tts
from hark.risk import classify_question, confirm_required
from hark.syslog import log as syslog
from hark.usage import UsageStore


@dataclass
class ListenResult:
    text: str
    provider: str
    duration_ms: int
    end_mode: str
    end_phrase: str | None = None
    cancelled: bool = False
    stream_id: str | None = None
    partials_emitted: int = 0


def run_tts(
    cfg: HarkConfig,
    text: str,
    *,
    provider: str | None = None,
    voice: str | None = None,
    play: bool = True,
    out: Path | None = None,
    max_chars: int | None = None,
    mute_mic: bool | None = None,
    on_near_end: Any | None = None,
    near_end_ms: int | None = None,
) -> dict[str, Any]:
    limit = max_chars if max_chars is not None else cfg.tts.max_chars
    truncated = False
    if limit and len(text) > limit:
        text = text[:limit]
        truncated = True
    if not text.strip():
        raise ProviderError("empty TTS text")

    do_mute = cfg.audio.mute_mic_during_tts if mute_mic is None else mute_mic
    store = UsageStore()
    t0 = time.monotonic()
    voice_id = voice or cfg.tts.voice or "eve"
    cached = lookup_cached_tts(voice_id, text)
    from_cache = False
    provider_name = provider or cfg.tts.provider
    content_type = "audio/mpeg"
    audio_bytes: bytes

    if cached is not None:
        audio_bytes = cached
        from_cache = True
        provider_name = "cache"
        latency_ms = int(1000 * (time.monotonic() - t0))
        used_voice = voice_id
    else:
        tts = resolve_tts(
            provider or cfg.tts.provider,
            voice=voice_id,
            language=cfg.tts.language,
        )
        try:
            result = tts.synthesize(text, voice=voice_id)
        except Exception as exc:
            store.record_tts(
                text=text,
                provider=provider or cfg.tts.provider,
                voice=voice_id,
                ok=False,
                error=str(exc)[:200],
                latency_ms=int(1000 * (time.monotonic() - t0)),
            )
            raise
        audio_bytes = result.audio
        provider_name = result.provider
        content_type = result.content_type
        used_voice = result.voice or voice_id
        latency_ms = int(1000 * (time.monotonic() - t0))
        # Persist common-ish short phrases for reuse
        if len(text) <= 120:
            try:
                store_cached_tts(used_voice, text, audio_bytes)
            except Exception:
                pass

    out_path = None
    if out:
        out_path = str(write_wav(out, audio_bytes))

    play_ms = 0
    mute_applied = False
    if play:
        near = (
            near_end_ms
            if near_end_ms is not None
            else int(cfg.audio.listen_pre_arm_ms)
        )
        with mic_muted_during_tts(enabled=do_mute) as mute_state:
            mute_applied = mute_state.applied
            pr = play_wav_bytes(
                audio_bytes,
                on_near_end=on_near_end,
                near_end_ms=near if on_near_end else 0,
            )
            play_ms = pr.duration_ms

    store.record_tts(
        text=text,
        provider=provider_name,
        voice=used_voice,
        audio_ms=play_ms,
        latency_ms=latency_ms,
        ok=True,
        meta={"from_cache": from_cache},
    )
    return {
        "ok": True,
        "provider": provider_name,
        "voice": used_voice,
        "truncated": truncated,
        "chars": len(text),
        "words": len(text.split()),
        "out": out_path,
        "content_type": content_type,
        "audio_ms": play_ms,
        "latency_ms": latency_ms,
        "mic_muted": mute_applied,
        "from_cache": from_cache,
    }


def _echo_overlap(transcript: str, last_tts: str | None) -> bool:
    if not last_tts or not transcript:
        return False
    a = re.sub(r"\W+", " ", transcript.lower()).strip()
    b = re.sub(r"\W+", " ", last_tts.lower()).strip()
    if len(a) < 8 or len(b) < 8:
        return False
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
    already_armed: bool = False,
    on_partial: Any | None = None,
    stream_id: str | None = None,
    partial_kind: str = "ambient.partial",
) -> ListenResult:
    """Capture speech. Radio mode streams partials via on_partial when enabled.

    on_partial(event_dict) is called for each non-final radio transcript so Mode A
    agents can start thinking early. Events always set partial=true and HOLD warnings.
    """
    mode = parse_end_mode(end_mode or cfg.listen.end_mode)
    max_listen = float(max_s if max_s is not None else cfg.listen.max_listen_s)
    if already_armed:
        guard = 0.0
    elif post_tts_guard_s is not None:
        guard = post_tts_guard_s
    else:
        # Tight handoff: config post_tts_guard_ms (default 100–150)
        guard = max(0.0, cfg.audio.post_tts_guard_ms / 1000.0)

    stt = resolve_stt(provider or cfg.stt.provider)
    end_silence = (
        float(cfg.listen.end_silence_s)
        if mode is EndMode.SILENCE
        else float(cfg.listen.radio_end_silence_s)
    )
    store = UsageStore()
    configure_cues_from_config(cfg)
    stream = stream_id or new_stream_id()
    # Partials only meaningful when waiting for an end phrase
    stream_partials = mode is EndMode.RADIO and getattr(
        cfg.listen, "stream_partials", True
    )

    with MicLease("listen"), BusySection("listen"):
        if mode is EndMode.SILENCE:
            if guard > 0:
                time.sleep(guard)
            play_record_start()
            try:
                cap = capture_utterance(
                    max_s=max_listen,
                    end_silence_s=end_silence,
                    post_tts_guard_s=0,
                )
            except TimeoutError as exc:
                play_record_stop()
                store.record_stt(
                    text="",
                    provider=getattr(stt, "name", None),
                    ok=False,
                    error=str(exc)[:200],
                )
                raise
            play_record_stop()
            t_api = time.monotonic()
            tr = stt.transcribe(cap.wav)
            latency_ms = int(1000 * (time.monotonic() - t_api))
            if not (tr.text or "").strip():
                store.record_stt(
                    text="",
                    provider=tr.provider,
                    audio_ms=cap.duration_ms,
                    latency_ms=latency_ms,
                    ok=False,
                    error="empty transcript",
                )
                raise TimeoutError(
                    "heard audio but STT returned empty text "
                    "(try speaking clearer, or check mic device)"
                )
            if _echo_overlap(tr.text, last_tts):
                store.record_stt(
                    text=tr.text,
                    provider=tr.provider,
                    audio_ms=cap.duration_ms,
                    latency_ms=latency_ms,
                    ok=False,
                    error="echo",
                )
                raise ProviderError("transcript rejected as TTS echo", code=ABORT)
            store.record_stt(
                text=tr.text,
                provider=tr.provider,
                audio_ms=cap.duration_ms,
                latency_ms=latency_ms,
                ok=True,
            )
            return ListenResult(
                text=tr.text,
                provider=tr.provider,
                duration_ms=cap.duration_ms,
                end_mode=mode.value,
                stream_id=stream,
            )

        # Radio mode — segment until end phrase; stream partials between segments
        pieces: list[bytes] = []
        started = time.monotonic()
        partial_seq = 0
        last_partial_text = ""
        if guard > 0:
            time.sleep(guard)
        play_record_start()
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
                    continue
                play_record_stop()
                store.record_stt(
                    text="",
                    provider=getattr(stt, "name", None),
                    ok=False,
                    error="timeout",
                )
                raise
            pieces.append(cap.pcm16)
            wav = write_wav_bytes(b"".join(pieces), cap.sample_rate)
            t_api = time.monotonic()
            tr = stt.transcribe(wav)
            latency_ms = int(1000 * (time.monotonic() - t_api))
            if _echo_overlap(tr.text, last_tts):
                pieces.clear()
                continue
            hit = evaluate_radio_transcript(
                tr.text,
                end_phrases=cfg.listen.end_phrases,
                cancel_phrases=cfg.listen.cancel_phrases,
            )
            if hit is None:
                # Interim: emit partial if text grew
                body_so_far = (tr.text or "").strip()
                if (
                    stream_partials
                    and body_so_far
                    and body_so_far != last_partial_text
                    and on_partial is not None
                ):
                    partial_seq += 1
                    last_partial_text = body_so_far
                    ev = make_partial_event(
                        stream_id=stream,
                        seq=partial_seq,
                        text=body_so_far,
                        kind=partial_kind,
                        provider=tr.provider,
                    )
                    try:
                        on_partial(ev)
                    except Exception:
                        pass
                    syslog(
                        "listen.partial",
                        component="stt",
                        level="info",
                        stream_id=stream,
                        seq=partial_seq,
                        text=body_so_far[:300],
                        provider=tr.provider,
                        partial=True,
                        final=False,
                    )
                continue
            play_record_stop()
            body = hit.body if cfg.listen.strip_phrase else tr.text
            store.record_stt(
                text=body,
                provider=tr.provider,
                audio_ms=int(1000 * (time.monotonic() - started)),
                latency_ms=latency_ms,
                ok=hit.kind != "cancel",
                error="cancel" if hit.kind == "cancel" else None,
            )
            if hit.kind == "cancel":
                return ListenResult(
                    text=hit.body,
                    provider=tr.provider,
                    duration_ms=int(1000 * (time.monotonic() - started)),
                    end_mode=mode.value,
                    end_phrase=hit.phrase,
                    cancelled=True,
                    stream_id=stream,
                    partials_emitted=partial_seq,
                )
            return ListenResult(
                text=body,
                provider=tr.provider,
                duration_ms=int(1000 * (time.monotonic() - started)),
                end_mode=mode.value,
                end_phrase=hit.phrase,
                stream_id=stream,
                partials_emitted=partial_seq,
            )
        play_record_stop()
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
    """Speak prompt (mic muted), then listen ASAP — optional pre-arm before TTS ends."""
    confirm_mode = confirm or cfg.confirm.mode
    pre_arm_ms = int(cfg.audio.listen_pre_arm_ms)
    arm_event = threading.Event()

    def _on_near_end() -> None:
        # Signal that TTS is nearly done — listen starts immediately after play returns
        # with already_armed / zero guard. True overlap capture needs a second thread;
        # we keep half-duplex: unmute happens when play exits mute context, then listen.
        arm_event.set()

    tts_info = run_tts(
        cfg,
        prompt,
        provider=provider,
        play=True,
        mute_mic=cfg.audio.mute_mic_during_tts,
        on_near_end=_on_near_end if pre_arm_ms > 0 else None,
        near_end_ms=pre_arm_ms if pre_arm_ms > 0 else 0,
    )
    # Mic unmuted as TTS context exits. Start listen with minimal guard.
    try:
        listened = run_listen(
            cfg,
            provider=provider,
            end_mode=end_mode,
            last_tts=prompt,
            post_tts_guard_s=cfg.audio.post_tts_guard_ms / 1000.0,
            already_armed=arm_event.is_set(),
        )
    except TimeoutError as exc:
        return {"ok": False, "error": str(exc), "exit": TIMEOUT, "tts": tts_info}
    except ProviderError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "exit": getattr(exc, "code", PROVIDER),
            "tts": tts_info,
        }

    if listened.cancelled:
        return {
            "ok": False,
            "cancelled": True,
            "text": listened.text,
            "exit": ABORT,
            "end_phrase": listened.end_phrase,
            "tts": tts_info,
        }

    risk = risk_hint or classify_question(prompt).risk
    need_confirm = confirm_required(risk, confirm_mode)
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
            return {
                "ok": False,
                "error": "confirm timeout",
                "exit": TIMEOUT,
                "text": listened.text,
                "tts": tts_info,
            }
        decision = classify_confirm_reply(conf.text)
        if decision != "yes":
            return {
                "ok": False,
                "cancelled": True,
                "confirm_reply": conf.text,
                "text": listened.text,
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

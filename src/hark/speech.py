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
from hark.audio.media import duck_media
from hark.audio.mic_mute import mic_muted_during_tts
from hark.audio.playback import play_wav_bytes, write_wav
from hark.config import HarkConfig
from hark.confirm_lexicon import classify_confirm_reply
from hark.exitcodes import ABORT, OK, PROVIDER, TIMEOUT
from hark.lifecycle import BusySection
from hark.listen_control import (
    clear_active_listen,
    consume_listen_action,
    poll_listen_action,
    register_active_listen,
)
from hark.endpointing import EndpointStrategy, build_endpoint_strategy
from hark.listen_end import EndMode, evaluate_radio_transcript, parse_end_mode
from hark.mic_coord import pause_ambient_for_mic
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
    meta_command: str | None = None


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
    conference_policy: str | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Synthesize and optionally play TTS.

    ``conference_policy`` (B017):
      - ``None`` / default: ``hold`` when ``audio.hold_during_conference``, else ``force``
      - ``hold``: wait for Zoom/Teams/Meet etc. to end (soft chime optional)
      - ``skip``: do not speak while conference active (lifecycle cues)
      - ``force``: always speak immediately

    ``use_cache``: when False, skip on-disk TTS phrase cache lookup and store
    (one-shot announces such as wake-label live-reload).
    """
    limit = max_chars if max_chars is not None else cfg.tts.max_chars
    truncated = False
    if limit and len(text) > limit:
        text = text[:limit]
        truncated = True
    if not text.strip():
        raise ProviderError("empty TTS text")

    # Mode A path (hark tts / ask): hold full question speech during conference.
    hold_meta: dict[str, Any] | None = None
    if play:
        from hark.conference import apply_conference_hold

        policy = conference_policy
        if policy is None:
            policy = "hold" if cfg.audio.hold_during_conference else "force"
        hold = apply_conference_hold(cfg, text, policy=policy)
        hold_meta = hold.as_meta()
        if hold.skipped:
            return {
                "ok": True,
                "provider": "skipped",
                "voice": voice or cfg.tts.voice or "eve",
                "truncated": truncated,
                "chars": len(text),
                "words": len(text.split()),
                "out": None,
                "content_type": None,
                "audio_ms": 0,
                "latency_ms": 0,
                "mic_muted": False,
                "from_cache": False,
                "conference": hold_meta,
                "skipped": True,
                "reason": "conference",
            }

    do_mute = cfg.audio.mute_mic_during_tts if mute_mic is None else mute_mic
    store = UsageStore()
    t0 = time.monotonic()
    voice_id = voice or cfg.tts.voice or "eve"
    cached = lookup_cached_tts(voice_id, text) if use_cache else None
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
        # Persist common-ish short phrases for reuse (skip one-shot announces)
        if use_cache and len(text) <= 120:
            try:
                store_cached_tts(used_voice, text, audio_bytes)
            except Exception:
                pass

    out_path = None
    if out:
        out_path = str(write_wav(out, audio_bytes))

    play_ms = 0
    mute_applied = False
    duck_meta: dict[str, Any] | None = None
    if play:
        near = (
            near_end_ms
            if near_end_ms is not None
            else int(cfg.audio.listen_pre_arm_ms)
        )
        do_duck = bool(getattr(cfg.audio, "duck_media_during_tts", True))
        # Mic mute and media duck are independent; duck before play so media
        # is quiet before TTS starts. Conference skip already returned above —
        # when speaking after hold frees, duck as normal (exclude_conference).
        with mic_muted_during_tts(enabled=do_mute) as mute_state:
            mute_applied = mute_state.applied
            with duck_media(cfg, enabled=do_duck, exclude_conference=True) as duck_state:
                duck_meta = duck_state.as_meta()
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
        meta={
            # dashboard TTS audit trail (B067): what was actually spoken
            "text_preview": text[:160],
            "from_cache": from_cache,
            "conference": hold_meta,
            "media_duck": duck_meta,
        },
    )
    result: dict[str, Any] = {
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
        "media_ducked": bool(duck_meta.get("media_ducked")) if duck_meta else False,
    }
    if hold_meta is not None:
        result["conference"] = hold_meta
    if duck_meta is not None:
        result["media_duck"] = duck_meta
    return result


EMPTY_STT_NUDGE_TEXT = "Sorry, I didn't catch that."
# Gate never opened (B031) — general listen; ambient post-wake may override
NO_OPEN_NUDGE_TEXT = "I didn't hear anything. Please speak after the beep."


def _is_no_open_timeout(exc: BaseException) -> bool:
    """True when energy gate never opened (vs empty STT after open)."""
    msg = str(exc).lower()
    return "no speech detected" in msg or "no speech captured" in msg


def _log_no_open(
    *,
    peak_rms: float | None = None,
    peak_db: float | None = None,
    open_thresh: float | None = None,
    after_tts: bool,
    attempt: int,
    stream_id: str | None,
    phase: str,
    error: str,
    abs_open_db: float | None = None,
) -> None:
    """Structured metric when capture times out before speech opens."""
    # Best-effort parse peak/open from TimeoutError message
    if peak_db is None:
        m = re.search(r"peak_db=(-?[\d.]+)", error)
        if m:
            try:
                peak_db = float(m.group(1))
            except ValueError:
                pass
    if peak_rms is None:
        m = re.search(r"peak_rms=(-?[\d.]+)", error)
        if m:
            try:
                peak_rms = float(m.group(1))
            except ValueError:
                pass
    if open_thresh is None:
        m = re.search(r"open_thresh≈(-?[\d.]+)", error)
        if m:
            try:
                open_thresh = float(m.group(1))
            except ValueError:
                pass
    syslog(
        "speech.no_open",
        component="stt",
        level="warn",
        message="energy gate never opened",
        peak_db=round(peak_db, 2) if peak_db is not None else None,
        rms=round(peak_rms, 6) if peak_rms is not None else None,
        open_thresh=round(open_thresh, 2) if open_thresh is not None else None,
        abs_open_db=abs_open_db,
        after_tts=after_tts,
        attempt=attempt,
        stream_id=stream_id,
        phase=phase,
        error=error[:240],
    )



def _tag_meta_command(result: "ListenResult") -> "ListenResult":
    """Classify a captured (non-cancelled) transcript as a meta-command (B009).

    Meta-commands (repeat/skip/next/status/cancel) spoken during an answer window
    must be honoured, not delivered to the worker agent as a prompt.
    """
    from hark.meta_commands import classify_meta_command

    if not result.cancelled:
        result.meta_command = classify_meta_command(result.text)
    return result


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


def _log_empty_stt(
    *,
    duration_ms: int,
    peak_rms: float | None,
    peak_db: float | None,
    wait_speech_ms: int,
    after_tts: bool,
    attempt: int,
    provider: str | None,
    stream_id: str | None,
    phase: str,
) -> None:
    """Structured metric for empty STT rate / residual-TTS diagnosis."""
    syslog(
        "speech.empty_stt",
        component="stt",
        level="warn",
        message="STT returned empty transcript",
        duration_ms=duration_ms,
        audio_ms=duration_ms,
        rms=round(peak_rms, 6) if peak_rms is not None else None,
        peak_db=round(peak_db, 2) if peak_db is not None else None,
        wait_speech_ms=wait_speech_ms,
        after_tts=after_tts,
        attempt=attempt,
        provider=provider,
        stream_id=stream_id,
        phase=phase,
    )



def _estimate_wav_audio_ms(wav_bytes: bytes, *, sample_rate: int = 16000) -> int:
    """Best-effort audio duration from a mono 16-bit PCM WAV payload."""
    n = len(wav_bytes or b"")
    if n <= 44 or sample_rate <= 0:
        return 0
    # Standard PCM WAV header is 44 bytes; 2 bytes/sample mono.
    pcm = max(0, n - 44)
    return int(1000 * pcm / (2 * sample_rate))


def _transcribe_logged(
    stt: Any,
    wav_bytes: bytes,
    *,
    stream_id: str | None,
    seq: int,
    mode: str,
    purpose: str = "listen",
    audio_ms: int | None = None,
    sample_rate: int = 16000,
) -> tuple[Any, int]:
    """Call cloud STT and emit stt.request / stt.response on system.jsonl (B038).

    Every upload is logged — including radio interim segments that never hit
    UsageStore.record_stt — so operators can see partial cadence and failures.
    ``seq`` is the 1-based STT call index within the listen stream (correlate
    with ``listen.partial`` / ambient.partial via stream_id + stt_seq).
    Returns ``(Transcript, latency_ms)``.
    """
    provider = getattr(stt, "name", None) or "unknown"
    nbytes = len(wav_bytes or b"")
    if audio_ms is None:
        audio_ms = _estimate_wav_audio_ms(wav_bytes, sample_rate=sample_rate)
    syslog(
        "stt.request",
        component="stt",
        level="info",
        message="STT upload",
        stream_id=stream_id,
        seq=seq,
        provider=provider,
        bytes=nbytes,
        audio_ms=int(audio_ms or 0),
        mode=mode,
        purpose=purpose,
    )
    t0 = time.monotonic()
    try:
        tr = stt.transcribe(wav_bytes)
        latency_ms = int(1000 * (time.monotonic() - t0))
        text = (getattr(tr, "text", None) or "").strip()
        prov = getattr(tr, "provider", None) or provider
        syslog(
            "stt.response",
            component="stt",
            level="info",
            message="STT ok" if text else "STT empty",
            stream_id=stream_id,
            seq=seq,
            provider=prov,
            latency_ms=latency_ms,
            ok=True,
            bytes=nbytes,
            audio_ms=int(audio_ms or 0),
            chars=len(text),
            empty=not bool(text),
            mode=mode,
            purpose=purpose,
            text=text[:200] if text else "",
        )
        return tr, latency_ms
    except Exception as exc:
        latency_ms = int(1000 * (time.monotonic() - t0))
        syslog(
            "stt.response",
            component="stt",
            level="error",
            message=str(exc)[:200] or "STT failed",
            stream_id=stream_id,
            seq=seq,
            provider=provider,
            latency_ms=latency_ms,
            ok=False,
            error=str(exc)[:300],
            bytes=nbytes,
            audio_ms=int(audio_ms or 0),
            mode=mode,
            purpose=purpose,
        )
        raise


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
    discard_leading_ms: int = 0,
    audio_ok_after: Any | None = None,
    # B031: energy-gate / post-wake overrides (None = config defaults)
    abs_open_db: float | None = None,
    open_margin_db: float | None = None,
    initial_timeout_s: float | None = None,
    lead_in_ms: int = 0,
    arm_cue: bool = False,
    no_open_retry: bool | None = None,
    no_open_nudge: bool | None = None,
    no_open_nudge_text: str | None = None,
) -> ListenResult:
    """Capture speech. Radio mode streams partials via on_partial when enabled.

    on_partial(event_dict) is called for each non-final radio transcript so Mode A
    agents can start thinking early. Events always set partial=true and HOLD warnings.

    Empty STT recovery (silence mode): log ``speech.empty_stt``, optionally
    re-listen once (``empty_stt_retry``), then TTS nudge + re-listen
    (``empty_stt_nudge``) before failing.

    No-open recovery (silence mode, B031): when the energy gate never opens
    (``no speech detected``), log ``speech.no_open``, optionally re-listen
    (``no_open_retry``), then TTS nudge + re-listen (``no_open_nudge``).

    Overlap pre-arm: pass ``audio_ok_after`` (callable → monotonic deadline or None)
    and/or ``discard_leading_ms`` so TTS tail / residual echo is dropped before the
    energy gate runs.

    Post-wake / soft gate: ``abs_open_db``, ``open_margin_db``, ``initial_timeout_s``
    override ``[listen]`` defaults. ``lead_in_ms`` settles before the first capture;
    ``arm_cue`` plays record-start when listen arms (not only when speech opens).
    ``no_open_nudge_text`` overrides the default no-open TTS line.
    """
    mode = parse_end_mode(end_mode or cfg.listen.end_mode)
    max_listen = float(max_s if max_s is not None else cfg.listen.max_listen_s)
    # Explicit post_tts_guard always wins. Pre-arm (already_armed) used to zero
    # the guard and race mute-unmute / residual TTS into the energy gate.
    if post_tts_guard_s is not None:
        guard = max(0.0, post_tts_guard_s)
    elif already_armed:
        # No explicit guard: still settle briefly for mute unmute / echo residual
        guard = max(0.0, cfg.audio.post_tts_guard_ms / 1000.0)
    else:
        guard = max(0.0, cfg.audio.post_tts_guard_ms / 1000.0)
    after_tts = last_tts is not None

    gate_abs_open = float(
        abs_open_db
        if abs_open_db is not None
        else getattr(cfg.listen, "abs_open_db", -48.0)
    )
    gate_open_margin = float(
        open_margin_db
        if open_margin_db is not None
        else getattr(cfg.listen, "open_margin_db", 8.0)
    )
    gate_timeout_s = float(
        initial_timeout_s
        if initial_timeout_s is not None
        else getattr(cfg.listen, "initial_timeout_s", 45.0)
    )
    nudge_no_open_text = (
        no_open_nudge_text
        if no_open_nudge_text is not None
        else NO_OPEN_NUDGE_TEXT
    )
    allow_no_open_retry = (
        bool(getattr(cfg.listen, "no_open_retry", True))
        if no_open_retry is None
        else bool(no_open_retry)
    )
    allow_no_open_nudge = (
        bool(getattr(cfg.listen, "no_open_nudge", True))
        if no_open_nudge is None
        else bool(no_open_nudge)
    )

    stt = resolve_stt(provider or cfg.stt.provider)
    # Silence mode: end_silence_s finalizes the answer window.
    # Radio mode: radio_partial_silence_s only cuts a segment for interim STT /
    # ambient.partial (B037). The turn still finalizes only on end phrase,
    # soft end (if enabled), agent listen-end, cancel, or max_listen_s.
    end_silence = (
        float(cfg.listen.end_silence_s)
        if mode is EndMode.SILENCE
        else float(getattr(cfg.listen, "radio_partial_silence_s", 0.6))
    )
    # Pluggable endpointing (B007): only for silence mode. Falls back to the
    # energy gate (strategy=None) if the smart detector can't load.
    endpoint_strategy: EndpointStrategy | None = None
    if mode is EndMode.SILENCE and str(
        getattr(cfg.listen, "endpoint_strategy", "energy")
    ).strip().lower() not in ("energy", "energy_gate", "gate", "off", "none", ""):
        endpoint_strategy = build_endpoint_strategy(
            strategy_name=cfg.listen.endpoint_strategy,
            smart_turn_model_path=cfg.listen.smart_turn_model_path,
            smart_turn_threshold=cfg.listen.smart_turn_threshold,
            on_warn=lambda msg: syslog(
                "listen.endpoint_fallback",
                component="stt",
                level="warn",
                message=msg,
            ),
        )
        if endpoint_strategy is not None:
            syslog(
                "listen.endpoint_strategy",
                component="stt",
                level="info",
                strategy=getattr(endpoint_strategy, "name", "?"),
            )

    def _endpoint_event(event: str, fields: dict) -> None:
        syslog(event, component="stt", level="debug", stream_id=stream, **fields)
    store = UsageStore()
    configure_cues_from_config(cfg)
    stream = stream_id or new_stream_id()
    # 1-based STT upload counter for this listen stream (B038 system.jsonl)
    stt_seq = 0
    # Partials only meaningful when waiting for an end phrase
    stream_partials = mode is EndMode.RADIO and getattr(
        cfg.listen, "stream_partials", True
    )
    recording_cued = False

    def _cue_start_once() -> None:
        """Play record-start only when speech opens (not during leading silence)."""
        nonlocal recording_cued
        if not recording_cued:
            recording_cued = True
            play_record_start()
            syslog(
                "listen.speech_opened",
                component="stt",
                level="info",
                stream_id=stream,
                mode=mode.value,
            )

    def _arm_cue_if_requested() -> None:
        """Early arm cue (answer window / post-wake): beep when listen is ready.

        Sets ``recording_cued`` so speech-open paths do not double-beep.
        """
        nonlocal recording_cued
        if arm_cue and not recording_cued:
            recording_cued = True
            play_record_start()
            syslog(
                "listen.armed_cue",
                component="stt",
                level="info",
                stream_id=stream,
                mode=mode.value,
            )

    def _agent_wants_stop(_pcm: bytes, _elapsed: float) -> bool:
        return poll_listen_action(stream) is not None

    # Duck/pause non-Hark media for the full answer-window capture (B046 / I002).
    # Explicit STT flags — do not inherit TTS defaults (pause_media_during_tts=false).
    # Idle ambient wake (local Vosk) never enters run_listen, so continuous wake
    # scanning does not duck/pause media.
    do_duck_stt = bool(getattr(cfg.audio, "duck_media_during_stt", True))
    do_pause_stt = bool(getattr(cfg.audio, "pause_media_during_stt", True))

    # Pause ambient wake scanning so we get the mic (dogfood B010)
    with (
        pause_ambient_for_mic(reason="listen"),
        MicLease("listen"),
        BusySection("listen"),
        duck_media(
            cfg,
            enabled=do_duck_stt,
            pause_players=do_pause_stt,
            exclude_conference=True,
        ),
    ):
        register_active_listen(stream, mode=mode.value)
        try:
            if mode is EndMode.SILENCE:
                # attempt: 0 = first, 1 = empty/no-open retry, 2 = after nudge
                attempt = 0
                did_retry = False
                did_nudge = False
                did_no_open_retry = False
                did_no_open_nudge = False
                settle = guard
                if lead_in_ms > 0:
                    time.sleep(max(0.0, lead_in_ms / 1000.0))

                while True:
                    if settle > 0:
                        time.sleep(settle)
                    # After first attempt, only short re-arm settle (mute/echo)
                    settle = max(0.05, min(0.2, guard if guard > 0 else 0.1))
                    # Fresh cue state each attempt; optional early arm for post-wake
                    recording_cued = False
                    if arm_cue:
                        _arm_cue_if_requested()
                    try:
                        # Overlap discard only on first attempt after TTS
                        lead_discard = discard_leading_ms if attempt == 0 else 0
                        lead_ok = audio_ok_after if attempt == 0 else None
                        # Skip double beep when we already armed; still log speech open
                        on_open = (
                            (lambda: syslog(
                                "listen.speech_opened",
                                component="stt",
                                level="info",
                                stream_id=stream,
                                mode=mode.value,
                            ))
                            if arm_cue
                            else _cue_start_once
                        )
                        cap = capture_utterance(
                            max_s=max_listen,
                            end_silence_s=end_silence,
                            post_tts_guard_s=0,
                            on_opened=on_open,
                            should_stop=_agent_wants_stop,
                            discard_leading_ms=lead_discard,
                            audio_ok_after=lead_ok,
                            endpoint_strategy=endpoint_strategy,
                            endpoint_probe_silence_s=cfg.listen.endpoint_probe_silence_s,
                            endpoint_max_silence_s=cfg.listen.endpoint_max_silence_s,
                            on_endpoint_event=_endpoint_event,
                            abs_open_db=gate_abs_open,
                            open_margin_db=gate_open_margin,
                            initial_timeout_s=gate_timeout_s,
                        )
                    except TimeoutError as exc:
                        if recording_cued:
                            play_record_stop()
                        err_s = str(exc)
                        store.record_stt(
                            text="",
                            provider=getattr(stt, "name", None),
                            ok=False,
                            error=err_s[:200],
                        )
                        if _is_no_open_timeout(exc):
                            phase = (
                                "nudge"
                                if did_no_open_nudge
                                else ("retry" if did_no_open_retry else "initial")
                            )
                            _log_no_open(
                                after_tts=after_tts,
                                attempt=attempt,
                                stream_id=stream,
                                phase=phase,
                                error=err_s,
                                abs_open_db=gate_abs_open,
                            )
                            if allow_no_open_retry and not did_no_open_retry:
                                did_no_open_retry = True
                                attempt = max(attempt, 1)
                                syslog(
                                    "speech.no_open_retry",
                                    component="stt",
                                    level="info",
                                    after_tts=after_tts,
                                    stream_id=stream,
                                    abs_open_db=gate_abs_open,
                                )
                                settle = max(0.05, min(0.2, guard if guard > 0 else 0.1))
                                continue
                            if allow_no_open_nudge and not did_no_open_nudge:
                                did_no_open_nudge = True
                                attempt = 2
                                syslog(
                                    "speech.no_open_nudge",
                                    component="stt",
                                    level="info",
                                    after_tts=after_tts,
                                    stream_id=stream,
                                    text=nudge_no_open_text,
                                )
                                try:
                                    run_tts(
                                        cfg,
                                        nudge_no_open_text,
                                        provider=provider,
                                        play=True,
                                        mute_mic=cfg.audio.mute_mic_during_tts,
                                    )
                                except Exception as nudge_exc:
                                    syslog(
                                        "speech.no_open_nudge_failed",
                                        component="stt",
                                        level="warn",
                                        error=str(nudge_exc)[:200],
                                        stream_id=stream,
                                    )
                                settle = max(0.1, cfg.audio.post_tts_guard_ms / 1000.0)
                                continue
                        raise
                    agent_act = consume_listen_action(stream)
                    if recording_cued:
                        play_record_stop()
                    if agent_act == "cancel":
                        store.record_stt(
                            text="",
                            provider=getattr(stt, "name", None),
                            audio_ms=cap.duration_ms,
                            ok=False,
                            error="agent_cancel",
                        )
                        return ListenResult(
                            text="",
                            provider=getattr(stt, "name", "unknown"),
                            duration_ms=cap.duration_ms,
                            end_mode=mode.value,
                            end_phrase="agent:cancel",
                            cancelled=True,
                            stream_id=stream,
                        )
                    stt_seq += 1
                    tr, latency_ms = _transcribe_logged(
                        stt,
                        cap.wav,
                        stream_id=stream,
                        seq=stt_seq,
                        mode=mode.value,
                        purpose="silence",
                        audio_ms=cap.duration_ms,
                        sample_rate=cap.sample_rate,
                    )
                    if not (tr.text or "").strip():
                        phase = (
                            "nudge"
                            if did_nudge
                            else ("retry" if did_retry else "initial")
                        )
                        _log_empty_stt(
                            duration_ms=cap.duration_ms,
                            peak_rms=getattr(cap, "peak_rms", None),
                            peak_db=getattr(cap, "peak_db", None),
                            wait_speech_ms=cap.wait_speech_ms,
                            after_tts=after_tts,
                            attempt=attempt,
                            provider=tr.provider,
                            stream_id=stream,
                            phase=phase,
                        )
                        store.record_stt(
                            text="",
                            provider=tr.provider,
                            audio_ms=cap.duration_ms,
                            latency_ms=latency_ms,
                            ok=False,
                            error="empty transcript",
                        )
                        # One automatic re-listen (residual TTS / mute race)
                        if cfg.listen.empty_stt_retry and not did_retry:
                            did_retry = True
                            attempt = 1
                            syslog(
                                "speech.empty_stt_retry",
                                component="stt",
                                level="info",
                                after_tts=after_tts,
                                stream_id=stream,
                                duration_ms=cap.duration_ms,
                            )
                            continue
                        # Operator nudge + one more listen
                        if cfg.listen.empty_stt_nudge and not did_nudge:
                            did_nudge = True
                            attempt = 2
                            syslog(
                                "speech.empty_stt_nudge",
                                component="stt",
                                level="info",
                                after_tts=after_tts,
                                stream_id=stream,
                                text=EMPTY_STT_NUDGE_TEXT,
                            )
                            try:
                                run_tts(
                                    cfg,
                                    EMPTY_STT_NUDGE_TEXT,
                                    provider=provider,
                                    play=True,
                                    mute_mic=cfg.audio.mute_mic_during_tts,
                                )
                            except Exception as nudge_exc:
                                syslog(
                                    "speech.empty_stt_nudge_failed",
                                    component="stt",
                                    level="warn",
                                    error=str(nudge_exc)[:200],
                                    stream_id=stream,
                                )
                            settle = max(0.1, cfg.audio.post_tts_guard_ms / 1000.0)
                            continue
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
                        raise ProviderError(
                            "transcript rejected as TTS echo", code=ABORT
                        )
                    store.record_stt(
                        text=tr.text,
                        provider=tr.provider,
                        audio_ms=cap.duration_ms,
                        latency_ms=latency_ms,
                        ok=True,
                    )
                    if cap.wait_speech_ms or agent_act or attempt:
                        syslog(
                            "listen.ok",
                            component="stt",
                            level="info",
                            wait_speech_ms=cap.wait_speech_ms,
                            agent_end=agent_act,
                            stream_id=stream,
                            empty_stt_attempts=attempt,
                            after_tts=after_tts,
                        )
                    return ListenResult(
                        text=tr.text,
                        provider=tr.provider,
                        duration_ms=cap.duration_ms,
                        end_mode=mode.value,
                        end_phrase="agent:finish" if agent_act == "finish" else None,
                        stream_id=stream,
                    )

            # Radio mode — segment until end phrase / agent finish; stream partials
            pieces: list[bytes] = []
            started = time.monotonic()
            partial_seq = 0
            last_partial_text = ""
            last_provider = getattr(stt, "name", "unknown")
            if guard > 0:
                time.sleep(guard)
            # Answer-window arm cue: beep as soon as listen is ready (radio too)
            if arm_cue:
                _arm_cue_if_requested()
            while time.monotonic() - started < max_listen:
                agent_act = poll_listen_action(stream)
                if agent_act is not None and pieces:
                    # Finalize with audio already captured
                    break
                remaining = max_listen - (time.monotonic() - started)
                try:
                    # Only first segment uses discard (TTS handoff); later segments clean
                    seg_discard = discard_leading_ms if not pieces else 0
                    seg_ok_after = audio_ok_after if not pieces else None
                    on_open = (
                        (
                            lambda: syslog(
                                "listen.speech_opened",
                                component="stt",
                                level="info",
                                stream_id=stream,
                                mode=mode.value,
                            )
                        )
                        if arm_cue
                        else _cue_start_once
                    )
                    cap = capture_utterance(
                        max_s=min(remaining, max_listen),
                        end_silence_s=end_silence,
                        initial_timeout_s=min(gate_timeout_s, remaining),
                        post_tts_guard_s=0,
                        on_opened=on_open,
                        should_stop=_agent_wants_stop,
                        discard_leading_ms=seg_discard,
                        audio_ok_after=seg_ok_after,
                        abs_open_db=gate_abs_open,
                        open_margin_db=gate_open_margin,
                    )
                except TimeoutError:
                    agent_act = poll_listen_action(stream)
                    if agent_act is not None and pieces:
                        break
                    if pieces:
                        continue
                    if recording_cued:
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
                stt_seq += 1
                tr, latency_ms = _transcribe_logged(
                    stt,
                    wav,
                    stream_id=stream,
                    seq=stt_seq,
                    mode=mode.value,
                    purpose="radio",
                    sample_rate=cap.sample_rate,
                )
                last_provider = tr.provider
                if _echo_overlap(tr.text, last_tts):
                    pieces.clear()
                    continue
                # Agent may have requested end while we were capturing/STT
                agent_act = consume_listen_action(stream)
                if agent_act == "cancel":
                    if recording_cued:
                        play_record_stop()
                    body = (tr.text or "").strip()
                    store.record_stt(
                        text=body,
                        provider=tr.provider,
                        audio_ms=int(1000 * (time.monotonic() - started)),
                        latency_ms=latency_ms,
                        ok=False,
                        error="agent_cancel",
                    )
                    return ListenResult(
                        text=body,
                        provider=tr.provider,
                        duration_ms=int(1000 * (time.monotonic() - started)),
                        end_mode=mode.value,
                        end_phrase="agent:cancel",
                        cancelled=True,
                        stream_id=stream,
                        partials_emitted=partial_seq,
                    )
                if agent_act == "finish":
                    if recording_cued:
                        play_record_stop()
                    body = (tr.text or "").strip()
                    store.record_stt(
                        text=body,
                        provider=tr.provider,
                        audio_ms=int(1000 * (time.monotonic() - started)),
                        latency_ms=latency_ms,
                        ok=True,
                    )
                    return ListenResult(
                        text=body,
                        provider=tr.provider,
                        duration_ms=int(1000 * (time.monotonic() - started)),
                        end_mode=mode.value,
                        end_phrase="agent:finish",
                        stream_id=stream,
                        partials_emitted=partial_seq,
                    )
                hit = evaluate_radio_transcript(
                    tr.text,
                    end_phrases=cfg.listen.end_phrases,
                    cancel_phrases=cfg.listen.cancel_phrases,
                    soft_end_phrases=getattr(
                        cfg.listen, "soft_end_phrases", ()
                    ),
                    soft_end_phrases_enabled=bool(
                        getattr(cfg.listen, "soft_end_phrases_enabled", True)
                    ),
                )
                if hit is None:
                    body_so_far = (tr.text or "").strip()
                    if (
                        stream_partials
                        and body_so_far
                        and body_so_far != last_partial_text
                        and on_partial is not None
                    ):
                        from hark.partial import partial_fragment

                        prev_body = last_partial_text
                        frag = partial_fragment(prev_body, body_so_far)
                        partial_seq += 1
                        last_partial_text = body_so_far
                        ev = make_partial_event(
                            stream_id=stream,
                            seq=partial_seq,
                            text=body_so_far,
                            kind=partial_kind,
                            provider=tr.provider,
                            fragment=frag,
                            prev_text=prev_body,
                        )
                        ev["stt_seq"] = stt_seq
                        try:
                            on_partial(ev)
                        except Exception:
                            pass
                        # Prefer fragment in logs so each radio slice is visible
                        # (full cumulative body is still on the event as text).
                        syslog(
                            "listen.partial",
                            component="stt",
                            level="info",
                            stream_id=stream,
                            seq=partial_seq,
                            stt_seq=stt_seq,
                            fragment=(frag or "")[:300],
                            text_len=len(body_so_far),
                            text=(body_so_far[:120] + "…")
                            if len(body_so_far) > 120
                            else body_so_far,
                            provider=tr.provider,
                            partial=True,
                            final=False,
                        )
                    continue
                if recording_cued:
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

            # Exit loop: agent finish with pieces, or max timeout
            agent_act = consume_listen_action(stream)
            if recording_cued:
                play_record_stop()
            if pieces and agent_act in ("finish", None):
                # Final STT on accumulated audio if agent finished or we fell through
                if agent_act == "finish" or agent_act is None:
                    wav = write_wav_bytes(b"".join(pieces), 16000)
                    stt_seq += 1
                    tr, latency_ms = _transcribe_logged(
                        stt,
                        wav,
                        stream_id=stream,
                        seq=stt_seq,
                        mode=mode.value,
                        purpose="radio_final",
                        sample_rate=16000,
                    )
                    body = (tr.text or "").strip()
                    if agent_act == "finish":
                        store.record_stt(
                            text=body,
                            provider=tr.provider,
                            audio_ms=int(1000 * (time.monotonic() - started)),
                            latency_ms=latency_ms,
                            ok=True,
                        )
                        return ListenResult(
                            text=body,
                            provider=tr.provider,
                            duration_ms=int(1000 * (time.monotonic() - started)),
                            end_mode=mode.value,
                            end_phrase="agent:finish",
                            stream_id=stream,
                            partials_emitted=partial_seq,
                        )
            if agent_act == "cancel":
                return ListenResult(
                    text=last_partial_text,
                    provider=last_provider,
                    duration_ms=int(1000 * (time.monotonic() - started)),
                    end_mode=mode.value,
                    end_phrase="agent:cancel",
                    cancelled=True,
                    stream_id=stream,
                    partials_emitted=partial_seq,
                )
            raise TimeoutError(f"radio listen exceeded max_listen_s={max_listen}")
        finally:
            clear_active_listen(stream)


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
    ``overlap_discard_ms`` so residual echo is not fed to STT.
    """
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
        done = handoff["tts_done_at"]
        if done is None:
            return None
        return float(done) + discard_ms / 1000.0

    def _listen_worker() -> None:
        try:
            listen_box["result"] = run_listen(
                cfg,
                provider=provider,
                end_mode=end_mode,
                last_tts=text,
                already_armed=True,
                post_tts_guard_s=0.0,
                on_partial=on_partial,
                partial_kind=partial_kind,
                audio_ok_after=audio_ok_after,
                arm_cue=bool(getattr(cfg.audio, "answer_arm_cue", True)),
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

    tts_info = run_tts(
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

    def _attach_tts(exc: BaseException) -> BaseException:
        try:
            setattr(exc, "tts_info", tts_info)
        except Exception:
            pass
        return exc

    if listen_thread is not None:
        listen_thread.join()
        err = listen_box.get("error")
        if err is not None:
            raise _attach_tts(err)
        listened = listen_box["result"]
        assert isinstance(listened, ListenResult)
        _tag_meta_command(listened)
        return tts_info, listened

    # Half-duplex path (default): start listen after TTS + optional guard
    try:
        listened = run_listen(
            cfg,
            provider=provider,
            end_mode=end_mode,
            last_tts=text,
            post_tts_guard_s=cfg.audio.post_tts_guard_ms / 1000.0,
            already_armed=arm_event.is_set(),
            on_partial=on_partial,
            partial_kind=partial_kind,
            # Immediate record-start beep when listen is ready (not when speech opens).
            # Dogfood: post-ask lag felt like a broken handoff when cue waited for gate.
            arm_cue=bool(getattr(cfg.audio, "answer_arm_cue", True)),
        )
    except BaseException as exc:
        raise _attach_tts(exc) from exc
    _tag_meta_command(listened)
    return tts_info, listened


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
    try:
        tts_info, listened = speak_and_listen(
            cfg,
            prompt,
            provider=provider,
            end_mode=end_mode,
        )
    except TimeoutError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "exit": TIMEOUT,
            "tts": getattr(exc, "tts_info", None),
        }
    except ProviderError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "exit": getattr(exc, "code", PROVIDER),
            "tts": getattr(exc, "tts_info", None),
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
                arm_cue=bool(getattr(cfg.audio, "answer_arm_cue", True)),
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

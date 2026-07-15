"""tts / listen / ask orchestration."""

from __future__ import annotations

import re
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, TextIO

from hark.audio.capture import (
    MicLease,  # noqa: F401 — open_answer_window / test monkeypatch seam
    capture_utterance,  # noqa: F401 — open_answer_window / test monkeypatch seam
    pad_pcm16_silence,  # noqa: F401 — open_answer_window late-bind
    radio_stt_window_pcm,  # noqa: F401 — open_answer_window late-bind
    write_wav_bytes,  # noqa: F401 — open_answer_window late-bind
)
from hark.audio.cues import (
    configure_cues_from_config,  # noqa: F401 — open_answer_window / tests
    lookup_cached_tts,
    play_record_start,  # noqa: F401 — open_answer_window / tests
    play_record_stop,  # noqa: F401 — open_answer_window / tests
    store_cached_tts,
)
from hark.audio.media import duck_media  # also open_answer_window late-bind
from hark.audio.mic_mute import mic_muted_during_tts, repair_tts_mute_after_play
from hark.audio.playback import (
    abandon_tts_play_ticket,
    claim_tts_play_ticket,
    exclusive_playback,
    play_wav_bytes,
    write_wav,
)
from hark.config import HarkConfig
from hark.exitcodes import ABORT, OK, PROVIDER, TIMEOUT  # noqa: F401 — answer_window / tests
from hark.lifecycle import BusySection  # noqa: F401 — open_answer_window / tests
from hark.listen_control import (
    clear_active_listen,  # noqa: F401 — open_answer_window / tests
    consume_listen_action,  # noqa: F401 — open_answer_window / tests
    poll_listen_action,  # noqa: F401 — open_answer_window / tests
    register_active_listen,  # noqa: F401 — open_answer_window / tests
    touch_voice_activity,  # noqa: F401 — open_answer_window / tests
)
from hark.endpointing import build_endpoint_strategy  # noqa: F401 — open_answer_window
from hark.mic_coord import (
    pause_ambient_for_mic,  # noqa: F401 — open_answer_window / tests
    wait_until_tts_play_allowed,
)
from hark.providers.base import ProviderError
from hark.providers.resolve import (
    resolve_stt,  # noqa: F401 — open_answer_window / test monkeypatch seam
    resolve_tts,
)
from hark.syslog import log as syslog
from hark.usage import UsageStore
from hark.answer_window.policy import AnswerWindowProfile
from hark.answer_window.result import ListenResult  # canonical; facade re-export
from hark.answer_window.silence import (
    EMPTY_STT_NUDGE_TEXT,  # noqa: F401 — re-export for tests
    NO_OPEN_NUDGE_TEXT,  # noqa: F401 — re-export for tests
    _echo_overlap,  # noqa: F401 — re-export for tests
    is_no_open_timeout as _is_no_open_timeout_impl,
    log_empty_stt as _log_empty_stt_impl,
    log_no_open as _log_no_open_impl,
)
from hark.answer_window.text_join import (  # noqa: F401 — re-export for back-compat
    join_radio_stt_segments,
    monotonic_partial_text,
    prefer_complete_transcript,
)


def soft_truncate_text(text: str, max_chars: int) -> str:
    """Cut *text* to ≤ ``max_chars`` at the last word/sentence boundary if possible."""
    text = text or ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    for sep in (". ", "? ", "! ", "; ", ", ", " "):
        cut = window.rfind(sep)
        if cut >= max(1, max_chars // 4):
            return window[: cut + len(sep)].rstrip()
    return window.rstrip()


# B095: operator visual quick-reference for TTS questions (ask / Mode A).
_ITEM_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:Q\s*)?\d+\s*[\.\)\:\-–—]"  # 1.  1)  Q1:  2-
    r"|[A-Za-z]\s*[\.\)]"  # A.  b)
    r"|[-*•·]"  # bullets
    r")\s+",
    re.IGNORECASE,
)
# Capitalized ordinals only, at start or after sentence/line break — avoids
# mid-clause false hits like "the second: attempt".
_WORD_ORDINAL_SPLIT_RE = re.compile(
    r"(?:(?<=^)|(?<=[.!?\n][ \t])|(?<=\n))"
    r"(?:"
    r"One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten|"
    r"First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth"
    r")"
    r"\s*[:.—–-]\s+"
)
_Q_LABEL_SPLIT_RE = re.compile(
    r"(?:(?<=^)|(?<=\s))Q\s*\d+\s*[:.\-–—]\s*",
    re.IGNORECASE,
)


def _strip_item_prefix(item: str) -> str:
    """Remove a leading list/number prefix so we can renumber cleanly."""
    s = (item or "").strip()
    if not s:
        return s
    return _ITEM_PREFIX_RE.sub("", s, count=1).strip() or s


def extract_question_items(text: str) -> list[str]:
    """Split multi-item interview prompts when the pattern is obvious.

    Returns a single-element list when no multi-item structure is detected.
    """
    text = (text or "").strip()
    if not text:
        return []

    # 1) Explicit multi-line list (numbered, lettered, or bulleted)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 2:
        prefixed = sum(1 for ln in lines if _ITEM_PREFIX_RE.match(ln))
        if prefixed >= max(2, (len(lines) + 1) // 2):
            return [_strip_item_prefix(ln) for ln in lines]

    # 2) Q1: / Q2: labels in a single block
    q_parts = [p.strip() for p in _Q_LABEL_SPLIT_RE.split(text) if p.strip()]
    if len(q_parts) >= 2 and _Q_LABEL_SPLIT_RE.search(text):
        return q_parts

    # 3) Word ordinals: "One: … Two: …" / "First — … Second — …"
    if _WORD_ORDINAL_SPLIT_RE.search(text):
        word_parts = [p.strip() for p in _WORD_ORDINAL_SPLIT_RE.split(text) if p.strip()]
        if len(word_parts) >= 2:
            return word_parts

    # 4) Several distinct questions (sentence-ending '?')
    q_sents = re.findall(r"[^?]+?\?", text)
    if len(q_sents) >= 2:
        cleaned = [s.strip() for s in q_sents if s.strip()]
        # Require leftover after last ? is short/empty so we don't chop prose badly
        tail = text[sum(len(s) for s in q_sents) :].strip()
        if cleaned and len(tail) < 40:
            if tail:
                cleaned[-1] = f"{cleaned[-1]} {tail}".strip()
            return cleaned

    # 5) Plain multi-line paragraphs (2+ non-trivial lines, not one prose block)
    if len(lines) >= 2:
        # Prefer list-like short lines over a reflowed paragraph
        if all(len(ln) <= 200 for ln in lines) and len(lines) <= 12:
            return lines

    return [text]


def format_tts_question_text(text: str) -> str:
    """Readable operator form of a TTS question; numbers multi-item prompts."""
    text = (text or "").strip()
    if not text:
        return ""
    items = extract_question_items(text)
    if len(items) <= 1:
        return text
    return "\n".join(f"{i}. {_strip_item_prefix(item)}" for i, item in enumerate(items, 1))


def print_tts_question_text(
    text: str,
    *,
    stream: TextIO | None = None,
) -> None:
    """Print full question text to the controlling terminal as TTS starts (B095).

    Uses stderr so JSON results / radio partials on stdout stay machine-parseable.
    """
    body = format_tts_question_text(text)
    if not body:
        return
    out = stream if stream is not None else sys.stderr
    bar = "=" * 36
    print(f"{bar} hark question {bar}", file=out, flush=True)
    print(body, file=out, flush=True)
    print("=" * (36 * 2 + len(" hark question ")), file=out, flush=True)


def maybe_print_tts_question(cfg: HarkConfig, text: str) -> None:
    """Print TTS question when ``tts.print_prompt`` is enabled (default on)."""
    if not bool(getattr(cfg.tts, "print_prompt", True)):
        return
    try:
        print_tts_question_text(text)
    except Exception:
        # Never fail speak/listen because the terminal write failed.
        pass


def surface_tts_event(kind: str, **fields: Any) -> None:
    """Syslog + ambient.jsonl so ``hark monitor`` can surface TTS lifecycle (B091)."""
    try:
        from hark.syslog import log

        log(kind, component="tts", **fields)
    except Exception:
        pass
    try:
        from hark.events import new_event_id, utc_now_iso
        from hark.monitor_feed import append_ambient_jsonl

        event = {
            "schema": "hark.event.v1",
            "kind": kind,
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            **fields,
        }
        append_ambient_jsonl(event)
    except Exception:
        pass


def pack_tts_chunks(text: str, max_chars: int) -> list[str]:
    """Split *text* into ≤ ``max_chars`` pieces at sentence/word boundaries (B091).

    Never mid-word cuts when a space exists in the window. ``max_chars <= 0``
    means a single chunk (no limit).
    """
    text = (text or "").strip()
    if not text:
        return []
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]

    # Prefer sentence ends, then clauses, then words.
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    buf = ""

    def _flush() -> None:
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    def _append_piece(piece: str) -> None:
        nonlocal buf
        piece = piece.strip()
        if not piece:
            return
        if len(piece) > max_chars:
            _flush()
            # Word-pack oversized sentences
            words = piece.split()
            for w in words:
                if not buf:
                    if len(w) > max_chars:
                        # single token longer than limit — hard split (rare)
                        while len(w) > max_chars:
                            chunks.append(w[:max_chars])
                            w = w[max_chars:]
                        buf = w
                    else:
                        buf = w
                elif len(buf) + 1 + len(w) <= max_chars:
                    buf = f"{buf} {w}"
                else:
                    _flush()
                    buf = w
            return
        if not buf:
            buf = piece
        elif len(buf) + 1 + len(piece) <= max_chars:
            buf = f"{buf} {piece}"
        else:
            _flush()
            buf = piece

    for s in sentences:
        _append_piece(s)
    _flush()
    return chunks or [text[:max_chars]]


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
    play_wait_timeout_s: float | None = None,
) -> dict[str, Any]:
    """Synthesize and optionally play TTS.

    ``conference_policy`` (B017):
      - ``None`` / default: ``hold`` when ``audio.hold_during_conference``, else ``force``
      - ``hold``: wait for Zoom/Teams/Meet etc. to end (soft chime optional)
      - ``skip``: do not speak while conference active (lifecycle cues)
      - ``force``: always speak immediately

    ``use_cache``: when False, skip on-disk TTS phrase cache lookup and store
    (one-shot announces such as wake-label live-reload).

    ``play_wait_timeout_s`` (B099): max seconds to wait for the exclusive play
    queue turn. On timeout the ticket is abandoned and ``TimeoutError`` is raised.
    Ambient boot uses a short timeout so a stuck queue never blocks wake arming.

    Long text (B091): speak the full agent reply by default.

    - ``tts.max_chars`` (0 = unlimited): optional **total** cap; soft word-boundary
      cut + ``tts.truncated`` HEP/syslog (monitor-visible) if exceeded.
    - ``tts.chunk_chars``: per-provider synth size; multi-chunk play under one
      mute/duck hold so long replies are not mid-word chopped.
    """
    full_text = (text or "").strip()
    if not full_text:
        raise ProviderError("empty TTS text")

    total_limit = max_chars if max_chars is not None else int(cfg.tts.max_chars or 0)
    chunk_limit = int(getattr(cfg.tts, "chunk_chars", 1500) or 1500)
    if chunk_limit <= 0:
        chunk_limit = 1500

    truncated = False
    original_chars = len(full_text)
    if total_limit > 0 and len(full_text) > total_limit:
        full_text = soft_truncate_text(full_text, total_limit)
        truncated = True
        surface_tts_event(
            "tts.truncated",
            original_chars=original_chars,
            kept_chars=len(full_text),
            max_chars=total_limit,
            text_preview=full_text[:160],
            instructions=(
                "TTS text was truncated to tts.max_chars. Full agent text was NOT spoken. "
                "Set [tts].max_chars = 0 (unlimited) or raise the limit."
            ),
        )

    chunks = pack_tts_chunks(full_text, chunk_limit)
    chunked = len(chunks) > 1
    if chunked:
        surface_tts_event(
            "tts.chunked",
            chars=len(full_text),
            chunk_chars=chunk_limit,
            n_chunks=len(chunks),
            chunk_lens=[len(c) for c in chunks],
            instructions="Long TTS multi-chunk play (informational).",
        )

    # Handsfree path (hark tts / ask): hold full question speech during conference.
    hold_meta: dict[str, Any] | None = None
    if play:
        from hark.conference import apply_conference_hold

        policy = conference_policy
        if policy is None:
            policy = "hold" if cfg.audio.hold_during_conference else "force"
        hold = apply_conference_hold(cfg, full_text, policy=policy)
        hold_meta = hold.as_meta()
        if hold.skipped:
            return {
                "ok": True,
                "provider": "skipped",
                "voice": voice or cfg.tts.voice or "eve",
                "truncated": truncated,
                "chunked": chunked,
                "chunks": len(chunks),
                "chars": len(full_text),
                "words": len(full_text.split()),
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
    provider_name = provider or cfg.tts.provider
    content_type = "audio/mpeg"
    used_voice = voice_id
    from_cache = False
    audio_parts: list[bytes] = []
    play_ms = 0
    mute_applied = False
    mute_repair: dict[str, Any] | None = None
    duck_meta: dict[str, Any] | None = None
    defer_meta: dict[str, Any] | None = None
    near = (
        near_end_ms
        if near_end_ms is not None
        else int(cfg.audio.listen_pre_arm_ms)
    )
    do_duck = bool(getattr(cfg.audio, "duck_media_during_tts", True))

    def _synth_one(piece: str) -> tuple[bytes, str, str, str, bool]:
        """Return (audio, provider, content_type, voice, from_cache)."""
        cached = lookup_cached_tts(voice_id, piece) if use_cache else None
        if cached is not None:
            return cached, "cache", "audio/mpeg", voice_id, True
        tts = resolve_tts(
            provider or cfg.tts.provider,
            voice=voice_id,
            language=cfg.tts.language,
        )
        result = tts.synthesize(piece, voice=voice_id)
        if use_cache and len(piece) <= 120:
            try:
                store_cached_tts(result.voice or voice_id, piece, result.audio)
            except Exception:
                pass
        return (
            result.audio,
            result.provider,
            result.content_type,
            result.voice or voice_id,
            False,
        )

    # B092: synth may run in parallel across processes; play is exclusive.
    # First chunk is synthesized *before* taking the speaker lock so a second
    # `hark tts` started in quick succession can hit xAI while we still play.
    # Multi-chunk: start synth(i+1) while playing chunk i (pipeline).
    def _record_synth_fail(piece: str, exc: BaseException) -> None:
        store.record_tts(
            text=piece,
            provider=provider or cfg.tts.provider,
            voice=voice_id,
            ok=False,
            error=str(exc)[:200],
            latency_ms=int(1000 * (time.monotonic() - t0)),
        )

    def _apply_synth(
        audio_bytes: bytes,
        p_name: str,
        c_type: str,
        v_used: str,
        fc: bool,
    ) -> bytes:
        nonlocal provider_name, content_type, used_voice, from_cache
        provider_name = p_name
        content_type = c_type
        used_voice = v_used
        from_cache = from_cache or fc
        audio_parts.append(audio_bytes)
        return audio_bytes

    try:
        if play:
            # Claim FIFO slot *before* synth so 5 concurrent launches keep order
            # even if later jobs finish synthesizing first (B092).
            play_ticket = claim_tts_play_ticket()
            try:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    # Kick first synth outside the play hold (parallel with other jobs)
                    fut: Future[tuple[bytes, str, str, str, bool]] = pool.submit(
                        _synth_one, chunks[0]
                    )
                    try:
                        audio_bytes, p_name, c_type, v_used, fc = fut.result()
                    except Exception as exc:
                        _record_synth_fail(chunks[0], exc)
                        raise
                    _apply_synth(audio_bytes, p_name, c_type, v_used, fc)

                    # Prefetch chunk 1 while we may still wait for our play turn
                    next_fut: Future[tuple[bytes, str, str, str, bool]] | None = None
                    if len(chunks) > 1:
                        next_fut = pool.submit(_synth_one, chunks[1])

                    # B097 / B105: if operator listen/radio is open, defer play +
                    # mic mute. HOLD mode waits until capture ends; streaming mode
                    # waits for streaming_ack_min_quiet_s of operator quiet (or
                    # stream end) so short acks do not barge into continuous speech.
                    # Synth may already be done; same-PID capture is ignored so
                    # in-listen nudges still speak.
                    if bool(getattr(cfg.audio, "defer_tts_while_listening", True)):
                        ambient_cfg = getattr(cfg, "ambient", None)
                        streaming_ack = bool(
                            getattr(ambient_cfg, "streaming", False)
                        )
                        min_quiet = float(
                            getattr(
                                ambient_cfg, "streaming_ack_min_quiet_s", 2.0
                            )
                            or 2.0
                        )
                        defer = wait_until_tts_play_allowed(
                            streaming=streaming_ack,
                            min_quiet_s=min_quiet,
                            max_wait_s=float(
                                getattr(cfg.audio, "defer_tts_max_wait_s", 45.0)
                            ),
                            poll_ms=int(
                                getattr(cfg.audio, "defer_tts_poll_ms", 100)
                            ),
                            quiet_ms=int(
                                getattr(cfg.audio, "defer_tts_quiet_ms", 200)
                            ),
                        )
                        defer_meta = defer.as_meta()
                        if defer.deferred:
                            if streaming_ack and defer.gate == "quiet":
                                instructions = (
                                    "TTS play waited for operator quiet "
                                    f"(≥{min_quiet:g}s) or listen end so "
                                    "streaming acks do not barge into continuous "
                                    "speech (B105). "
                                    "Set [audio].defer_tts_while_listening = false "
                                    "to disable; [ambient].streaming_ack_min_quiet_s "
                                    "tunes the quiet gate."
                                )
                            else:
                                instructions = (
                                    "TTS play waited for operator listen/radio to "
                                    "finish so mic mute would not cut off capture. "
                                    "Set [audio].defer_tts_while_listening = false "
                                    "to disable."
                                )
                            surface_tts_event(
                                "tts.deferred_for_listen",
                                **defer_meta,
                                instructions=instructions,
                            )

                    with exclusive_playback(
                        ticket=play_ticket,
                        wait_timeout_s=play_wait_timeout_s,
                    ):
                        with mic_muted_during_tts(enabled=do_mute) as mute_state:
                            mute_applied = mute_state.applied
                            with duck_media(
                                cfg, enabled=do_duck, exclude_conference=True
                            ) as duck_state:
                                duck_meta = duck_state.as_meta()
                                for i in range(len(chunks)):
                                    is_last = i == len(chunks) - 1
                                    pr = play_wav_bytes(
                                        audio_parts[i],
                                        on_near_end=on_near_end if is_last else None,
                                        near_end_ms=near
                                        if (on_near_end and is_last)
                                        else 0,
                                        exclusive=False,
                                    )
                                    play_ms += pr.duration_ms
                                    if i + 1 >= len(chunks):
                                        break
                                    # Resolve prefetched next; kick following while we play
                                    assert next_fut is not None
                                    try:
                                        ab, pn, ct, vu, fch = next_fut.result()
                                    except Exception as exc:
                                        _record_synth_fail(chunks[i + 1], exc)
                                        raise
                                    _apply_synth(ab, pn, ct, vu, fch)
                                    if i + 2 < len(chunks):
                                        next_fut = pool.submit(
                                            _synth_one, chunks[i + 2]
                                        )
                                    else:
                                        next_fut = None
            except BaseException:
                # Don't stall the FIFO if we never reached exclusive_playback
                try:
                    abandon_tts_play_ticket(play_ticket)
                except Exception:
                    pass
                raise
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                futs = [pool.submit(_synth_one, piece) for piece in chunks]
                for piece, f in zip(chunks, futs):
                    try:
                        ab, pn, ct, vu, fch = f.result()
                    except Exception as exc:
                        _record_synth_fail(piece, exc)
                        raise
                    _apply_synth(ab, pn, ct, vu, fch)
    finally:
        if play:
            # B086: never leave depth>0 or Pulse stuck muted after TTS
            try:
                mute_repair = repair_tts_mute_after_play(
                    mute_was_enabled=bool(do_mute),
                    mute_applied=mute_applied,
                )
            except Exception:
                mute_repair = None

    latency_ms = int(1000 * (time.monotonic() - t0))
    out_path = None
    if out and audio_parts:
        # Single-chunk: write as before. Multi: write first part only (formats
        # may not concatenate cleanly); full speech was already played if play.
        out_path = str(write_wav(out, audio_parts[0]))

    store.record_tts(
        text=full_text,
        provider=provider_name,
        voice=used_voice,
        audio_ms=play_ms,
        latency_ms=latency_ms,
        ok=True,
        meta={
            # dashboard TTS audit trail (B067): what was actually spoken
            "text_preview": full_text[:160],
            "from_cache": from_cache,
            "conference": hold_meta,
            "media_duck": duck_meta,
            "listen_defer": defer_meta,
            "chunked": chunked,
            "chunks": len(chunks),
        },
    )
    result: dict[str, Any] = {
        "ok": True,
        "provider": provider_name,
        "voice": used_voice,
        "truncated": truncated,
        "chunked": chunked,
        "chunks": len(chunks),
        "chars": len(full_text),
        "original_chars": original_chars,
        "words": len(full_text.split()),
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
    if defer_meta is not None:
        result["listen_defer"] = defer_meta
    if mute_repair is not None and mute_repair.get("repaired"):
        result["mute_repaired"] = mute_repair
    return result


# EMPTY_STT_NUDGE_TEXT / NO_OPEN_NUDGE_TEXT: imported from answer_window.silence


def _is_no_open_timeout(exc: BaseException) -> bool:
    """True when energy gate never opened (vs empty STT after open)."""
    return _is_no_open_timeout_impl(exc)


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
    _log_no_open_impl(
        peak_rms=peak_rms,
        peak_db=peak_db,
        open_thresh=open_thresh,
        after_tts=after_tts,
        attempt=attempt,
        stream_id=stream_id,
        phase=phase,
        error=error,
        abs_open_db=abs_open_db,
        syslog_fn=syslog,
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
    _log_empty_stt_impl(
        duration_ms=duration_ms,
        peak_rms=peak_rms,
        peak_db=peak_db,
        wait_speech_ms=wait_speech_ms,
        after_tts=after_tts,
        attempt=attempt,
        provider=provider,
        stream_id=stream_id,
        phase=phase,
        syslog_fn=syslog,
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



# join_radio_stt_segments / monotonic_partial_text / prefer_complete_transcript
# live in hark.answer_window.text_join (owned by RadioSession); imported above.


def effective_radio_idle_end_s(
    cfg: HarkConfig,
    *,
    streaming: bool | None = None,
    streaming_ack_min_quiet_s: float | None = None,
) -> float:
    """Post-speech quiet before radio auto-finish (B074 + B112 streaming).

    Prefer :func:`hark.answer_window.policy.effective_radio_idle_s` with an
    :class:`AnswerWindowPolicy` built at the call seam. This helper remains for
    callers that still pass cfg; it must not be used *inside* a session loop to
    re-read ``cfg.ambient`` — pass ``streaming`` / ``streaming_ack_min_quiet_s``
    explicitly from policy fields instead.
    """
    from hark.answer_window.policy import AnswerWindowPolicy, effective_radio_idle_s
    from hark.listen_end import EndMode as _EM

    # Seam-only defaults for legacy callers that omit streaming flags.
    if streaming is None:
        streaming = bool(getattr(getattr(cfg, "ambient", None), "streaming", False))
    if streaming_ack_min_quiet_s is None:
        streaming_ack_min_quiet_s = float(
            getattr(getattr(cfg, "ambient", None), "streaming_ack_min_quiet_s", 2.0)
            or 2.0
        )
    pol = AnswerWindowPolicy(
        profile="bound_answer",
        end_mode=_EM.RADIO,
        max_listen_s=1.0,
        end_silence_s=float(getattr(cfg.listen, "end_silence_s", 2.1) or 2.1),
        radio_idle_end_silence_s=float(
            getattr(cfg.listen, "radio_idle_end_silence_s", 0.0) or 0.0
        ),
        streaming=bool(streaming),
        streaming_ack_min_quiet_s=float(streaming_ack_min_quiet_s),
    )
    return effective_radio_idle_s(pol)


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
    # Prefer profile builders (policy_from_config) for gate knobs.
    profile: AnswerWindowProfile | None = None,
    policy: Any | None = None,
    streaming: bool | None = None,
) -> ListenResult:
    """Capture speech. Radio mode streams partials via on_partial when enabled.

    Prefer ``profile=`` (``bound_answer`` / ``post_wake`` / ``confirm``) or an
    explicit ``policy=`` so gate knobs come from :func:`policy_from_config`.
    Default profile is **bound_answer** (streaming off — does **not** inherit
    ``[ambient].streaming``; P1.M6). Ambient post-wake must pass
    ``profile="post_wake"``. Optional ``streaming=`` overrides the profile default.

    on_partial(event_dict) is called for each non-final radio transcript so the
    orchestrator can start thinking early. Events always set partial=true.

    Empty STT / no-open recovery (silence mode) and overlap pre-arm
    (``audio_ok_after`` / ``discard_leading_ms``) are unchanged.
    """

    # Thin facade (P1.M1.E4 / P1.M6): build policy + deps, open answer window.
    # No cfg.ambient reads in this function — ambient is only consumed inside
    # policy_from_config at the seam.
    from hark.answer_window.deps import AnswerWindowDeps
    from hark.answer_window.open_window import open_answer_window
    from hark.answer_window.policy import AnswerWindowPolicy, policy_from_config

    if policy is not None:
        if not isinstance(policy, AnswerWindowPolicy):
            raise TypeError(
                f"policy must be AnswerWindowPolicy/ListenSessionPolicy, got {type(policy)!r}"
            )
        deps = AnswerWindowDeps(
            cfg=cfg,
            on_partial=on_partial,
            audio_ok_after=audio_ok_after,
        )
        return open_answer_window(policy, deps=deps)

    # Explicit post_tts_guard always wins. Pre-arm (already_armed) used to zero
    # the guard and race mute-unmute / residual TTS into the energy gate.
    if post_tts_guard_s is not None:
        guard = max(0.0, float(post_tts_guard_s))
    else:
        # already_armed or not: still settle briefly for mute unmute / echo residual
        guard = max(0.0, cfg.audio.post_tts_guard_ms / 1000.0)

    effective_profile: AnswerWindowProfile = (
        profile if profile is not None else "bound_answer"
    )

    overrides: dict[str, Any] = {
        "max_listen_s": float(max_s if max_s is not None else cfg.listen.max_listen_s),
        "last_tts": last_tts,
        "post_tts_guard_s": guard,
        "already_armed": bool(already_armed),
        "stream_id": stream_id,
        "partial_kind": partial_kind,
        "discard_leading_ms": int(discard_leading_ms or 0),
        "stt_provider": provider,
    }
    if end_mode is not None:
        overrides["end_mode"] = end_mode
    if streaming is not None:
        overrides["streaming"] = bool(streaming)

    built = policy_from_config(cfg, effective_profile, **overrides)
    deps = AnswerWindowDeps(
        cfg=cfg,
        on_partial=on_partial,
        audio_ok_after=audio_ok_after,
    )
    return open_answer_window(built, deps=deps)



# SpeakThenListen (P1.M4): half-duplex handoff + confirm live in
# hark.speak_then_listen; thin re-exports keep hark.speech.* import paths stable.
from hark.speak_then_listen import run_ask as run_ask  # noqa: E402
from hark.speak_then_listen import speak_and_listen as speak_and_listen  # noqa: E402

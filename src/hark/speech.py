"""tts / listen / ask orchestration."""

from __future__ import annotations

import re
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from hark.audio.capture import (
    MicLease,
    capture_utterance,
    clamp_pre_roll_ms,
    effective_radio_segment_pad_ms,
    pad_pcm16_silence,
    radio_stt_window_pcm,
    write_wav_bytes,
)
from hark.audio.cues import (
    configure_cues_from_config,
    lookup_cached_tts,
    play_record_start,
    play_record_stop,
    store_cached_tts,
)
from hark.audio.media import duck_media
from hark.audio.mic_mute import mic_muted_during_tts, repair_tts_mute_after_play
from hark.audio.playback import (
    abandon_tts_play_ticket,
    claim_tts_play_ticket,
    exclusive_playback,
    play_wav_bytes,
    write_wav,
)
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
        import json

        from hark.events import new_event_id, utc_now_iso
        from hark.paths import state_dir

        event = {
            "schema": "hark.event.v1",
            "kind": kind,
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            **fields,
        }
        path = state_dir() / "ambient.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
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
) -> dict[str, Any]:
    """Synthesize and optionally play TTS.

    ``conference_policy`` (B017):
      - ``None`` / default: ``hold`` when ``audio.hold_during_conference``, else ``force``
      - ``hold``: wait for Zoom/Teams/Meet etc. to end (soft chime optional)
      - ``skip``: do not speak while conference active (lifecycle cues)
      - ``force``: always speak immediately

    ``use_cache``: when False, skip on-disk TTS phrase cache lookup and store
    (one-shot announces such as wake-label live-reload).

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

                    with exclusive_playback(ticket=play_ticket):
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
    if mute_repair is not None and mute_repair.get("repaired"):
        result["mute_repaired"] = mute_repair
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
    """True when *transcript* looks like residual TTS, not a real answer.

    Short answers that *quote a word from the question* (e.g. ``BitLocker.`` after
    the prompt asked about BitLocker) must **not** match — dogfood B093: that
    used to wipe the whole radio assembly via ``pieces.clear()``.
    """
    if not last_tts or not transcript:
        return False
    a = re.sub(r"\W+", " ", transcript.lower()).strip()
    b = re.sub(r"\W+", " ", last_tts.lower()).strip()
    # Need substantial text on both sides; one-word replies are never "echo"
    if len(a) < 24 or len(b) < 24:
        return False
    # Substring only when the transcript is long enough to be residual TTS bleed
    if len(a) >= 40 and (a in b or b in a):
        return True
    aw, bw = set(a.split()), set(b.split())
    if not aw or not bw:
        return False
    # Require enough shared mass that a short answer cannot clear the session
    if len(aw) < 6:
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



def join_radio_stt_segments(segments: list[str]) -> str:
    """Join per-segment STT without cumulative re-STT (avoids long-audio word loss).

    Each radio segment is transcribed alone; we assemble text with light overlap
    trim so repeated phrase tails do not double. Empty segments are skipped so a
    failed mid-slice STT does not erase prior text.
    """
    out: list[str] = []
    for raw in segments:
        part = " ".join((raw or "").split()).strip()
        if not part:
            continue
        if not out:
            out.append(part)
            continue
        prev = out[-1]
        # If new starts with end of previous (common STT re-prefix), drop overlap
        prev_toks = prev.split()
        part_toks = part.split()
        max_olap = min(len(prev_toks), len(part_toks), 8)
        olap = 0
        for n in range(max_olap, 0, -1):
            if prev_toks[-n:] == part_toks[:n]:
                olap = n
                break
        if olap:
            part_toks = part_toks[olap:]
        if part_toks:
            out.append(" ".join(part_toks))
    return " ".join(out).strip()


def prefer_complete_transcript(a: str, b: str) -> str:
    """Pick the more complete of two transcripts without inventing words.

    Used so a full-audio re-STT cannot *replace* a longer joined partial body
    with a shorter rewrite (the original word-loss symptom).
    """
    aa = " ".join((a or "").split()).strip()
    bb = " ".join((b or "").split()).strip()
    if not aa:
        return bb
    if not bb:
        return aa
    if aa == bb:
        return aa
    # One properly extends the other
    if bb.startswith(aa) or aa in bb:
        return bb
    if aa.startswith(bb) or bb in aa:
        return aa
    # Prefer more tokens (conservative: do not merge incompatible rewrites)
    if len(bb.split()) > len(aa.split()):
        return bb
    return aa


def monotonic_partial_text(prev: str, candidate: str) -> str:
    """Never shrink the published partial body across radio slices."""
    p = " ".join((prev or "").split()).strip()
    c = " ".join((candidate or "").split()).strip()
    if not p:
        return c
    if not c:
        return p
    return prefer_complete_transcript(p, c)


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

    on_partial(event_dict) is called for each non-final radio transcript so the orchestrator
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
    # B079: ≥250 ms pre-speech from the capture ring when the gate opens
    gate_pre_roll_ms = clamp_pre_roll_ms(getattr(cfg.listen, "pre_roll_ms", 300))
    gate_mute_pad_ms = int(getattr(cfg.audio, "mute_edge_pad_ms", 300) or 0)
    radio_overlap_ms = int(getattr(cfg.listen, "radio_segment_overlap_ms", 300) or 0)
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

    stt = resolve_stt(provider or cfg.stt.provider, stt_cfg=cfg.stt)
    # Silence mode: end_silence_s finalizes the answer window.
    # Radio mode: radio_partial_silence_s only cuts a segment for interim STT /
    # ambient.partial (B037). The turn still finalizes on end phrase, soft end
    # (if enabled), agent listen-end, cancel, max_listen_s, or (B074) post-speech
    # idle quiet of radio_idle_end_silence_s (default 3× end_silence_s).
    end_silence = (
        float(cfg.listen.end_silence_s)
        if mode is EndMode.SILENCE
        else float(getattr(cfg.listen, "radio_partial_silence_s", 0.6))
    )
    # Radio answer windows: long continuous quiet after speech has opened → finish
    # (not cancel). Before first open, use normal initial_timeout / nudges only.
    radio_idle_end = float(
        getattr(cfg.listen, "radio_idle_end_silence_s", 0.0) or 0.0
    )
    if radio_idle_end <= 0:
        radio_idle_end = 3.0 * float(cfg.listen.end_silence_s)
    # Radio-only: silence pad around each segment before STT (B075). Silence
    # end_mode never pads. Clamped under radio_partial_silence_s so pad is hush.
    radio_pad_ms = (
        effective_radio_segment_pad_ms(
            int(getattr(cfg.listen, "radio_segment_pad_ms", 250)),
            float(getattr(cfg.listen, "radio_partial_silence_s", 0.6)),
        )
        if mode is EndMode.RADIO
        else 0
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
                            preroll_ms=gate_pre_roll_ms,
                            mute_edge_pad_ms=gate_mute_pad_ms,
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

            # Radio mode — segment until end phrase / agent finish / post-speech
            # idle (B074); stream partials. Short pauses stay open; long quiet
            # after speech has opened auto-finishes (same path as soft-end).
            pieces: list[bytes] = []
            text_segments: list[str] = []
            # B085: last segment tail for STT window overlap (real PCM, not silence)
            segment_overlap_tail = b""
            started = time.monotonic()
            partial_seq = 0
            last_partial_text = ""
            last_provider = getattr(stt, "name", "unknown")
            last_sample_rate = 16000
            speech_opened_once = False
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

                    def _on_speech_opened() -> None:
                        nonlocal speech_opened_once
                        speech_opened_once = True
                        if arm_cue:
                            syslog(
                                "listen.speech_opened",
                                component="stt",
                                level="info",
                                stream_id=stream,
                                mode=mode.value,
                            )
                        else:
                            _cue_start_once()

                    # After speech has opened at least once, wait only
                    # radio_idle_end_silence_s for more speech before auto-finish.
                    # Before first open: normal initial_timeout (nudges / timeout).
                    if speech_opened_once:
                        seg_timeout = min(radio_idle_end, remaining)
                    else:
                        seg_timeout = min(gate_timeout_s, remaining)
                    cap = capture_utterance(
                        max_s=min(remaining, max_listen),
                        end_silence_s=end_silence,
                        initial_timeout_s=seg_timeout,
                        post_tts_guard_s=0,
                        on_opened=_on_speech_opened,
                        should_stop=_agent_wants_stop,
                        discard_leading_ms=seg_discard,
                        audio_ok_after=seg_ok_after,
                        abs_open_db=gate_abs_open,
                        open_margin_db=gate_open_margin,
                        preroll_ms=gate_pre_roll_ms,
                        mute_edge_pad_ms=gate_mute_pad_ms,
                    )
                except TimeoutError:
                    agent_act = poll_listen_action(stream)
                    if agent_act is not None and pieces:
                        break
                    # B074: post-speech continuous quiet → auto-finish (not cancel)
                    if pieces and speech_opened_once:
                        if recording_cued:
                            play_record_stop()
                        wav = write_wav_bytes(
                            b"".join(pieces), last_sample_rate or 16000
                        )
                        stt_seq += 1
                        tr, latency_ms = _transcribe_logged(
                            stt,
                            wav,
                            stream_id=stream,
                            seq=stt_seq,
                            mode=mode.value,
                            purpose="radio_idle",
                            sample_rate=last_sample_rate or 16000,
                        )
                        last_provider = tr.provider
                        hit = evaluate_radio_transcript(
                            tr.text,
                            end_phrases=cfg.listen.end_phrases,
                            cancel_phrases=cfg.listen.cancel_phrases,
                            soft_end_phrases=getattr(
                                cfg.listen, "soft_end_phrases", ()
                            ),
                            soft_end_phrases_enabled=bool(
                                getattr(
                                    cfg.listen, "soft_end_phrases_enabled", True
                                )
                            ),
                        )
                        if hit is not None:
                            body = (
                                hit.body if cfg.listen.strip_phrase else tr.text
                            )
                            store.record_stt(
                                text=body,
                                provider=tr.provider,
                                audio_ms=int(
                                    1000 * (time.monotonic() - started)
                                ),
                                latency_ms=latency_ms,
                                ok=hit.kind != "cancel",
                                error="cancel" if hit.kind == "cancel" else None,
                            )
                            if hit.kind == "cancel":
                                return ListenResult(
                                    text=hit.body,
                                    provider=tr.provider,
                                    duration_ms=int(
                                        1000 * (time.monotonic() - started)
                                    ),
                                    end_mode=mode.value,
                                    end_phrase=hit.phrase,
                                    cancelled=True,
                                    stream_id=stream,
                                    partials_emitted=partial_seq,
                                )
                            return ListenResult(
                                text=body,
                                provider=tr.provider,
                                duration_ms=int(
                                    1000 * (time.monotonic() - started)
                                ),
                                end_mode=mode.value,
                                end_phrase=hit.phrase,
                                stream_id=stream,
                                partials_emitted=partial_seq,
                            )
                        body = (tr.text or "").strip()
                        store.record_stt(
                            text=body,
                            provider=tr.provider,
                            audio_ms=int(1000 * (time.monotonic() - started)),
                            latency_ms=latency_ms,
                            ok=True,
                        )
                        syslog(
                            "listen.radio_idle_end",
                            component="stt",
                            level="info",
                            stream_id=stream,
                            idle_s=radio_idle_end,
                            text_len=len(body),
                            partials_emitted=partial_seq,
                        )
                        return ListenResult(
                            text=body,
                            provider=tr.provider,
                            duration_ms=int(
                                1000 * (time.monotonic() - started)
                            ),
                            end_mode=mode.value,
                            end_phrase="radio_idle",
                            stream_id=stream,
                            partials_emitted=partial_seq,
                        )
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
                # Successful capture always means the energy gate opened
                speech_opened_once = True
                last_sample_rate = cap.sample_rate
                # Pad segment bounds into silence so gate-cut edge phonemes are
                # less often clipped by STT (B075). Mid-speech samples unchanged.
                seg_pcm = (
                    pad_pcm16_silence(
                        cap.pcm16,
                        pad_ms=radio_pad_ms,
                        sample_rate=cap.sample_rate,
                    )
                    if radio_pad_ms > 0
                    else cap.pcm16
                )
                pieces.append(seg_pcm)
                # B085: STT window includes real PCM lookback from prior segment
                stt_pcm, segment_overlap_tail = radio_stt_window_pcm(
                    seg_pcm,
                    segment_overlap_tail,
                    overlap_ms=radio_overlap_ms,
                    sample_rate=cap.sample_rate,
                )
                # STT this window alone, then assemble text (B083).
                seg_wav = write_wav_bytes(stt_pcm, cap.sample_rate)
                stt_seq += 1
                tr, latency_ms = _transcribe_logged(
                    stt,
                    seg_wav,
                    stream_id=stream,
                    seq=stt_seq,
                    mode=mode.value,
                    purpose="radio",
                    sample_rate=cap.sample_rate,
                )
                last_provider = tr.provider
                if _echo_overlap(tr.text, last_tts):
                    # Skip this segment only — never wipe prior radio assembly (B093)
                    try:
                        syslog(
                            "speech.echo_skip_segment",
                            component="stt",
                            level="info",
                            stream_id=stream,
                            stt_seq=stt_seq,
                            text=(tr.text or "")[:120],
                            message="skipped echo-like segment; kept prior text",
                        )
                    except Exception:
                        pass
                    continue
                if (tr.text or "").strip():
                    text_segments.append((tr.text or "").strip())
                # End-phrase / partial paths use assembled cumulative text
                from types import SimpleNamespace

                joined = join_radio_stt_segments(text_segments)
                tr = SimpleNamespace(
                    text=joined or (tr.text or ""),
                    provider=getattr(tr, "provider", last_provider),
                )
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
                    # Joined segment STT is append-only; still refuse shrink so a
                    # flaky mid-slice rewrite cannot drop words the orchestrator already saw.
                    body_so_far = monotonic_partial_text(
                        last_partial_text, (tr.text or "").strip()
                    )
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
                # Primary: per-segment join (B083). Optional full-audio re-STT is a
                # *candidate only* — never replace a longer joined body (word loss).
                if agent_act == "finish" or agent_act is None:
                    from types import SimpleNamespace

                    joined = join_radio_stt_segments(text_segments)
                    # Monotonic vs last partial the orchestrator already saw
                    body = monotonic_partial_text(last_partial_text, joined)
                    latency_ms = 0
                    tr_provider = last_provider
                    if len(pieces) >= 1:
                        wav = write_wav_bytes(
                            b"".join(pieces), last_sample_rate or 16000
                        )
                        stt_seq += 1
                        tr_full, latency_ms = _transcribe_logged(
                            stt,
                            wav,
                            stream_id=stream,
                            seq=stt_seq,
                            mode=mode.value,
                            purpose="radio_final",
                            sample_rate=last_sample_rate or 16000,
                        )
                        tr_provider = getattr(tr_full, "provider", None) or tr_provider
                        body = prefer_complete_transcript(
                            body, (tr_full.text or "").strip()
                        )
                    tr = SimpleNamespace(text=body, provider=tr_provider)
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
                    # max_listen / fall-through: return assembled body if any
                    if body:
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
                            end_phrase="max_listen",
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

    # Operator visual quick-reference (B095): print full question as TTS starts.
    # Only this path (ask / tts --listen) — not ambient acks or confirm readbacks.
    maybe_print_tts_question(cfg, text)

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

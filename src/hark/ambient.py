"""Ambient listen: local wake snippets, then cloud STT for the prompt body."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import Any, TextIO

from hark.audio.capture import (
    ContinuousMicStream,
    MicBusyError,
    score_window_plan,
)
from hark.config import HarkConfig, load_config
from hark.debug_snips import purge_old_debug_snips, save_wake_snippet
from hark.events import new_event_id, utc_now_iso
from hark.config_watch import start_config_watcher
from hark.lifecycle import (
    clear_reload_request,
    clear_shutdown_reason,
    get_shutdown_reason,
    install_signal_handlers,
    reload_requested,
    reload_source,
    shutdown_phrase,
    shutdown_requested,
)
from hark.monitor_feed import emit_hep
from hark.paths import default_config_path
from hark.mic_coord import ambient_pause_requested
from hark.listen_end import (
    DEFAULT_CANCEL_PHRASES,
    DEFAULT_END_PHRASES,
    evaluate_radio_transcript,
)
from hark.partial import make_turn_event, new_stream_id
from hark.providers.base import ProviderError
from hark.speech import run_listen, run_tts
from hark.syslog import log as syslog
from hark.wake import (
    NearMiss,
    NearMissAccumulator,
    WakeBackend,
    WakeHit,
    WakePolicy,
    build_wake_backend,
    default_wake_policy,
    make_wake_near_miss_event,
    plausible_near_miss,
    suggest_learn_from_near_miss,
)
from hark.wake_learn import (
    LearnedWake,
    learn_name_alias,
    learn_phrase_alias,
    learned_event,
    load_learned,
    load_learned_if_changed,
)

# Default spoken wake example in ambient boot TTS (names mode / stock config).
DEFAULT_BOOT_WAKE_LABEL = "hey hark"
_EXCEPTION_DETAIL_MAX_CHARS = 240
_EXCEPTION_TYPE_MAX_CHARS = 80
_EVENT_ID_MAX_CHARS = 160
_LAST_SUCCESS_TEXT_MAX_CHARS = 240
_LAST_SUCCESS_PROVIDER_MAX_CHARS = 120


def primary_wake_label(cfg: HarkConfig) -> str:
    """Primary (first) name as ``hey <name>``, or first custom full phrase.

    Used for ambient startup TTS so the spoken line — and thus the on-disk TTS
    cache key (voice + full text) — tracks the operator's configured wake.
    """
    amb = cfg.ambient
    mode = str(getattr(amb, "wake_mode", "") or "").strip().lower()
    names = [
        str(n).strip() for n in (getattr(amb, "names", None) or []) if str(n).strip()
    ]
    phrases = [
        str(p).strip()
        for p in (getattr(amb, "activation_phrases", None) or [])
        if p and str(p).strip()
    ]

    if mode in ("phrases", "phrase", "full", "full_phrase", "full-phrase"):
        return phrases[0] if phrases else DEFAULT_BOOT_WAKE_LABEL

    # Exclusive full-phrase list (no product names) wins over default names=
    # so custom-only configs speak "start prompt" not "hey hark".
    if phrases:
        joined = " ".join(phrases).lower()
        if "hark" not in joined and "herald" not in joined:
            return phrases[0]

    if names:
        return f"hey {names[0]}"

    if phrases:
        return phrases[0]

    return DEFAULT_BOOT_WAKE_LABEL


def ambient_boot_tts_text(cfg: HarkConfig) -> str:
    """Startup TTS line; cache is keyed by voice + this full string (includes label).

    Includes the primary wake phrase always; when listen end_mode is radio, also
    a brief how-to-finish hint (B115).
    """
    from hark.audio.cues import ambient_boot_line

    end_mode = getattr(getattr(cfg, "listen", None), "end_mode", None)
    return ambient_boot_line(primary_wake_label(cfg), end_mode=end_mode)


def wake_label_change_tts_text(old_label: str, new_label: str) -> str:
    """One-shot TTS when live-reload changes the primary wake name/phrase.

    Not intended for the on-disk phrase cache (ephemeral announce).
    """
    old = (old_label or "").strip() or "your previous wake phrase"
    new = (new_label or "").strip() or DEFAULT_BOOT_WAKE_LABEL
    if old.lower() == new.lower():
        return ""
    return f"Wake phrase updated from {old} to {new}. Say {new} when you need me."


@dataclass
class AmbientResult:
    activated: bool
    phrase: str | None
    text: str | None
    wake_backend: str | None = None
    listen: dict[str, Any] | None = None
    event_id: str | None = None
    stream_id: str | None = None
    partials_emitted: int = 0
    final: bool = True
    partial: bool = False
    # B121/B122: conversation session fields
    conversation_id: str | None = None
    turn: int | None = None
    # When True, complete_after_wake already dual-wrote HEP; outer loop skips.
    skip_emit: bool = False
    # Optional kind override for ambient_event_line (e.g. ambient.conversation_end)
    kind: str | None = None


def _safe_text(value: Any) -> str | None:
    """Render strict-UTF-8 text without letting hostile objects escape."""
    if value is None:
        return None
    try:
        rendered = str(value)
        if not isinstance(rendered, str):
            return None
        text = _sanitize_utf8_text(str.strip(rendered))
    except BaseException:
        return None
    return text or None


def _safe_bounded_text(value: Any, *, max_chars: int) -> str | None:
    """Render bounded strict-UTF-8 text for event and log fields."""
    text = _safe_text(value)
    return text[:max_chars] if text else None


def _sanitize_utf8_text(text: str) -> str:
    """Replace lone surrogates while preserving all valid Unicode verbatim."""
    try:
        text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return "".join(
            "\N{REPLACEMENT CHARACTER}" if 0xD800 <= ord(char) <= 0xDFFF else char
            for char in text
        )
    return text


def _safe_exception_type_name(exc: BaseException) -> str:
    """Return a bounded exception type name that itself cannot raise."""
    try:
        name = _safe_bounded_text(
            type(exc).__name__, max_chars=_EXCEPTION_TYPE_MAX_CHARS
        )
        if name:
            return name
    except BaseException:
        pass
    return "Exception"


def _safe_exception_details(exc: BaseException) -> tuple[str | None, str, str]:
    """Return rendered text, display fallback, and type without conflating them.

    Only successfully rendered exception text is suitable for semantic
    classification.  The type name is a display fallback, not evidence about
    the failure itself: hostile exception classes can control both surfaces.
    """
    error_type = _safe_exception_type_name(exc)
    rendered = _safe_text(exc)
    return rendered, rendered or error_type, error_type


def _apply_policy_to_backend(backend: WakeBackend, policy: WakePolicy) -> None:
    if hasattr(backend, "rebuild_keywords"):
        # Sherpa KWS: rebuild keyword graph from names/phrases (no process restart)
        backend.rebuild_keywords(policy)  # type: ignore[attr-defined]
        return
    if hasattr(backend, "policy"):
        backend.policy = policy  # type: ignore[attr-defined]
    if hasattr(backend, "phrases"):
        backend.phrases = policy.display_phrases()  # type: ignore[attr-defined]


def _maybe_learn_from_miss(
    miss: NearMiss,
    *,
    policy: WakePolicy,
    learned: LearnedWake | None,
    out: TextIO | None,
) -> tuple[WakePolicy, LearnedWake | None]:
    """Persist a learned alias from a near-miss; hot-apply without restart."""
    suggestion = suggest_learn_from_near_miss(miss, policy)
    if suggestion is None:
        return policy, learned
    kind, value, canonical = suggestion
    if kind == "name" and canonical:
        state, changed = learn_name_alias(value, canonical, learned=learned)
        if not changed:
            return policy, state
        new_pol = policy.merge_learned(name_aliases={value: canonical})
        if out is not None:
            ev = learned_event(
                kind="name",
                value=value,
                canonical=canonical,
                mode=new_pol.normalized_mode(),
                total_name_aliases=len(state.name_aliases),
                total_phrase_aliases=len(state.phrase_aliases),
            )
            emit_hep(ev, out)
        syslog(
            "ambient.wake_learned",
            component="ambient",
            learn_kind="name",
            value=value,
            canonical=canonical,
        )
        return new_pol, state
    if kind == "phrase":
        state, changed = learn_phrase_alias(value, learned=learned)
        if not changed:
            return policy, state
        new_pol = policy.merge_learned(phrase_aliases=[value])
        if out is not None:
            ev = learned_event(
                kind="phrase",
                value=value,
                canonical=None,
                mode=new_pol.normalized_mode(),
                total_name_aliases=len(state.name_aliases),
                total_phrase_aliases=len(state.phrase_aliases),
            )
            emit_hep(ev, out)
        syslog(
            "ambient.wake_learned",
            component="ambient",
            learn_kind="phrase",
            value=value,
        )
        return new_pol, state
    return policy, learned


def _wait_for_wake(
    backend: WakeBackend,
    *,
    snippet_s: float,
    deadline: float,
    out: TextIO | None = None,
    debug_every_s: float = 15.0,
    debug_save: bool = False,
    debug_retention_days: float = 7.0,
    near_miss_acc: NearMissAccumulator | None = None,
    phrases: list[str] | tuple[str, ...] | None = None,
    policy: WakePolicy | None = None,
    learned: LearnedWake | None = None,
    hop_s: float | None = None,
    ring_s: float = 5.0,
) -> WakeHit | None:
    """Score overlapping windows from a continuous mic stream until wake/deadline.

    Holds :class:`ContinuousMicStream` (MicLease + InputStream + ring) for the
    whole ambient arm so the OS mic indicator stays steady. Overlapping score
    windows use hop < snippet so greeting+name rarely splits across cuts.
    Yields cleanly when ``ambient.pause`` is set (answer/ask) or on shutdown.

    Plausible failed activations are grouped and emitted as
    ``ambient.wake_near_miss`` on *out* (see NearMissAccumulator schedule).
    Near-misses may expand learned aliases immediately (no restart).
    """
    snippet, hop = score_window_plan(snippet_s, hop_s)
    ring_capacity = max(float(ring_s), snippet + 0.5)
    last_debug = 0.0
    snips_since_purge = 0
    acc = near_miss_acc
    pol = policy or getattr(backend, "policy", None) or default_wake_policy()
    learned_state = learned
    phrase_list = list(
        phrases
        if phrases is not None
        else getattr(backend, "phrases", None) or pol.display_phrases()
    )
    stream: ContinuousMicStream | None = None
    # First open fills a full snippet; later ticks only need hop of new audio
    need_fill_s = snippet

    def _should_yield() -> bool:
        return (
            ambient_pause_requested()
            or shutdown_requested()
            or reload_requested()
            or time.monotonic() >= deadline
        )

    def _close_stream() -> None:
        nonlocal stream, need_fill_s
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
            stream = None
        need_fill_s = snippet

    try:
        while time.monotonic() < deadline:
            if shutdown_requested():
                return None
            # SIGHUP / config file-watch: exit wait so the loop can re-read config
            if reload_requested():
                return None
            # Bound listen/ask requested the mic — release continuous hold
            if ambient_pause_requested():
                _close_stream()
                time.sleep(0.05)
                continue

            if stream is None:
                try:
                    stream = ContinuousMicStream(
                        sample_rate=16000,
                        ring_s=ring_capacity,
                        lease_name="ambient",
                    )
                    stream.open()
                    need_fill_s = snippet
                except MicBusyError:
                    # Another process holds mic — wait and retry
                    stream = None
                    time.sleep(0.1)
                    continue
                except Exception as exc:
                    stream = None
                    if out is not None:
                        err = {
                            "schema": "hark.event.v1",
                            "kind": "ambient.error",
                            "event_id": new_event_id(),
                            "observed_at": utc_now_iso(),
                            "error": f"record: {exc}",
                        }
                        emit_hep(err, out)
                    time.sleep(0.5)
                    continue

            try:
                ok = stream.read_for(need_fill_s, should_stop=_should_yield)
            except Exception as exc:
                _close_stream()
                if out is not None:
                    err = {
                        "schema": "hark.event.v1",
                        "kind": "ambient.error",
                        "event_id": new_event_id(),
                        "observed_at": utc_now_iso(),
                        "error": f"record: {exc}",
                    }
                    emit_hep(err, out)
                time.sleep(0.5)
                continue

            if not ok:
                if ambient_pause_requested():
                    _close_stream()
                    continue
                if shutdown_requested() or reload_requested():
                    return None
                # deadline hit mid-read
                if time.monotonic() >= deadline:
                    return None
                continue

            # Subsequent ticks: hop of new audio, score last snippet window
            need_fill_s = hop
            if stream.available_s + 1e-9 < snippet * 0.9:
                # Still priming after a partial read — gather more
                need_fill_s = max(hop, snippet - stream.available_s)
                continue

            pcm = stream.window_pcm16(snippet)
            hit = backend.score_snippet(pcm, 16000)
            text = getattr(backend, "last_text", "") or ""
            rms = float(getattr(backend, "last_rms", 0.0) or 0.0)

            # Dev: keep audio+transcript for scored snippets (hits and misses)
            if debug_save and (hit is not None or text):
                save_wake_snippet(
                    pcm16=pcm,
                    sample_rate=16000,
                    text=text or None,
                    matched=hit is not None,
                    phrase=hit.phrase if hit else None,
                    rms=rms,
                    backend=getattr(backend, "name", None),
                    enabled=True,
                )
                snips_since_purge += 1
                if snips_since_purge >= 20:
                    purge_old_debug_snips(retention_days=debug_retention_days)
                    snips_since_purge = 0

            if hit is not None:
                if acc is not None:
                    acc.reset_pending()
                syslog(
                    "ambient.wake",
                    component="ambient",
                    message=hit.phrase,
                    phrase=hit.phrase,
                    raw=hit.raw,
                    remainder=hit.remainder,
                    backend=hit.backend,
                    confidence=hit.confidence,
                    snippet_s=snippet,
                    hop_s=hop,
                )
                return hit

            # Hot-reload learned aliases only when the on-disk file changes.
            # Applying every hop would thrash Sherpa KWS (rebuild_keywords →
            # null KeywordSpotter + full ONNX reload) under continuous ambient.
            prev_learned = learned_state
            learned_state = load_learned_if_changed(learned_state)
            if (
                pol.learn
                and learned_state is not None
                and learned_state is not prev_learned
            ):
                pol = pol.merge_learned(
                    name_aliases=learned_state.name_aliases,
                    phrase_aliases=learned_state.phrase_aliases,
                )
                _apply_policy_to_backend(backend, pol)
                phrase_list = pol.display_phrases()

            # Plausible failed wake → handsfree monitor (grouped), never spam on noise
            if acc is not None and text:
                miss = plausible_near_miss(text, phrase_list, policy=pol)
                if miss is not None:
                    # Dynamically expand alternates (names or full phrases) — no restart
                    pol, learned_state = _maybe_learn_from_miss(
                        miss, policy=pol, learned=learned_state, out=out
                    )
                    _apply_policy_to_backend(backend, pol)
                    phrase_list = pol.display_phrases()
                    group = acc.add(miss)
                    if group is not None:
                        ev = make_wake_near_miss_event(
                            group,
                            total_near_misses=acc.total,
                            group_index=acc.group_index,
                            phrases=phrase_list,
                        )
                        if out is not None:
                            emit_hep(ev, out)
                        syslog(
                            "ambient.wake_near_miss",
                            component="ambient",
                            level="info",
                            message=f"{len(group)} near-miss(es)",
                            count=len(group),
                            total_near_misses=acc.total,
                            attempts=[m.text for m in group],
                        )

            now = time.monotonic()
            if now - last_debug >= debug_every_s:
                last_debug = now
                dbg = {
                    "schema": "hark.event.v1",
                    "kind": "ambient.debug",
                    "event_id": new_event_id(),
                    "observed_at": utc_now_iso(),
                    "rms": round(rms, 5),
                    "last_text": text or None,
                    "scored": getattr(backend, "snippets_scored", None),
                    "skipped_quiet": getattr(backend, "snippets_skipped_quiet", None),
                    "snippet_s": snippet,
                    "hop_s": hop,
                    "ring_s": round(stream.available_s, 3),
                }
                if out is not None:
                    # Debug heartbeats: stdout only — do not dual-write (noisy).
                    emit_hep(dbg, out, dual_write=False)
                syslog(
                    "ambient.debug",
                    component="ambient",
                    level="debug",
                    message=text or "quiet",
                    rms=dbg["rms"],
                    last_text=dbg["last_text"],
                    scored=dbg["scored"],
                    skipped_quiet=dbg["skipped_quiet"],
                )
                # B120/B123: near-zero RMS often means Pulse stuck Mute:yes after
                # half-duplex TTS. Lightweight self-heal on the debug cadence.
                if rms < 0.002:
                    try:
                        from hark.audio.mic_mute import (
                            default_source,
                            ensure_unmuted,
                            source_is_muted,
                        )

                        src = default_source()
                        if src and source_is_muted(src) is True:
                            ensure_unmuted(source=src)
                            syslog(
                                "ambient.mute_self_heal",
                                component="ambient",
                                level="warn",
                                message="Pulse source muted with near-zero RMS; unmuted",
                                source=src,
                                rms=round(rms, 5),
                            )
                    except Exception:
                        pass
        return None
    finally:
        _close_stream()


def _emit_ambient_hep(
    ev: dict[str, Any],
    *,
    out: TextIO | None,
    on_partial: Any | None = None,
    syslog_kind: str | None = None,
) -> None:
    """Dual-write ambient HEP (stdout/out + ambient.jsonl) and optional syslog."""
    if out is not None:
        emit_hep(ev, out)
    if on_partial is not None:
        on_partial(ev)
    kind = syslog_kind or str(ev.get("kind") or "ambient.event")
    syslog(
        kind,
        component="ambient",
        level="info",
        stream_id=ev.get("stream_id"),
        conversation_id=ev.get("conversation_id"),
        turn=ev.get("turn"),
        seq=ev.get("seq"),
        text=(ev.get("text") or "")[:300],
        partial=ev.get("partial"),
        final=ev.get("final"),
        phrase=ev.get("phrase"),
    )


def _single_post_wake_listen(
    cfg: HarkConfig,
    hit: WakeHit,
    *,
    on_partial: Any | None = None,
    out: TextIO | None = None,
) -> AmbientResult:
    """Classic one-shot post-wake listen (radio/silence) → ambient.prompt."""
    event_id = new_event_id()
    stream_id = new_stream_id()

    def _emit_partial(ev: dict[str, Any]) -> None:
        ev = {**ev, "phrase": hit.phrase}
        _emit_ambient_hep(
            ev, out=out, on_partial=on_partial, syslog_kind="ambient.partial"
        )

    from hark.answer_window.policy import policy_from_config

    pw_policy = policy_from_config(
        cfg,
        "post_wake",
        stream_id=stream_id,
        partial_kind="ambient.partial",
    )

    syslog(
        "ambient.post_wake_listen",
        component="ambient",
        level="info",
        phrase=hit.phrase,
        wake_backend=hit.backend,
        stream_id=stream_id,
        abs_open_db=float(pw_policy.abs_open_db),
        initial_timeout_s=float(pw_policy.initial_timeout_s),
        lead_in_ms=int(pw_policy.lead_in_ms),
        arm_cue=bool(pw_policy.arm_cue),
        streaming=False,
    )

    try:
        listened = run_listen(
            cfg,
            profile="post_wake",
            end_mode=cfg.listen.end_mode,
            on_partial=_emit_partial if cfg.listen.end_mode == "radio" else None,
            stream_id=stream_id,
            partial_kind="ambient.partial",
        )
    except Exception as exc:
        rendered_error, error_text, _error_type = _safe_exception_details(exc)
        err_s = error_text[:_EXCEPTION_DETAIL_MAX_CHARS]
        is_no_open = rendered_error is not None and (
            "no speech detected" in rendered_error.lower()
            or "no speech captured" in rendered_error.lower()
        )
        syslog(
            "ambient.error",
            component="ambient",
            level="error",
            message=err_s[:300],
            phrase=hit.phrase,
            wake_backend=hit.backend,
            stream_id=stream_id,
            event_id=event_id,
            reason="no_open" if is_no_open else "listen_failed",
            listen_error=err_s[:240],
            abs_open_db=float(pw_policy.abs_open_db),
            initial_timeout_s=float(pw_policy.initial_timeout_s),
        )
        return AmbientResult(
            activated=True,
            phrase=hit.phrase,
            text=None,
            wake_backend=hit.backend,
            listen={
                "error": err_s,
                "reason": "no_open" if is_no_open else "listen_failed",
                "abs_open_db": float(pw_policy.abs_open_db),
                "initial_timeout_s": float(pw_policy.initial_timeout_s),
            },
            event_id=event_id,
            stream_id=stream_id,
            final=True,
            partial=False,
        )

    if listened.cancelled:
        end_phrase = listened.end_phrase
        listen_meta: dict[str, Any] = {
            "provider": listened.provider,
            "duration_ms": listened.duration_ms,
            "end_phrase": end_phrase,
            "cancelled": True,
        }
        # B124: config reload mid post-wake/radio listen — channel drop, not operator cancel
        if end_phrase in ("reload", "config_reload"):
            listen_meta["reason"] = "config_reload"
            listen_meta["listen_aborted"] = True
        return AmbientResult(
            activated=True,
            phrase=hit.phrase,
            text=listened.text,
            wake_backend=hit.backend,
            listen=listen_meta,
            event_id=event_id,
            stream_id=listened.stream_id or stream_id,
            partials_emitted=listened.partials_emitted,
            final=True,
            partial=False,
        )

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
        stream_id=listened.stream_id or stream_id,
        partials_emitted=listened.partials_emitted,
        final=True,
        partial=False,
    )


def _conversation_after_wake(
    cfg: HarkConfig,
    hit: WakeHit,
    *,
    on_partial: Any | None = None,
    out: TextIO | None = None,
) -> AmbientResult:
    """B121/B122: open post-wake conversation — turns on quiet; no re-wake.

    After the first wake, re-open silence-mode listens without waiting for
    iris/hark again until cancel, product end-phrase finalize, conversation
    idle, shutdown, or config reload.
    """
    from hark.answer_window.policy import policy_from_config

    amb = cfg.ambient
    conversation_id = new_stream_id()
    ack_quiet = float(getattr(amb, "streaming_ack_min_quiet_s", 2.0) or 2.0)
    conv_idle = float(getattr(amb, "streaming_conversation_idle_s", 45.0) or 45.0)
    end_silence = float(getattr(cfg.listen, "end_silence_s", 2.1) or 2.1)
    # Turn quiet ≈ max(ack quiet, end_silence) so turn-taking matches B105 gate.
    turn_silence = max(end_silence, ack_quiet)
    end_phrases = tuple(getattr(cfg.listen, "end_phrases", None) or DEFAULT_END_PHRASES)
    cancel_phrases = tuple(
        getattr(cfg.listen, "cancel_phrases", None) or DEFAULT_CANCEL_PHRASES
    )

    syslog(
        "ambient.conversation_start",
        component="ambient",
        level="info",
        phrase=hit.phrase,
        wake_backend=hit.backend,
        conversation_id=conversation_id,
        streaming_ack_min_quiet_s=ack_quiet,
        streaming_conversation_idle_s=conv_idle,
        turn_silence_s=turn_silence,
    )

    turn = 0
    last_text: str | None = None
    last_stream_id: str | None = None
    last_event_id: str | None = None
    last_listen: dict[str, Any] | None = None
    last_partials = 0

    while not shutdown_requested() and not reload_requested():
        stream_id = new_stream_id()
        is_first = turn == 0
        # First turn: post_wake gate timeout; later turns: conversation idle.
        gate_timeout = (
            float(
                getattr(amb, "post_wake_timeout_s", None)
                or getattr(cfg.listen, "initial_timeout_s", 45.0)
                or 45.0
            )
            if is_first
            else conv_idle
        )
        pol = policy_from_config(
            cfg,
            "post_wake",
            stream_id=stream_id,
            end_mode="silence",
            streaming=True,
            streaming_ack_min_quiet_s=ack_quiet,
            end_silence_s=turn_silence,
            initial_timeout_s=gate_timeout,
            # First turn may nudge; later turns just end the conversation.
            no_open_retry=bool(is_first and getattr(cfg.listen, "no_open_retry", True)),
            no_open_nudge=bool(
                is_first and getattr(amb, "post_wake_no_open_nudge", True)
            ),
            # Re-arm cue each turn so operator hears the channel is still open.
            arm_cue=bool(getattr(amb, "post_wake_arm_cue", True)),
            lead_in_ms=(
                int(getattr(amb, "post_wake_lead_in_ms", 150) or 0) if is_first else 50
            ),
        )

        syslog(
            "ambient.post_wake_listen",
            component="ambient",
            level="info",
            phrase=hit.phrase,
            wake_backend=hit.backend,
            stream_id=stream_id,
            conversation_id=conversation_id,
            turn=turn + 1,
            abs_open_db=float(pol.abs_open_db),
            initial_timeout_s=float(pol.initial_timeout_s),
            lead_in_ms=int(pol.lead_in_ms),
            arm_cue=bool(pol.arm_cue),
            streaming=True,
            end_mode="silence",
        )

        try:
            listened = run_listen(cfg, policy=pol)
        except Exception as exc:
            rendered_error, error_text, error_type = _safe_exception_details(exc)
            err_s = error_text[:_EXCEPTION_DETAIL_MAX_CHARS]
            is_no_open = rendered_error is not None and (
                "no speech detected" in rendered_error.lower()
                or "no speech captured" in rendered_error.lower()
            )
            if is_first:
                event_id = new_event_id()
                syslog(
                    "ambient.error",
                    component="ambient",
                    level="error",
                    message=err_s[:300],
                    phrase=hit.phrase,
                    wake_backend=hit.backend,
                    stream_id=stream_id,
                    conversation_id=conversation_id,
                    event_id=event_id,
                    reason="no_open" if is_no_open else "listen_failed",
                    listen_error=err_s[:240],
                )
                return AmbientResult(
                    activated=True,
                    phrase=hit.phrase,
                    text=None,
                    wake_backend=hit.backend,
                    listen={
                        "error": err_s,
                        "reason": "no_open" if is_no_open else "listen_failed",
                        "abs_open_db": float(pol.abs_open_db),
                        "initial_timeout_s": float(pol.initial_timeout_s),
                        "conversation_id": conversation_id,
                    },
                    event_id=event_id,
                    stream_id=stream_id,
                    conversation_id=conversation_id,
                    final=True,
                    partial=False,
                )
            # Later turns only become idle for the timeout raised when the
            # silence gate genuinely expires.  A provider, microphone, or
            # internal failure must remain distinguishable to operators and
            # HEP consumers.
            is_conversation_idle = isinstance(exc, TimeoutError) and is_no_open
            if isinstance(exc, ProviderError):
                end_reason = "listen_provider_error"
            elif isinstance(exc, MicBusyError):
                end_reason = "listen_mic_busy"
            elif is_conversation_idle:
                end_reason = "conversation_idle"
            else:
                end_reason = "listen_failed"
            error_detail = err_s
            last_text_detail = _safe_bounded_text(
                last_text, max_chars=_LAST_SUCCESS_TEXT_MAX_CHARS
            )
            last_provider_detail = _safe_bounded_text(
                (last_listen or {}).get("provider"),
                max_chars=_LAST_SUCCESS_PROVIDER_MAX_CHARS,
            )
            failure_stream_detail = _safe_bounded_text(
                stream_id, max_chars=_EVENT_ID_MAX_CHARS
            )
            last_event_detail = _safe_bounded_text(
                last_event_id, max_chars=_EVENT_ID_MAX_CHARS
            )
            last_stream_detail = _safe_bounded_text(
                last_stream_id, max_chars=_EVENT_ID_MAX_CHARS
            )

            if is_conversation_idle:
                syslog(
                    "ambient.conversation_end",
                    component="ambient",
                    level="info",
                    conversation_id=conversation_id,
                    reason=end_reason,
                    turns=turn,
                    idle_s=conv_idle,
                )
            else:
                syslog(
                    "ambient.error",
                    component="ambient",
                    level="error",
                    message=error_detail,
                    conversation_id=conversation_id,
                    stream_id=failure_stream_detail,
                    reason=end_reason,
                    listen_error=error_detail,
                    error_type=error_type,
                    turns=turn,
                    last_event_id=last_event_detail,
                    last_stream_id=last_stream_detail,
                    last_turn=turn,
                    last_text=last_text_detail,
                    last_provider=last_provider_detail,
                )
            end_ev = {
                "schema": "hark.event.v1",
                "kind": "ambient.conversation_end",
                "event_id": new_event_id(),
                "observed_at": utc_now_iso(),
                "conversation_id": conversation_id,
                "turns": turn,
                "phrase": hit.phrase,
                "reason": end_reason,
                "partial": False,
                "final": True,
                "streaming": True,
                "instructions": (
                    (
                        "Conversation session ended (idle). Wake is re-armed; "
                        "operator must say the wake name for a new session. "
                        "No new operator prompt on this event."
                    )
                    if is_conversation_idle
                    else (
                        f"Conversation session ended after listen failure "
                        f"({end_reason}). Wake is re-armed; operator must say "
                        "the wake name for a new session."
                    )
                ),
            }
            if not is_conversation_idle:
                end_ev.update(
                    {
                        "listen_error": error_detail,
                        "error_type": error_type,
                        "failure_stream_id": failure_stream_detail,
                        "last_event_id": last_event_detail,
                        "last_stream_id": last_stream_detail,
                        "last_turn": turn,
                        "last_text": last_text_detail,
                        "last_provider": last_provider_detail,
                    }
                )
            _emit_ambient_hep(end_ev, out=out, syslog_kind="ambient.conversation_end")
            listen_result = {
                **(last_listen or {}),
                "reason": end_reason,
                "turns": turn,
                "conversation_id": conversation_id,
            }
            if not is_conversation_idle:
                listen_result.update(
                    {
                        "error": error_detail,
                        "error_type": error_type,
                        "failure_stream_id": stream_id,
                    }
                )
            return AmbientResult(
                activated=True,
                phrase=hit.phrase,
                text=last_text,
                wake_backend=hit.backend,
                listen=listen_result,
                event_id=last_event_id or end_ev["event_id"],
                stream_id=last_stream_id or stream_id,
                conversation_id=conversation_id,
                turn=turn or None,
                partials_emitted=last_partials,
                final=True,
                partial=False,
                skip_emit=True,
                kind="ambient.conversation_end",
            )

        sid = listened.stream_id or stream_id
        body = (listened.text or "").strip()
        event_stream_id = _safe_bounded_text(sid, max_chars=_EVENT_ID_MAX_CHARS)
        event_body = _sanitize_utf8_text(body)
        event_provider = _safe_bounded_text(
            listened.provider, max_chars=_LAST_SUCCESS_PROVIDER_MAX_CHARS
        )

        if listened.cancelled:
            event_id = new_event_id()
            cancel_ev = {
                "schema": "hark.event.v1",
                "kind": "ambient.cancelled",
                "event_id": event_id,
                "observed_at": utc_now_iso(),
                "stream_id": event_stream_id,
                "conversation_id": conversation_id,
                "turn": turn + 1,
                "text": event_body or None,
                "phrase": hit.phrase,
                "partial": False,
                "final": True,
                "streaming": True,
                "instructions": (
                    "Conversation cancelled — ignore prior turns for this "
                    "conversation_id. Wake re-arms."
                ),
            }
            _emit_ambient_hep(cancel_ev, out=out, syslog_kind="ambient.cancelled")
            return AmbientResult(
                activated=True,
                phrase=hit.phrase,
                text=body or None,
                wake_backend=hit.backend,
                listen={
                    "provider": listened.provider,
                    "duration_ms": listened.duration_ms,
                    "end_phrase": listened.end_phrase,
                    "cancelled": True,
                    "conversation_id": conversation_id,
                    "turns": turn,
                },
                event_id=event_id,
                stream_id=sid,
                conversation_id=conversation_id,
                turn=turn + 1,
                partials_emitted=listened.partials_emitted,
                final=True,
                partial=False,
                skip_emit=True,
                kind="ambient.cancelled",
            )

        if not body:
            if is_first:
                event_id = new_event_id()
                return AmbientResult(
                    activated=True,
                    phrase=hit.phrase,
                    text=None,
                    wake_backend=hit.backend,
                    listen={
                        "error": "empty transcript",
                        "reason": "empty_stt",
                        "conversation_id": conversation_id,
                    },
                    event_id=event_id,
                    stream_id=sid,
                    conversation_id=conversation_id,
                    final=True,
                    partial=False,
                )
            break

        # Cancel / product end on the turn transcript (silence path has no radio eval).
        # Soft ends do NOT force session finalize — quiet already ended the turn.
        hit_phrase = evaluate_radio_transcript(
            body,
            end_phrases=end_phrases,
            cancel_phrases=cancel_phrases,
            soft_end_phrases=(),
            soft_end_phrases_enabled=False,
        )
        if hit_phrase is not None and hit_phrase.kind == "cancel":
            event_id = new_event_id()
            cancel_ev = {
                "schema": "hark.event.v1",
                "kind": "ambient.cancelled",
                "event_id": event_id,
                "observed_at": utc_now_iso(),
                "stream_id": event_stream_id,
                "conversation_id": conversation_id,
                "turn": turn + 1,
                "text": _sanitize_utf8_text(hit_phrase.body or body),
                "phrase": hit.phrase,
                "end_phrase": hit_phrase.phrase,
                "partial": False,
                "final": True,
                "streaming": True,
                "instructions": (
                    "Conversation cancelled by operator phrase. Wake re-arms."
                ),
            }
            _emit_ambient_hep(cancel_ev, out=out, syslog_kind="ambient.cancelled")
            return AmbientResult(
                activated=True,
                phrase=hit.phrase,
                text=hit_phrase.body or body,
                wake_backend=hit.backend,
                listen={
                    "provider": listened.provider,
                    "duration_ms": listened.duration_ms,
                    "end_phrase": hit_phrase.phrase,
                    "cancelled": True,
                    "conversation_id": conversation_id,
                    "turns": turn,
                },
                event_id=event_id,
                stream_id=sid,
                conversation_id=conversation_id,
                turn=turn + 1,
                partials_emitted=listened.partials_emitted,
                final=True,
                partial=False,
                skip_emit=True,
                kind="ambient.cancelled",
            )

        turn_text = body
        if hit_phrase is not None and hit_phrase.kind == "end" and hit_phrase.body:
            turn_text = hit_phrase.body.strip() or body

        turn += 1
        event_id = new_event_id()
        listen_meta = {
            "provider": listened.provider,
            "duration_ms": listened.duration_ms,
            "end_phrase": (
                hit_phrase.phrase
                if hit_phrase is not None and hit_phrase.kind == "end"
                else listened.end_phrase or "turn_quiet"
            ),
            "cancelled": False,
            "conversation_id": conversation_id,
            "turn": turn,
        }
        event_listen_meta = {
            **listen_meta,
            "provider": event_provider,
            "end_phrase": _safe_bounded_text(
                listen_meta["end_phrase"],
                max_chars=_LAST_SUCCESS_PROVIDER_MAX_CHARS,
            ),
        }
        last_text = turn_text
        last_stream_id = sid
        last_event_id = event_id
        last_listen = listen_meta
        last_partials = listened.partials_emitted

        # Explicit product end → ambient.prompt final + end conversation.
        if hit_phrase is not None and hit_phrase.kind == "end":
            final_ev = {
                "schema": "hark.event.v1",
                "kind": "ambient.prompt",
                "event_id": event_id,
                "observed_at": utc_now_iso(),
                "stream_id": event_stream_id,
                "conversation_id": conversation_id,
                "turn": turn,
                "text": _sanitize_utf8_text(turn_text),
                "phrase": hit.phrase,
                "provider": event_provider,
                "end_phrase": _safe_bounded_text(
                    hit_phrase.phrase,
                    max_chars=_LAST_SUCCESS_PROVIDER_MAX_CHARS,
                ),
                "partial": False,
                "final": True,
                "streaming": True,
                "conversation": True,
                "listen": event_listen_meta,
                "partials_emitted": listened.partials_emitted,
                "instructions": (
                    "FINAL conversation turn (explicit end phrase). "
                    "Reply with hark tts. Session ends; wake re-arms. "
                    "Not bound to a pane unless they ask."
                ),
            }
            _emit_ambient_hep(final_ev, out=out, syslog_kind="ambient.prompt")
            syslog(
                "ambient.conversation_end",
                component="ambient",
                level="info",
                conversation_id=conversation_id,
                reason="end_phrase",
                turns=turn,
                end_phrase=hit_phrase.phrase,
            )
            return AmbientResult(
                activated=True,
                phrase=hit.phrase,
                text=turn_text,
                wake_backend=hit.backend,
                listen=listen_meta,
                event_id=event_id,
                stream_id=sid,
                conversation_id=conversation_id,
                turn=turn,
                partials_emitted=listened.partials_emitted,
                final=True,
                partial=False,
                skip_emit=True,
                kind="ambient.prompt",
            )

        # Normal quiet-ended turn — full reply OK; stay in conversation.
        turn_ev = make_turn_event(
            stream_id=event_stream_id or "",
            text=_sanitize_utf8_text(turn_text),
            conversation_id=conversation_id,
            turn=turn,
            provider=event_provider,
            phrase=hit.phrase,
            event_id=event_id,
            ack_min_quiet_s=ack_quiet,
            listen=event_listen_meta,
        )
        _emit_ambient_hep(
            turn_ev, out=out, on_partial=on_partial, syslog_kind="ambient.turn"
        )

        if shutdown_requested() or reload_requested():
            break

    # Loop exit: idle after empty body, shutdown, or reload.
    reason = (
        "shutdown"
        if shutdown_requested()
        else ("reload" if reload_requested() else "conversation_idle")
    )
    end_ev = {
        "schema": "hark.event.v1",
        "kind": "ambient.conversation_end",
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "conversation_id": conversation_id,
        "turns": turn,
        "phrase": hit.phrase,
        "reason": reason,
        "partial": False,
        "final": True,
        "streaming": True,
        "instructions": (
            f"Conversation session ended ({reason}). Wake is re-armed; "
            "operator must say the wake name for a new session."
        ),
    }
    _emit_ambient_hep(end_ev, out=out, syslog_kind="ambient.conversation_end")
    return AmbientResult(
        activated=True,
        phrase=hit.phrase,
        text=last_text,
        wake_backend=hit.backend,
        listen={
            **(last_listen or {}),
            "reason": reason,
            "turns": turn,
            "conversation_id": conversation_id,
        },
        event_id=last_event_id or end_ev["event_id"],
        stream_id=last_stream_id,
        conversation_id=conversation_id,
        turn=turn or None,
        partials_emitted=last_partials,
        final=True,
        partial=False,
        skip_emit=True,
        kind="ambient.conversation_end",
    )


def complete_after_wake(
    cfg: HarkConfig,
    hit: WakeHit,
    *,
    announce: bool = True,
    on_partial: Any | None = None,
    out: TextIO | None = None,
) -> AmbientResult:
    """After wake: optional lead-in + arm cue → energy-gated cloud listen.

    No spoken 'okay' / 'listening' by default. Post-wake settle, softer/configurable
    open threshold, and no-open retry/nudge are driven by ``[ambient]`` /
    ``[listen]`` (B031).

    Classic (``[ambient].streaming = false``): one radio/silence capture →
    ambient.prompt, then re-arm wake.

    Conversation (``streaming = true``, B121/B122): after first wake, stay in an
    open post-wake session — quiet ends a *turn* (ambient.turn; full TTS OK);
    no re-saying iris/hark until cancel, product end phrase, long idle, or
    shutdown/reload.
    """
    del announce
    if bool(getattr(cfg.ambient, "streaming", False)):
        return _conversation_after_wake(cfg, hit, on_partial=on_partial, out=out)
    return _single_post_wake_listen(cfg, hit, on_partial=on_partial, out=out)


def _wake_deadline(timeout_s: float | None, amb_timeout_s: float | None) -> float:
    """Monotonic deadline for a wake wait.

    ``timeout_s`` (call arg) overrides config ``amb_timeout_s``. Values
    ``None`` fall through to the other / default 300s. ``0`` (or negative)
    means wait indefinitely — no ambient.timeout cycle (continuous handsfree
    can use this, or one-shot with an explicit hang-until-wake).
    """
    if timeout_s is not None:
        effective = float(timeout_s)
    elif amb_timeout_s is not None:
        effective = float(amb_timeout_s)
    else:
        effective = 300.0
    if effective <= 0:
        return float("inf")
    return time.monotonic() + effective


def run_ambient(
    cfg: HarkConfig,
    *,
    once: bool = True,
    timeout_s: float | None = None,
    announce: bool = True,
    backend: WakeBackend | None = None,
    out: TextIO | None = None,
    near_miss_acc: NearMissAccumulator | None = None,
) -> AmbientResult:
    """One wake→prompt cycle. Pass backend= to avoid reloading vosk each time."""
    amb = cfg.ambient
    if not amb.enabled and not once:
        return AmbientResult(activated=False, phrase=None, text=None)

    policy = amb.wake_policy or default_wake_policy()
    if isinstance(policy, WakePolicy) and amb.learn_from_near_misses:
        learned0 = load_learned()
        policy = policy.merge_learned(
            name_aliases=learned0.name_aliases,
            phrase_aliases=learned0.phrase_aliases,
        )
    else:
        learned0 = None

    if backend is None:
        eng = (amb.engine or "").lower()
        if not amb.model_path and eng == "vosk":
            raise RuntimeError(
                "ambient.engine=vosk requires model_path — run ./scripts/setup-ambient.sh"
            )
        if not amb.model_path and eng in ("sherpa_kws", "sherpa", "kws"):
            raise RuntimeError(
                "ambient.engine=sherpa_kws requires model_path — "
                "run ./scripts/download-sherpa-kws-model.sh"
            )
        backend = build_wake_backend(
            amb.engine,
            phrases=amb.activation_phrases,
            model_path=amb.model_path,
            policy=policy if isinstance(policy, WakePolicy) else None,
        )
    else:
        if isinstance(policy, WakePolicy):
            _apply_policy_to_backend(backend, policy)

    deadline = _wake_deadline(timeout_s, amb.timeout_s)

    # Continuous MicLease + ring held inside _wait_for_wake for the whole arm;
    # released on wake hit, pause (answer/ask), reload, or deadline.
    hit = _wait_for_wake(
        backend,
        snippet_s=amb.snippet_s,
        hop_s=getattr(amb, "snippet_hop_s", None),
        ring_s=float(getattr(amb, "ring_s", 5.0) or 5.0),
        deadline=deadline,
        out=out,
        debug_save=bool(amb.debug),
        debug_retention_days=float(amb.debug_retention_days),
        near_miss_acc=near_miss_acc,
        phrases=amb.activation_phrases,
        policy=policy if isinstance(policy, WakePolicy) else None,
        learned=learned0,
    )

    if hit is None:
        return AmbientResult(
            activated=False,
            phrase=None,
            text=None,
            wake_backend=getattr(backend, "name", None),
        )

    # Mic lease released — cloud listen / TTS may take the mic
    return complete_after_wake(cfg, hit, announce=announce, out=out)


def apply_config_reload(
    cfg: HarkConfig,
    backend: WakeBackend,
) -> tuple[HarkConfig, WakeBackend, dict[str, Any]]:
    """Re-read config from disk and hot-apply ambient wake settings.

    Phrase changes update ``backend.phrases`` in place so vosk keeps its
    loaded model. Engine/model_path changes rebuild the backend.
    """
    path = cfg.path
    prev_wake_label = primary_wake_label(cfg)
    new_cfg = load_config(path)
    # Stay armed while the ambient loop is running
    new_cfg.ambient.enabled = True

    policy = new_cfg.ambient.wake_policy or default_wake_policy()
    if isinstance(policy, WakePolicy) and new_cfg.ambient.learn_from_near_misses:
        learned = load_learned()
        policy = policy.merge_learned(
            name_aliases=learned.name_aliases,
            phrase_aliases=learned.phrase_aliases,
        )
        new_cfg.ambient.wake_policy = policy
    phrases = list(
        policy.display_phrases()
        if isinstance(policy, WakePolicy)
        else new_cfg.ambient.activation_phrases
    )
    new_cfg.ambient.activation_phrases = phrases
    engine_changed = (new_cfg.ambient.engine or "").lower() != (
        cfg.ambient.engine or ""
    ).lower()
    model_changed = new_cfg.ambient.model_path != cfg.ambient.model_path
    new_wake_label = primary_wake_label(new_cfg)
    wake_label_changed = (prev_wake_label or "").strip().lower() != (
        new_wake_label or ""
    ).strip().lower()

    info: dict[str, Any] = {
        "phrases": phrases,
        "wake_mode": getattr(new_cfg.ambient, "wake_mode", None),
        "names": list(getattr(new_cfg.ambient, "names", []) or []),
        "engine": new_cfg.ambient.engine,
        "model_path": new_cfg.ambient.model_path,
        "rebuilt_backend": False,
        "path": str(path) if path else None,
        "end_mode": getattr(new_cfg.listen, "end_mode", None),
        "surface_timeouts": getattr(new_cfg.ambient, "surface_timeouts", None),
        "snippet_s": getattr(new_cfg.ambient, "snippet_s", None),
        "timeout_s": getattr(new_cfg.ambient, "timeout_s", None),
        "wake_label": new_wake_label,
        "wake_label_prev": prev_wake_label,
        "wake_label_changed": wake_label_changed,
    }

    eng = (new_cfg.ambient.engine or "").lower()
    if engine_changed or model_changed:
        if not new_cfg.ambient.model_path and eng == "vosk":
            raise RuntimeError(
                "ambient.engine=vosk requires model_path — run ./scripts/setup-ambient.sh"
            )
        if not new_cfg.ambient.model_path and eng in ("sherpa_kws", "sherpa", "kws"):
            raise RuntimeError(
                "ambient.engine=sherpa_kws requires model_path — "
                "run ./scripts/download-sherpa-kws-model.sh"
            )
        backend = build_wake_backend(
            new_cfg.ambient.engine,
            phrases=phrases,
            model_path=new_cfg.ambient.model_path,
            policy=policy if isinstance(policy, WakePolicy) else None,
        )
        info["rebuilt_backend"] = True
    else:
        if isinstance(policy, WakePolicy):
            _apply_policy_to_backend(backend, policy)
        elif hasattr(backend, "phrases"):
            backend.phrases = phrases

    return new_cfg, backend, info


def ambient_event_line(result: AmbientResult) -> dict[str, Any]:
    if result.kind:
        kind = result.kind
    elif (
        result.activated
        and result.text
        and not (result.listen and result.listen.get("cancelled"))
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

    base = {
        "schema": "hark.event.v1",
        "kind": kind,
        "event_id": result.event_id or new_event_id(),
        "observed_at": utc_now_iso(),
        "phrase": result.phrase,
        "text": result.text,
        "wake_backend": result.wake_backend,
        "listen": result.listen,
        "partial": False,
        "final": True,
        "stream_id": result.stream_id,
        "partials_emitted": result.partials_emitted,
    }
    if result.conversation_id:
        base["conversation_id"] = result.conversation_id
    if result.turn is not None:
        base["turn"] = result.turn
    if kind == "ambient.prompt":
        base["instructions"] = (
            "FINAL operator prompt for this stream_id (supersedes all ambient.partial "
            "events with the same stream_id). You may now respond/act. "
            "Not bound to a pane — use judgment; do not invent answers."
        )
        base["warning"] = None
    elif kind == "ambient.cancelled":
        base["instructions"] = "Cancelled — ignore prior partials for this stream_id."
        # B124: reload-aborted active listen (operator heard stop cue)
        if result.listen and result.listen.get("listen_aborted"):
            base["listen_aborted"] = True
            base["reason"] = result.listen.get("reason") or "config_reload"
            base["instructions"] = (
                "Listen aborted by config reload — channel dropped; "
                "ignore prior partials for this stream_id. "
                "ambient.reloaded will follow with listen_aborted=true."
            )
    elif kind == "ambient.conversation_end":
        base["instructions"] = (
            "Conversation session ended. Wake re-armed; no new prompt on this event."
        )
        base["warning"] = None
    return base


def _emit_reload_event(
    out: TextIO,
    info: dict[str, Any],
    *,
    error: str | None = None,
    source: str | None = None,
    listen_aborted: bool = False,
) -> None:
    src = source or info.get("source") or "unknown"
    if error:
        line = {
            "schema": "hark.event.v1",
            "kind": "ambient.error",
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            "error": f"config reload: {error}",
            "source": src,
            "listen_aborted": bool(listen_aborted),
        }
    else:
        line = {
            "schema": "hark.event.v1",
            "kind": "ambient.reloaded",
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            "phrases": info.get("phrases"),
            "engine": info.get("engine"),
            "model_path": info.get("model_path"),
            "rebuilt_backend": info.get("rebuilt_backend"),
            "path": info.get("path"),
            "end_mode": info.get("end_mode"),
            "surface_timeouts": info.get("surface_timeouts"),
            "wake_label": info.get("wake_label"),
            "wake_label_prev": info.get("wake_label_prev"),
            "wake_label_changed": info.get("wake_label_changed"),
            "source": src,
            # B124: true when reload cut an active post-wake/radio listen
            "listen_aborted": bool(listen_aborted),
            "instructions": (
                "Config reloaded. Activation phrases, listen settings "
                "(e.g. end_mode), and ambient knobs now match disk. "
                "File-watch (default mtime poll on config.toml) and "
                "SIGHUP (kill -HUP <pid>) share this path; full restart "
                "always works. See docs/CUSTOM_WAKE.md."
                + (
                    " Primary wake label changed — ambient spoke the new phrase."
                    if info.get("wake_label_changed")
                    else ""
                )
                + (
                    " An active listen was aborted (stop cue played); "
                    "operator channel dropped mid-capture."
                    if listen_aborted
                    else ""
                )
            ),
        }
    emit_hep(line, out)
    syslog(
        "ambient.reloaded" if not error else "ambient.reload_error",
        component="ambient",
        level="info" if not error else "warn",
        phrases=info.get("phrases") if not error else None,
        error=error,
        rebuilt_backend=info.get("rebuilt_backend") if not error else None,
        source=src,
        end_mode=info.get("end_mode") if not error else None,
        listen_aborted=bool(listen_aborted) if not error else None,
    )


def run_ambient_loop(
    cfg: HarkConfig,
    *,
    out: TextIO | None = None,
    announce: bool = True,
    idle_log_s: float = 60.0,
) -> int:
    """Continuous ambient: load vosk once, wake→prompt→repeat until Ctrl+C/SIGTERM.

    SIGTERM during an active listen does not abort mid-recording: we finish the
    current wake→STT cycle, emit the event, then exit.

    Config reloads (SIGHUP **or** config.toml file-watch) re-read config
    (custom activation phrases, listen end_mode, surface_timeouts, etc.)
    without stopping the process. Phrase-only changes hot-update the backend;
    engine/model changes rebuild it.
    """
    out = out or sys.stdout
    cfg.ambient.enabled = True
    install_signal_handlers()

    # Hardware unmute → OS unmute (Wave button / ALSA → Pulse)
    if getattr(cfg.audio, "sync_hw_unmute", True):
        try:
            from hark.audio.mic_mute import start_mute_sync_watcher

            start_mute_sync_watcher(enabled=True)
        except Exception:
            pass

    eng0 = (cfg.ambient.engine or "").lower()
    if not cfg.ambient.model_path and eng0 == "vosk":
        err = {
            "schema": "hark.event.v1",
            "kind": "ambient.error",
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            "error": "no vosk model_path — run ./scripts/setup-ambient.sh",
        }
        emit_hep(err, out)
        return 1
    if not cfg.ambient.model_path and eng0 in ("sherpa_kws", "sherpa", "kws"):
        err = {
            "schema": "hark.event.v1",
            "kind": "ambient.error",
            "event_id": new_event_id(),
            "observed_at": utc_now_iso(),
            "error": (
                "no sherpa_kws model_path — run ./scripts/download-sherpa-kws-model.sh"
            ),
        }
        emit_hep(err, out)
        return 1

    policy = cfg.ambient.wake_policy or default_wake_policy()
    if isinstance(policy, WakePolicy) and cfg.ambient.learn_from_near_misses:
        learned_boot = load_learned()
        policy = policy.merge_learned(
            name_aliases=learned_boot.name_aliases,
            phrase_aliases=learned_boot.phrase_aliases,
        )
        cfg.ambient.wake_policy = policy
        cfg.ambient.activation_phrases = policy.display_phrases()

    # Load model once
    backend = build_wake_backend(
        cfg.ambient.engine,
        phrases=cfg.ambient.activation_phrases,
        model_path=cfg.ambient.model_path,
        policy=policy if isinstance(policy, WakePolicy) else None,
    )
    # Persist near-miss grouping across wake cycles for handsfree monitor
    near_miss_acc = NearMissAccumulator()
    # Eager load vosk so boot TTS happens after model is ready
    try:
        backend.score_snippet(b"\x00\x00" * 1600, 16000)
    except Exception:
        pass

    mode = (
        policy.normalized_mode()
        if isinstance(policy, WakePolicy)
        else cfg.ambient.wake_mode
    )
    names = (
        policy.canonical_names()
        if isinstance(policy, WakePolicy)
        else list(cfg.ambient.names)
    )
    boot = {
        "schema": "hark.event.v1",
        "kind": "ambient.armed",
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "engine": cfg.ambient.engine,
        "model_path": cfg.ambient.model_path,
        "wake_mode": mode,
        "names": names,
        "phrases": list(cfg.ambient.activation_phrases),
        "snippet_s": cfg.ambient.snippet_s,
        "instructions": (
            "Ambient armed. Wake mode is name-based (defaults: hark/herald; "
            "say hey/hello/yo/sup + name, or bare herald/harold) or full-phrase "
            "(trigger_phrases). Near-misses auto-learn alternates without restart "
            "(wake_learned.json). Config: docs/CUSTOM_WAKE.md. config.toml "
            "file-watch (default) or SIGHUP reloads config. Mic mutes during "
            "TTS. Energy-gated vosk (quiet frames skipped)."
        ),
    }
    emit_hep(boot, out)
    syslog(
        "ambient.armed",
        component="ambient",
        engine=cfg.ambient.engine,
        model_path=cfg.ambient.model_path,
        wake_mode=mode,
        names=names,
        phrases=list(cfg.ambient.activation_phrases),
    )

    if announce:
        try:
            # Lifecycle cues: keep mic unmuted (Wave ring stays white).
            # Skip (do not hold/block ambient boot) if operator is in a call.
            # Text (and TTS disk cache key) follows primary name or custom phrase.
            # B099: heal abandoned play-queue tickets and never wait forever —
            # a stuck tts_play_queue must not prevent wake arming.
            try:
                from hark.audio.playback import heal_tts_play_queue

                healed = heal_tts_play_queue(missing_as_abandoned=True)
                if healed.get("healed_count"):
                    syslog(
                        "ambient.tts_queue_healed",
                        component="ambient",
                        level="warn",
                        healed_count=healed.get("healed_count"),
                        serving=healed.get("serving"),
                        next=healed.get("next"),
                        message="auto-healed stuck TTS play queue before boot announce",
                    )
            except Exception:
                pass
            boot_text = ambient_boot_tts_text(cfg)
            # ~15s: enough for a short legitimate wait; skips if queue still stuck
            run_tts(
                cfg,
                boot_text,
                play=True,
                mute_mic=False,
                conference_policy="skip",
                play_wait_timeout_s=15.0,
            )
        except Exception as exc:
            err = {
                "schema": "hark.event.v1",
                "kind": "ambient.error",
                "event_id": new_event_id(),
                "observed_at": utc_now_iso(),
                "error": f"boot tts: {exc}",
            }
            emit_hep(err, out)
            try:
                syslog(
                    "ambient.boot_tts_error",
                    component="ambient",
                    level="warn",
                    error=str(exc)[:200],
                    message="boot TTS failed or timed out; continuing to arm wake",
                )
            except Exception:
                pass

    last_idle = time.monotonic()
    # B124: when reload aborts an active post-wake listen, tag ambient.reloaded
    pending_listen_aborted = False
    watch_path = cfg.path or default_config_path()
    config_watcher = start_config_watcher(
        watch_path,
        enabled=bool(getattr(cfg.ambient, "config_watch", True)),
        poll_ms=int(getattr(cfg.ambient, "config_watch_poll_ms", 1000)),
        debounce_ms=int(getattr(cfg.ambient, "config_watch_debounce_ms", 400)),
    )
    try:
        while not shutdown_requested():
            if reload_requested():
                src = reload_source()
                clear_reload_request()
                aborted = pending_listen_aborted
                pending_listen_aborted = False
                try:
                    cfg, backend, info = apply_config_reload(cfg, backend)
                    info["source"] = src
                    _emit_reload_event(out, info, source=src, listen_aborted=aborted)
                    # Live-reload name/phrase change → one-shot TTS (no cache).
                    if announce and info.get("wake_label_changed"):
                        try:
                            line = wake_label_change_tts_text(
                                str(info.get("wake_label_prev") or ""),
                                str(info.get("wake_label") or ""),
                            )
                            if line:
                                run_tts(
                                    cfg,
                                    line,
                                    play=True,
                                    mute_mic=False,
                                    conference_policy="force",
                                    use_cache=False,
                                )
                                syslog(
                                    "ambient.wake_label_changed",
                                    component="ambient",
                                    level="info",
                                    wake_label_prev=info.get("wake_label_prev"),
                                    wake_label=info.get("wake_label"),
                                    source=src,
                                )
                        except Exception as tts_exc:
                            syslog(
                                "ambient.wake_label_tts_error",
                                component="ambient",
                                level="warn",
                                error=str(tts_exc)[:200],
                                source=src,
                            )
                except Exception as exc:
                    _emit_reload_event(
                        out,
                        {},
                        error=str(exc)[:200],
                        source=src,
                        listen_aborted=aborted,
                    )
                continue

            result = run_ambient(
                cfg,
                once=True,
                timeout_s=cfg.ambient.timeout_s,
                announce=announce,
                backend=backend,
                out=out,
                near_miss_acc=near_miss_acc,
            )

            # Active post-wake listen aborted by reload (stop cue already played)
            if (
                reload_requested()
                and result.activated
                and result.listen
                and result.listen.get("listen_aborted")
            ):
                pending_listen_aborted = True

            # Wake wait aborted for config reload — apply next loop, skip timeout
            if reload_requested() and not result.activated:
                continue

            # Always emit if we got something useful; skip pure timeouts when shutting down.
            # ambient.timeout on continuous idle cycles is optional (surface_timeouts).
            # Conversation path dual-writes turns/finals itself (skip_emit).
            if result.skip_emit:
                pass
            elif result.activated or not shutdown_requested():
                line = ambient_event_line(result)
                kind = str(line.get("kind") or "ambient.event")
                if kind == "ambient.timeout" and not getattr(
                    cfg.ambient, "surface_timeouts", True
                ):
                    # Quiet continuous handsfree: still re-enter wake wait, no NDJSON/syslog
                    pass
                else:
                    line = {k: v for k, v in line.items() if v is not None}
                    # Dual-write ambient.prompt / timeout / cancelled / error so
                    # Mode A monitors see finals even when stdout → restart log (B104).
                    emit_hep(line, out)
                    syslog(
                        kind,
                        component="ambient",
                        level="info" if result.activated else "debug",
                        message=(result.text or result.phrase or "")[:200],
                        phrase=result.phrase,
                        text=result.text,
                        wake_backend=result.wake_backend,
                        listen=result.listen,
                        event_id=result.event_id,
                    )

            if shutdown_requested():
                break
            if result.activated:
                last_idle = time.monotonic()
            elif time.monotonic() - last_idle >= idle_log_s:
                last_idle = time.monotonic()
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if config_watcher is not None:
            try:
                config_watcher.stop()
            except Exception:
                pass

    reason = get_shutdown_reason()
    phrase = shutdown_phrase(reason)
    # Speak after any in-flight recording finished (we only get here post-cycle)
    if announce:
        try:
            # Shutdown/restart cues: do not mute the mic; skip during conference
            run_tts(
                cfg,
                phrase,
                play=True,
                mute_mic=False,
                conference_policy="skip",
            )
        except Exception as exc:
            syslog(
                "ambient.shutdown_tts_error",
                component="ambient",
                level="warn",
                error=str(exc)[:160],
                reason=reason,
            )

    stop = {
        "schema": "hark.event.v1",
        "kind": "ambient.stopped",
        "event_id": new_event_id(),
        "observed_at": utc_now_iso(),
        "graceful": True,
        "reason": reason,
        "phrase": phrase,
    }
    emit_hep(stop, out)
    syslog(
        "ambient.stopped",
        component="ambient",
        graceful=True,
        reason=reason,
        phrase=phrase,
    )
    clear_shutdown_reason()
    return 0

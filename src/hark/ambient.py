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
from hark.partial import make_final_event, new_stream_id
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


def primary_wake_label(cfg: HarkConfig) -> str:
    """Primary (first) name as ``hey <name>``, or first custom full phrase.

    Used for ambient startup TTS so the spoken line — and thus the on-disk TTS
    cache key (voice + full text) — tracks the operator's configured wake.
    """
    amb = cfg.ambient
    mode = str(getattr(amb, "wake_mode", "") or "").strip().lower()
    names = [str(n).strip() for n in (getattr(amb, "names", None) or []) if str(n).strip()]
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
    """Startup TTS line; cache is keyed by voice + this full string (includes label)."""
    from hark.audio.cues import ambient_boot_line

    return ambient_boot_line(primary_wake_label(cfg))


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
        return None
    finally:
        _close_stream()


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
    ``[listen]`` (B031). In radio end_mode, interim STT is streamed as
    ambient.partial via on_partial / out until the end phrase yields
    ambient.prompt (final). Default HOLD; when ``[ambient].streaming`` is true
    (B098), partial instructions allow short live TTS acks (not pane delivery).
    """
    del announce
    event_id = new_event_id()
    stream_id = new_stream_id()
    amb = cfg.ambient

    def _emit_partial(ev: dict[str, Any]) -> None:
        ev = {**ev, "phrase": hit.phrase}
        # Dual-write partials to ambient.jsonl so radio stream reaches
        # hark monitor even when ambient stdout is redirected (B104).
        if out is not None:
            emit_hep(ev, out)
        if on_partial is not None:
            on_partial(ev)
        syslog(
            "ambient.partial",
            component="ambient",
            level="info",
            stream_id=ev.get("stream_id"),
            seq=ev.get("seq"),
            text=(ev.get("text") or "")[:300],
            partial=True,
            final=False,
            warning=ev.get("warning"),
        )

    # Post-wake gate overrides (B031)
    post_abs = getattr(amb, "post_wake_abs_open_db", None)
    if post_abs is None:
        post_abs = getattr(cfg.listen, "abs_open_db", -48.0)
    post_timeout = getattr(amb, "post_wake_timeout_s", None)
    if post_timeout is None:
        post_timeout = getattr(cfg.listen, "initial_timeout_s", 45.0)
    lead_in_ms = int(getattr(amb, "post_wake_lead_in_ms", 150) or 0)
    arm_cue = bool(getattr(amb, "post_wake_arm_cue", True))
    no_open_nudge = bool(getattr(amb, "post_wake_no_open_nudge", True))
    no_open_tts = str(
        getattr(
            amb,
            "post_wake_no_open_tts",
            "I heard the wake but not your prompt.",
        )
        or "I heard the wake but not your prompt."
    )

    syslog(
        "ambient.post_wake_listen",
        component="ambient",
        level="info",
        phrase=hit.phrase,
        wake_backend=hit.backend,
        stream_id=stream_id,
        abs_open_db=float(post_abs),
        initial_timeout_s=float(post_timeout),
        lead_in_ms=lead_in_ms,
        arm_cue=arm_cue,
    )

    try:
        listened = run_listen(
            cfg,
            end_mode=cfg.listen.end_mode,
            on_partial=_emit_partial if cfg.listen.end_mode == "radio" else None,
            stream_id=stream_id,
            partial_kind="ambient.partial",
            abs_open_db=float(post_abs),
            initial_timeout_s=float(post_timeout),
            lead_in_ms=lead_in_ms,
            arm_cue=arm_cue,
            no_open_nudge=no_open_nudge,
            no_open_nudge_text=no_open_tts,
        )
    except Exception as exc:
        err_s = str(exc)
        is_no_open = (
            "no speech detected" in err_s.lower()
            or "no speech captured" in err_s.lower()
        )
        # run_listen already spoke post_wake_no_open_tts as the mid-path nudge
        # when enabled — do not double-speak on final fail (metrics are enough).
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
            abs_open_db=float(post_abs),
            initial_timeout_s=float(post_timeout),
        )
        return AmbientResult(
            activated=True,
            phrase=hit.phrase,
            text=None,
            wake_backend=hit.backend,
            listen={
                "error": err_s,
                "reason": "no_open" if is_no_open else "listen_failed",
                "abs_open_db": float(post_abs),
                "initial_timeout_s": float(post_timeout),
            },
            event_id=event_id,
            stream_id=stream_id,
            final=True,
            partial=False,
        )

    if listened.cancelled:
        return AmbientResult(
            activated=True,
            phrase=hit.phrase,
            text=listened.text,
            wake_backend=hit.backend,
            listen={
                "provider": listened.provider,
                "duration_ms": listened.duration_ms,
                "end_phrase": listened.end_phrase,
                "cancelled": True,
            },
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
    wake_label_changed = (
        (prev_wake_label or "").strip().lower()
        != (new_wake_label or "").strip().lower()
    )

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
    if result.activated and result.text and not (
        result.listen and result.listen.get("cancelled")
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
    if kind == "ambient.prompt":
        base["instructions"] = (
            "FINAL operator prompt for this stream_id (supersedes all ambient.partial "
            "events with the same stream_id). You may now respond/act. "
            "Not bound to a pane — use judgment; do not invent answers."
        )
        base["warning"] = None
    elif kind == "ambient.cancelled":
        base["instructions"] = (
            "Cancelled — ignore prior partials for this stream_id."
        )
    return base


def _emit_reload_event(
    out: TextIO,
    info: dict[str, Any],
    *,
    error: str | None = None,
    source: str | None = None,
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
                "no sherpa_kws model_path — "
                "run ./scripts/download-sherpa-kws-model.sh"
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
                try:
                    cfg, backend, info = apply_config_reload(cfg, backend)
                    info["source"] = src
                    _emit_reload_event(out, info, source=src)
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
                    _emit_reload_event(out, {}, error=str(exc)[:200], source=src)
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

            # Wake wait aborted for config reload — apply next loop, skip timeout
            if reload_requested() and not result.activated:
                continue

            # Always emit if we got something useful; skip pure timeouts when shutting down.
            # ambient.timeout on continuous idle cycles is optional (surface_timeouts).
            if result.activated or not shutdown_requested():
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

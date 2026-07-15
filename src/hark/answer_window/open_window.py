"""Answer Window primary entry: open(policy) → ListenResult.

Implementation of the listen capture loop (radio + silence). Callers should
prefer :func:`open_answer_window`; :func:`hark.speech.run_listen` is a thin
compat facade that builds policy/deps from kwargs.

Infrastructure callables that tests historically monkeypatch on ``hark.speech``
are resolved via that module at runtime (late binding) so public behavior and
test seams stay stable during the E4 migration.
"""

from __future__ import annotations

from dataclasses import replace

from hark.answer_window.deps import AnswerWindowDeps
from hark.answer_window.policy import AnswerWindowPolicy, effective_radio_idle_s
from hark.answer_window.radio import RadioSession
from hark.answer_window.result import ListenResult
from hark.answer_window.silence import (
    EMPTY_STT_NUDGE_TEXT,
    NO_OPEN_NUDGE_TEXT,
    SilenceEvent,
    SilenceSession,
    _echo_overlap,
    is_no_open_timeout as _is_no_open_timeout,
)
from hark.audio.capture import (
    clamp_pre_roll_ms,
    effective_radio_segment_pad_ms,
)
from hark.endpointing import EndpointStrategy
from hark.listen_end import EndMode
from hark.partial import new_stream_id


def open_answer_window(
    policy: AnswerWindowPolicy,
    *,
    deps: AnswerWindowDeps | None = None,
) -> ListenResult:
    """Run one answer-window capture under *policy*. Blocking until result or error.

    *deps* injects runtime seams (STT, capture, cues, control). When a field is
    left as the default, production implementations are taken from
    :mod:`hark.speech` (and related modules) so existing monkeypatches on
    ``hark.speech`` continue to work through the ``run_listen`` facade.
    """
    deps = deps if deps is not None else AnswerWindowDeps()
    # Late import: speech facade imports this module; resolve cycle at call time.
    import hark.speech as speech

    cfg = getattr(deps, "cfg", None)
    if cfg is None:
        raise ValueError(
            "open_answer_window requires deps.cfg (HarkConfig) until STT/cues "
            "are fully injected without config (E4.T002+)"
        )

    # --- bind runtime symbols (deps override; else speech module for patches) ---
    resolve_stt = speech.resolve_stt
    capture_utterance = (
        deps.capture if deps.capture is not None else speech.capture_utterance
    )
    pause_ambient_for_mic = speech.pause_ambient_for_mic
    MicLease = speech.MicLease
    BusySection = speech.BusySection
    duck_media = (
        deps.duck_media if deps.duck_media is not None else speech.duck_media
    )
    configure_cues_from_config = speech.configure_cues_from_config
    play_record_start = (
        deps.play_record_start
        if deps.play_record_start is not None
        else speech.play_record_start
    )
    play_record_stop = (
        deps.play_record_stop
        if deps.play_record_stop is not None
        else speech.play_record_stop
    )
    register_active_listen = (
        deps.register_active_listen
        if deps.register_active_listen is not None
        else speech.register_active_listen
    )
    clear_active_listen = (
        deps.clear_active_listen
        if deps.clear_active_listen is not None
        else speech.clear_active_listen
    )
    poll_listen_action = (
        deps.poll_listen_action
        if deps.poll_listen_action is not None
        else speech.poll_listen_action
    )
    consume_listen_action = (
        deps.consume_listen_action
        if deps.consume_listen_action is not None
        else speech.consume_listen_action
    )
    touch_voice_activity = (
        deps.touch_voice_activity
        if deps.touch_voice_activity is not None
        else speech.touch_voice_activity
    )

    def _make_store():
        if deps.usage_store is not None:
            return deps.usage_store
        return speech.UsageStore()

    syslog = deps.syslog if deps.syslog is not None else speech.syslog
    time = speech.time  # so tests can monkeypatch speech.time.sleep
    write_wav_bytes = speech.write_wav_bytes
    pad_pcm16_silence = speech.pad_pcm16_silence
    radio_stt_window_pcm = speech.radio_stt_window_pcm
    _transcribe_logged = speech._transcribe_logged
    run_tts = speech.run_tts
    ProviderError = speech.ProviderError
    ABORT = speech.ABORT
    _build_endpoint_strategy = speech.build_endpoint_strategy

    # --- policy → local knobs (parity with former run_listen locals) ---
    mode = policy.end_mode
    max_listen = float(policy.max_listen_s)
    guard = max(0.0, float(policy.post_tts_guard_s))
    last_tts = policy.last_tts
    after_tts = last_tts is not None
    on_partial = deps.on_partial
    stream_id = policy.stream_id
    partial_kind = policy.partial_kind
    discard_leading_ms = int(policy.discard_leading_ms or 0)
    audio_ok_after = deps.audio_ok_after
    arm_cue = bool(policy.arm_cue)
    lead_in_ms = int(policy.lead_in_ms or 0)

    gate_abs_open = float(policy.abs_open_db)
    gate_open_margin = float(policy.open_margin_db)
    gate_timeout_s = float(policy.initial_timeout_s)
    gate_pre_roll_ms = clamp_pre_roll_ms(policy.pre_roll_ms)
    gate_mute_pad_ms = int(policy.mute_edge_pad_ms or 0)
    radio_overlap_ms = int(policy.radio_segment_overlap_ms or 0)
    nudge_no_open_text = policy.no_open_nudge_text or NO_OPEN_NUDGE_TEXT

    provider = policy.stt_provider
    stt = deps.stt if deps.stt is not None else resolve_stt(
        provider or cfg.stt.provider, stt_cfg=cfg.stt
    )

    end_silence = (
        float(policy.end_silence_s)
        if mode is EndMode.SILENCE
        else float(policy.radio_partial_silence_s)
    )
    ambient_streaming = bool(policy.streaming)
    radio_idle_end = effective_radio_idle_s(policy)

    radio_pad_ms = (
        effective_radio_segment_pad_ms(
            int(policy.radio_segment_pad_ms or 0),
            float(policy.radio_partial_silence_s),
        )
        if mode is EndMode.RADIO
        else 0
    )

    endpoint_strategy: EndpointStrategy | None = deps.endpoint_strategy
    if endpoint_strategy is None and mode is EndMode.SILENCE:
        strat_name = str(policy.endpoint_strategy_name or "energy").strip().lower()
        if strat_name not in ("energy", "energy_gate", "gate", "off", "none", ""):
            endpoint_strategy = _build_endpoint_strategy(
                strategy_name=policy.endpoint_strategy_name,
                smart_turn_model_path=policy.smart_turn_model_path,
                smart_turn_threshold=policy.smart_turn_threshold,
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

    store = _make_store()
    configure_cues_from_config(cfg)
    stream = stream_id or new_stream_id()
    stt_seq = 0
    stream_partials = mode is EndMode.RADIO and bool(policy.stream_partials)
    recording_cued = False
    stop_cued = False
    suppress_stop_cue = (
        bool(policy.suppress_stop_cue)
        if policy.suppress_stop_cue is not None
        else bool(ambient_streaming)
    )

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
        Used for ambient wake capture (``post_wake_arm_cue``) and ask/tts
        --listen (``answer_arm_cue``) so both paths share the same start cue.
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

    # Channel-drop paths must always beep even when streaming suppresses the
    # normal end-of-turn stop cue (B110). Reload abort + explicit cancel are
    # not "short pause" finals — the operator must hear the channel drop (B124).
    _FORCE_STOP_CUE_REASONS = frozenset(
        {
            "reload",
            "config_reload",
            "agent:cancel",
            "cancel",
        }
    )

    def _reload_abort() -> bool:
        """True when config reload (SIGHUP / file-watch) should end this listen."""
        try:
            from hark.lifecycle import reload_requested

            return bool(reload_requested())
        except Exception:
            return False

    def _cue_stop_once(*, reason: str = "finalize", force: bool | None = None) -> None:
        """Play record-stop once when capture finalizes (not between radio partials).

        Skipped when ``[ambient].streaming`` is on (B110): short pauses are not
        end-of-capture. Ambient start/arm still plays independently (B113).

        ``force=True`` (or reason in reload/cancel set) overrides suppress so the
        operator always hears channel drop on reload-abort / cancel (B124).
        """
        nonlocal stop_cued
        if not recording_cued or stop_cued:
            return
        stop_cued = True
        do_force = bool(force) if force is not None else reason in _FORCE_STOP_CUE_REASONS
        if suppress_stop_cue and not do_force:
            syslog(
                "listen.stop_cue_suppressed",
                component="stt",
                level="info",
                stream_id=stream,
                mode=mode.value,
                reason="ambient.streaming",
                finalize_reason=reason,
            )
            return
        play_record_stop()
        syslog(
            "listen.stop_cue",
            component="stt",
            level="info",
            stream_id=stream,
            mode=mode.value,
            reason=reason,
            forced=do_force and suppress_stop_cue,
        )

    def _reload_cancel_result(
        *,
        duration_ms: int = 0,
        text: str = "",
        provider: str | None = None,
        partials_emitted: int = 0,
    ) -> ListenResult:
        """Clean cancel when config reload aborts an active listen (B124)."""
        _cue_stop_once(reason="reload")
        prov = provider or getattr(stt, "name", "unknown")
        body = (text or "").strip()
        store.record_stt(
            text=body,
            provider=prov,
            audio_ms=duration_ms,
            ok=False,
            error="config_reload",
        )
        syslog(
            "listen.reload_abort",
            component="stt",
            level="info",
            stream_id=stream,
            mode=mode.value,
            duration_ms=duration_ms,
            text_len=len(body),
        )
        return ListenResult(
            text=body,
            provider=prov,
            duration_ms=duration_ms,
            end_mode=mode.value,
            end_phrase="reload",
            cancelled=True,
            stream_id=stream,
            partials_emitted=partials_emitted,
        )

    def _agent_wants_stop(_pcm: bytes, _elapsed: float) -> bool:
        if _reload_abort():
            return True
        return poll_listen_action(stream) is not None

    # Duck/pause non-Hark media for the full answer-window capture (B046 / I002).
    # Explicit STT flags — do not inherit TTS defaults (pause_media_during_tts=false).
    # Idle ambient wake (local Vosk) never enters run_listen, so continuous wake
    # scanning does not duck/pause media.
    do_duck_stt = bool(policy.duck_media_during_stt)
    do_pause_stt = bool(policy.pause_media_during_stt)

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
        register_active_listen(
            stream,
            mode=mode.value,
            streaming=bool(policy.streaming),
            streaming_ack_min_quiet_s=float(policy.streaming_ack_min_quiet_s or 2.0),
        )
        try:
            if mode is EndMode.SILENCE:
                # Recovery decision + attempt bookkeeping live on SilenceSession.
                silence_sess = SilenceSession(
                    policy=replace(policy, stream_id=stream),
                    deps=AnswerWindowDeps(
                        syslog=syslog,
                        endpoint_strategy=endpoint_strategy,
                    ),
                    stream_id=stream,
                )
                silence_sess.apply(SilenceEvent.START)
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
                    stop_cued = False
                    if arm_cue:
                        _arm_cue_if_requested()
                    try:
                        # Overlap discard only on first attempt after TTS
                        attempt = silence_sess.attempt
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
                            on_voice=lambda: touch_voice_activity(stream_id=stream),
                            should_stop=_agent_wants_stop,
                            discard_leading_ms=lead_discard,
                            audio_ok_after=lead_ok,
                            endpoint_strategy=endpoint_strategy,
                            endpoint_probe_silence_s=policy.endpoint_probe_silence_s,
                            endpoint_max_silence_s=policy.endpoint_max_silence_s,
                            on_endpoint_event=_endpoint_event,
                            abs_open_db=gate_abs_open,
                            open_margin_db=gate_open_margin,
                            initial_timeout_s=gate_timeout_s,
                            preroll_ms=gate_pre_roll_ms,
                            mute_edge_pad_ms=gate_mute_pad_ms,
                        )
                    except TimeoutError as exc:
                        # Reload mid-wait (before speech / empty capture) → clean cancel
                        if _reload_abort():
                            return _reload_cancel_result()
                        _cue_stop_once(reason="timeout")
                        err_s = str(exc)
                        store.record_stt(
                            text="",
                            provider=getattr(stt, "name", None),
                            ok=False,
                            error=err_s[:200],
                        )
                        if _is_no_open_timeout(exc):
                            decision = silence_sess.on_no_open(
                                after_tts=after_tts,
                                error=err_s,
                                abs_open_db=gate_abs_open,
                            )
                            if decision.action is SilenceEvent.RETRY:
                                settle = max(
                                    0.05, min(0.2, guard if guard > 0 else 0.1)
                                )
                                continue
                            if decision.action is SilenceEvent.NUDGE:
                                try:
                                    run_tts(
                                        cfg,
                                        decision.nudge_text or nudge_no_open_text,
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
                                settle = max(
                                    0.1, cfg.audio.post_tts_guard_ms / 1000.0
                                )
                                continue
                        raise
                    # Config reload mid-capture (should_stop) — not a normal silence end
                    if _reload_abort():
                        return _reload_cancel_result(duration_ms=cap.duration_ms)
                    agent_act = consume_listen_action(stream)
                    _cue_stop_once(
                        reason=(
                            "agent:cancel"
                            if agent_act == "cancel"
                            else ("agent:finish" if agent_act == "finish" else "silence")
                        )
                    )
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
                        store.record_stt(
                            text="",
                            provider=tr.provider,
                            audio_ms=cap.duration_ms,
                            latency_ms=latency_ms,
                            ok=False,
                            error="empty transcript",
                        )
                        decision = silence_sess.on_empty_stt(
                            duration_ms=cap.duration_ms,
                            peak_rms=getattr(cap, "peak_rms", None),
                            peak_db=getattr(cap, "peak_db", None),
                            wait_speech_ms=cap.wait_speech_ms,
                            after_tts=after_tts,
                            provider=tr.provider,
                        )
                        if decision.action is SilenceEvent.RETRY:
                            continue
                        if decision.action is SilenceEvent.NUDGE:
                            try:
                                run_tts(
                                    cfg,
                                    decision.nudge_text or EMPTY_STT_NUDGE_TEXT,
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
                    # Echo reject uses policy.last_tts owned by SilenceSession (E3.T003)
                    if silence_sess.should_reject_echo(tr.text):
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
                    attempt = silence_sess.attempt
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
            # Segment join + partial HEP (E2.T002); listen_end + listen_control
            # poll/consume are RadioSession internals (E2.T003).
            pieces: list[bytes] = []
            radio_sess = RadioSession(
                # Policy fields only — streaming/idle set at call seam, not re-read.
                policy=replace(
                    policy,
                    stream_id=stream,
                    stream_partials=bool(stream_partials),
                ),
                deps=AnswerWindowDeps(
                    # Local late-bound poll/consume (speech monkeypatches apply).
                    poll_listen_action=lambda sid: poll_listen_action(sid),
                    consume_listen_action=lambda sid: consume_listen_action(sid),
                    on_partial=on_partial,
                ),
                stream_id=stream,
            )
            # Idle / streaming from policy for the entire radio session body.
            radio_idle_end = radio_sess.radio_idle_s
            radio_streaming = bool(radio_sess.policy.streaming)
            radio_ack_quiet = float(radio_sess.policy.streaming_ack_min_quiet_s)
            # B085: last segment tail for STT window overlap (real PCM, not silence)
            segment_overlap_tail = b""
            started = time.monotonic()
            last_provider = getattr(stt, "name", "unknown")
            last_sample_rate = 16000
            speech_opened_once = False
            if radio_streaming:
                classic_idle = float(
                    radio_sess.policy.radio_idle_end_silence_s or 0.0
                )
                if classic_idle <= 0:
                    classic_idle = 3.0 * float(radio_sess.policy.end_silence_s)
                if radio_idle_end + 1e-9 < classic_idle:
                    syslog(
                        "listen.streaming_idle_clamp",
                        component="stt",
                        level="info",
                        stream_id=stream,
                        idle_s=radio_idle_end,
                        classic_idle_s=classic_idle,
                        streaming_ack_min_quiet_s=radio_ack_quiet,
                        message=(
                            "streaming mode: radio idle auto-finish uses quieter "
                            "window so ambient.prompt is not delayed (B112)"
                        ),
                    )
            # Post-wake settle before arm cue (same as silence path; B031/B113)
            if lead_in_ms > 0:
                time.sleep(max(0.0, lead_in_ms / 1000.0))
            if guard > 0:
                time.sleep(guard)
            # Answer-window / ambient post-wake arm cue: beep as soon as listen
            # is ready (radio too). Independent of streaming stop-cue policy.
            if arm_cue:
                _arm_cue_if_requested()
            while time.monotonic() - started < max_listen:
                if _reload_abort():
                    return _reload_cancel_result(
                        duration_ms=int(1000 * (time.monotonic() - started)),
                        text=radio_sess.last_partial_text,
                        provider=last_provider,
                        partials_emitted=radio_sess.partial_seq,
                    )
                agent_act = radio_sess.poll_agent_action()
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
                        on_voice=lambda: touch_voice_activity(stream_id=stream),
                        should_stop=lambda *_a: (
                            _reload_abort() or radio_sess.agent_wants_stop()
                        ),
                        discard_leading_ms=seg_discard,
                        audio_ok_after=seg_ok_after,
                        abs_open_db=gate_abs_open,
                        open_margin_db=gate_open_margin,
                        preroll_ms=gate_pre_roll_ms,
                        mute_edge_pad_ms=gate_mute_pad_ms,
                    )
                except TimeoutError:
                    if _reload_abort():
                        return _reload_cancel_result(
                            duration_ms=int(1000 * (time.monotonic() - started)),
                            text=radio_sess.last_partial_text,
                            provider=last_provider,
                            partials_emitted=radio_sess.partial_seq,
                        )
                    agent_act = radio_sess.poll_agent_action()
                    if agent_act is not None and pieces:
                        break
                    # B074: post-speech continuous quiet → auto-finish (not cancel)
                    if pieces and speech_opened_once:
                        _cue_stop_once(reason="radio_idle")
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
                        hit = radio_sess.evaluate_transcript(tr.text)
                        if hit is not None:
                            body = (
                                hit.body
                                if radio_sess.policy.strip_phrase
                                else tr.text
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
                            return radio_sess.result_for_phrase_hit(
                                hit,
                                text=tr.text,
                                provider=tr.provider,
                                duration_ms=int(
                                    1000 * (time.monotonic() - started)
                                ),
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
                            partials_emitted=radio_sess.partial_seq,
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
                            partials_emitted=radio_sess.partial_seq,
                        )
                    if pieces:
                        continue
                    _cue_stop_once(reason="timeout")
                    store.record_stt(
                        text="",
                        provider=getattr(stt, "name", None),
                        ok=False,
                        error="timeout",
                    )
                    raise
                # Config reload mid-segment (should_stop) — clean cancel with stop cue
                if _reload_abort():
                    return _reload_cancel_result(
                        duration_ms=int(1000 * (time.monotonic() - started)),
                        text=radio_sess.last_partial_text,
                        provider=last_provider,
                        partials_emitted=radio_sess.partial_seq,
                    )
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
                # RadioSession owns segment text accumulation + join + monotonic body
                from types import SimpleNamespace

                body_so_far = radio_sess.ingest_segment_transcript(
                    tr.text, provider=getattr(tr, "provider", None)
                )
                tr = SimpleNamespace(
                    text=body_so_far or (tr.text or ""),
                    provider=getattr(tr, "provider", last_provider),
                )
                # Agent finish/cancel or soft/hard end phrase (session internals)
                ended = radio_sess.handle_agent_or_phrase(
                    tr.text,
                    provider=tr.provider,
                    duration_ms=int(1000 * (time.monotonic() - started)),
                    consume_agent=True,
                )
                if ended is not None:
                    if ended.cancelled:
                        _cue_stop_once(reason="agent:cancel" if ended.end_phrase == "agent:cancel" else "cancel")
                        store.record_stt(
                            text=ended.text,
                            provider=tr.provider,
                            audio_ms=ended.duration_ms,
                            latency_ms=latency_ms,
                            ok=False,
                            error=(
                                "agent_cancel"
                                if ended.end_phrase == "agent:cancel"
                                else "cancel"
                            ),
                        )
                    else:
                        reason = (
                            "agent:finish"
                            if ended.end_phrase == "agent:finish"
                            else f"end:{ended.end_phrase}"
                        )
                        _cue_stop_once(reason=reason)
                        store.record_stt(
                            text=ended.text,
                            provider=tr.provider,
                            audio_ms=ended.duration_ms,
                            latency_ms=latency_ms,
                            ok=True,
                        )
                    return ended
                # Joined segment STT is append-only; RadioSession refuses shrink
                # so a flaky mid-slice rewrite cannot drop words already seen.
                if radio_sess.emit_partial_if_needed(
                    body_so_far,
                    provider=tr.provider,
                    stt_seq=stt_seq,
                    on_partial=on_partial,
                    streaming=radio_streaming,
                    streaming_ack_min_quiet_s=radio_ack_quiet,
                    partial_kind=partial_kind,
                ):
                    ev = radio_sess.last_partial_event or {}
                    frag = ev.get("fragment") or ""
                    # Prefer fragment in logs so each radio slice is visible
                    # (full cumulative body is still on the event as text).
                    syslog(
                        "listen.partial",
                        component="stt",
                        level="info",
                        stream_id=stream,
                        seq=radio_sess.partial_seq,
                        stt_seq=stt_seq,
                        fragment=(frag or "")[:300],
                        text_len=len(radio_sess.last_partial_text),
                        text=(
                            (radio_sess.last_partial_text[:120] + "…")
                            if len(radio_sess.last_partial_text) > 120
                            else radio_sess.last_partial_text
                        ),
                        provider=tr.provider,
                        partial=True,
                        final=False,
                    )
                continue

            # Exit loop: agent finish with pieces, or max timeout
            agent_act = radio_sess.consume_agent_action()
            _cue_stop_once(
                reason=(
                    "agent:finish"
                    if agent_act == "finish"
                    else ("agent:cancel" if agent_act == "cancel" else "max_listen")
                )
            )
            if pieces and agent_act in ("finish", None):
                # Primary: per-segment join (B083). Optional full-audio re-STT is a
                # *candidate only* — never replace a longer joined body (word loss).
                if agent_act == "finish" or agent_act is None:
                    from types import SimpleNamespace

                    body = radio_sess.finalize_joined_body()
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
                        body = radio_sess.finalize_joined_body(
                            (tr_full.text or "").strip()
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
                        return radio_sess.result_for_agent_action(
                            "finish",
                            text=body,
                            provider=tr.provider,
                            duration_ms=int(1000 * (time.monotonic() - started)),
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
                            partials_emitted=radio_sess.partial_seq,
                        )
            if agent_act == "cancel":
                return radio_sess.result_for_agent_action(
                    "cancel",
                    text=radio_sess.last_partial_text,
                    provider=last_provider,
                    duration_ms=int(1000 * (time.monotonic() - started)),
                )
            raise TimeoutError(f"radio listen exceeded max_listen_s={max_listen}")
        finally:
            clear_active_listen(stream)

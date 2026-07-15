"""E2.T001–T003: RadioSession state machine, segment join, end/control internals."""

from __future__ import annotations

import pytest

from hark.answer_window import (
    AnswerWindowDeps,
    AnswerWindowPolicy,
    RadioEvent,
    RadioSession,
    RadioState,
    effective_radio_idle_s,
    join_radio_stt_segments,
    monotonic_partial_text,
    prefer_complete_transcript,
    radio_transition,
)
from hark.listen_end import EndMode
from hark.partial import HOLD_INSTRUCTIONS, STREAMING_INSTRUCTIONS


def _policy(**kwargs) -> AnswerWindowPolicy:
    base = dict(
        profile="bound_answer",
        end_mode=EndMode.RADIO,
        max_listen_s=60.0,
        end_silence_s=2.1,
        radio_idle_end_silence_s=0.0,
        streaming=False,
        streaming_ack_min_quiet_s=2.0,
        stream_partials=True,
        stream_id="s-test",
        partial_kind="ambient.partial",
    )
    base.update(kwargs)
    return AnswerWindowPolicy(**base)


def test_radio_happy_path_soft_end():
    s = RadioSession(policy=_policy())
    assert s.state is RadioState.ARMED
    s.apply(RadioEvent.START)
    assert s.state is RadioState.WAIT_OPEN
    s.apply(RadioEvent.SPEECH_OPENED)
    assert s.speech_opened
    s.apply(RadioEvent.SEGMENT_BOUNDARY)
    assert s.state is RadioState.PARTIAL_EMIT
    s.note_segment_text("hello world")
    s.note_partial_emitted("hello world")
    s.apply(RadioEvent.PARTIAL_EMITTED)
    assert s.state is RadioState.SEGMENTING
    s.apply(RadioEvent.SOFT_END, end_phrase="over")
    assert s.state is RadioState.FINALIZING
    s.apply(RadioEvent.SOFT_END, end_phrase="over")
    assert s.state is RadioState.DONE
    assert s.end_phrase == "over"
    assert not s.cancelled
    r = s.result_stub(text="hello world", provider="xai")
    assert r.end_mode == "radio"
    assert r.partials_emitted == 1
    assert r.text == "hello world"


def test_radio_agent_cancel_from_segmenting():
    s = RadioSession(policy=_policy(stream_id="s1"), stream_id="s1")
    s.apply(RadioEvent.START)
    s.apply(RadioEvent.SPEECH_OPENED)
    s.apply(RadioEvent.AGENT_CANCEL)
    assert s.state is RadioState.CANCELLED
    assert s.cancelled
    assert s.end_phrase == "agent:cancel"


def test_radio_idle_timeout_only_after_open():
    """Idle finalize is a post-open event (product rule); machine allows it from SEGMENTING."""
    s = RadioSession(policy=_policy())
    s.apply(RadioEvent.START)
    with pytest.raises(ValueError, match="illegal"):
        s.apply(RadioEvent.IDLE_TIMEOUT)
    s.apply(RadioEvent.SPEECH_OPENED)
    s.apply(RadioEvent.IDLE_TIMEOUT)
    assert s.state is RadioState.FINALIZING
    s.apply(RadioEvent.IDLE_TIMEOUT)
    assert s.state is RadioState.DONE
    assert s.end_phrase == "radio_idle"


def test_radio_max_listen_with_body_finalizes():
    s = RadioSession(policy=_policy())
    s.apply(RadioEvent.START)
    s.apply(RadioEvent.SPEECH_OPENED)
    s.note_segment_text("partial thought")
    s.apply(RadioEvent.MAX_LISTEN)
    assert s.state is RadioState.FINALIZING
    s.apply(RadioEvent.MAX_LISTEN)
    assert s.state is RadioState.DONE
    assert s.end_phrase == "max_listen"


def test_radio_no_open_timeout_fails():
    s = RadioSession(policy=_policy())
    s.apply(RadioEvent.START)
    s.apply(RadioEvent.NO_OPEN_TIMEOUT)
    assert s.state is RadioState.FAILED
    assert s.is_terminal
    with pytest.raises(ValueError, match="terminal"):
        s.apply(RadioEvent.START)


def test_illegal_transition_raises():
    with pytest.raises(ValueError, match="illegal"):
        radio_transition(RadioState.ARMED, RadioEvent.SOFT_END)


def test_effective_radio_idle_classic_three_x_end_silence():
    p = _policy(radio_idle_end_silence_s=0.0, end_silence_s=2.1, streaming=False)
    assert effective_radio_idle_s(p) == pytest.approx(6.3)


def test_effective_radio_idle_explicit():
    p = _policy(radio_idle_end_silence_s=5.0, streaming=False)
    assert effective_radio_idle_s(p) == pytest.approx(5.0)


def test_streaming_idle_clamp_tightens_without_ambient():
    """Streaming clamp uses policy fields only (M1/M6 seam)."""
    classic = _policy(
        radio_idle_end_silence_s=6.3,
        end_silence_s=2.1,
        streaming=False,
        streaming_ack_min_quiet_s=2.0,
    )
    stream = _policy(
        radio_idle_end_silence_s=6.3,
        end_silence_s=2.1,
        streaming=True,
        streaming_ack_min_quiet_s=2.0,
    )
    assert effective_radio_idle_s(classic) == pytest.approx(6.3)
    # clamp to max(end_silence, ack_min) = 2.1
    assert effective_radio_idle_s(stream) == pytest.approx(2.1)
    sess = RadioSession(policy=stream)
    assert sess.radio_idle_s == pytest.approx(2.1)


def test_bound_answer_policy_streaming_default_off():
    from types import SimpleNamespace

    from hark.answer_window import policy_from_config

    cfg = SimpleNamespace(
        listen=SimpleNamespace(
            end_mode="radio",
            max_listen_s=90.0,
            end_silence_s=2.1,
            radio_idle_end_silence_s=0.0,
            end_phrases=("hark send",),
            cancel_phrases=("hark cancel",),
            soft_end_phrases=("over",),
            soft_end_phrases_enabled=True,
            strip_phrase=True,
            stream_partials=True,
            radio_partial_silence_s=0.6,
            radio_segment_overlap_ms=300,
            radio_segment_pad_ms=250,
            abs_open_db=-48.0,
            open_margin_db=8.0,
            initial_timeout_s=45.0,
            pre_roll_ms=300,
            no_open_retry=True,
            no_open_nudge=True,
            empty_stt_retry=True,
            empty_stt_nudge=True,
            endpoint_strategy="energy",
            smart_turn_model_path=None,
            smart_turn_threshold=None,
            endpoint_probe_silence_s=0.4,
            endpoint_max_silence_s=6.0,
        ),
        audio=SimpleNamespace(
            mute_edge_pad_ms=300,
            duck_media_during_stt=True,
            pause_media_during_stt=False,
            answer_arm_cue=True,
        ),
        ambient=SimpleNamespace(streaming=True, streaming_ack_min_quiet_s=2.0),
        stt=SimpleNamespace(provider="xai"),
    )
    bound = policy_from_config(cfg, "bound_answer")
    assert bound.streaming is False  # bound must not inherit ambient dogfood
    post = policy_from_config(cfg, "post_wake")
    assert post.streaming is True
    assert post.arm_cue is True


# --- E2.T002: segment join + partial emit owned by RadioSession ---


def test_ingest_segment_join_and_monotonic():
    s = RadioSession(policy=_policy(stream_id="s1"), stream_id="s1")
    body1 = s.ingest_segment_transcript("hello world")
    assert body1 == "hello world"
    assert s.text_segments == ["hello world"]
    assert s.joined_body() == "hello world"

    body2 = s.ingest_segment_transcript("world there")
    assert body2 == "hello world there"  # overlap trim
    assert s.joined_body() == "hello world there"

    # Empty segment does not erase prior
    body3 = s.ingest_segment_transcript("  ")
    assert body3 == "hello world there"
    assert s.text_segments == ["hello world", "world there"]


def test_monotonic_vs_last_partial_refuses_shrink():
    """Joined/finalize body never shrinks below the last emitted partial."""
    s = RadioSession(policy=_policy(stream_id="s1"), stream_id="s1")
    s.ingest_segment_transcript("one two three")
    s.note_partial_emitted("one two three")
    # Simulate a flaky reassembly that drops tokens (segments are shorter)
    s.text_segments[:] = ["one two"]
    assert s.joined_body() == "one two"
    assert s.finalize_joined_body() == "one two three"
    assert monotonic_partial_text("one two three", "one two") == "one two three"


def test_emit_partial_if_needed_hold_shape():
    events: list[dict] = []
    s = RadioSession(
        policy=_policy(stream_id="s-hold", streaming=False),
        stream_id="s-hold",
    )
    body = s.ingest_segment_transcript("hello radio", provider="xai")
    assert s.emit_partial_if_needed(
        body,
        provider="xai",
        stt_seq=1,
        on_partial=events.append,
        streaming=False,
    )
    assert len(events) == 1
    ev = events[0]
    assert ev["partial"] is True
    assert ev["final"] is False
    assert ev["stream_id"] == "s-hold"
    assert ev["seq"] == 1
    assert ev["text"] == "hello radio"
    assert ev["fragment"] == "hello radio"
    assert ev["stt_seq"] == 1
    assert ev["provider"] == "xai"
    assert ev["streaming"] is False
    assert "HOLD" in (ev.get("instructions") or "")
    assert HOLD_INSTRUCTIONS in (ev.get("instructions") or "")
    assert s.partial_seq == 1
    assert s.last_partial_text == "hello radio"

    # Same body → no second emit
    assert not s.emit_partial_if_needed(
        body, provider="xai", stt_seq=2, on_partial=events.append
    )
    assert len(events) == 1


def test_emit_partial_streaming_shape_and_fragment():
    events: list[dict] = []
    s = RadioSession(
        policy=_policy(
            stream_id="s-stream",
            streaming=True,
            streaming_ack_min_quiet_s=2.5,
        ),
        stream_id="s-stream",
    )
    b1 = s.ingest_segment_transcript("plan the")
    assert s.emit_partial_if_needed(
        b1, provider="local", stt_seq=1, on_partial=events.append
    )
    b2 = s.ingest_segment_transcript("the feature over")
    assert s.emit_partial_if_needed(
        b2, provider="local", stt_seq=2, on_partial=events.append
    )
    assert len(events) == 2
    assert events[1]["text"] == "plan the feature over"
    assert events[1]["fragment"] == "feature over"
    assert events[1]["seq"] == 2
    assert events[1]["streaming"] is True
    assert events[1]["ack_min_quiet_s"] == pytest.approx(2.5)
    assert STREAMING_INSTRUCTIONS in (events[1].get("instructions") or "")


def test_emit_partial_uses_deps_on_partial():
    events: list[dict] = []
    s = RadioSession(
        policy=_policy(stream_id="s-deps"),
        deps=AnswerWindowDeps(on_partial=events.append),
        stream_id="s-deps",
    )
    body = s.ingest_segment_transcript("via deps")
    assert s.emit_partial_if_needed(body, provider="xai", stt_seq=3)
    assert len(events) == 1
    assert events[0]["text"] == "via deps"


def test_emit_partial_respects_stream_partials_off():
    events: list[dict] = []
    s = RadioSession(
        policy=_policy(stream_id="s-off", stream_partials=False),
        stream_id="s-off",
    )
    body = s.ingest_segment_transcript("secret")
    assert not s.emit_partial_if_needed(
        body, on_partial=events.append, provider="xai", stt_seq=1
    )
    assert events == []
    assert s.partial_seq == 0


def test_finalize_joined_body_prefers_complete():
    s = RadioSession(policy=_policy(stream_id="s-fin"), stream_id="s-fin")
    s.ingest_segment_transcript("hello world")
    s.ingest_segment_transcript("world there friend")
    s.note_partial_emitted("hello world there friend")
    # Full re-STT shorter → keep joined
    assert (
        s.finalize_joined_body("hello world")
        == "hello world there friend"
    )
    # Full re-STT longer extension → take it
    assert (
        s.finalize_joined_body("hello world there friend extra")
        == "hello world there friend extra"
    )
    assert prefer_complete_transcript("a b", "a") == "a b"


def test_text_join_helpers_reexported_from_speech():
    from hark.speech import (
        join_radio_stt_segments as j2,
        monotonic_partial_text as m2,
        prefer_complete_transcript as p2,
    )

    assert j2 is join_radio_stt_segments
    assert m2 is monotonic_partial_text
    assert p2 is prefer_complete_transcript
    assert j2(["alpha beta", "beta gamma"]) == "alpha beta gamma"


# --- E2.T003: listen_end + listen_control as RadioSession internals ---


def test_evaluate_transcript_soft_end_uses_policy():
    """Soft end evaluation is pure and driven by policy phrase lists."""
    s = RadioSession(
        policy=_policy(
            soft_end_phrases=("over", "send it"),
            soft_end_phrases_enabled=True,
            end_phrases=("hark send",),
            cancel_phrases=("hark cancel",),
        )
    )
    hit = s.evaluate_transcript("please implement the fix over")
    assert hit is not None
    assert hit.kind == "end"
    assert hit.phrase == "over"
    assert "implement" in hit.body


def test_evaluate_transcript_cancel_priority():
    s = RadioSession(
        policy=_policy(
            soft_end_phrases_enabled=True,
            end_phrases=("hark send",),
            cancel_phrases=("hark cancel",),
        )
    )
    hit = s.evaluate_transcript("scratch that hark cancel")
    assert hit is not None
    assert hit.kind == "cancel"
    assert hit.phrase == "hark cancel"


def test_evaluate_transcript_soft_disabled():
    s = RadioSession(
        policy=_policy(
            soft_end_phrases=("send it",),
            soft_end_phrases_enabled=False,
            end_phrases=("hark send",),
        )
    )
    assert s.evaluate_transcript("ship it send it") is None
    hit = s.evaluate_transcript("ship it hark send")
    assert hit is not None
    assert hit.phrase == "hark send"


def test_poll_and_consume_agent_action_via_deps():
    """Agent control IPC is injectable; no filesystem required."""
    pending: list[str | None] = ["finish"]

    def poll(_sid: str) -> str | None:
        return pending[0]

    def consume(_sid: str) -> str | None:
        act = pending[0]
        pending[0] = None
        return act

    s = RadioSession(
        policy=_policy(stream_id="s-ctrl"),
        deps=AnswerWindowDeps(
            poll_listen_action=poll,
            consume_listen_action=consume,
        ),
        stream_id="s-ctrl",
    )
    assert s.poll_agent_action() == "finish"
    assert s.agent_wants_stop() is True
    # poll is non-destructive
    assert s.poll_agent_action() == "finish"
    assert s.consume_agent_action() == "finish"
    assert s.poll_agent_action() is None
    assert s.agent_wants_stop() is False


def test_handle_agent_or_phrase_agent_cancel_priority():
    """Agent cancel wins even when the transcript has a soft end."""
    s = RadioSession(
        policy=_policy(stream_id="s-ag", soft_end_phrases_enabled=True),
        deps=AnswerWindowDeps(
            poll_listen_action=lambda _s: "cancel",
            consume_listen_action=lambda _s: "cancel",
        ),
        stream_id="s-ag",
    )
    s.ingest_segment_transcript("long prompt over")
    r = s.handle_agent_or_phrase(
        "long prompt over",
        provider="fake",
        duration_ms=100,
        consume_agent=True,
    )
    assert r is not None
    assert r.cancelled is True
    assert r.end_phrase == "agent:cancel"
    assert r.stream_id == "s-ag"


def test_handle_agent_or_phrase_soft_end_result():
    s = RadioSession(
        policy=_policy(
            stream_id="s-soft",
            soft_end_phrases_enabled=True,
            strip_phrase=True,
        ),
        deps=AnswerWindowDeps(
            poll_listen_action=lambda _s: None,
            consume_listen_action=lambda _s: None,
        ),
        stream_id="s-soft",
    )
    r = s.handle_agent_or_phrase(
        "please implement the fix over",
        provider="fake",
        duration_ms=50,
    )
    assert r is not None
    assert r.cancelled is False
    assert r.end_phrase == "over"
    assert "implement" in (r.text or "").lower()
    assert "over" not in (r.text or "").lower()
    assert r.partials_emitted == 0


def test_handle_agent_or_phrase_none_when_continue():
    s = RadioSession(
        policy=_policy(stream_id="s-cont"),
        deps=AnswerWindowDeps(
            poll_listen_action=lambda _s: None,
            consume_listen_action=lambda _s: None,
        ),
        stream_id="s-cont",
    )
    assert (
        s.handle_agent_or_phrase(
            "still thinking about the design",
            provider="fake",
            duration_ms=10,
        )
        is None
    )


def test_result_for_agent_finish_and_cancel():
    s = RadioSession(policy=_policy(stream_id="s-res"), stream_id="s-res")
    s.note_partial_emitted("partial body")
    fin = s.result_for_agent_action(
        "finish", text="partial body", provider="xai", duration_ms=12
    )
    assert fin.end_phrase == "agent:finish"
    assert fin.cancelled is False
    assert fin.text == "partial body"
    can = s.result_for_agent_action(
        "cancel", text="", provider="xai", duration_ms=12
    )
    assert can.end_phrase == "agent:cancel"
    assert can.cancelled is True
    assert can.text == "partial body"  # falls back to last_partial_text


def test_profiles_streaming_differ_without_session_ambient_read():
    """E2.T004: bound_answer vs post_wake; idle from policy fields only."""
    from types import SimpleNamespace

    from hark.answer_window import RadioSession, policy_from_config

    cfg = SimpleNamespace(
        listen=SimpleNamespace(
            end_mode="radio",
            max_listen_s=90.0,
            end_silence_s=2.1,
            radio_idle_end_silence_s=6.3,
            end_phrases=("hark send",),
            cancel_phrases=("hark cancel",),
            soft_end_phrases=("over",),
            soft_end_phrases_enabled=True,
            strip_phrase=True,
            stream_partials=True,
            radio_partial_silence_s=0.6,
            radio_segment_overlap_ms=300,
            radio_segment_pad_ms=250,
            abs_open_db=-48.0,
            open_margin_db=8.0,
            initial_timeout_s=45.0,
            pre_roll_ms=300,
            no_open_retry=True,
            no_open_nudge=True,
            empty_stt_retry=True,
            empty_stt_nudge=True,
            endpoint_strategy="energy",
            smart_turn_model_path=None,
            smart_turn_threshold=None,
            endpoint_probe_silence_s=0.4,
            endpoint_max_silence_s=6.0,
        ),
        audio=SimpleNamespace(
            mute_edge_pad_ms=300,
            duck_media_during_stt=True,
            pause_media_during_stt=False,
            answer_arm_cue=True,
        ),
        ambient=SimpleNamespace(streaming=True, streaming_ack_min_quiet_s=2.0),
        stt=SimpleNamespace(provider="xai"),
    )
    bound = policy_from_config(cfg, "bound_answer")
    post = policy_from_config(cfg, "post_wake")
    assert bound.streaming is False
    assert post.streaming is True
    bound_sess = RadioSession(policy=bound)
    post_sess = RadioSession(policy=post)
    assert bound_sess.radio_idle_s == pytest.approx(6.3)
    # streaming clamp: max(2.1, 2.0) = 2.1
    assert post_sess.radio_idle_s == pytest.approx(2.1)
    # Session does not need cfg.ambient — only policy fields.
    assert "ambient" not in bound_sess.as_debug_dict()


def test_effective_radio_idle_end_s_accepts_explicit_ack():
    """Helper must not re-read ambient when streaming flags are explicit."""
    from types import SimpleNamespace

    from hark.speech import effective_radio_idle_end_s

    cfg = SimpleNamespace(
        listen=SimpleNamespace(end_silence_s=2.1, radio_idle_end_silence_s=6.3),
        ambient=SimpleNamespace(streaming=True, streaming_ack_min_quiet_s=99.0),
    )
    # Explicit streaming=False ignores ambient streaming and ack.
    assert effective_radio_idle_end_s(
        cfg, streaming=False, streaming_ack_min_quiet_s=2.0
    ) == pytest.approx(6.3)
    assert effective_radio_idle_end_s(
        cfg, streaming=True, streaming_ack_min_quiet_s=2.0
    ) == pytest.approx(2.1)


# --- E5.T001: open_answer_window deep seam (no private _loop hooks) ---


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _cap(*, duration_ms: int = 40, wait_speech_ms: int = 20):
    from hark.audio.capture import CaptureResult

    return CaptureResult(
        pcm16=b"\0\0" * max(1, int(16 * duration_ms)),
        sample_rate=16000,
        duration_ms=duration_ms,
        speech_ms=duration_ms,
        wait_speech_ms=wait_speech_ms,
        peak_rms=0.02,
        peak_db=-34.0,
    )


def _fake_stt(texts: list[str] | str):
    from types import SimpleNamespace

    seq = [texts] if isinstance(texts, str) else list(texts)
    idx = {"n": 0}

    def transcribe(_wav, *, language=None):
        del language
        i = min(idx["n"], len(seq) - 1)
        idx["n"] += 1
        return SimpleNamespace(text=seq[i], provider="fake")

    return SimpleNamespace(name="fake", transcribe=transcribe, calls=idx)


def _stub_speech_shell(monkeypatch):
    """Lease / duck / cues / time still late-bound via hark.speech (not private loops)."""
    import hark.speech as speech

    class FakeStore:
        def record_stt(self, **kwargs):
            pass

    monkeypatch.setattr(speech, "pause_ambient_for_mic", lambda **k: _NullCtx())
    monkeypatch.setattr(speech, "MicLease", lambda *a, **k: _NullCtx())
    monkeypatch.setattr(speech, "BusySection", lambda *a, **k: _NullCtx())
    monkeypatch.setattr(speech, "duck_media", lambda *a, **k: _NullCtx())
    monkeypatch.setattr(speech, "configure_cues_from_config", lambda cfg: None)
    monkeypatch.setattr(speech, "UsageStore", FakeStore)
    monkeypatch.setattr(speech.time, "sleep", lambda s: None)


def _open_radio(
    policy: AnswerWindowPolicy,
    *,
    cfg,
    stt,
    capture,
    on_partial=None,
    syslog=None,
    **deps_extra,
):
    from hark.answer_window import open_answer_window

    deps_kw = dict(
        cfg=cfg,
        stt=stt,
        capture=capture,
        on_partial=on_partial,
        syslog=syslog or (lambda *a, **k: None),
        play_record_start=lambda: None,
        play_record_stop=lambda: None,
        register_active_listen=lambda *a, **k: None,
        clear_active_listen=lambda *a, **k: None,
        poll_listen_action=lambda *a: None,
        consume_listen_action=lambda *a: None,
        touch_voice_activity=lambda **k: None,
    )
    deps_kw.update(deps_extra)
    return open_answer_window(policy, deps=AnswerWindowDeps(**deps_kw))


def test_open_answer_window_radio_soft_end_over(monkeypatch):
    """Deep seam: multi-segment content + sole 'over' finalizes (not cancel)."""
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="radio",
            soft_end_phrases_enabled=True,
            stream_partials=False,
            strip_phrase=True,
        )
    )
    policy = _policy(
        soft_end_phrases_enabled=True,
        stream_partials=False,
        strip_phrase=True,
        post_tts_guard_s=0,
        max_listen_s=30.0,
    )
    stt = _fake_stt(["please implement the fix", "over"])
    result = _open_radio(
        policy,
        cfg=cfg,
        stt=stt,
        capture=lambda **k: _cap(),
    )
    assert result.cancelled is False
    assert result.end_phrase == "over"
    assert result.end_mode == "radio"
    assert "implement" in (result.text or "").lower()
    assert "over" not in (result.text or "").lower()


def test_open_answer_window_radio_idle_auto_finish(monkeypatch):
    """Deep seam: post-open quiet → radio_idle end (policy idle, not ambient)."""
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    idle_s = 0.15
    cfg = HarkConfig(listen=ListenConfig(end_mode="radio"))
    policy = _policy(
        radio_partial_silence_s=0.05,
        radio_idle_end_silence_s=idle_s,
        soft_end_phrases_enabled=False,
        stream_partials=True,
        post_tts_guard_s=0,
        streaming=False,
    )
    calls: list[dict] = []
    outcomes = ["speech", "idle"]
    logs: list[tuple[str, dict]] = []

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        step = outcomes.pop(0) if outcomes else "idle"
        if step == "speech":
            on_opened = kwargs.get("on_opened")
            if on_opened is not None:
                on_opened()
            return _cap(duration_ms=80)
        raise TimeoutError("no speech detected (peak_db=-60.0)")

    result = _open_radio(
        policy,
        cfg=cfg,
        stt=_fake_stt("ship the auth fix"),
        capture=fake_capture,
        syslog=lambda event, **data: logs.append((event, data)),
    )
    assert result.cancelled is False
    assert result.end_phrase == "radio_idle"
    assert "auth" in (result.text or "").lower()
    assert len(calls) >= 2
    assert calls[0]["initial_timeout_s"] == pytest.approx(policy.initial_timeout_s)
    assert calls[0]["end_silence_s"] == pytest.approx(0.05)
    assert calls[1]["initial_timeout_s"] == pytest.approx(idle_s)
    idle_logs = [d for e, d in logs if e == "listen.radio_idle_end"]
    assert idle_logs
    assert idle_logs[0]["idle_s"] == pytest.approx(idle_s)


def test_open_answer_window_radio_partials_via_deps(monkeypatch):
    """Deep seam: on_partial on AnswerWindowDeps receives HOLD partials."""
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    cfg = HarkConfig(listen=ListenConfig(end_mode="radio"))
    policy = _policy(
        radio_partial_silence_s=0.5,
        stream_partials=True,
        streaming=False,
        soft_end_phrases_enabled=False,
        strip_phrase=True,
        post_tts_guard_s=0,
        stream_id="s-deep",
        end_phrases=("okay hark send", "hark send"),
    )
    transcripts = [
        "please open the pull request",
        "please open the pull request for auth",
        "please open the pull request for auth okay hark send",
    ]
    partials: list[dict] = []
    capture_kwargs: list[dict] = []

    def fake_capture(**kwargs):
        capture_kwargs.append(kwargs)
        assert kwargs["end_silence_s"] == pytest.approx(0.5)
        return _cap()

    result = _open_radio(
        policy,
        cfg=cfg,
        stt=_fake_stt(transcripts),
        capture=fake_capture,
        on_partial=partials.append,
    )
    assert len(partials) >= 2
    assert all(p.get("partial") is True for p in partials)
    assert all(p.get("final") is False for p in partials)
    assert all(p.get("stream_id") == "s-deep" for p in partials)
    assert result.end_phrase == "okay hark send"
    assert result.cancelled is False
    assert "pull request" in (result.text or "").lower()
    assert result.partials_emitted >= 2
    assert capture_kwargs


def test_open_answer_window_radio_pre_open_timeout_not_idle(monkeypatch):
    """Deep seam: quiet before first open uses initial_timeout, not radio_idle."""
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    cfg = HarkConfig(listen=ListenConfig(end_mode="radio"))
    policy = _policy(
        initial_timeout_s=0.2,
        radio_idle_end_silence_s=0.05,
        soft_end_phrases_enabled=False,
        post_tts_guard_s=0,
    )
    calls: list[dict] = []

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        raise TimeoutError("no speech detected (peak_db=-60.0)")

    with pytest.raises(TimeoutError, match="no speech detected"):
        _open_radio(
            policy,
            cfg=cfg,
            stt=_fake_stt(""),
            capture=fake_capture,
        )
    assert calls
    assert calls[0]["initial_timeout_s"] == pytest.approx(0.2)


def test_open_answer_window_radio_streaming_idle_from_policy(monkeypatch):
    """Deep seam: streaming clamp is policy-only; capture gets tightened idle."""
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    cfg = HarkConfig(listen=ListenConfig(end_mode="radio"))
    # Classic idle 6.3s; streaming clamp → max(end_silence, ack) = 2.1
    policy = _policy(
        radio_idle_end_silence_s=6.3,
        end_silence_s=2.1,
        streaming=True,
        streaming_ack_min_quiet_s=2.0,
        radio_partial_silence_s=0.05,
        soft_end_phrases_enabled=False,
        stream_partials=True,
        post_tts_guard_s=0,
    )
    assert effective_radio_idle_s(policy) == pytest.approx(2.1)
    calls: list[dict] = []
    outcomes = ["speech", "idle"]

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        step = outcomes.pop(0) if outcomes else "idle"
        if step == "speech":
            on_opened = kwargs.get("on_opened")
            if on_opened is not None:
                on_opened()
            return _cap()
        raise TimeoutError("no speech detected (peak_db=-60.0)")

    result = _open_radio(
        policy,
        cfg=cfg,
        stt=_fake_stt("streamed thought"),
        capture=fake_capture,
    )
    assert result.end_phrase == "radio_idle"
    assert len(calls) >= 2
    # Post-open segment timeout uses streaming-clamped idle from policy
    assert calls[1]["initial_timeout_s"] == pytest.approx(2.1)


def test_open_answer_window_radio_agent_cancel_via_deps(monkeypatch):
    """Deep seam: agent cancel is deps poll/consume — no filesystem hooks."""
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    cfg = HarkConfig(listen=ListenConfig(end_mode="radio"))
    policy = _policy(
        stream_id="s-cancel",
        soft_end_phrases_enabled=False,
        stream_partials=False,
        post_tts_guard_s=0,
    )
    opened = {"n": 0}

    def fake_capture(**kwargs):
        opened["n"] += 1
        on_opened = kwargs.get("on_opened")
        if on_opened is not None:
            on_opened()
        return _cap()

    result = _open_radio(
        policy,
        cfg=cfg,
        stt=_fake_stt("half a thought"),
        capture=fake_capture,
        poll_listen_action=lambda _s: "cancel",
        consume_listen_action=lambda _s: "cancel",
    )
    assert result.cancelled is True
    assert result.end_phrase == "agent:cancel"
    assert result.stream_id == "s-cancel"
    assert opened["n"] >= 1

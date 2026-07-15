"""E2.T001: RadioSession state machine + policy idle (no audio hardware)."""

from __future__ import annotations

import pytest

from hark.answer_window import (
    AnswerWindowPolicy,
    RadioEvent,
    RadioSession,
    RadioState,
    effective_radio_idle_s,
    radio_transition,
)
from hark.listen_end import EndMode


def _policy(**kwargs) -> AnswerWindowPolicy:
    base = dict(
        profile="bound_answer",
        end_mode=EndMode.RADIO,
        max_listen_s=60.0,
        end_silence_s=2.1,
        radio_idle_end_silence_s=0.0,
        streaming=False,
        streaming_ack_min_quiet_s=2.0,
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

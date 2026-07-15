"""E3.T001–T003: SilenceSession, endpoint strategy, empty/no-open, echo reject."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hark.answer_window import (
    EMPTY_STT_NUDGE_TEXT,
    NO_OPEN_NUDGE_TEXT,
    AnswerWindowDeps,
    AnswerWindowPolicy,
    SilenceEvent,
    SilenceSession,
    SilenceState,
    echo_overlap,
    is_no_open_timeout,
    log_no_open,
    resolve_endpoint_strategy,
    silence_transition,
)
from hark.endpointing import EndpointFrame, SmartTurnStrategy
from hark.listen_end import EndMode


def _policy(**kwargs) -> AnswerWindowPolicy:
    base = dict(
        profile="bound_answer",
        end_mode=EndMode.SILENCE,
        max_listen_s=60.0,
        end_silence_s=2.1,
        empty_stt_retry=True,
        empty_stt_nudge=True,
        no_open_retry=True,
        no_open_nudge=True,
        endpoint_strategy_name="energy",
    )
    base.update(kwargs)
    return AnswerWindowPolicy(**base)


def test_silence_happy_path_energy():
    s = SilenceSession(policy=_policy())
    assert s.uses_energy_gate
    assert s.endpoint_strategy is None
    s.apply(SilenceEvent.START)
    s.apply(SilenceEvent.SPEECH_OPENED)
    s.apply(SilenceEvent.CAPTURE_ENDED)
    s.apply(SilenceEvent.TRANSCRIPT_OK)
    assert s.state is SilenceState.FINALIZING
    s.apply(SilenceEvent.TRANSCRIPT_OK)
    assert s.state is SilenceState.DONE
    r = s.result_stub(text="yes", provider="xai")
    assert r.end_mode == "silence"
    assert r.text == "yes"


def test_empty_stt_retry_then_nudge_then_give_up():
    s = SilenceSession(policy=_policy())
    s.apply(SilenceEvent.START)
    s.apply(SilenceEvent.SPEECH_OPENED)
    s.apply(SilenceEvent.CAPTURE_ENDED)
    s.apply(SilenceEvent.TRANSCRIPT_EMPTY)
    assert s.state is SilenceState.RECOVER_EMPTY
    assert s.plan_empty_stt_recovery() is SilenceEvent.RETRY
    s.apply(SilenceEvent.RETRY)
    assert s.state is SilenceState.WAIT_OPEN
    assert s.did_empty_retry
    # second empty
    s.apply(SilenceEvent.SPEECH_OPENED)
    s.apply(SilenceEvent.CAPTURE_ENDED)
    s.apply(SilenceEvent.TRANSCRIPT_EMPTY)
    assert s.plan_empty_stt_recovery() is SilenceEvent.NUDGE
    s.apply(SilenceEvent.NUDGE)
    assert s.did_empty_nudge
    s.apply(SilenceEvent.SPEECH_OPENED)
    s.apply(SilenceEvent.CAPTURE_ENDED)
    s.apply(SilenceEvent.TRANSCRIPT_EMPTY)
    assert s.plan_empty_stt_recovery() is SilenceEvent.GIVE_UP
    s.apply(SilenceEvent.GIVE_UP)
    assert s.state is SilenceState.FAILED


def test_no_open_recovery_plan():
    s = SilenceSession(policy=_policy())
    s.apply(SilenceEvent.START)
    s.apply(SilenceEvent.NO_OPEN_TIMEOUT)
    assert s.state is SilenceState.RECOVER_NO_OPEN
    assert s.plan_no_open_recovery() is SilenceEvent.RETRY
    s.apply(SilenceEvent.RETRY)
    assert s.did_no_open_retry
    s.apply(SilenceEvent.NO_OPEN_TIMEOUT)
    assert s.plan_no_open_recovery() is SilenceEvent.NUDGE
    s.apply(SilenceEvent.NUDGE)
    assert s.did_no_open_nudge
    s.apply(SilenceEvent.NO_OPEN_TIMEOUT)
    assert s.plan_no_open_recovery() is SilenceEvent.GIVE_UP


def test_on_empty_stt_owns_logging_and_bookkeeping():
    logs: list[tuple[str, dict]] = []

    def _syslog(event, **data):
        logs.append((event, data))

    s = SilenceSession(
        policy=_policy(),
        deps=AnswerWindowDeps(syslog=_syslog),
        stream_id="s-empty",
    )
    s.apply(SilenceEvent.START)
    d1 = s.on_empty_stt(
        duration_ms=2540,
        peak_rms=0.02,
        peak_db=-34.0,
        wait_speech_ms=80,
        after_tts=True,
        provider="fake",
    )
    assert d1.action is SilenceEvent.RETRY
    assert d1.phase == "initial"
    assert s.did_empty_retry
    assert s.attempt == 1
    assert s.state is SilenceState.WAIT_OPEN
    assert any(e == "speech.empty_stt" for e, _ in logs)
    assert any(e == "speech.empty_stt_retry" for e, _ in logs)
    empty0 = next(d for e, d in logs if e == "speech.empty_stt")
    assert empty0["phase"] == "initial"
    assert empty0["duration_ms"] == 2540
    assert empty0["after_tts"] is True

    d2 = s.on_empty_stt(duration_ms=1000, after_tts=True, provider="fake")
    assert d2.action is SilenceEvent.NUDGE
    assert d2.phase == "retry"
    assert d2.nudge_text == EMPTY_STT_NUDGE_TEXT
    assert s.did_empty_nudge
    assert s.attempt == 2
    assert any(e == "speech.empty_stt_nudge" for e, _ in logs)

    d3 = s.on_empty_stt(duration_ms=900, after_tts=True, provider="fake")
    assert d3.action is SilenceEvent.GIVE_UP
    assert d3.phase == "nudge"
    assert s.state is SilenceState.FAILED
    empty_phases = [d["phase"] for e, d in logs if e == "speech.empty_stt"]
    assert empty_phases == ["initial", "retry", "nudge"]


def test_on_no_open_owns_logging_and_bookkeeping():
    logs: list[tuple[str, dict]] = []

    def _syslog(event, **data):
        logs.append((event, data))

    s = SilenceSession(
        policy=_policy(no_open_nudge_text=NO_OPEN_NUDGE_TEXT),
        deps=AnswerWindowDeps(syslog=_syslog),
        stream_id="s-no",
    )
    s.apply(SilenceEvent.START)
    err = "no speech detected (peak_db=-45.4 peak_rms=0.00537 open_thresh≈-38.0dB)"
    d1 = s.on_no_open(after_tts=False, error=err, abs_open_db=-48.0)
    assert d1.action is SilenceEvent.RETRY
    assert d1.phase == "initial"
    assert s.did_no_open_retry
    assert any(e == "speech.no_open" for e, _ in logs)
    assert any(e == "speech.no_open_retry" for e, _ in logs)
    payload = next(d for e, d in logs if e == "speech.no_open")
    assert payload["peak_db"] == pytest.approx(-45.4)
    assert payload["phase"] == "initial"

    d2 = s.on_no_open(after_tts=False, error=err, abs_open_db=-48.0)
    assert d2.action is SilenceEvent.NUDGE
    assert d2.nudge_text == NO_OPEN_NUDGE_TEXT
    assert s.did_no_open_nudge
    assert any(e == "speech.no_open_nudge" for e, _ in logs)

    d3 = s.on_no_open(after_tts=False, error=err, abs_open_db=-48.0)
    assert d3.action is SilenceEvent.GIVE_UP
    assert d3.phase == "nudge"
    assert s.state is SilenceState.FAILED


def test_on_empty_stt_respects_disabled_recovery():
    s = SilenceSession(
        policy=_policy(empty_stt_retry=False, empty_stt_nudge=False),
        deps=AnswerWindowDeps(syslog=lambda *a, **k: None),
    )
    d = s.on_empty_stt(duration_ms=100, after_tts=False, provider="x")
    assert d.action is SilenceEvent.GIVE_UP
    assert s.state is SilenceState.FAILED


def test_is_no_open_timeout_helper():
    assert is_no_open_timeout(
        TimeoutError("no speech detected (peak_db=-45.4 peak_rms=0.005)")
    )
    assert is_no_open_timeout(TimeoutError("no speech captured (peak_db=-50.0)"))
    assert not is_no_open_timeout(
        TimeoutError("heard audio but STT returned empty text")
    )


def test_log_no_open_module_helper():
    logs: list[tuple[str, dict]] = []
    log_no_open(
        after_tts=False,
        attempt=0,
        stream_id="s1",
        phase="initial",
        error="no speech detected (peak_db=-45.4 peak_rms=0.00537 open_thresh≈-38.0dB)",
        abs_open_db=-48.0,
        syslog_fn=lambda event, **data: logs.append((event, data)),
    )
    assert logs[0][0] == "speech.no_open"
    assert logs[0][1]["peak_db"] == pytest.approx(-45.4)


def test_inject_smart_turn_strategy():
    strat = SmartTurnStrategy(lambda samples, sr: 0.9, threshold=0.5)
    s = SilenceSession(
        policy=_policy(endpoint_strategy_name="smart_turn"),
        deps=AnswerWindowDeps(endpoint_strategy=strat),
    )
    assert not s.uses_energy_gate
    assert s.endpoint_strategy is strat
    assert s.endpoint_strategy.should_end(
        EndpointFrame(pcm16=b"\x00\x01" * 100, sample_rate=16000, trailing_silence_s=0.5, speech_s=1.0)
    )


def test_resolve_energy_names():
    for name in ("energy", "energy_gate", "gate", "off", "none", ""):
        p = _policy(endpoint_strategy_name=name)
        assert resolve_endpoint_strategy(p) is None


def test_resolve_smart_turn_fail_open():
    warns: list[str] = []
    p = _policy(
        endpoint_strategy_name="smart_turn",
        smart_turn_model_path="/nonexistent/model.onnx",
    )
    strat = resolve_endpoint_strategy(p, on_warn=warns.append)
    assert strat is None
    assert warns and "smart_turn" in warns[0].lower()


def test_resolve_smart_turn_with_predict_fn():
    p = _policy(endpoint_strategy_name="smart_turn", smart_turn_threshold=0.6)
    strat = resolve_endpoint_strategy(p, predict_fn=lambda s, sr: 0.7)
    assert strat is not None
    assert getattr(strat, "name", None) == "smart_turn"


def test_illegal_transition():
    with pytest.raises(ValueError, match="illegal"):
        silence_transition(SilenceState.ARMED, SilenceEvent.TRANSCRIPT_OK)


def test_agent_cancel():
    s = SilenceSession(policy=_policy())
    s.apply(SilenceEvent.START)
    s.apply(SilenceEvent.SPEECH_OPENED)
    s.apply(SilenceEvent.AGENT_CANCEL)
    assert s.state is SilenceState.CANCELLED
    assert s.cancelled
    assert s.end_phrase == "agent:cancel"


def test_should_reject_echo_uses_policy_last_tts():
    """E3.T003: echo decision owns last_tts via policy, not free kwargs."""
    tts = (
        "Please answer what you know about the laptop state including Windows version "
        "BitLocker encryption local admin disk size free space and dual boot status "
        "when you are ready to continue with the backup plan to the NAS device."
    )
    s = SilenceSession(policy=_policy(last_tts=tts))
    # Short quote of a word from the prompt is not residual TTS (B093)
    assert s.should_reject_echo("BitLocker.") is False
    assert s.should_reject_echo("on") is False
    # Near-full re-speak of the prompt is echo
    assert s.should_reject_echo(tts) is True
    almost = tts[10:-10]
    assert len(almost) >= 40
    assert s.should_reject_echo(almost) is True


def test_should_reject_echo_false_when_no_last_tts():
    s = SilenceSession(policy=_policy(last_tts=None))
    longish = (
        "Please answer what you know about the laptop state including Windows version "
        "BitLocker encryption local admin disk size free space."
    )
    assert s.should_reject_echo(longish) is False


def test_echo_overlap_helper_matches_session():
    """Pure helper and session method share the same decision."""
    tts = (
        "Please answer what you know about the laptop state including Windows version "
        "BitLocker encryption local admin disk size free space and dual boot status "
        "when you are ready to continue with the backup plan to the NAS device."
    )
    s = SilenceSession(policy=_policy(last_tts=tts))
    assert echo_overlap("BitLocker.", tts) is s.should_reject_echo("BitLocker.")
    assert echo_overlap(tts, tts) is s.should_reject_echo(tts)


def test_policy_from_config_endpoint_fields():
    from hark.answer_window import policy_from_config

    cfg = SimpleNamespace(
        listen=SimpleNamespace(
            end_mode="silence",
            max_listen_s=45.0,
            end_silence_s=2.1,
            endpoint_strategy="smart_turn",
            smart_turn_model_path="/tmp/m.onnx",
            smart_turn_threshold=0.55,
            endpoint_probe_silence_s=0.4,
            endpoint_max_silence_s=6.0,
            abs_open_db=-48.0,
            open_margin_db=8.0,
            initial_timeout_s=45.0,
            pre_roll_ms=300,
            no_open_retry=True,
            no_open_nudge=True,
            empty_stt_retry=True,
            empty_stt_nudge=True,
            stream_partials=True,
            radio_partial_silence_s=0.6,
            radio_segment_overlap_ms=300,
            radio_segment_pad_ms=250,
            radio_idle_end_silence_s=0.0,
            end_phrases=(),
            cancel_phrases=(),
            soft_end_phrases=(),
            soft_end_phrases_enabled=True,
            strip_phrase=True,
        ),
        audio=SimpleNamespace(
            mute_edge_pad_ms=300,
            duck_media_during_stt=True,
            pause_media_during_stt=False,
            answer_arm_cue=False,
        ),
        ambient=SimpleNamespace(streaming=False, streaming_ack_min_quiet_s=2.0),
        stt=SimpleNamespace(provider="xai"),
    )
    pol = policy_from_config(cfg, "bound_answer")
    assert pol.endpoint_strategy_name == "smart_turn"
    assert pol.smart_turn_model_path == "/tmp/m.onnx"
    assert pol.smart_turn_threshold == pytest.approx(0.55)


# --- E5.T001: open_answer_window deep seam (no private _loop hooks) ---


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _cap(*, duration_ms: int = 2540, peak_rms: float = 0.02):
    from hark.audio.capture import CaptureResult

    return CaptureResult(
        pcm16=b"\x00\x00" * 1600,
        sample_rate=16000,
        duration_ms=duration_ms,
        speech_ms=duration_ms,
        wait_speech_ms=80,
        peak_rms=peak_rms,
        peak_db=-34.0,
    )


def _fake_stt(texts: list[str]):
    from types import SimpleNamespace

    seq = list(texts)
    state = {"n": 0, "calls": 0}

    def transcribe(_wav, *, language=None):
        del language
        state["calls"] += 1
        i = min(state["n"], len(seq) - 1)
        state["n"] += 1
        text = seq[i] if seq else ""
        return SimpleNamespace(text=text, provider="fake")

    return SimpleNamespace(name="fake", transcribe=transcribe, state=state)


def _stub_speech_shell(monkeypatch):
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


def _open_silence(
    policy: AnswerWindowPolicy,
    *,
    cfg,
    stt,
    capture,
    syslog=None,
    **deps_extra,
):
    from hark.answer_window import open_answer_window

    deps_kw = dict(
        cfg=cfg,
        stt=stt,
        capture=capture,
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


def test_open_answer_window_empty_stt_retry_then_success(monkeypatch):
    """Deep seam: empty STT recovery via SilenceSession inside open_answer_window."""
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    cfg = HarkConfig(listen=ListenConfig(end_mode="silence"))
    policy = _policy(
        empty_stt_retry=True,
        empty_stt_nudge=False,
        last_tts="Pick one or two",
        post_tts_guard_s=0.1,
        end_mode=EndMode.SILENCE,
    )
    logs: list[tuple[str, dict]] = []
    caps = [_cap(duration_ms=2540), _cap(duration_ms=1200)]
    cap_iter = iter(caps)

    def fake_capture(**kwargs):
        del kwargs
        return next(cap_iter)

    stt = _fake_stt(["", "option two"])
    result = _open_silence(
        policy,
        cfg=cfg,
        stt=stt,
        capture=fake_capture,
        syslog=lambda event, **data: logs.append((event, data)),
    )
    assert result.text == "option two"
    assert result.end_mode == "silence"
    assert stt.state["calls"] == 2
    empty_events = [e for e, _ in logs if e == "speech.empty_stt"]
    assert len(empty_events) == 1
    payload = next(d for e, d in logs if e == "speech.empty_stt")
    assert payload["after_tts"] is True
    assert payload["duration_ms"] == 2540
    assert any(e == "speech.empty_stt_retry" for e, _ in logs)


def test_open_answer_window_empty_stt_nudge_then_success(monkeypatch):
    """Deep seam: empty STT nudge uses speech.run_tts (late-bound); recovery on session."""
    import hark.speech as speech
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    tts_calls: list[str] = []
    monkeypatch.setattr(
        speech,
        "run_tts",
        lambda cfg, text, **k: tts_calls.append(text) or {"ok": True},
    )
    cfg = HarkConfig(listen=ListenConfig(end_mode="silence"))
    policy = _policy(
        empty_stt_retry=False,
        empty_stt_nudge=True,
        last_tts="menu text here",
        post_tts_guard_s=0,
    )
    logs: list[str] = []
    caps = [_cap(), _cap()]
    cap_iter = iter(caps)

    result = _open_silence(
        policy,
        cfg=cfg,
        stt=_fake_stt(["", "yes please"]),
        capture=lambda **k: next(cap_iter),
        syslog=lambda event, **data: logs.append(event),
    )
    assert result.text == "yes please"
    assert tts_calls == [EMPTY_STT_NUDGE_TEXT]
    assert "speech.empty_stt" in logs
    assert "speech.empty_stt_nudge" in logs


def test_open_answer_window_empty_stt_exhausted(monkeypatch):
    import hark.speech as speech
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    tts_calls: list[str] = []
    monkeypatch.setattr(
        speech,
        "run_tts",
        lambda cfg, text, **k: tts_calls.append(text) or {"ok": True},
    )
    cfg = HarkConfig(listen=ListenConfig(end_mode="silence"))
    policy = _policy(
        empty_stt_retry=True,
        empty_stt_nudge=True,
        last_tts="long menu",
        post_tts_guard_s=0.05,
    )
    logs: list[tuple[str, dict]] = []
    caps = [_cap(), _cap(), _cap()]
    cap_iter = iter(caps)

    with pytest.raises(TimeoutError, match="empty text"):
        _open_silence(
            policy,
            cfg=cfg,
            stt=_fake_stt(["", "", ""]),
            capture=lambda **k: next(cap_iter),
            syslog=lambda event, **data: logs.append((event, data)),
        )
    assert tts_calls == [EMPTY_STT_NUDGE_TEXT]
    empty = [d for e, d in logs if e == "speech.empty_stt"]
    assert len(empty) == 3
    assert empty[0]["phase"] == "initial"
    assert empty[1]["phase"] == "retry"
    assert empty[2]["phase"] == "nudge"


def test_open_answer_window_silence_uses_end_silence_s(monkeypatch):
    """Deep seam: silence path ignores radio_partial_silence for capture hang."""
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    cfg = HarkConfig(listen=ListenConfig(end_mode="silence"))
    policy = _policy(
        end_silence_s=2.1,
        radio_partial_silence_s=0.5,
        radio_idle_end_silence_s=0.1,
        post_tts_guard_s=0,
    )
    calls: list[dict] = []

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        return _cap(duration_ms=40)

    result = _open_silence(
        policy,
        cfg=cfg,
        stt=_fake_stt(["one two three"]),
        capture=fake_capture,
    )
    assert result.end_mode == "silence"
    assert result.text == "one two three"
    assert calls[0]["end_silence_s"] == pytest.approx(2.1)
    assert calls[0]["end_silence_s"] != pytest.approx(0.5)


def test_open_answer_window_echo_reject(monkeypatch):
    """Deep seam: policy.last_tts drives echo reject without private hooks."""
    from hark.config import HarkConfig, ListenConfig
    from hark.providers.base import ProviderError

    _stub_speech_shell(monkeypatch)
    cfg = HarkConfig(listen=ListenConfig(end_mode="silence"))
    tts = (
        "Please answer what you know about the laptop state including Windows version "
        "BitLocker encryption local admin disk size free space and dual boot status "
        "when you are ready to continue with the backup plan to the NAS device."
    )
    policy = _policy(last_tts=tts, post_tts_guard_s=0, empty_stt_retry=False)
    with pytest.raises(ProviderError, match="TTS echo"):
        _open_silence(
            policy,
            cfg=cfg,
            stt=_fake_stt([tts]),
            capture=lambda **k: _cap(duration_ms=500),
        )
    # Short quote from the prompt is not echo (B093) — already unit-tested on
    # SilenceSession; deep seam accepts a real short answer.
    result = _open_silence(
        policy,
        cfg=cfg,
        stt=_fake_stt(["BitLocker."]),
        capture=lambda **k: _cap(duration_ms=500),
    )
    assert result.text == "BitLocker."


def test_open_answer_window_no_open_recovery(monkeypatch):
    """Deep seam: no-open retry/nudge/give-up without run_listen facade."""
    import hark.speech as speech
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    tts_calls: list[str] = []
    monkeypatch.setattr(
        speech,
        "run_tts",
        lambda cfg, text, **k: tts_calls.append(text) or {"ok": True},
    )
    cfg = HarkConfig(listen=ListenConfig(end_mode="silence"))
    policy = _policy(
        no_open_retry=True,
        no_open_nudge=True,
        post_tts_guard_s=0,
        no_open_nudge_text=NO_OPEN_NUDGE_TEXT,
    )
    logs: list[tuple[str, dict]] = []
    err = TimeoutError(
        "no speech detected (peak_db=-45.4 peak_rms=0.00537 open_thresh≈-38.0dB)"
    )

    def always_timeout(**kwargs):
        del kwargs
        raise err

    with pytest.raises(TimeoutError, match="no speech detected"):
        _open_silence(
            policy,
            cfg=cfg,
            stt=_fake_stt(["unused"]),
            capture=always_timeout,
            syslog=lambda event, **data: logs.append((event, data)),
        )
    assert tts_calls == [NO_OPEN_NUDGE_TEXT]
    no_open = [d for e, d in logs if e == "speech.no_open"]
    assert len(no_open) == 3
    assert [d["phase"] for d in no_open] == ["initial", "retry", "nudge"]

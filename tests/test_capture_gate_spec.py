"""One gate spec in, one typed outcome out (B079/B084/B007 seam).

Two things are pinned here:

* ``capture_utterance`` takes the gate facts as a single :class:`CaptureGateSpec`
  built once from :class:`AnswerWindowPolicy` — neither answer-window call site
  hand-assembles scalars, and neither re-applies the B079 pre-roll clamp that
  capture owns (so ``listen.pre_roll_ms = 0`` really does disable pre-roll).
* Why a capture stopped is a typed :class:`CaptureReason`, on the result or on
  :class:`CaptureTimeout`. No caller reads the English message.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import numpy as np
import pytest

from hark.answer_window import AnswerWindowDeps, AnswerWindowPolicy
from hark.answer_window.policy import gate_spec_from_policy, policy_from_config
from hark.answer_window.silence import is_no_open_timeout
from hark.audio.capture import CaptureGateSpec, CaptureReason, CaptureTimeout
from hark.listen_end import EndMode


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _policy(**kwargs) -> AnswerWindowPolicy:
    base = dict(
        profile="bound_answer",
        end_mode=EndMode.SILENCE,
        max_listen_s=60.0,
        end_silence_s=2.1,
        endpoint_strategy_name="energy",
    )
    base.update(kwargs)
    return AnswerWindowPolicy(**base)


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


def _fake_stt(texts: list[str]):
    seq = list(texts)
    state = {"n": 0}

    def transcribe(_wav, *, language=None):
        del language
        i = min(state["n"], len(seq) - 1)
        state["n"] += 1
        return SimpleNamespace(text=seq[i] if seq else "", provider="fake")

    return SimpleNamespace(name="fake", transcribe=transcribe, state=state)


def _cap(**kw):
    from hark.audio.capture import CaptureResult

    base = dict(
        pcm16=b"\x00\x00" * 1600,
        sample_rate=16000,
        duration_ms=2540,
        speech_ms=2540,
        wait_speech_ms=80,
        peak_rms=0.02,
        peak_db=-34.0,
    )
    base.update(kw)
    return CaptureResult(**base)


def _open(policy, *, cfg, stt, capture, **deps_extra):
    from hark.answer_window import open_answer_window

    deps_kw = dict(
        cfg=cfg,
        stt=stt,
        capture=capture,
        syslog=lambda *a, **k: None,
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


def _install_fake_stream(monkeypatch, stream):
    from hark.audio import capture as cap_mod

    monkeypatch.setattr(cap_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        cap_mod, "sd", SimpleNamespace(InputStream=lambda **kw: stream)
    )
    return cap_mod


class _RampThenSpeech:
    """Quiet ramp (detectable as pre-roll), then speech, then trailing silence."""

    def __init__(self, *, quiet_frames: int = 15, speech_frames: int = 25) -> None:
        self.reads = 0
        self._quiet = quiet_frames
        self._speech = speech_frames

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, block):
        self.reads += 1
        if self.reads <= self._quiet:
            level = 0.001 + 0.0001 * self.reads
            samples = np.full(block, level, dtype=np.float32)
        elif self.reads <= self._quiet + self._speech:
            samples = np.full(block, 0.4, dtype=np.float32)
        else:
            samples = np.zeros(block, dtype=np.float32)
        return samples.reshape(-1, 1), False


# ---------------------------------------------------------------------------
# One spec, derived once from the policy
# ---------------------------------------------------------------------------

def test_gate_spec_from_policy_carries_every_gate_fact():
    policy = _policy(
        max_listen_s=42.0,
        abs_open_db=-51.0,
        open_margin_db=6.5,
        initial_timeout_s=12.0,
        pre_roll_ms=420,
        mute_edge_pad_ms=180,
        endpoint_probe_silence_s=0.3,
        endpoint_max_silence_s=5.0,
    )
    spec = gate_spec_from_policy(policy, end_silence_s=1.5)

    assert spec.max_s == 42.0
    assert spec.end_silence_s == 1.5
    assert spec.abs_open_db == -51.0
    assert spec.open_margin_db == 6.5
    assert spec.initial_timeout_s == 12.0
    assert spec.preroll_ms == 420
    assert spec.mute_edge_pad_ms == 180
    assert spec.endpoint_probe_silence_s == 0.3
    assert spec.endpoint_max_silence_s == 5.0
    # Tuning without a config key keeps the product defaults.
    assert spec.sample_rate == 16000
    assert spec.min_speech_s == 0.25
    assert spec.open_confirm_blocks == 4
    assert spec.post_tts_guard_s == 0.0


def test_gate_spec_never_reapplies_the_pre_roll_clamp():
    """B079 clamp belongs to capture alone — the spec forwards what policy says."""
    assert gate_spec_from_policy(_policy(pre_roll_ms=0)).preroll_ms == 0
    assert gate_spec_from_policy(_policy(pre_roll_ms=100)).preroll_ms == 100


def test_policy_from_config_keeps_an_explicit_pre_roll_zero():
    cfg = SimpleNamespace(
        listen=SimpleNamespace(pre_roll_ms=0, end_mode="silence"),
        audio=None,
        ambient=None,
        stt=None,
    )
    assert policy_from_config(cfg).pre_roll_ms == 0
    # Absent key still defaults to the B079 300 ms.
    cfg.listen = SimpleNamespace(end_mode="silence")
    assert policy_from_config(cfg).pre_roll_ms == 300


def test_pre_roll_zero_reaches_capture_from_a_config_file(monkeypatch, tmp_path):
    """End to end: config → policy → answer window → capture spec, no clamp."""
    from hark.config import load_config

    path = tmp_path / "hark.toml"
    path.write_text("[listen]\nend_mode = \"silence\"\npre_roll_ms = 0\n")
    cfg = load_config(path)
    assert cfg.listen.pre_roll_ms == 0

    _stub_speech_shell(monkeypatch)
    seen: list[CaptureGateSpec] = []

    def spy(*, spec, **kwargs):
        del kwargs
        seen.append(spec)
        return _cap()

    _open(
        policy_from_config(cfg),
        cfg=cfg,
        stt=_fake_stt(["yes"]),
        capture=spy,
    )
    assert [s.preroll_ms for s in seen] == [0]


def test_out_of_range_pre_roll_still_clamps_at_config_load(tmp_path):
    from hark.config import load_config

    path = tmp_path / "hark.toml"
    path.write_text("[listen]\npre_roll_ms = 90\n")
    assert load_config(path).listen.pre_roll_ms == 250


def test_silence_call_site_passes_one_spec_and_no_gate_scalars(monkeypatch):
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    seen: list[dict] = []

    def spy(**kwargs):
        seen.append(kwargs)
        return _cap()

    policy = _policy(abs_open_db=-44.0, initial_timeout_s=9.0, pre_roll_ms=310)
    _open(
        policy,
        cfg=HarkConfig(listen=ListenConfig(end_mode="silence")),
        stt=_fake_stt(["ok"]),
        capture=spy,
    )
    assert len(seen) == 1
    kwargs = seen[0]
    spec = kwargs["spec"]
    assert spec.abs_open_db == -44.0
    assert spec.initial_timeout_s == 9.0
    assert spec.preroll_ms == 310
    # Gate facts travel on the spec only.
    for stale in (
        "abs_open_db",
        "open_margin_db",
        "initial_timeout_s",
        "preroll_ms",
        "mute_edge_pad_ms",
        "max_s",
        "end_silence_s",
        "post_tts_guard_s",
    ):
        assert stale not in kwargs, f"{stale} still passed alongside the spec"


def test_radio_call_site_passes_one_spec_with_its_own_timeout(monkeypatch):
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    seen: list[dict] = []

    def spy(**kwargs):
        seen.append(kwargs)
        raise CaptureTimeout("no speech detected (peak_db=-60.0)", peak_db=-60.0)

    policy = _policy(
        end_mode=EndMode.RADIO,
        abs_open_db=-44.0,
        initial_timeout_s=9.0,
        pre_roll_ms=310,
        radio_partial_silence_s=0.6,
        max_listen_s=30.0,
    )
    with pytest.raises(TimeoutError):
        _open(
            policy,
            cfg=HarkConfig(listen=ListenConfig(end_mode="radio")),
            stt=_fake_stt(["ok"]),
            capture=spy,
        )
    assert seen
    spec = seen[0]["spec"]
    assert spec.abs_open_db == -44.0
    assert spec.preroll_ms == 310
    # Radio keeps its own per-segment window (B074), not policy.initial_timeout_s
    # verbatim: it is min(gate timeout, remaining).
    assert 0 < spec.initial_timeout_s <= 9.0
    # Radio's end_silence is the *segment cut*, not "utterance ended".
    assert spec.end_silence_s == 0.6
    for stale in ("abs_open_db", "initial_timeout_s", "preroll_ms", "max_s"):
        assert stale not in seen[0]


# ---------------------------------------------------------------------------
# Capture owns the B079 clamp
# ---------------------------------------------------------------------------

def test_capture_disables_pre_roll_when_the_spec_says_zero(monkeypatch):
    """``preroll_ms=0`` keeps no leading audio — unreachable while callers clamped."""
    stream = _RampThenSpeech()
    cap_mod = _install_fake_stream(monkeypatch, stream)

    result = cap_mod.capture_utterance(
        spec=CaptureGateSpec(
            max_s=3.0,
            end_silence_s=0.12,
            min_speech_s=0.05,
            open_confirm_blocks=2,
            open_margin_db=6.0,
            # Ramp (≈ -52 dB at its loudest) must stay under the floor, or the
            # gate opens on it and there is no pre-roll question to ask.
            abs_open_db=-40.0,
            initial_timeout_s=2.0,
            preroll_ms=0,
        )
    )
    samples = np.frombuffer(result.pcm16, dtype=np.int16).astype(np.float32) / 32767.0
    # Buffer starts at the speech burst: no quiet ramp head at all.
    assert float(np.min(np.abs(samples[: int(0.05 * 16000)]))) > 0.2


def test_capture_clamps_a_too_small_pre_roll_itself(monkeypatch):
    stream = _RampThenSpeech()
    cap_mod = _install_fake_stream(monkeypatch, stream)

    result = cap_mod.capture_utterance(
        spec=CaptureGateSpec(
            max_s=3.0,
            end_silence_s=0.12,
            min_speech_s=0.05,
            open_confirm_blocks=2,
            open_margin_db=6.0,
            abs_open_db=-40.0,
            initial_timeout_s=2.0,
            preroll_ms=100,  # below the B079 floor
        )
    )
    samples = np.frombuffer(result.pcm16, dtype=np.int16).astype(np.float32) / 32767.0
    head = samples[: int(0.2 * 16000)]
    mid = samples[int(0.2 * 16000) : int(0.5 * 16000)]
    assert float(np.max(np.abs(head))) < float(np.max(np.abs(mid))) * 0.5


# ---------------------------------------------------------------------------
# Typed outcome
# ---------------------------------------------------------------------------

def test_result_reason_is_silence_when_the_gate_ends_the_turn(monkeypatch):
    stream = _RampThenSpeech(quiet_frames=2, speech_frames=5)
    cap_mod = _install_fake_stream(monkeypatch, stream)

    result = cap_mod.capture_utterance(
        spec=CaptureGateSpec(
            max_s=1.0,
            end_silence_s=0.1,
            min_speech_s=0.04,
            open_confirm_blocks=2,
            abs_open_db=-60.0,
            initial_timeout_s=1.0,
            preroll_ms=0,
        )
    )
    assert result.reason is CaptureReason.SILENCE


def test_result_reason_is_agent_stop_when_should_stop_fires(monkeypatch):
    class Loud:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, block):
            return np.full(block, 0.4, dtype=np.float32).reshape(-1, 1), False

    cap_mod = _install_fake_stream(monkeypatch, Loud())
    calls = {"n": 0}

    def should_stop(_pcm, _elapsed):
        calls["n"] += 1
        return calls["n"] > 3

    result = cap_mod.capture_utterance(
        spec=CaptureGateSpec(
            max_s=5.0,
            end_silence_s=0.2,
            min_speech_s=0.02,
            open_confirm_blocks=2,
            abs_open_db=-60.0,
            initial_timeout_s=1.0,
            preroll_ms=0,
        ),
        should_stop=should_stop,
    )
    assert result.reason is CaptureReason.AGENT_STOP


def test_result_reason_is_max_duration_when_max_s_runs_out(monkeypatch):
    class Loud:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, block):
            return np.full(block, 0.4, dtype=np.float32).reshape(-1, 1), False

    cap_mod = _install_fake_stream(monkeypatch, Loud())

    result = cap_mod.capture_utterance(
        spec=CaptureGateSpec(
            max_s=0.1,  # 5 blocks
            end_silence_s=2.0,
            min_speech_s=0.02,
            open_confirm_blocks=2,
            abs_open_db=-60.0,
            initial_timeout_s=1.0,
            preroll_ms=0,
        )
    )
    assert result.reason is CaptureReason.MAX_DURATION


def test_no_open_timeout_is_typed_and_carries_the_peaks(monkeypatch):
    class Quiet:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, block):
            return np.full(block, 1e-5, dtype=np.float32).reshape(-1, 1), False

    cap_mod = _install_fake_stream(monkeypatch, Quiet())

    with pytest.raises(CaptureTimeout) as excinfo:
        cap_mod.capture_utterance(
            spec=CaptureGateSpec(
                max_s=5.0,
                end_silence_s=0.2,
                min_speech_s=0.02,
                initial_timeout_s=0.1,  # 5 blocks
                preroll_ms=0,
            )
        )
    exc = excinfo.value
    assert isinstance(exc, TimeoutError)  # existing handlers keep working
    assert exc.reason is CaptureReason.NO_OPEN
    assert exc.no_open is True
    assert exc.peak_db < -60.0
    assert exc.peak_rms >= 0.0
    assert exc.open_thresh_db is not None


def test_no_audio_captured_is_typed_no_open(monkeypatch):
    """should_stop before the gate opens → no audio, still a typed no-open."""
    class Quiet:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, block):
            return np.full(block, 1e-5, dtype=np.float32).reshape(-1, 1), False

    cap_mod = _install_fake_stream(monkeypatch, Quiet())

    with pytest.raises(CaptureTimeout) as excinfo:
        cap_mod.capture_utterance(
            spec=CaptureGateSpec(
                max_s=0.1,
                end_silence_s=0.2,
                min_speech_s=0.02,
                initial_timeout_s=5.0,
                preroll_ms=0,
            )
        )
    assert excinfo.value.reason is CaptureReason.NO_OPEN


def test_discard_timeout_is_typed_and_is_not_a_no_open(monkeypatch):
    class Quiet:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, block):
            time.sleep(0.002)
            return np.zeros(block, dtype=np.float32).reshape(-1, 1), False

    cap_mod = _install_fake_stream(monkeypatch, Quiet())
    monkeypatch.setattr(cap_mod, "_still_discarding", lambda **kw: True)

    with pytest.raises(CaptureTimeout) as excinfo:
        cap_mod.capture_utterance(
            spec=CaptureGateSpec(
                max_s=1.0,
                initial_timeout_s=0.05,
                preroll_ms=0,
                # Without this the cap floors at 30 s and the test spends 30 s of
                # real wall clock reaching the branch it is asserting on.
                discard_max_floor_s=0.05,
            ),
            audio_ok_after=lambda: None,
        )
    exc = excinfo.value
    assert exc.reason is CaptureReason.DISCARD_TIMEOUT
    assert exc.no_open is False
    assert is_no_open_timeout(exc) is False


def test_is_no_open_timeout_reads_the_reason_not_the_message():
    """The substring sniff is gone: only the typed reason decides."""
    assert is_no_open_timeout(CaptureTimeout("anything at all")) is True
    assert (
        is_no_open_timeout(
            CaptureTimeout("x", reason=CaptureReason.DISCARD_TIMEOUT)
        )
        is False
    )
    # Message text alone no longer votes.
    assert is_no_open_timeout(TimeoutError("no speech detected (peak_db=-60)")) is False
    assert is_no_open_timeout(RuntimeError("no speech captured")) is False


def test_no_open_log_gets_the_peaks_from_the_exception(monkeypatch):
    """The typed exception feeds speech.no_open — no regex over the message."""
    from hark.config import HarkConfig, ListenConfig

    _stub_speech_shell(monkeypatch)
    logs: list[tuple[str, dict]] = []

    def spy(**kwargs):
        del kwargs
        raise CaptureTimeout(
            "gate stayed shut",  # deliberately carries no peak_db= text
            peak_db=-45.4,
            peak_rms=0.00537,
            open_thresh_db=-38.0,
        )

    with pytest.raises(TimeoutError):
        _open(
            _policy(no_open_retry=False, no_open_nudge=False),
            cfg=HarkConfig(listen=ListenConfig(end_mode="silence")),
            stt=_fake_stt(["unused"]),
            capture=spy,
            syslog=lambda event, **data: logs.append((event, data)),
        )
    payload = next(d for e, d in logs if e == "speech.no_open")
    assert payload["peak_db"] == pytest.approx(-45.4)
    assert payload["rms"] == pytest.approx(0.00537)
    assert payload["open_thresh"] == pytest.approx(-38.0)


# ---------------------------------------------------------------------------
# Test-facing seam for tuning that has no config key
# ---------------------------------------------------------------------------

def test_spec_field_overrides_reach_the_gate(monkeypatch):
    """Kwargs are ``replace`` on the spec — the documented test way in."""
    stream = _RampThenSpeech(quiet_frames=2, speech_frames=5)
    cap_mod = _install_fake_stream(monkeypatch, stream)

    result = cap_mod.capture_utterance(
        max_s=1.0,
        end_silence_s=0.1,
        min_speech_s=0.04,
        open_confirm_blocks=2,
        initial_timeout_s=1.0,
        abs_open_db=-60.0,
        preroll_ms=0,
    )
    assert result.reason is CaptureReason.SILENCE


def test_unknown_spec_override_still_fails_loudly(monkeypatch):
    _install_fake_stream(monkeypatch, _RampThenSpeech())
    from hark.audio import capture as cap_mod

    with pytest.raises(TypeError):
        cap_mod.capture_utterance(no_such_gate_knob=1)

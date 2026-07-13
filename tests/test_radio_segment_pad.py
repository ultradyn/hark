"""B075: radio segment silence pad at boundaries (post-cut, not pre-roll)."""

from __future__ import annotations

from types import SimpleNamespace

from hark.audio.capture import (
    effective_radio_segment_pad_ms,
    pad_pcm16_silence,
    write_wav_bytes,
)
from hark.config import HarkConfig, ListenConfig, config_to_dict, load_config


def test_pad_pcm16_expands_bounds_preserves_mid_speech():
    """Synthetic PCM: pad grows lead/trail silence; mid samples unchanged."""
    sr = 16000
    # 100 ms of non-zero "speech" samples (int16 little-endian 0x0001)
    speech = b"\x01\x00" * 1600
    pad_ms = 200
    out = pad_pcm16_silence(speech, pad_ms=pad_ms, sample_rate=sr)
    n_side = int(sr * pad_ms / 1000.0)  # samples each side
    side_bytes = n_side * 2
    assert len(out) == len(speech) + 2 * side_bytes
    assert out[:side_bytes] == b"\x00\x00" * n_side
    assert out[-side_bytes:] == b"\x00\x00" * n_side
    assert out[side_bytes:-side_bytes] == speech
    # Mid-speech identity: no sample mutation
    mid = speech[100:200]
    assert mid in out
    assert out[side_bytes + 100 : side_bytes + 200] == mid


def test_pad_pcm16_asymmetric_lead_trail():
    speech = b"\x02\x00" * 100
    out = pad_pcm16_silence(
        speech, pad_ms=0, sample_rate=16000, lead_ms=50, trail_ms=100
    )
    n_lead = int(16000 * 50 / 1000)
    n_trail = int(16000 * 100 / 1000)
    assert out[: n_lead * 2] == b"\x00\x00" * n_lead
    assert out[-n_trail * 2 :] == b"\x00\x00" * n_trail
    assert out[n_lead * 2 : -n_trail * 2] == speech
    assert len(out) == len(speech) + (n_lead + n_trail) * 2


def test_pad_pcm16_zero_is_noop():
    speech = b"\x03\x00" * 50
    assert pad_pcm16_silence(speech, pad_ms=0) == speech


def test_effective_pad_clamped_under_silence_budget():
    # Default silence 0.6s → budget 240 ms; absolute max 300
    assert effective_radio_segment_pad_ms(250, 0.6) == 240
    assert effective_radio_segment_pad_ms(200, 0.6) == 200
    assert effective_radio_segment_pad_ms(500, 0.6) == 240
    assert effective_radio_segment_pad_ms(500, 1.0) == 300  # hit absolute max
    assert effective_radio_segment_pad_ms(0, 0.6) == 0
    assert effective_radio_segment_pad_ms(-10, 0.6) == 0
    assert effective_radio_segment_pad_ms(250, 0.0) == 0


def test_default_radio_segment_pad_config():
    cfg = HarkConfig()
    assert cfg.listen.radio_segment_pad_ms == 250
    # Effective with default partial silence
    eff = effective_radio_segment_pad_ms(
        cfg.listen.radio_segment_pad_ms, cfg.listen.radio_partial_silence_s
    )
    assert 150 <= eff <= 300
    assert eff <= int(cfg.listen.radio_partial_silence_s * 1000 * 0.4)


def test_config_loads_radio_segment_pad(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[listen]
end_mode = "radio"
radio_partial_silence_s = 0.5
radio_segment_pad_ms = 180
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.listen.radio_segment_pad_ms == 180
    dumped = config_to_dict(cfg)["listen"]
    assert dumped["radio_segment_pad_ms"] == 180
    assert dumped["radio_partial_silence_s"] == 0.5


def _null_ctx_factory():
    class NullContext:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return NullContext


def _stub_listen_deps(monkeypatch, speech, *, transcript: str = "okay hark send"):
    NullContext = _null_ctx_factory()

    class FakeStore:
        def record_stt(self, **kwargs):
            pass

    monkeypatch.setattr(
        speech,
        "resolve_stt",
        lambda *args, **kwargs: SimpleNamespace(
            name="fake",
            transcribe=lambda wav: SimpleNamespace(text=transcript, provider="fake"),
        ),
    )
    monkeypatch.setattr(speech, "pause_ambient_for_mic", lambda **kwargs: NullContext())
    monkeypatch.setattr(speech, "MicLease", lambda *args: NullContext())
    monkeypatch.setattr(speech, "BusySection", lambda *args: NullContext())
    monkeypatch.setattr(speech, "UsageStore", FakeStore)
    monkeypatch.setattr(speech, "configure_cues_from_config", lambda cfg: None)
    monkeypatch.setattr(speech, "register_active_listen", lambda *args, **kwargs: None)
    monkeypatch.setattr(speech, "clear_active_listen", lambda *args, **kwargs: None)
    monkeypatch.setattr(speech, "poll_listen_action", lambda *args: None)
    monkeypatch.setattr(speech, "consume_listen_action", lambda *args: None)
    monkeypatch.setattr(speech, "play_record_start", lambda: None)
    monkeypatch.setattr(speech, "play_record_stop", lambda: None)


def test_radio_stt_upload_includes_silence_pad(monkeypatch):
    """Radio path pads segment PCM before STT; mid-speech samples preserved."""
    import hark.speech as speech
    from hark.audio.capture import CaptureResult

    sr = 16000
    # Distinct non-zero pattern so we can find it inside the uploaded WAV payload
    speech_pcm = bytes([(i % 200) + 1 for i in range(3200)])  # 1600 samples, odd pattern
    # Ensure even length for PCM16 (already even)
    assert len(speech_pcm) % 2 == 0

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="radio",
            radio_partial_silence_s=0.6,
            radio_segment_pad_ms=200,
            stream_partials=True,
        )
    )
    uploaded: list[bytes] = []

    def fake_capture(**kwargs):
        return CaptureResult(
            pcm16=speech_pcm,
            sample_rate=sr,
            duration_ms=100,
            speech_ms=100,
        )

    def fake_transcribe(wav: bytes):
        uploaded.append(wav)
        return SimpleNamespace(text="hello world okay hark send", provider="fake")

    _stub_listen_deps(monkeypatch, speech)
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)
    monkeypatch.setattr(
        speech,
        "resolve_stt",
        lambda *args, **kwargs: SimpleNamespace(
            name="fake", transcribe=fake_transcribe
        ),
    )

    result = speech.run_listen(cfg, end_mode="radio", post_tts_guard_s=0)
    assert uploaded
    wav = uploaded[0]
    # WAV should contain the original mid-speech bytes
    assert speech_pcm in wav
    # And be longer than unpadded: 200ms * 2 sides * 16k * 2 bytes = 12800
    bare = write_wav_bytes(speech_pcm, sr)
    assert len(wav) >= len(bare) + 12800 - 44  # header variance ok; pad is bulk
    # Explicit: pad zeros surround speech inside PCM frames of the WAV
    # Parse via wave would be ideal; simpler: padded PCM reconstruct
    pad_ms = effective_radio_segment_pad_ms(200, 0.6)
    expected_pcm = pad_pcm16_silence(speech_pcm, pad_ms=pad_ms, sample_rate=sr)
    assert expected_pcm in wav
    assert result.end_mode == "radio"


def test_silence_mode_does_not_pad_stt_upload(monkeypatch):
    """Silence end_mode uploads raw capture PCM without radio segment pad."""
    import hark.speech as speech
    from hark.audio.capture import CaptureResult

    sr = 16000
    speech_pcm = b"\x05\x00" * 800

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="silence",
            end_silence_s=2.1,
            radio_segment_pad_ms=250,
            radio_partial_silence_s=0.6,
        )
    )
    uploaded: list[bytes] = []

    def fake_capture(**kwargs):
        return CaptureResult(
            pcm16=speech_pcm,
            sample_rate=sr,
            duration_ms=50,
            speech_ms=50,
        )

    def fake_transcribe(wav: bytes):
        uploaded.append(wav)
        return SimpleNamespace(text="one two three", provider="fake")

    _stub_listen_deps(monkeypatch, speech, transcript="one two three")
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)
    monkeypatch.setattr(
        speech,
        "resolve_stt",
        lambda *args, **kwargs: SimpleNamespace(
            name="fake", transcribe=fake_transcribe
        ),
    )

    result = speech.run_listen(cfg, end_mode="silence", post_tts_guard_s=0)
    assert uploaded
    # Exact bare WAV — no pad
    assert uploaded[0] == write_wav_bytes(speech_pcm, sr)
    assert result.end_mode == "silence"
    assert result.text == "one two three"


def test_radio_partials_still_work_with_pad(monkeypatch):
    """Pad must not break soft-end / partial / multi-segment finalize path."""
    import hark.speech as speech
    from hark.audio.capture import CaptureResult

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="radio",
            radio_partial_silence_s=0.5,
            radio_segment_pad_ms=200,
            stream_partials=True,
        )
    )
    transcripts = [
        "please open the pull request",
        "please open the pull request for auth",
        "please open the pull request for auth okay hark send",
    ]
    idx = {"n": 0}
    partials: list[dict] = []

    def fake_capture(**kwargs):
        return CaptureResult(
            pcm16=b"\0\0" * 10,
            sample_rate=16000,
            duration_ms=40,
            speech_ms=40,
        )

    def fake_transcribe(_wav):
        i = min(idx["n"], len(transcripts) - 1)
        idx["n"] += 1
        return SimpleNamespace(text=transcripts[i], provider="fake")

    _stub_listen_deps(monkeypatch, speech)
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)
    monkeypatch.setattr(
        speech,
        "resolve_stt",
        lambda *args, **kwargs: SimpleNamespace(
            name="fake", transcribe=fake_transcribe
        ),
    )

    result = speech.run_listen(
        cfg,
        end_mode="radio",
        post_tts_guard_s=0,
        on_partial=partials.append,
    )
    assert len(partials) >= 2
    assert result.end_phrase == "okay hark send"
    assert result.cancelled is False
    assert "pull request" in (result.text or "").lower()


def test_join_radio_stt_segments_overlap_trim():
    from hark.speech import join_radio_stt_segments

    assert join_radio_stt_segments(["hello world", "world there"]) == "hello world there"
    assert join_radio_stt_segments(["alpha", "beta gamma"]) == "alpha beta gamma"


def test_prefer_complete_transcript_never_shrinks():
    from hark.speech import monotonic_partial_text, prefer_complete_transcript

    assert prefer_complete_transcript("hello world", "hello") == "hello world"
    assert prefer_complete_transcript("hello", "hello world there") == "hello world there"
    assert prefer_complete_transcript("", "x") == "x"
    assert monotonic_partial_text("one two three", "one two") == "one two three"
    assert monotonic_partial_text("one two", "one two three four") == "one two three four"

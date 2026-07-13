"""B037: radio mode uses a shorter silence for interim STT/partials only."""

from __future__ import annotations

from types import SimpleNamespace

from hark.config import HarkConfig, ListenConfig, config_to_dict, load_config


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


def test_default_radio_partial_silence_is_short():
    cfg = HarkConfig()
    assert cfg.listen.radio_partial_silence_s == 0.6
    # Silence-mode end hang stays long; radio partial is independent
    assert cfg.listen.end_silence_s == 2.1
    assert cfg.listen.radio_partial_silence_s < cfg.listen.end_silence_s
    assert cfg.listen.radio_partial_silence_s < cfg.listen.radio_end_silence_s


def test_config_loads_radio_partial_silence(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[listen]
end_mode = "radio"
radio_partial_silence_s = 0.5
end_silence_s = 2.1
radio_end_silence_s = 2.5
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.listen.radio_partial_silence_s == 0.5
    assert cfg.listen.end_silence_s == 2.1
    assert cfg.listen.radio_end_silence_s == 2.5
    dumped = config_to_dict(cfg)["listen"]
    assert dumped["radio_partial_silence_s"] == 0.5
    assert dumped["end_silence_s"] == 2.1


def test_radio_capture_uses_partial_silence_not_end_silence(monkeypatch):
    """Radio segments pass radio_partial_silence_s as capture end_silence_s."""
    import hark.speech as speech
    from hark.audio.capture import CaptureResult

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="radio",
            end_silence_s=2.1,
            radio_end_silence_s=2.5,
            radio_partial_silence_s=0.55,
            stream_partials=True,
        )
    )
    calls: list[dict] = []

    def fake_capture(**kwargs):
        calls.append(kwargs)
        return CaptureResult(
            pcm16=b"\0\0",
            sample_rate=16000,
            duration_ms=20,
            speech_ms=20,
        )

    _stub_listen_deps(monkeypatch, speech, transcript="please open the PR okay hark send")
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)

    result = speech.run_listen(cfg, end_mode="radio", post_tts_guard_s=0)
    assert calls
    assert calls[0]["end_silence_s"] == 0.55
    # Must not use silence-mode or legacy radio hang for partial cadence
    assert calls[0]["end_silence_s"] != cfg.listen.end_silence_s
    assert calls[0]["end_silence_s"] != cfg.listen.radio_end_silence_s
    assert result.end_mode == "radio"
    assert result.end_phrase  # still finalized by end phrase path


def test_silence_mode_still_uses_end_silence_s(monkeypatch):
    """Normal answer windows keep end_silence_s; radio partial key ignored."""
    import hark.speech as speech
    from hark.audio.capture import CaptureResult

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="silence",
            end_silence_s=2.1,
            radio_partial_silence_s=0.5,
            radio_end_silence_s=2.5,
        )
    )
    calls: list[dict] = []

    def fake_capture(**kwargs):
        calls.append(kwargs)
        return CaptureResult(
            pcm16=b"\0\0",
            sample_rate=16000,
            duration_ms=20,
            speech_ms=20,
        )

    _stub_listen_deps(monkeypatch, speech, transcript="one two three")
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)

    result = speech.run_listen(cfg, end_mode="silence", post_tts_guard_s=0)
    assert calls
    assert calls[0]["end_silence_s"] == 2.1
    assert result.text == "one two three"
    assert result.end_mode == "silence"


def test_radio_partials_emit_before_end_phrase(monkeypatch):
    """Multiple short segments: growing text → partials; end phrase finalizes."""
    import hark.speech as speech
    from hark.audio.capture import CaptureResult

    cfg = HarkConfig(
        listen=ListenConfig(
            end_mode="radio",
            radio_partial_silence_s=0.5,
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
        assert kwargs["end_silence_s"] == 0.5
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
        lambda *args, **kwargs: SimpleNamespace(name="fake", transcribe=fake_transcribe),
    )

    result = speech.run_listen(
        cfg,
        end_mode="radio",
        post_tts_guard_s=0,
        on_partial=partials.append,
    )
    assert len(partials) >= 2
    assert all(p.get("partial") is True for p in partials)
    assert all(p.get("final") is False for p in partials)
    assert result.end_phrase == "okay hark send"
    assert result.cancelled is False
    assert "okay hark send" not in (result.text or "").lower() or cfg.listen.strip_phrase
    # Body strips end phrase when strip_phrase (default)
    assert "pull request" in (result.text or "").lower()
    assert result.partials_emitted >= 2

"""Bound silent synthesis, partial results and supervisor exit without real audio."""

import os
import struct
import sys
import time
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from hark.providers.base import ProviderError
from hark.speech import _InterruptibleSynthPool
from hark.tts_isolation import SubprocessSynthTransport, SynthRequest, _DeadlinePipe


def test_deadline_pipe_bounds_a_partial_frame():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, struct.pack("!I", 10)[:2])
        with pytest.raises(ProviderError, match="deadline exceeded"):
            _DeadlinePipe(read_fd, time.monotonic() + 0.05).read(4)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_deadline_pipe_reads_exact_size_and_eof():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"hello")
        os.close(write_fd)
        write_fd = -1
        reader = _DeadlinePipe(read_fd, time.monotonic() + 5)
        assert reader.read(3) == b"hel"
        assert reader.read(3) == b"lo"
        assert reader.read(1) == b""
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def test_deadline_pipe_bounds_a_worker_that_does_not_read_request():
    read_fd, write_fd = os.pipe()
    try:
        with pytest.raises(ProviderError, match="deadline exceeded"):
            _DeadlinePipe(write_fd, time.monotonic() + 0.05).write(b"x" * 1000000)
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_deadline_rejected(timeout):
    with pytest.raises(ValueError):
        SubprocessSynthTransport(object(), timeout_s=timeout)


@pytest.mark.skipif(
    sys.platform != "linux", reason="isolated worker supervisor uses Linux cleanup"
)
def test_hung_synthesis_cancels_owned_worker_and_releases_authority():
    # Use the actual cleanup supervisor; only the provider payload is replaced.
    owner = _InterruptibleSynthPool()
    transport = SubprocessSynthTransport(
        owner,
        command_factory=lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-hang",
        ],
        timeout_s=1.0,
    )
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError, match="deadline exceeded"):
            transport.synthesize(SynthRequest("xai", "eve", "en", "hello"))
        assert time.monotonic() - started < 10
        assert not owner._process_lifecycle.active
    finally:
        owner._process_lifecycle.cancel()
        owner._pool.shutdown(wait=False, cancel_futures=True)


@pytest.mark.skipif(sys.platform != "linux", reason="POSIX result pipe")
def test_deadline_covers_supervisor_exit_after_valid_result():
    source = (
        "import os,json,struct,time; "
        "fd=int(os.environ['HARK_TTS_RESULT_FD']); "
        "m=json.dumps(dict(status='ok',audio_size=0,provider='test',content_type='audio/wav',voice='v')).encode(); "
        "os.write(fd,struct.pack('!I',len(m))+m); os.close(fd); time.sleep(120)"
    )
    owner = _InterruptibleSynthPool()
    transport = SubprocessSynthTransport(
        owner,
        command_factory=lambda: [sys.executable, "-c", source],
        timeout_s=1.0,
    )
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError, match="deadline exceeded"):
            transport.synthesize(SynthRequest("test", "v", "en", "hello"))
        assert time.monotonic() - started < 10
        assert not owner._process_lifecycle.active
    finally:
        owner._process_lifecycle.cancel()
        owner._pool.shutdown(wait=False, cancel_futures=True)


def test_run_tts_later_timeout_restores_playback_mic_media_and_queue(monkeypatch):
    from hark import speech
    from hark.config import HarkConfig

    events = []

    @contextmanager
    def hold(name, value):
        events.append(f"{name}.enter")
        try:
            yield value
        finally:
            events.append(f"{name}.exit")

    class Transport:
        def synthesize(self, request):
            if request.text == "second":
                raise ProviderError("TTS synthesis deadline exceeded")
            return SimpleNamespace(
                audio=b"test", provider="test", content_type="audio/wav", voice="v"
            )

    class Store:
        def record_tts(self, **kwargs):
            events.append("record")

    monkeypatch.setattr(speech, "UsageStore", Store)
    monkeypatch.setattr(
        speech, "resolve_tts", lambda *a, **k: SimpleNamespace(name="test")
    )
    monkeypatch.setattr(speech, "_synth_transport_factory", lambda owner: Transport())
    monkeypatch.setattr(speech, "pack_tts_chunks", lambda *a: ["first", "second"])
    monkeypatch.setattr(speech, "claim_tts_play_ticket", lambda: "ticket")
    monkeypatch.setattr(
        speech, "abandon_tts_play_ticket", lambda ticket, **kw: events.append("abandon")
    )
    monkeypatch.setattr(speech, "exclusive_playback", lambda **kw: hold("play", None))
    monkeypatch.setattr(
        speech, "user_capture_active", lambda **kw: SimpleNamespace(active=False)
    )
    monkeypatch.setattr(
        speech,
        "mic_muted_during_tts",
        lambda **kw: hold("mic", SimpleNamespace(applied=True, was_muted=False)),
    )
    monkeypatch.setattr(
        speech,
        "duck_media",
        lambda *a, **kw: hold("media", SimpleNamespace(as_meta=dict)),
    )
    monkeypatch.setattr(speech, "tts_skip_notification", lambda *a: nullcontext())
    monkeypatch.setattr(
        speech, "play_wav_bytes", lambda *a, **kw: SimpleNamespace(duration_ms=1)
    )
    monkeypatch.setattr(
        speech, "repair_tts_mute_after_play", lambda **kw: events.append("repair")
    )
    cfg = HarkConfig()
    cfg.audio.defer_tts_while_listening = False
    cfg.audio.hold_during_conference = False
    with pytest.raises(ProviderError, match="deadline exceeded"):
        speech.run_tts(cfg, "first second", use_cache=False, conference_policy="force")
    assert [e for e in events if e.endswith(".exit")] == [
        "media.exit",
        "mic.exit",
        "play.exit",
    ]
    assert "abandon" in events
    assert "repair" in events

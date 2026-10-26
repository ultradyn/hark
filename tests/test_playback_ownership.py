"""Owned PCM streams: cue overlap must not truncate TTS or share cleanup."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from hark.audio import playback


class CallbackStop(Exception):
    pass


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = threading.Event()
        self.closed = 0
        self.aborted = 0
        self.buffers = []

    def start(self):
        self.started.set()

    def emit(self, frames):
        output = bytearray(frames * 2)
        try:
            self.kwargs["callback"](output, frames, None, None)
        except CallbackStop:
            self.kwargs["finished_callback"]()
        self.buffers.append(bytes(output))

    def abort(self):
        self.aborted += 1

    def close(self):
        self.closed += 1


def test_overlapping_cue_and_tts_deliver_full_pcm_to_independent_streams(monkeypatch):
    streams = []
    created = threading.Condition()
    failures = []

    def make_stream(**kwargs):
        stream = FakeStream(**kwargs)
        with created:
            streams.append(stream)
            created.notify_all()
        return stream

    monkeypatch.setattr(playback, "sd", SimpleNamespace(
        RawOutputStream=make_stream, CallbackStop=CallbackStop,
    ))
    # Distinctive first samples make preamble loss observable; use uneven
    # callback sizes to exercise the final partial buffer and zero padding.
    tts_pcm = bytes(range(20))
    cue_pcm = b"\x11\x22" * 3

    def play(pcm):
        try:
            playback._play_pcm16(pcm, 16000)
        except BaseException as exc:
            failures.append(exc)

    tts = threading.Thread(target=play, args=(tts_pcm,))
    cue = threading.Thread(target=play, args=(cue_pcm,))
    tts.start()
    try:
        with created:
            assert created.wait_for(lambda: len(streams) >= 1, timeout=5)
        assert streams[0].started.wait(5)
        streams[0].emit(3)
        cue.start()
        with created:
            assert created.wait_for(lambda: len(streams) >= 2, timeout=5)
        assert streams[1].started.wait(5)
        streams[1].emit(4)
        cue.join(5)
        assert not cue.is_alive()
        assert tts.is_alive()
        assert streams[0].closed == streams[0].aborted == 0
        streams[0].emit(4)
        streams[0].emit(4)
        tts.join(5)
        assert not tts.is_alive()
        assert not failures
        assert b"".join(streams[0].buffers) == tts_pcm + b"\x00" * 2
        assert b"".join(streams[1].buffers) == cue_pcm + b"\x00" * 2
        assert [stream.closed for stream in streams] == [1, 1]
        assert [stream.aborted for stream in streams] == [0, 0]
        assert all(stream.kwargs["dtype"] == "int16" for stream in streams)
    finally:
        for stream in streams:
            stream.kwargs["finished_callback"]()
        tts.join(5)
        if cue.ident is not None:
            cue.join(5)


def test_missing_completion_callback_times_out_and_cleans_owned_stream(monkeypatch):
    stream = FakeStream()
    budgets = []

    class FakeEvent:
        active = False

        def set(self):
            self.active = True

        def is_set(self):
            return self.active

        def wait(self, timeout):
            budgets.append(timeout)
            return False

    monkeypatch.setattr(playback.threading, "Event", FakeEvent)
    monkeypatch.setattr(playback, "sd", SimpleNamespace(
        RawOutputStream=lambda **kwargs: stream, CallbackStop=CallbackStop,
    ))
    with pytest.raises(TimeoutError, match="duration budget"):
        playback._play_pcm16(b"\x01\x00" * 1600, 16000)
    assert budgets == [pytest.approx(5.1)]
    assert stream.aborted == stream.closed == 1


def test_start_failure_survives_secondary_close_failure(monkeypatch):
    class BrokenStream(FakeStream):
        def start(self):
            raise RuntimeError("start failed")

        def close(self):
            super().close()
            raise OSError("close failed")

    stream = BrokenStream()
    monkeypatch.setattr(playback, "sd", SimpleNamespace(
        RawOutputStream=lambda **kwargs: stream, CallbackStop=CallbackStop,
    ))
    with pytest.raises(RuntimeError, match="start failed"):
        playback._play_pcm16(b"\x01\x00", 16000)
    assert stream.aborted == stream.closed == 1


@pytest.mark.parametrize("pcm,sample_rate", [(b"x", 16000), (b"xx", 0)])
def test_malformed_pcm_rejected_before_stream_creation(monkeypatch, pcm, sample_rate):
    monkeypatch.setattr(playback, "sd", SimpleNamespace())
    with pytest.raises(ValueError, match="complete int16"):
        playback._play_pcm16(pcm, sample_rate)

"""B065: dictation endpoints — transcribe (wav + transcode) and host capture."""

from __future__ import annotations

import io
import json
import shutil
import struct
import threading
import time
import wave

import pytest

import hark.dashboard.dictation as dictation
from hark.config import load_config
from hark.providers.base import Transcript


def make_wav(ms: int = 200, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        n = int(rate * ms / 1000)
        w.writeframes(struct.pack(f"<{n}h", *([0] * n)))
    return buf.getvalue()


class FakeStt:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        self.calls.append(wav_bytes)
        return Transcript(text="hello world", provider="fake", duration_ms=200)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    cfg = load_config(tmp_path / "missing.toml")
    fake = FakeStt()
    monkeypatch.setattr(
        "hark.providers.resolve.resolve_stt",
        lambda name="auto", **kw: fake,
    )
    return cfg, fake


def test_transcribe_wav_passthrough(env):
    cfg, fake = env
    status, payload = dictation.transcribe_blob(cfg, make_wav(), "audio/wav")
    assert status == 200, payload
    assert payload["text"] == "hello world"
    assert payload["provider"] == "fake"
    assert fake.calls and fake.calls[0][:4] == b"RIFF"


def test_transcribe_no_ffmpeg_501(env, monkeypatch):
    cfg, _ = env
    monkeypatch.setattr(dictation.shutil, "which", lambda _: None)
    status, payload = dictation.transcribe_blob(cfg, b"\x1aE\xdf\xa3fake", "audio/webm")
    assert status == 501
    assert payload["error"]["code"] == "transcode_unavailable"


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_transcribe_transcodes_ogg(env, tmp_path):
    import subprocess

    cfg, fake = env
    ogg = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "ogg", "-c:a", "libvorbis", "pipe:1"],
        input=make_wav(300),
        capture_output=True,
        check=True,
    ).stdout
    status, payload = dictation.transcribe_blob(cfg, ogg, "audio/ogg")
    assert status == 200, payload
    assert fake.calls[-1][:4] == b"RIFF"  # provider always sees WAV


def test_transcribe_bad_audio_400(env):
    cfg, _ = env
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg")
    status, payload = dictation.transcribe_blob(cfg, b"not audio at all", "audio/webm")
    assert status == 400
    assert payload["error"]["code"] == "bad_audio"


def test_transcribe_records_usage(env):
    from hark.usage import UsageStore

    cfg, _ = env
    dictation.transcribe_blob(cfg, make_wav(), "audio/wav")
    events = UsageStore().iter_events()
    assert events and events[-1]["kind"] == "stt" and events[-1]["ok"] is True


# ---------------------------------------------------------------------------
# host capture
# ---------------------------------------------------------------------------


class FakeListenResult:
    def __init__(self, text="use option two", cancelled=False):
        self.text = text
        self.provider = "fake"
        self.cancelled = cancelled


def test_host_dictation_flow(env, monkeypatch):
    cfg, _ = env
    published: list[dict] = []
    release = threading.Event()

    def fake_run_listen(cfg_, *, stream_id=None, on_partial=None, **kw):
        on_partial({"text": "use option", "partial": True})
        release.wait(timeout=5)
        return FakeListenResult()

    monkeypatch.setattr("hark.speech.run_listen", fake_run_listen)
    host = dictation.HostDictation()
    status, payload = host.start(cfg, published.append)
    assert status == 200 and payload["state"] == "recording"

    # double start refused while recording
    status2, payload2 = host.start(cfg, published.append)
    assert status2 == 409 and payload2["error"]["code"] == "capture_active"

    release.set()
    for _ in range(100):
        if any(p.get("state") == "done" for p in published):
            break
        time.sleep(0.02)
    states = [p["state"] for p in published]
    assert "recording" in states and "done" in states
    done = next(p for p in published if p["state"] == "done")
    assert done["text"] == "use option two"
    partials = [p for p in published if p.get("partial")]
    assert partials and partials[0]["text"] == "use option"
    host._thread.join(timeout=5)


def test_host_dictation_stop_requests_finish(env, monkeypatch):
    cfg, _ = env
    requested: list[tuple] = []
    release = threading.Event()

    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda cfg_, **kw: (release.wait(timeout=5), FakeListenResult())[1],
    )
    monkeypatch.setattr(
        "hark.listen_control.request_listen_action",
        lambda action, stream_id=None, **kw: requested.append((action, stream_id)) or {"ok": True},
    )
    host = dictation.HostDictation()
    host.start(cfg, lambda _: None)
    status, payload = host.control("stop")
    assert status == 200 and payload["state"] == "transcribing"
    assert requested and requested[0][0] == "finish"
    status, _ = host.control("cancel")
    assert requested[-1][0] == "cancel"
    release.set()
    host._thread.join(timeout=5)


def test_host_dictation_no_capture_409(env):
    host = dictation.HostDictation()
    status, payload = host.control("stop")
    assert status == 409 and payload["error"]["code"] == "no_capture"


def test_host_dictation_listen_error_publishes_failed(env, monkeypatch):
    cfg, _ = env

    def boom(cfg_, **kw):
        raise RuntimeError("no audio device")

    monkeypatch.setattr("hark.speech.run_listen", boom)
    published: list[dict] = []
    host = dictation.HostDictation()
    status, _ = host.start(cfg, published.append)
    assert status == 200
    for _ in range(100):
        if any(p.get("state") == "failed" for p in published):
            break
        time.sleep(0.02)
    failed = next(p for p in published if p["state"] == "failed")
    assert "no audio device" in failed["error"]
    host._thread.join(timeout=5)


def test_server_transcribe_endpoint(env, tmp_path, monkeypatch):
    """End-to-end over HTTP: content-type routing + auth-free localhost."""
    import http.client

    from hark.dashboard.server import DashboardServer

    cfg, fake = env
    server = DashboardServer(cfg, "127.0.0.1", 0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        c = http.client.HTTPConnection(*server.server_address, timeout=5)
        wav = make_wav()
        c.request(
            "POST", "/api/v1/dictation/transcribe", body=wav,
            headers={"Content-Type": "audio/wav", "Content-Length": str(len(wav))},
        )
        r = c.getresponse()
        body = json.loads(r.read())
        assert r.status == 200, body
        assert body["text"] == "hello world"
        c.close()
    finally:
        server.shutdown()

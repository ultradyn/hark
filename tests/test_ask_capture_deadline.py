"""B145: bounded ask capture and observable timeout lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hark.audio import capture as capture_mod
from hark.config import HarkConfig
from hark.exitcodes import TIMEOUT
from hark.speech import speak_and_listen


class _BlockingStream:
    """PortAudio stand-in: only ``abort`` can release a blocked read."""

    def __init__(self) -> None:
        self.reading = threading.Event()
        self.released = threading.Event()
        self.aborted = 0
        self.exited = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.exited.set()
        return False

    def read(self, _block):
        self.reading.set()
        if not self.released.wait(timeout=2.0):
            raise AssertionError("capture deadline failed to abort blocked read")
        raise RuntimeError("input stream aborted")

    def abort(self) -> None:
        self.aborted += 1
        self.released.set()


def _install_blocking_stream(monkeypatch) -> _BlockingStream:
    stream = _BlockingStream()
    monkeypatch.setattr(capture_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        capture_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **_kwargs: stream),
    )
    return stream


def test_blocking_capture_read_uses_configured_gate_deadline(monkeypatch):
    stream = _install_blocking_stream(monkeypatch)

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="initial_timeout_s=0.05"):
        capture_mod.capture_utterance(
            max_s=1.0,
            initial_timeout_s=0.05,
            post_tts_guard_s=0.0,
        )
    elapsed = time.monotonic() - started

    assert elapsed < 0.75
    assert stream.reading.is_set()
    assert stream.aborted >= 1
    assert stream.exited.is_set()
    assert not any(t.name == "hark-capture-deadline" for t in threading.enumerate())


def test_overlap_capture_timeout_joins_owned_listener(monkeypatch):
    stream = _install_blocking_stream(monkeypatch)
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 50
    cfg.audio.overlap_discard_ms = 0
    cfg.listen.initial_timeout_s = 0.05
    cfg.listen.max_listen_s = 0.25
    cfg.listen.no_open_retry = False
    cfg.listen.no_open_nudge = False

    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)
    monkeypatch.setattr("hark.speech.resolve_stt", lambda *_a, **_k: SimpleNamespace(name="unused"))
    monkeypatch.setattr("hark.speech.configure_cues_from_config", lambda _cfg: None)
    monkeypatch.setattr("hark.speech.play_record_start", lambda: None)
    monkeypatch.setattr("hark.speech.play_record_stop", lambda: None)

    def fake_tts(_cfg, _text, **kwargs):
        kwargs["on_near_end"]()
        return {"ok": True, "provider": "mock"}

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)

    with pytest.raises(TimeoutError, match="initial_timeout_s=0.05"):
        speak_and_listen(cfg, "Still there?")

    assert stream.aborted >= 1
    assert not any(t.name == "hark-overlap-listen" for t in threading.enumerate())
    assert not any(t.name == "hark-capture-deadline" for t in threading.enumerate())


def test_ask_cli_timeout_is_printed_and_process_exits(tmp_path):
    state = tmp_path / "state"
    script = r'''
import contextlib
import pathlib
import threading
from types import SimpleNamespace

import hark.audio.capture as capture
import hark.cli as cli
import hark.speech as speech

state = pathlib.Path(__import__("sys").argv[1])
state.mkdir(parents=True, exist_ok=True)

class BlockingStream:
    def __init__(self):
        self.released = threading.Event()

    def __enter__(self):
        (state / "capture-entered").write_text("yes")
        return self

    def __exit__(self, exc_type, _exc, _tb):
        (state / "capture-exited").write_text(exc_type.__name__ if exc_type else "none")
        return False

    def read(self, _block):
        if not self.released.wait(timeout=2.0):
            raise AssertionError("deadline did not abort input")
        raise RuntimeError("input stream aborted")

    def abort(self):
        self.released.set()

@contextlib.contextmanager
def tracked(name):
    marker = state / name
    marker.write_text("active")
    try:
        yield
    finally:
        marker.unlink(missing_ok=True)
        (state / f"{name}-cleaned").write_text("yes")

capture._require_sd = lambda: None
capture.sd = SimpleNamespace(InputStream=lambda **_kwargs: BlockingStream())
speech.pause_ambient_for_mic = lambda **_kwargs: tracked("ambient")
speech.MicLease = lambda *_a, **_k: tracked("mic")
speech.BusySection = lambda *_a, **_k: tracked("busy")
speech.duck_media = lambda *_a, **_k: tracked("media")
speech.configure_cues_from_config = lambda _cfg: None
speech.play_record_start = lambda: None
speech.play_record_stop = lambda: None
speech.resolve_stt = lambda *_a, **_k: SimpleNamespace(name="unused")
speech.register_active_listen = lambda stream, **_kwargs: (state / "active-listen").write_text(stream)
speech.clear_active_listen = lambda _stream: (state / "active-listen").unlink(missing_ok=True)
speech.poll_listen_action = lambda _stream: None
speech.consume_listen_action = lambda _stream: None
speech.touch_voice_activity = lambda **_kwargs: None
speech.maybe_print_tts_question = lambda _cfg, text: print(text, file=__import__("sys").stderr, flush=True)
speech.run_tts = lambda *_a, **_k: {"ok": True, "provider": "mock"}

cfg = __import__("hark.config", fromlist=["HarkConfig"]).HarkConfig()
cfg.audio.overlap_prearm = False
cfg.listen.initial_timeout_s = 0.05
cfg.listen.max_listen_s = 0.25
cfg.listen.no_open_retry = False
cfg.listen.no_open_nudge = False
cli.load_config = lambda *_args, **_kwargs: cfg
raise SystemExit(cli.main(["ask", "--confirm", "never", "--json", "Still there?"]))
'''
    env = os.environ.copy()
    src_dir = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(src_dir), env.get("PYTHONPATH", "")))
    )
    env["XDG_STATE_HOME"] = str(tmp_path / "xdg-state")
    process = subprocess.run(
        [sys.executable, "-c", script, str(state)],
        capture_output=True,
        text=True,
        env=env,
        timeout=4.0,
        check=False,
    )

    assert process.returncode == TIMEOUT
    result = json.loads(process.stdout)
    assert result["ok"] is False
    assert result["exit"] == TIMEOUT
    assert "initial_timeout_s=0.05" in result["error"]
    assert result["tts"]["provider"] == "mock"
    assert "Still there?" in process.stderr
    assert "Traceback" not in process.stderr
    assert (state / "capture-exited").is_file()
    assert not (state / "active-listen").exists()
    for name in ("ambient", "mic", "busy", "media"):
        assert not (state / name).exists()
        assert (state / f"{name}-cleaned").is_file()

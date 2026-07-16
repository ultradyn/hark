"""B143: an interrupted bound ask cancels capture and exits cleanly."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from hark.answer_window import AnswerWindowDeps, AnswerWindowPolicy, open_answer_window
from hark.audio import capture as capture_mod
from hark.audio.capture import MicBusyError
from hark.config import HarkConfig
from hark.exitcodes import ABORT
from hark.listen_end import EndMode
from hark.speech import run_ask, speak_and_listen


class _BlockingStream:
    def __init__(self) -> None:
        self.reading = threading.Event()
        self.released = threading.Event()
        self.aborted = 0
        self.exited_with: type[BaseException] | None = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self.exited_with = exc_type
        self.released.set()
        return False

    def read(self, _block):
        self.reading.set()
        self.released.wait(timeout=5.0)
        raise RuntimeError("aborted blocking read")

    def abort(self) -> None:
        self.aborted += 1
        self.released.set()


def test_capture_sigint_aborts_stream_and_restores_handlers(monkeypatch):
    stream = _BlockingStream()
    monkeypatch.setattr(capture_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        capture_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **_kwargs: stream),
    )
    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupt_when_reading() -> None:
        assert stream.reading.wait(timeout=2.0)
        os.kill(os.getpid(), signal.SIGINT)

    interrupter = threading.Thread(target=interrupt_when_reading, daemon=True)
    interrupter.start()
    started = time.monotonic()
    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.capture_interrupt_signals():
            capture_mod.capture_utterance(
                max_s=30.0,
                initial_timeout_s=30.0,
                post_tts_guard_s=0.0,
            )
    interrupter.join(timeout=2.0)

    assert time.monotonic() - started < 2.0
    assert caught.value.signal_name == "SIGINT"
    assert stream.aborted >= 1
    assert stream.exited_with is capture_mod.CaptureInterrupted
    assert capture_mod.cancel_active_capture() is False
    assert signal.getsignal(signal.SIGINT) is old_sigint
    assert signal.getsignal(signal.SIGTERM) is old_sigterm


def test_run_ask_maps_keyboard_interrupt_to_structured_abort(monkeypatch):
    interrupted = KeyboardInterrupt()
    interrupted.tts_info = {"ok": True, "provider": "mock"}

    def interrupt(*_args, **_kwargs):
        raise interrupted

    monkeypatch.setattr("hark.speech.speak_and_listen", interrupt)
    result = run_ask(HarkConfig(), "Still there?", confirm="never")

    assert result == {
        "ok": False,
        "cancelled": True,
        "error": "interrupted",
        "text": "",
        "end_phrase": "interrupt",
        "signal": None,
        "exit": ABORT,
        "tts": {"ok": True, "provider": "mock"},
    }


@pytest.mark.parametrize("phase", ["enter", "exit"])
def test_cli_ask_structures_interrupt_across_entire_signal_scope(
    monkeypatch, capsys, phase
):
    import hark.cli as cli

    @contextmanager
    def interrupting_scope():
        if phase == "enter":
            raise capture_mod.CaptureInterrupted(signal.SIGINT)
        yield
        raise capture_mod.CaptureInterrupted(signal.SIGINT)

    monkeypatch.setattr(
        "hark.audio.capture.capture_interrupt_signals", interrupting_scope
    )
    monkeypatch.setattr(
        "hark.speech.run_ask",
        lambda *_a, **_k: {"ok": True, "exit": 0, "text": "answer"},
    )

    exit_code = cli.main(["ask", "--confirm", "never", "--json", "Still there?"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == ABORT
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["signal"] == "SIGINT"
    assert result["exit"] == ABORT


@pytest.mark.parametrize(
    ("interrupt_call", "expected_calls"),
    [(1, 3), (3, 4)],
)
def test_real_signal_scope_structures_install_and_restore_interrupts(
    monkeypatch, capsys, interrupt_call, expected_calls
):
    import hark.cli as cli

    calls: list[tuple[int, object]] = []

    def interrupted_signal(signum, handler):
        calls.append((signum, handler))
        if len(calls) == interrupt_call:
            raise capture_mod.CaptureInterrupted(signal.SIGINT)
        return None

    monkeypatch.setattr(capture_mod.signal, "signal", interrupted_signal)
    monkeypatch.setattr(
        "hark.speech.run_ask",
        lambda *_a, **_k: {"ok": True, "exit": 0, "text": "answer"},
    )

    exit_code = cli.main(["ask", "--confirm", "never", "--json", "Still there?"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == ABORT
    assert result["signal"] == "SIGINT"
    assert len(calls) == expected_calls


def test_sigterm_after_ask_operation_is_structured_during_scope_exit(monkeypatch):
    installed: dict[int, object] = {}

    def capture_handler(signum, handler):
        installed[signum] = handler
        return None

    monkeypatch.setattr(capture_mod.signal, "signal", capture_handler)

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.capture_interrupt_signals():
            with capture_mod.ask_signal_operation():
                pass
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)

    assert caught.value.signal_name == "SIGTERM"


def test_signal_scope_restoration_does_not_replace_primary_interrupt(monkeypatch):
    primary = capture_mod.CaptureInterrupted(signal.SIGINT)
    cleanup = capture_mod.CaptureInterrupted(signal.SIGTERM)
    calls = 0

    def interrupted_signal(_signum, _handler):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise primary
        if calls == 2:
            raise cleanup
        return None

    monkeypatch.setattr(capture_mod.signal, "signal", interrupted_signal)

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.capture_interrupt_signals():
            pytest.fail("interrupted handler installation reached scope body")

    assert caught.value is primary
    assert calls == 3


def test_cancel_active_capture_reports_abort_failure_and_closes():
    class AbortFailureStream:
        def __init__(self) -> None:
            self.abort_calls = 0
            self.close_calls = 0

        def abort(self) -> None:
            self.abort_calls += 1
            raise RuntimeError("PortAudio abort failed")

        def close(self) -> None:
            self.close_calls += 1

    stream = AbortFailureStream()
    with capture_mod.capture_interrupt_signals():
        with capture_mod._registered_input_stream(stream):
            assert capture_mod.cancel_active_capture(signal.SIGINT) is False

    assert stream.abort_calls == 1
    assert stream.close_calls == 1


def test_sticky_cancel_preempts_late_input_stream_registration():
    stream = _BlockingStream()

    with capture_mod.capture_interrupt_signals():
        capture_mod.request_capture_cancel(signal.SIGTERM)
        with pytest.raises(capture_mod.CaptureInterrupted) as caught:
            with capture_mod._registered_input_stream(stream):
                pytest.fail("cancelled capture reached stream body")

    assert caught.value.signal_name == "SIGTERM"
    assert stream.aborted == 1


def test_answer_window_interrupt_releases_mic_and_active_listen(monkeypatch):
    events: list[str] = []

    @contextmanager
    def tracked(name: str):
        events.append(f"{name}:enter")
        try:
            yield
        finally:
            events.append(f"{name}:exit")

    monkeypatch.setattr(
        "hark.speech.pause_ambient_for_mic",
        lambda **_kwargs: tracked("ambient"),
    )
    monkeypatch.setattr("hark.speech.MicLease", lambda *_a, **_k: tracked("mic"))
    monkeypatch.setattr("hark.speech.BusySection", lambda *_a, **_k: tracked("busy"))
    monkeypatch.setattr("hark.speech.configure_cues_from_config", lambda _cfg: None)
    policy = AnswerWindowPolicy(
        profile="bound_answer",
        end_mode=EndMode.SILENCE,
        max_listen_s=30.0,
        no_open_retry=False,
        no_open_nudge=False,
        empty_stt_retry=False,
        empty_stt_nudge=False,
        arm_cue=False,
        duck_media_during_stt=True,
    )
    interrupted = capture_mod.CaptureInterrupted(signal.SIGINT)
    deps = AnswerWindowDeps(
        cfg=HarkConfig(),
        stt=SimpleNamespace(name="unused"),
        capture=lambda **_kwargs: (_ for _ in ()).throw(interrupted),
        duck_media=lambda *_a, **_k: tracked("media"),
        play_record_start=lambda: None,
        play_record_stop=lambda: None,
        register_active_listen=lambda stream, **_kwargs: events.append(
            f"listen:{stream}:register"
        ),
        clear_active_listen=lambda stream: events.append(f"listen:{stream}:clear"),
        poll_listen_action=lambda _stream: None,
        consume_listen_action=lambda _stream: None,
        touch_voice_activity=lambda **_kwargs: None,
        syslog=lambda *_a, **_k: None,
    )

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        open_answer_window(policy, deps=deps)

    assert caught.value is interrupted
    registered = next(event for event in events if event.endswith(":register"))
    cleared = registered.removesuffix(":register") + ":clear"
    assert events[-5:] == [
        cleared,
        "media:exit",
        "busy:exit",
        "mic:exit",
        "ambient:exit",
    ]


def test_overlap_join_interrupt_aborts_worker_capture(monkeypatch):
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 50
    joins: list[float | None] = []
    cancels: list[bool] = []
    interrupted = KeyboardInterrupt()

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            assert callable(target)
            assert name == "hark-overlap-listen"
            assert daemon is True

        def start(self) -> None:
            return None

        def join(self, timeout=None) -> None:
            joins.append(timeout)
            if len(joins) == 1:
                raise interrupted

    def fake_tts(_cfg, _text, **kwargs):
        kwargs["on_near_end"]()
        return {"ok": True, "provider": "mock"}

    monkeypatch.setattr("hark.speak_then_listen.handoff.threading.Thread", FakeThread)
    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr(
        "hark.audio.capture.cancel_active_capture",
        lambda: cancels.append(True) or True,
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        speak_and_listen(cfg, "Still there?")

    assert caught.value is interrupted
    assert cancels == [True]
    assert joins == [None, None]


def test_overlap_sigint_during_first_join_waits_for_worker_terminal(monkeypatch):
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 50
    worker_started = threading.Event()
    tts_returned = threading.Event()
    worker_released = threading.Event()

    def cancellable_listen(*_args, **_kwargs):
        worker_started.set()
        try:
            while True:
                capture_mod.raise_if_capture_cancelled()
                time.sleep(0.01)
        finally:
            worker_released.set()

    def fake_tts(_cfg, _text, **kwargs):
        kwargs["on_near_end"]()
        assert worker_started.wait(timeout=2.0)
        tts_returned.set()
        return {"ok": True, "provider": "mock"}

    def interrupt_first_join() -> None:
        assert tts_returned.wait(timeout=2.0)
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)
    monkeypatch.setattr("hark.speech.run_listen", cancellable_listen)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    interrupter = threading.Thread(target=interrupt_first_join, daemon=True)
    interrupter.start()

    with capture_mod.capture_interrupt_signals():
        result = run_ask(cfg, "Still there?", confirm="never")
    interrupter.join(timeout=2.0)

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["signal"] == "SIGINT"
    assert worker_released.is_set()


def test_overlap_interrupt_during_remaining_tts_waits_for_pause_release(monkeypatch):
    import hark.mic_coord as mic_coord

    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 50
    waiting = threading.Event()
    released = threading.Event()
    cleared: list[str] = []

    class BusyMic:
        def __init__(self, _name):
            pass

        def __enter__(self):
            waiting.set()
            raise MicBusyError("ambient still owns mic")

        def __exit__(self, *_args):
            return None

    tokens = iter(["pause-token"])
    monkeypatch.setattr(mic_coord, "MicLease", BusyMic)
    monkeypatch.setattr(
        mic_coord, "request_ambient_pause", lambda **_kwargs: next(tokens)
    )
    monkeypatch.setattr(mic_coord, "clear_ambient_pause", cleared.append)
    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)

    def blocked_listen(*_args, **_kwargs):
        try:
            with capture_mod.capture_attempt():
                with mic_coord.pause_ambient_for_mic(timeout_s=15.0):
                    raise AssertionError("cancelled pause unexpectedly acquired mic")
        finally:
            released.set()

    def interrupting_tts(_cfg, _text, **kwargs):
        kwargs["on_near_end"]()
        assert waiting.wait(timeout=2.0)
        raise capture_mod.CaptureInterrupted(signal.SIGINT)

    monkeypatch.setattr("hark.speech.run_listen", blocked_listen)
    monkeypatch.setattr("hark.speech.run_tts", interrupting_tts)

    started = time.monotonic()
    with capture_mod.capture_interrupt_signals():
        result = run_ask(cfg, "Still there?", confirm="never")

    assert time.monotonic() - started < 2.0
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["signal"] == "SIGINT"
    assert released.is_set()
    assert cleared == ["pause-token"]


def test_sigterm_during_hung_tts_provider_uses_hard_termination(tmp_path):
    ready = tmp_path / "provider-ready"
    script = r"""
import pathlib
import sys
import time
from types import SimpleNamespace

import hark.cli as cli
import hark.conference as conference
import hark.speech as speech

ready = pathlib.Path(sys.argv[1])

class HungProvider:
    def synthesize(self, _text, *, voice=None):
        ready.write_text("synthesizing")
        while True:
            time.sleep(1.0)

conference.apply_conference_hold = lambda *_a, **_k: SimpleNamespace(
    skipped=False,
    as_meta=lambda: {},
)
speech.lookup_cached_tts = lambda *_a, **_k: None
speech.resolve_tts = lambda *_a, **_k: HungProvider()
speech.claim_tts_play_ticket = lambda: object()
speech.abandon_tts_play_ticket = lambda *_a, **_k: None
raise SystemExit(cli.main(["ask", "--confirm", "never", "--json", "Still there?"]))
"""
    env = os.environ.copy()
    src_dir = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(src_dir), env.get("PYTHONPATH", "")))
    )
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5.0
        while (
            not ready.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if not ready.exists():
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=2.0)
            pytest.fail(f"provider did not block: stdout={stdout!r} stderr={stderr!r}")

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2.0)

    assert process.returncode == -signal.SIGTERM
    assert "Traceback" not in stderr


@pytest.mark.parametrize(
    ("sent_signal", "signal_name"),
    [(signal.SIGINT, "SIGINT"), (signal.SIGTERM, "SIGTERM")],
)
def test_ask_subprocess_signal_is_structured_and_releases_stream(
    tmp_path, sent_signal, signal_name
):
    ready = tmp_path / "ready"
    cleaned = tmp_path / "cleaned"
    script = r"""
import pathlib
import sys
import time
from types import SimpleNamespace

import hark.audio.capture as capture
import hark.cli as cli
import hark.speech as speech

ready = pathlib.Path(sys.argv[1])
cleaned = pathlib.Path(sys.argv[2])

class BlockingStream:
    def __init__(self):
        self.aborted = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        cleaned.write_text(exc_type.__name__ if exc_type else "none")
        return False

    def abort(self):
        self.aborted = True

    def read(self, _block):
        ready.write_text("reading")
        while not self.aborted:
            time.sleep(0.05)
        raise RuntimeError("aborted blocking read")

stream = BlockingStream()
capture._require_sd = lambda: None
capture.sd = SimpleNamespace(InputStream=lambda **_kwargs: stream)

def blocking_speak_and_listen(_cfg, _prompt, **_kwargs):
    capture.capture_utterance(
        max_s=30.0,
        initial_timeout_s=30.0,
        post_tts_guard_s=0.0,
    )
    raise AssertionError("capture unexpectedly returned")

speech.speak_and_listen = blocking_speak_and_listen
raise SystemExit(cli.main(["ask", "--confirm", "never", "--json", "Still there?"]))
"""
    env = os.environ.copy()
    src_dir = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(src_dir), env.get("PYTHONPATH", "")))
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ready), str(cleaned)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5.0
        while (
            not ready.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        if not ready.exists():
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=2.0)
            pytest.fail(f"capture did not start: stdout={stdout!r} stderr={stderr!r}")

        process.send_signal(sent_signal)
        stdout, stderr = process.communicate(timeout=5.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2.0)

    assert process.returncode == ABORT
    result = json.loads(stdout)
    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["error"] == "interrupted"
    assert result["signal"] == signal_name
    assert result["exit"] == ABORT
    assert cleaned.read_text() == "CaptureInterrupted"
    assert "Traceback" not in stderr
    assert "KeyboardInterrupt" not in stderr

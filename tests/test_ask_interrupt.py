"""B143: an interrupted bound ask cancels capture and exits cleanly."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from hark.answer_window import AnswerWindowDeps, AnswerWindowPolicy, open_answer_window
from hark.answer_window.result import ListenResult
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
    deadline = time.monotonic() + 1.0
    while stream.aborted == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert stream.aborted >= 1
    assert stream.exited_with is None
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


def test_confirmation_interrupt_preserves_answer_and_initial_tts(monkeypatch):
    tts_info = {"ok": True, "provider": "mock"}
    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *_a, **_k: (
            tts_info,
            ListenResult(
                text="ship the release",
                provider="mock",
                duration_ms=15,
                end_mode="silence",
                stream_id="answer",
            ),
        ),
    )
    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *_a, **_k: (_ for _ in ()).throw(
            capture_mod.CaptureInterrupted(signal.SIGINT)
        ),
    )

    result = run_ask(HarkConfig(), "Deploy now?", confirm="always")

    assert result["cancelled"] is True
    assert result["signal"] == "SIGINT"
    assert result["text"] == "ship the release"
    assert result["tts"] is tts_info


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


def test_signal_after_capture_state_publication_restores_global(monkeypatch):
    real_lock = capture_mod._capture_state_lock

    class SignalOnPublicationExit:
        def __init__(self) -> None:
            self.exits = 0

        def __enter__(self):
            real_lock.acquire()
            return self

        def __exit__(self, *_args):
            real_lock.release()
            self.exits += 1
            if self.exits == 2:
                os.kill(os.getpid(), signal.SIGINT)

    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(
        capture_mod,
        "_capture_state_lock",
        SignalOnPublicationExit(),
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            with capture_mod.capture_interrupt_signals():
                pytest.fail("signal after capture-state publication reached body")
        assert capture_mod._capture_signal_state is None
        assert signal.getsignal(signal.SIGINT) is old_sigint
        assert signal.getsignal(signal.SIGTERM) is old_sigterm
    finally:
        # Keep this RED regression isolated on implementations that leak state.
        capture_mod._capture_signal_state = None


def test_parent_guard_construction_interrupt_restores_capture_state(monkeypatch):
    primary = capture_mod.CaptureInterrupted(signal.SIGINT)

    def interrupt_construction(_state):
        raise primary

    monkeypatch.setattr(capture_mod, "_ParentLifetimeGuard", interrupt_construction)
    try:
        with pytest.raises(capture_mod.CaptureInterrupted) as caught:
            with capture_mod.capture_interrupt_signals():
                pytest.fail("interrupted parent-guard construction reached body")
        assert caught.value is primary
        assert capture_mod._capture_signal_state is None
    finally:
        # Keep this RED regression isolated on implementations that leak state.
        capture_mod._capture_signal_state = None


def test_parent_guard_stop_failure_still_restores_state_and_handlers(monkeypatch):
    primary = capture_mod.CaptureInterrupted(signal.SIGINT)
    previous = {
        signal.SIGINT: object(),
        signal.SIGTERM: object(),
    }
    installed = dict(previous)

    class StopFailureGuard:
        def __init__(self, _state):
            pass

        def start(self) -> None:
            return None

        def stop(self) -> None:
            raise RuntimeError("parent guard stop failed")

    monkeypatch.setattr(capture_mod, "_ParentLifetimeGuard", StopFailureGuard)
    monkeypatch.setattr(
        capture_mod.signal,
        "getsignal",
        lambda signum: installed[signum],
    )
    monkeypatch.setattr(
        capture_mod.signal,
        "signal",
        lambda signum, handler: installed.__setitem__(signum, handler),
    )
    try:
        with pytest.raises(capture_mod.CaptureInterrupted) as caught:
            with capture_mod.capture_interrupt_signals():
                raise primary
        assert caught.value is primary
        assert capture_mod._capture_signal_state is None
        assert installed == previous
    finally:
        # Keep this RED regression isolated on implementations that leak state.
        capture_mod._capture_signal_state = None


def test_sigterm_without_active_capture_is_structured_during_scope(monkeypatch):
    installed: dict[int, object] = {}

    def capture_handler(signum, handler):
        installed[signum] = handler
        return None

    monkeypatch.setattr(capture_mod.signal, "signal", capture_handler)

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.capture_interrupt_signals():
            handler = installed[signal.SIGTERM]
            assert callable(handler)
            handler(signal.SIGTERM, None)

    assert caught.value.signal_name == "SIGTERM"


def test_repeated_cancel_preserves_first_signal_identity():
    with capture_mod.capture_interrupt_signals():
        capture_mod.request_capture_cancel(signal.SIGINT)
        capture_mod.request_capture_cancel(signal.SIGTERM)
        with pytest.raises(capture_mod.CaptureInterrupted) as caught:
            capture_mod.raise_if_capture_cancelled()

    assert caught.value.signal_name == "SIGINT"


def test_capture_attempt_lease_release_is_idempotent():
    with capture_mod.capture_interrupt_signals():
        first = capture_mod.register_capture_attempt()
        second = capture_mod.register_capture_attempt()
        assert capture_mod.capture_in_progress() is True

        capture_mod.release_capture_attempt(first)
        capture_mod.release_capture_attempt(first)
        assert capture_mod.capture_in_progress() is True

        capture_mod.release_capture_attempt(second)
        assert capture_mod.capture_in_progress() is False


def test_sigterm_during_pre_stream_guard_is_structured_and_releases_attempt(
    monkeypatch,
):
    monkeypatch.setattr(capture_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        capture_mod,
        "sd",
        SimpleNamespace(
            InputStream=lambda **_kwargs: pytest.fail(
                "stream opened after cancellation"
            )
        ),
    )

    def interrupt_guard() -> None:
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    interrupter = threading.Thread(target=interrupt_guard, daemon=True)
    interrupter.start()
    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.capture_interrupt_signals():
            capture_mod.capture_utterance(post_tts_guard_s=5.0)
    interrupter.join(timeout=1.0)

    assert caught.value.signal_name == "SIGTERM"
    assert capture_mod.capture_in_progress() is False


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
            assert capture_mod.cancel_active_capture(signal.SIGINT) is True

    deadline = time.monotonic() + 1.0
    while stream.close_calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert stream.abort_calls == 1
    assert stream.close_calls == 1


def test_repeated_signal_during_cleanup_thread_start_is_reentrant(monkeypatch):
    real_thread = threading.Thread
    starts = 0

    class Stream:
        def abort(self) -> None:
            return None

        def close(self) -> None:
            return None

    class ReentrantStartThread:
        def __init__(self, *, target, name, daemon):
            assert name == "hark-capture-cancel"
            assert daemon is True
            self._target = target
            self._inner = real_thread(target=target, name=name, daemon=daemon)

        @property
        def ident(self):
            return self._inner.ident

        def start(self) -> None:
            nonlocal starts
            starts += 1
            repeated = signal.getsignal(signal.SIGTERM)
            assert callable(repeated)
            repeated(signal.SIGTERM, None)
            self._inner.start()

        def join(self, timeout=None) -> None:
            self._inner.join(timeout=timeout)

        def is_alive(self) -> bool:
            return self._inner.is_alive()

    stream = Stream()
    monkeypatch.setattr(capture_mod.threading, "Thread", ReentrantStartThread)

    with capture_mod.capture_interrupt_signals():
        with capture_mod._registered_input_stream(stream):
            assert capture_mod.cancel_active_capture(signal.SIGINT) is True

    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert starts == 1
    assert capture_mod._stream_cancel_workers == {}


def test_definite_prelaunch_cleanup_failure_does_not_stick_registry(monkeypatch):
    class Stream:
        def abort(self) -> None:
            pytest.fail("pre-launch target unexpectedly ran")

    class FailedThread:
        ident = None

        def __init__(self, *, target, name, daemon):
            assert callable(target)
            assert name == "hark-capture-cancel"
            assert daemon is True

        def start(self) -> None:
            raise RuntimeError("thread launch rejected")

        def is_alive(self) -> bool:
            return False

    stream = Stream()
    monkeypatch.setattr(capture_mod.threading, "Thread", FailedThread)

    with capture_mod.capture_interrupt_signals():
        with capture_mod._registered_input_stream(stream):
            assert capture_mod.cancel_active_capture(signal.SIGINT) is False

    assert capture_mod._stream_cancel_workers == {}
    assert capture_mod.capture_in_progress() is False


def test_postlaunch_start_interrupt_retains_single_cleanup_owner(monkeypatch):
    real_thread = threading.Thread
    allow_target = threading.Event()
    target_finished = threading.Event()
    starts = 0

    class Stream:
        def abort(self) -> None:
            return None

        def close(self) -> None:
            target_finished.set()

    class LaunchedThenInterrupted:
        def __init__(self, *, target, name, daemon):
            assert name == "hark-capture-cancel"
            assert daemon is True
            self._target = target
            self._inner: threading.Thread | None = None

        @property
        def ident(self):
            return None if self._inner is None else self._inner.ident

        def start(self) -> None:
            nonlocal starts
            starts += 1

            def delayed_target() -> None:
                assert allow_target.wait(timeout=2.0)
                self._target()

            self._inner = real_thread(target=delayed_target, daemon=True)
            self._inner.start()
            raise capture_mod.CaptureInterrupted(signal.SIGINT)

        def is_alive(self) -> bool:
            return bool(self._inner and self._inner.is_alive())

    stream = Stream()
    monkeypatch.setattr(capture_mod.threading, "Thread", LaunchedThenInterrupted)

    assert capture_mod._request_stream_cancel(stream) is True
    assert capture_mod._request_stream_cancel(stream) is True
    assert starts == 1
    assert len(capture_mod._stream_cancel_workers) == 1
    assert capture_mod.capture_in_progress() is True

    allow_target.set()
    assert target_finished.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert capture_mod._stream_cancel_workers == {}


@pytest.mark.parametrize("phase", ["before_publish", "after_publish"])
def test_signal_at_normal_cleanup_transfer_never_loses_stream_owner(monkeypatch, phase):
    calls = {"abort": 0, "stop": 0, "close": 0}

    class Stream:
        def start(self) -> None:
            return None

        def read(self, block):
            import numpy as np

            return np.zeros((block, 1), dtype=np.float32), False

        def abort(self) -> None:
            calls["abort"] += 1

        def stop(self) -> None:
            calls["stop"] += 1

        def close(self) -> None:
            calls["close"] += 1

    stream = Stream()
    real_reserve = capture_mod._reserve_stream_cleanup

    def interrupting_reserve(target, *, cancel, fallback_exit=None):
        if cancel:
            return real_reserve(
                target,
                cancel=True,
                fallback_exit=fallback_exit,
            )
        if phase == "before_publish":
            os.kill(os.getpid(), signal.SIGINT)
        owner = real_reserve(
            target,
            cancel=False,
            fallback_exit=fallback_exit,
        )
        if phase == "after_publish":
            os.kill(os.getpid(), signal.SIGINT)
        return owner

    monkeypatch.setattr(capture_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        capture_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **_kwargs: stream),
    )
    monkeypatch.setattr(
        capture_mod,
        "_reserve_stream_cleanup",
        interrupting_reserve,
    )

    with pytest.raises(capture_mod.CaptureInterrupted):
        with capture_mod.capture_interrupt_signals():
            capture_mod.capture_utterance(
                max_s=1.0,
                initial_timeout_s=1.0,
                post_tts_guard_s=0.0,
                should_stop=lambda *_args: True,
            )

    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == {"abort": 1, "stop": 0, "close": 1}
    assert capture_mod._active_input_stream is None
    assert capture_mod._stream_cancel_workers == {}
    assert capture_mod.capture_in_progress() is False


def test_ordinary_capture_exception_transfers_exactly_one_cleanup_owner(monkeypatch):
    calls = {"abort": 0, "close": 0}

    class Stream:
        def start(self) -> None:
            return None

        def read(self, _block):
            raise RuntimeError("capture backend failed")

        def abort(self) -> None:
            calls["abort"] += 1

        def close(self) -> None:
            calls["close"] += 1

    stream = Stream()
    monkeypatch.setattr(capture_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        capture_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **_kwargs: stream),
    )

    with pytest.raises(RuntimeError, match="capture backend failed"):
        capture_mod.capture_utterance(post_tts_guard_s=0.0)

    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == {"abort": 1, "close": 1}
    assert capture_mod._active_input_stream is None
    assert capture_mod._stream_cancel_workers == {}


def test_signal_after_normal_detach_starts_and_upgrades_reserved_owner(monkeypatch):
    calls = {"abort": 0, "stop": 0, "close": 0}
    fired = False
    real_start = capture_mod._start_stream_cleanup

    class Stream:
        def start(self) -> None:
            return None

        def read(self, block):
            import numpy as np

            return np.zeros((block, 1), dtype=np.float32), False

        def abort(self) -> None:
            calls["abort"] += 1

        def stop(self) -> None:
            calls["stop"] += 1

        def close(self) -> None:
            calls["close"] += 1

    def interrupt_before_start(owner):
        nonlocal fired
        if not fired:
            fired = True
            assert capture_mod._active_input_stream is None
            os.kill(os.getpid(), signal.SIGINT)
        return real_start(owner)

    monkeypatch.setattr(capture_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        capture_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **_kwargs: Stream()),
    )
    monkeypatch.setattr(capture_mod, "_start_stream_cleanup", interrupt_before_start)

    with pytest.raises(capture_mod.CaptureInterrupted):
        with capture_mod.capture_interrupt_signals():
            capture_mod.capture_utterance(
                post_tts_guard_s=0.0,
                should_stop=lambda *_args: True,
            )

    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == {"abort": 1, "stop": 0, "close": 1}
    assert capture_mod.capture_in_progress() is False


def test_signal_after_exception_detach_starts_reserved_owner(monkeypatch):
    calls = {"abort": 0, "close": 0}
    fired = False
    real_start = capture_mod._start_stream_cleanup

    class Stream:
        def start(self) -> None:
            return None

        def read(self, _block):
            raise RuntimeError("backend failed")

        def abort(self) -> None:
            calls["abort"] += 1

        def close(self) -> None:
            calls["close"] += 1

    def interrupt_before_start(owner):
        nonlocal fired
        if not fired:
            fired = True
            assert capture_mod._active_input_stream is None
            os.kill(os.getpid(), signal.SIGTERM)
        return real_start(owner)

    monkeypatch.setattr(capture_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        capture_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **_kwargs: Stream()),
    )
    monkeypatch.setattr(capture_mod, "_start_stream_cleanup", interrupt_before_start)

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.capture_interrupt_signals():
            capture_mod.capture_utterance(post_tts_guard_s=0.0)

    assert caught.value.signal_name == "SIGTERM"
    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == {"abort": 1, "close": 1}
    assert capture_mod.capture_in_progress() is False


def test_signal_after_worker_start_upgrades_before_native_cleanup(monkeypatch):
    calls = {"abort": 0, "stop": 0, "close": 0}
    before_native = threading.Event()
    allow_native = threading.Event()
    real_perform = capture_mod._perform_stream_cleanup
    owner_box = {}

    class Stream:
        def start(self) -> None:
            return None

        def read(self, block):
            import numpy as np

            return np.zeros((block, 1), dtype=np.float32), False

        def abort(self) -> None:
            calls["abort"] += 1

        def stop(self) -> None:
            calls["stop"] += 1

        def close(self) -> None:
            calls["close"] += 1

    def gated_perform(owner):
        owner_box["owner"] = owner
        before_native.set()
        assert allow_native.wait(timeout=2.0)
        real_perform(owner)

    def interrupt_worker() -> None:
        assert before_native.wait(timeout=2.0)
        os.kill(os.getpid(), signal.SIGINT)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with capture_mod._stream_cancel_lock:
                if owner_box["owner"].cancel_requested:
                    break
            time.sleep(0.001)
        else:
            pytest.fail("signal handler did not upgrade cleanup owner")
        allow_native.set()

    monkeypatch.setattr(capture_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        capture_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **_kwargs: Stream()),
    )
    monkeypatch.setattr(capture_mod, "_perform_stream_cleanup", gated_perform)
    interrupter = threading.Thread(target=interrupt_worker, daemon=True)
    interrupter.start()

    with pytest.raises(capture_mod.CaptureInterrupted):
        with capture_mod.capture_interrupt_signals():
            capture_mod.capture_utterance(
                post_tts_guard_s=0.0,
                should_stop=lambda *_args: True,
            )

    interrupter.join(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == {"abort": 1, "stop": 0, "close": 1}
    assert capture_mod.capture_in_progress() is False


def test_pending_signal_is_delivered_only_after_cleanup_worker_start(monkeypatch):
    real_thread = threading.Thread
    real_perform = capture_mod._perform_stream_cleanup
    allow_native = threading.Event()
    calls = {"abort": 0, "stop": 0, "close": 0}
    sent = False

    class Stream:
        def start(self) -> None:
            return None

        def read(self, block):
            import numpy as np

            return np.zeros((block, 1), dtype=np.float32), False

        def abort(self) -> None:
            calls["abort"] += 1

        def stop(self) -> None:
            calls["stop"] += 1

        def close(self) -> None:
            calls["close"] += 1

    class SignalDuringStart:
        def __init__(self, *, target, name, daemon):
            self._inner = real_thread(target=target, name=name, daemon=daemon)

        @property
        def ident(self):
            return self._inner.ident

        def start(self) -> None:
            nonlocal sent
            if not sent:
                sent = True
                os.kill(os.getpid(), signal.SIGINT)
            self._inner.start()

        def join(self, timeout=None) -> None:
            self._inner.join(timeout=timeout)

        def is_alive(self) -> bool:
            return self._inner.is_alive()

    def gated_perform(owner):
        assert allow_native.wait(timeout=2.0)
        real_perform(owner)

    monkeypatch.setattr(capture_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        capture_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **_kwargs: Stream()),
    )
    monkeypatch.setattr(capture_mod.threading, "Thread", SignalDuringStart)
    monkeypatch.setattr(capture_mod, "_perform_stream_cleanup", gated_perform)
    # A process-directed signal may be received by another unblocked thread;
    # Python then runs its pending handler on the main thread even though the
    # main thread's pthread mask is set. Model that deterministically.
    monkeypatch.setattr(
        capture_mod.signal,
        "pthread_sigmask",
        lambda *_args, **_kwargs: set(),
    )

    with pytest.raises(capture_mod.CaptureInterrupted):
        with capture_mod.capture_interrupt_signals():
            capture_mod.capture_utterance(
                post_tts_guard_s=0.0,
                should_stop=lambda *_args: True,
            )

    owner = next(iter(capture_mod._stream_cancel_workers.values()))
    assert owner.start_claimed is True
    assert owner.worker is not None
    assert owner.worker.is_alive() is True
    assert owner.cancel_requested is True
    allow_native.set()
    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == {"abort": 1, "stop": 0, "close": 1}
    assert capture_mod.capture_in_progress() is False


def test_signal_after_start_claim_cannot_leave_unstarted_cleanup_owner(monkeypatch):
    calls = {"abort": 0, "close": 0}
    real_lock = capture_mod._stream_cancel_lock
    fired = False

    class Stream:
        def abort(self) -> None:
            calls["abort"] += 1

        def close(self) -> None:
            calls["close"] += 1

    class SignalAfterClaimRLock:
        def __enter__(self):
            real_lock.acquire()
            return self

        def __exit__(self, *_args):
            nonlocal fired
            real_lock.release()
            if not fired:
                fired = True
                os.kill(os.getpid(), signal.SIGINT)

    stream = Stream()
    owner = capture_mod._reserve_stream_cleanup(stream, cancel=True)
    monkeypatch.setattr(capture_mod, "_stream_cancel_lock", SignalAfterClaimRLock())
    monkeypatch.setattr(
        capture_mod.signal,
        "pthread_sigmask",
        lambda *_args, **_kwargs: set(),
    )

    with pytest.raises(capture_mod.CaptureInterrupted):
        with capture_mod.capture_interrupt_signals():
            capture_mod._start_stream_cleanup(owner)

    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert calls == {"abort": 1, "close": 1}
    assert capture_mod._stream_cancel_workers == {}
    assert capture_mod.capture_in_progress() is False


def test_signal_while_blocking_cleanup_signals_restores_original_mask(monkeypatch):
    calls = {"abort": 0, "close": 0}
    current_mask: set[signal.Signals] = set()
    inject = True

    class Stream:
        def abort(self) -> None:
            calls["abort"] += 1

        def close(self) -> None:
            calls["close"] += 1

    def interrupting_sigmask(how, mask):
        nonlocal inject
        requested = set(mask)
        previous = set(current_mask)
        if how == signal.SIG_BLOCK:
            current_mask.update(requested)
        elif how == signal.SIG_SETMASK:
            current_mask.clear()
            current_mask.update(requested)
        if inject and how == signal.SIG_BLOCK and requested:
            inject = False
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler)
            handler(signal.SIGINT, None)
        return previous

    stream = Stream()
    owner = capture_mod._reserve_stream_cleanup(stream, cancel=True)
    monkeypatch.setattr(
        capture_mod.signal,
        "pthread_sigmask",
        interrupting_sigmask,
    )

    with pytest.raises(capture_mod.CaptureInterrupted):
        with capture_mod.capture_interrupt_signals():
            capture_mod._start_stream_cleanup(owner)

    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert current_mask == set()
    assert calls == {"abort": 1, "close": 1}
    assert capture_mod._stream_cancel_workers == {}


def test_signal_during_parent_watch_start_remains_structured(monkeypatch):
    real_thread = threading.Thread

    class SignalDuringParentStart:
        def __init__(self, *, target, name, daemon):
            assert name == "hark-ask-parent-watch"
            self._inner = real_thread(target=target, name=name, daemon=daemon)

        @property
        def ident(self):
            return self._inner.ident

        def start(self) -> None:
            os.kill(os.getpid(), signal.SIGINT)
            self._inner.start()

        def join(self, timeout=None) -> None:
            self._inner.join(timeout=timeout)

        def is_alive(self) -> bool:
            return self._inner.is_alive()

    monkeypatch.setattr(capture_mod, "_ParentWatchThread", SignalDuringParentStart)

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.capture_interrupt_signals():
            pytest.fail("signal during parent-watch start reached ask body")

    assert caught.value.signal_name == "SIGINT"
    assert capture_mod.capture_in_progress() is False


def test_nonreturning_native_abort_cannot_block_structured_parent_exit(monkeypatch):
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 50
    entered = threading.Event()
    abort_started = threading.Event()
    release_abort = threading.Event()
    release_read = threading.Event()
    worker_finished = threading.Event()

    class HangingAbortStream:
        def abort(self) -> None:
            abort_started.set()
            assert release_abort.wait(timeout=2.0)
            release_read.set()

        def close(self) -> None:
            release_read.set()

    stream = HangingAbortStream()

    def blocking_listen(*_args, **_kwargs):
        try:
            with capture_mod._registered_input_stream(stream):
                entered.set()
                assert release_read.wait(timeout=2.0)
                capture_mod.raise_if_capture_cancelled()
        finally:
            worker_finished.set()

    def fake_tts(_cfg, _text, **kwargs):
        kwargs["on_near_end"]()
        assert entered.wait(timeout=2.0)
        return {"ok": True, "provider": "mock"}

    def interrupt_join() -> None:
        assert entered.wait(timeout=2.0)
        time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr("hark.speak_then_listen.handoff._OVERLAP_CANCEL_JOIN_S", 0.02)
    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)
    monkeypatch.setattr("hark.speech.run_listen", blocking_listen)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    interrupter = threading.Thread(target=interrupt_join, daemon=True)
    interrupter.start()

    started = time.monotonic()
    result = run_ask(cfg, "Still there?", confirm="never")

    assert time.monotonic() - started < 0.5
    assert result["cancelled"] is True
    assert result["signal"] == "SIGINT"
    assert abort_started.wait(timeout=1.0)
    assert capture_mod._active_input_stream is None
    assert capture_mod.cancel_active_capture(signal.SIGTERM) is True
    assert len(capture_mod._stream_cancel_workers) == 1
    assert capture_mod.capture_in_progress() is True
    assert worker_finished.is_set() is False

    release_abort.set()
    assert worker_finished.wait(timeout=1.0)
    interrupter.join(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while capture_mod._stream_cancel_workers and time.monotonic() < deadline:
        time.sleep(0.01)
    assert capture_mod._stream_cancel_workers == {}
    assert capture_mod.capture_in_progress() is False


def test_sticky_cancel_preempts_late_input_stream_registration():
    stream = _BlockingStream()

    with capture_mod.capture_interrupt_signals():
        capture_mod.request_capture_cancel(signal.SIGTERM)
        with pytest.raises(capture_mod.CaptureInterrupted) as caught:
            with capture_mod._registered_input_stream(stream):
                pytest.fail("cancelled capture reached stream body")

    assert caught.value.signal_name == "SIGTERM"
    assert stream.aborted == 1


def test_mic_lease_interrupt_after_flock_rolls_back_fd_and_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    real_flock = capture_mod.fcntl.flock

    def interrupt_after_acquire(fd, operation):
        real_flock(fd, operation)
        if operation & capture_mod.fcntl.LOCK_EX:
            raise capture_mod.CaptureInterrupted(signal.SIGINT)

    monkeypatch.setattr(capture_mod.fcntl, "flock", interrupt_after_acquire)
    with pytest.raises(capture_mod.CaptureInterrupted):
        capture_mod.MicLease("interrupted").__enter__()
    monkeypatch.setattr(capture_mod.fcntl, "flock", real_flock)

    probe_fd = os.open(tmp_path / "state" / "hark" / "mic.lock", os.O_RDWR)
    try:
        real_flock(
            probe_fd,
            capture_mod.fcntl.LOCK_EX | capture_mod.fcntl.LOCK_NB,
        )
    finally:
        os.close(probe_fd)
    assert capture_mod.MicLease._holder is None


def test_mic_lease_release_preserves_primary_and_close_releases_lock(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    real_flock = capture_mod.fcntl.flock
    primary = capture_mod.CaptureInterrupted(signal.SIGINT)

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.MicLease("primary"):

            def fail_unlock(fd, operation):
                if operation == capture_mod.fcntl.LOCK_UN:
                    raise RuntimeError("unlock cleanup failed")
                return real_flock(fd, operation)

            monkeypatch.setattr(capture_mod.fcntl, "flock", fail_unlock)
            raise primary
    assert caught.value is primary
    monkeypatch.setattr(capture_mod.fcntl, "flock", real_flock)

    probe_fd = os.open(tmp_path / "state" / "hark" / "mic.lock", os.O_RDWR)
    try:
        real_flock(
            probe_fd,
            capture_mod.fcntl.LOCK_EX | capture_mod.fcntl.LOCK_NB,
        )
    finally:
        os.close(probe_fd)
    assert capture_mod.MicLease._holder is None


def test_repeated_signal_during_stream_cleanup_preserves_first_and_clears_global(
    monkeypatch,
):
    stream = _BlockingStream()
    real_lock = capture_mod._active_stream_lock

    class InterruptingRLock:
        def __init__(self) -> None:
            self.enters = 0

        def __enter__(self):
            real_lock.acquire()
            self.enters += 1
            if self.enters == 2:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
            return self

        def __exit__(self, *_args):
            real_lock.release()

    primary = capture_mod.CaptureInterrupted(signal.SIGINT)
    with capture_mod.capture_interrupt_signals():
        capture_mod.request_capture_cancel(signal.SIGINT)
        monkeypatch.setattr(capture_mod, "_active_stream_lock", InterruptingRLock())
        with pytest.raises(capture_mod.CaptureInterrupted) as caught:
            with capture_mod._registered_input_stream(stream):
                raise primary

    assert caught.value.signal_name == "SIGINT"
    assert capture_mod._active_input_stream is None


def test_first_signal_during_scope_teardown_finishes_state_cleanup(monkeypatch):
    real_lock = capture_mod._capture_state_lock

    class TeardownSignalRLock:
        def __init__(self) -> None:
            self.enters = 0

        def __enter__(self):
            real_lock.acquire()
            self.enters += 1
            if self.enters == 3:
                handler = signal.getsignal(signal.SIGTERM)
                assert callable(handler)
                handler(signal.SIGTERM, None)
            return self

        def __exit__(self, *_args):
            real_lock.release()

    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigterm = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(capture_mod, "_capture_state_lock", TeardownSignalRLock())

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.capture_interrupt_signals():
            pass

    assert caught.value.signal_name == "SIGTERM"
    assert capture_mod._capture_signal_state is None
    assert signal.getsignal(signal.SIGINT) is old_sigint
    assert signal.getsignal(signal.SIGTERM) is old_sigterm


def test_unsurfaced_parent_cancel_during_cleanup_raises_after_cleanup(monkeypatch):
    installed: dict[int, object] = {}

    def capture_handler(signum, handler):
        installed[signum] = handler
        return None

    monkeypatch.setattr(capture_mod.signal, "signal", capture_handler)

    with pytest.raises(capture_mod.CaptureCancelled) as caught:
        with capture_mod.capture_interrupt_signals():
            state = capture_mod._capture_signal_state
            assert state is not None
            assert state.prepare_wake_signal("orchestrator_disappeared") is True
            with capture_mod.cancellation_cleanup(state):
                handler = installed[signal.SIGTERM]
                assert callable(handler)
                handler(signal.SIGTERM, None)

    assert caught.value.reason == "orchestrator_disappeared"


def test_active_listen_registration_rolls_back_on_interrupt(monkeypatch, tmp_path):
    import hark.listen_control as listen_control

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def interrupt_after_publication() -> None:
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(
        listen_control,
        "clear_voice_activity",
        interrupt_after_publication,
    )

    with pytest.raises(capture_mod.CaptureInterrupted):
        with capture_mod.capture_interrupt_signals():
            listen_control.register_active_listen("publication-race", mode="silence")

    assert listen_control.active_path().exists() is False


def test_parent_guard_rejects_reused_ancestor_pid(monkeypatch):
    read_fd, write_fd = os.pipe()
    state = capture_mod._CaptureSignalState()
    cancels: list[str | None] = []
    kills: list[tuple[int, int]] = []

    monkeypatch.setattr(
        capture_mod,
        "_ancestor_identities",
        lambda: ((424242, "original-start"),),
    )
    monkeypatch.setattr(
        capture_mod.os,
        "pidfd_open",
        lambda _pid, _flags: read_fd,
        raising=False,
    )
    monkeypatch.setattr(
        capture_mod,
        "_proc_parent_and_start",
        lambda _pid: (1, "reused-start"),
    )
    monkeypatch.setattr(
        capture_mod.select,
        "select",
        lambda *_args, **_kwargs: pytest.fail("guard watched a reused PID"),
    )
    monkeypatch.setattr(
        capture_mod,
        "cancel_active_capture",
        lambda *_args, reason=None, **_kwargs: cancels.append(reason) or True,
    )
    monkeypatch.setattr(
        capture_mod.os,
        "kill",
        lambda pid, signum: kills.append((pid, signum)),
    )

    guard = capture_mod._ParentLifetimeGuard(state)
    try:
        guard._watch()
    finally:
        os.close(write_fd)

    interruption = state.interruption()
    assert isinstance(interruption, capture_mod.CaptureCancelled)
    assert interruption.reason == "orchestrator_disappeared"
    assert cancels == ["orchestrator_disappeared"]
    assert kills == [(os.getpid(), signal.SIGTERM)]


def test_parent_guard_without_pidfd_uses_procfs_fallback(monkeypatch):
    state = capture_mod._CaptureSignalState()
    cancels: list[str | None] = []
    kills: list[tuple[int, int]] = []

    monkeypatch.delattr(capture_mod.os, "pidfd_open", raising=False)
    monkeypatch.setattr(
        capture_mod,
        "_ancestor_identities",
        lambda: ((424242, "original-start"),),
    )
    monkeypatch.setattr(
        capture_mod,
        "_proc_parent_and_start",
        lambda _pid: (1, "reused-start"),
    )
    monkeypatch.setattr(
        capture_mod.select,
        "select",
        lambda *_args, **_kwargs: pytest.fail(
            "pidfd-free fallback unexpectedly called select"
        ),
    )
    monkeypatch.setattr(
        capture_mod,
        "cancel_active_capture",
        lambda *_args, reason=None, **_kwargs: cancels.append(reason) or True,
    )
    monkeypatch.setattr(
        capture_mod.os,
        "kill",
        lambda pid, signum: kills.append((pid, signum)),
    )

    guard = capture_mod._ParentLifetimeGuard(state)
    guard._watch()

    interruption = state.interruption()
    assert isinstance(interruption, capture_mod.CaptureCancelled)
    assert interruption.reason == "orchestrator_disappeared"
    assert cancels == ["orchestrator_disappeared"]
    assert kills == [(os.getpid(), signal.SIGTERM)]


def test_ambient_pause_cleanup_does_not_replace_primary(monkeypatch):
    import hark.mic_coord as mic_coord

    primary = capture_mod.CaptureInterrupted(signal.SIGINT)
    monkeypatch.setattr(
        mic_coord, "request_ambient_pause", lambda **_kwargs: "pause-token"
    )
    monkeypatch.setattr(mic_coord, "wait_for_mic_free", lambda **_kwargs: None)
    monkeypatch.setattr(
        mic_coord,
        "clear_ambient_pause",
        lambda _token: (_ for _ in ()).throw(
            capture_mod.CaptureInterrupted(signal.SIGTERM)
        ),
    )

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with mic_coord.pause_ambient_for_mic():
            raise primary

    assert caught.value is primary


def test_signal_after_ambient_pause_publication_rolls_back_owner(monkeypatch, tmp_path):
    import hark.mic_coord as mic_coord

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    def interrupting_syslog(*_args, **_kwargs):
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(mic_coord, "syslog", interrupting_syslog)
    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        with capture_mod.capture_interrupt_signals():
            mic_coord.request_ambient_pause(reason="post-publication-signal")

    assert caught.value.signal_name == "SIGINT"
    assert mic_coord.ambient_pause_requested() is False
    assert mic_coord.read_ambient_pause() is None


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
    worker_started = threading.Event()
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
        return {"ok": True, "provider": "mock"}

    def interrupt_wait() -> None:
        assert worker_started.wait(timeout=2.0)
        time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)
    monkeypatch.setattr("hark.speech.run_listen", cancellable_listen)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    interrupter = threading.Thread(target=interrupt_wait, daemon=True)
    interrupter.start()

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        speak_and_listen(cfg, "Still there?")
    interrupter.join(timeout=1.0)

    assert caught.value.signal_name == "SIGINT"
    assert worker_released.is_set()


def test_overlap_interrupt_during_thread_start_waits_for_publication(monkeypatch):
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 50
    start_entered = threading.Event()
    allow_start = threading.Event()
    callback_done = threading.Event()
    late_capture = threading.Event()
    primary = capture_mod.CaptureInterrupted(signal.SIGINT)
    real_thread = threading.Thread

    class StartingThread:
        def __init__(self, *, target, name, daemon):
            assert callable(target)
            assert name == "hark-overlap-listen"
            assert daemon is True
            self._target = target

        def start(self) -> None:
            start_entered.set()
            assert allow_start.wait(timeout=2.0)
            self._target()

    def release_start() -> None:
        assert start_entered.wait(timeout=2.0)
        time.sleep(0.05)
        allow_start.set()

    def fake_tts(_cfg, _text, **kwargs):
        def invoke_callback() -> None:
            try:
                kwargs["on_near_end"]()
            finally:
                callback_done.set()

        real_thread(target=invoke_callback, daemon=True).start()
        assert start_entered.wait(timeout=2.0)
        raise primary

    monkeypatch.setattr(
        "hark.speak_then_listen.handoff.threading.Thread", StartingThread
    )
    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)
    monkeypatch.setattr("hark.speech.run_listen", lambda *_a, **_k: late_capture.set())
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    releaser = real_thread(target=release_start, daemon=True)
    releaser.start()

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        speak_and_listen(cfg, "Still there?")
    releaser.join(timeout=1.0)

    assert caught.value is primary
    assert callback_done.wait(timeout=1.0)
    assert not late_capture.is_set()


def test_overlap_start_exception_after_launch_keeps_handle_for_cleanup(monkeypatch):
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 50
    primary = capture_mod.CaptureInterrupted(signal.SIGINT)
    allow_bootstrap = threading.Event()
    worker_done = threading.Event()
    late_capture = threading.Event()
    real_thread = threading.Thread

    class LaunchedThenInterrupted:
        def __init__(self, *, target, name, daemon):
            assert name == "hark-overlap-listen"
            assert daemon is True
            self._target = target

        def start(self) -> None:
            def delayed_bootstrap() -> None:
                assert allow_bootstrap.wait(timeout=2.0)
                self._target()
                worker_done.set()

            real_thread(target=delayed_bootstrap, daemon=True).start()
            raise primary

    def fake_tts(_cfg, _text, **kwargs):
        kwargs["on_near_end"]()
        return {"ok": True, "provider": "mock"}

    monkeypatch.setattr(
        "hark.speak_then_listen.handoff.threading.Thread",
        LaunchedThenInterrupted,
    )
    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)
    monkeypatch.setattr("hark.speech.run_listen", lambda *_a, **_k: late_capture.set())
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)

    with pytest.raises(capture_mod.CaptureInterrupted) as caught:
        speak_and_listen(cfg, "Still there?")

    assert caught.value is primary
    allow_bootstrap.set()
    assert worker_done.wait(timeout=1.0)
    assert not late_capture.is_set()


def test_overlap_cancel_cleanup_has_fixed_deadline(monkeypatch):
    import hark.speak_then_listen.handoff as handoff_mod

    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 50
    primary = capture_mod.CaptureInterrupted(signal.SIGINT)
    worker_started = threading.Event()
    allow_terminal = threading.Event()
    worker_terminal = threading.Event()

    def stuck_listen(*_args, **_kwargs):
        worker_started.set()
        assert allow_terminal.wait(timeout=2.0)
        worker_terminal.set()
        return ListenResult(
            text="owned",
            provider="mock",
            duration_ms=0,
            end_mode="silence",
            stream_id="deadline-owned",
        )

    def fake_tts(_cfg, _text, **kwargs):
        kwargs["on_near_end"]()
        assert worker_started.wait(timeout=2.0)
        raise primary

    monkeypatch.setattr(handoff_mod, "_OVERLAP_CANCEL_JOIN_S", 0.02)
    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)
    monkeypatch.setattr("hark.speech.run_listen", stuck_listen)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)

    quarantine_before = len(handoff_mod._QUARANTINED_ATTEMPTS)
    started = time.monotonic()
    try:
        with pytest.raises(capture_mod.CaptureInterrupted) as caught:
            speak_and_listen(cfg, "Still there?")
        assert caught.value is primary
        assert time.monotonic() - started < 0.5
        assert len(handoff_mod._QUARANTINED_ATTEMPTS) == quarantine_before + 1
    finally:
        allow_terminal.set()

    assert worker_terminal.wait(timeout=1.0)


def test_delayed_overlap_worker_retains_cancel_token_after_parent_returns(
    monkeypatch,
):
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 50
    entered = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()
    finished = threading.Event()
    proceeded = threading.Event()

    def delayed_listen(*_args, **_kwargs):
        entered.set()
        try:
            assert release.wait(timeout=2.0)
            try:
                capture_mod.raise_if_capture_cancelled()
            except capture_mod.CaptureInterrupted:
                cancelled.set()
                raise
            proceeded.set()
            raise AssertionError("delayed worker proceeded after cancellation")
        finally:
            finished.set()

    def fake_tts(_cfg, _text, **kwargs):
        kwargs["on_near_end"]()
        assert entered.wait(timeout=2.0)
        return {"ok": True, "provider": "mock"}

    def interrupt_join() -> None:
        assert entered.wait(timeout=2.0)
        time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr("hark.speak_then_listen.handoff._OVERLAP_CANCEL_JOIN_S", 0.02)
    monkeypatch.setattr("hark.speech.maybe_print_tts_question", lambda *_a, **_k: None)
    monkeypatch.setattr("hark.speech.run_listen", delayed_listen)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    interrupter = threading.Thread(target=interrupt_join, daemon=True)
    interrupter.start()

    result = run_ask(cfg, "Still there?", confirm="never")
    assert result["cancelled"] is True
    assert result["signal"] == "SIGINT"
    assert capture_mod._capture_signal_state is None
    assert finished.is_set() is False

    release.set()
    assert finished.wait(timeout=1.0)
    interrupter.join(timeout=1.0)
    assert cancelled.is_set()
    assert proceeded.is_set() is False
    assert capture_mod._active_input_stream is None
    assert capture_mod.MicLease._holder is None


@pytest.mark.parametrize("outer_scope", [False, True])
def test_overlap_sigint_during_first_join_waits_for_worker_terminal(
    monkeypatch, outer_scope
):
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

    scope = capture_mod.capture_interrupt_signals() if outer_scope else nullcontext()
    with scope:
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
        cleaned.write_text("sync-exit")
        while True:
            time.sleep(1.0)

    def abort(self):
        self.aborted = True
        cleaned.write_text("abort")

    def close(self):
        while True:
            time.sleep(1.0)

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
    assert cleaned.read_text() == "abort"
    assert "Traceback" not in stderr
    assert "KeyboardInterrupt" not in stderr


@pytest.mark.parametrize(
    ("deliver_internal_wake", "native_abort_returns"),
    [(True, True), (False, True), (True, False)],
)
def test_orchestrator_disappearance_without_signal_cancels_and_preserves_workers(
    tmp_path,
    deliver_internal_wake,
    native_abort_returns,
):
    """A vanished launcher must not orphan Pa_ReadStream or stop Mode A workers."""
    state_root = tmp_path / "state" / "hark"
    state_root.mkdir(parents=True)
    worker = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    (state_root / "mode-a.pids").write_text(f"{worker.pid}\n", encoding="utf-8")

    ready = tmp_path / "orphan-ready"
    child_pid_path = tmp_path / "orphan-child.pid"
    intermediary_pid_path = tmp_path / "orphan-intermediary.pid"
    result_path = tmp_path / "orphan-result.json"
    cleaned = tmp_path / "orphan-cleaned"
    script = r"""\
import contextlib
import io
import json
import os
import pathlib
import sys
import time
from types import SimpleNamespace

ready = pathlib.Path(sys.argv[1])
child_pid_path = pathlib.Path(sys.argv[2])
intermediary_pid_path = pathlib.Path(sys.argv[3])
result_path = pathlib.Path(sys.argv[4])
cleaned = pathlib.Path(sys.argv[5])
deliver_internal_wake = sys.argv[6] == "1"
native_abort_returns = sys.argv[7] == "1"

intermediary = os.fork()
if intermediary:
    deadline = time.monotonic() + 5.0
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    os._exit(0)  # Simulate Codex/orchestrator disappearance: no signal to ask.

# Model a surviving shell/uv wrapper between Codex and the ask process.
intermediary_pid_path.write_text(str(os.getpid()), encoding="utf-8")
pid = os.fork()
if pid:
    while True:
        time.sleep(1.0)

child_pid_path.write_text(str(os.getpid()), encoding="utf-8")

import hark.audio.capture as capture
import hark.cli as cli
import hark.listen_control as listen_control
import hark.mic_coord as mic_coord
import hark.speech as speech
import hark.workers as workers
from hark.config import HarkConfig

if not deliver_internal_wake:
    real_kill = os.kill

    def suppress_internal_wake(pid, signum):
        if pid == os.getpid() and signum == signal.SIGTERM:
            raise OSError("internal wake unavailable")
        return real_kill(pid, signum)

    import signal
    os.kill = suppress_internal_wake

class BlockingStream:
    def __init__(self):
        self.aborted = False

    def start(self):
        return None

    def read(self, _block):
        ready.write_text("reading", encoding="utf-8")
        while not self.aborted:
            time.sleep(0.01)
        raise RuntimeError("aborted blocking read")

    def abort(self):
        with cleaned.open("a", encoding="utf-8") as handle:
            handle.write("abort\n")
        if not native_abort_returns:
            while True:
                time.sleep(1.0)
        self.aborted = True

    def close(self):
        with cleaned.open("a", encoding="utf-8") as handle:
            handle.write("close\n")

stream = BlockingStream()
capture._require_sd = lambda: None
capture.sd = SimpleNamespace(InputStream=lambda **_kwargs: stream)

cfg = HarkConfig()
cfg.audio.overlap_prearm = False
cfg.audio.listen_pre_arm_ms = 0
cfg.audio.duck_media_during_stt = False
cfg.audio.pause_media_during_stt = False
cfg.audio.answer_arm_cue = False
cfg.listen.empty_stt_retry = False
cfg.listen.empty_stt_nudge = False
cli.load_config = lambda *_args, **_kwargs: cfg
speech.run_tts = lambda *_args, **_kwargs: {"ok": True, "provider": "mock"}
speech.resolve_stt = lambda *_args, **_kwargs: SimpleNamespace(name="mock")
speech.configure_cues_from_config = lambda *_args, **_kwargs: None
speech.play_record_start = lambda: None
speech.play_record_stop = lambda: None
speech.duck_media = lambda *_args, **_kwargs: contextlib.nullcontext()

stdout = io.StringIO()
stderr = io.StringIO()
with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
    exit_code = cli.main(["ask", "--confirm", "never", "--json", "Still there?"])

# Native cleanup is asynchronous but bounded; wait briefly for its registry.
deadline = time.monotonic() + 1.0
while capture.capture_in_progress() and time.monotonic() < deadline:
    time.sleep(0.01)

try:
    with capture.MicLease("post-interrupt-probe"):
        mic_available = True
except Exception:
    mic_available = False

result_tmp = result_path.with_suffix(".tmp")
result_tmp.write_text(
    json.dumps(
        {
            "exit_code": exit_code,
            "ask": json.loads(stdout.getvalue()),
            "stderr": stderr.getvalue(),
            "capture_in_progress": capture.capture_in_progress(),
            "mic_available": mic_available,
            "mic_holder": capture.MicLease._holder,
            "ambient_pause": mic_coord.read_ambient_pause(),
            "active_listen": listen_control.read_active(),
            "workers_running": workers.workers_status()["workers"]["running"],
            "worker_pids": workers.workers_status()["workers"]["pids"],
        }
    ),
    encoding="utf-8",
)
os.replace(result_tmp, result_path)
"""
    env = os.environ.copy()
    src_dir = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(src_dir), env.get("PYTHONPATH", "")))
    )
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    launcher = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(ready),
            str(child_pid_path),
            str(intermediary_pid_path),
            str(result_path),
            str(cleaned),
            "1" if deliver_internal_wake else "0",
            "1" if native_abort_returns else "0",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    child_pid: int | None = None
    intermediary_pid: int | None = None
    try:
        launcher.wait(timeout=5.0)
        deadline = time.monotonic() + 5.0
        while not result_path.exists() and time.monotonic() < deadline:
            if child_pid is None and child_pid_path.exists():
                child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            time.sleep(0.01)
        assert result_path.exists(), "orphan ask did not cancel after launcher exit"
        result = json.loads(result_path.read_text(encoding="utf-8"))
    finally:
        if child_pid is None and child_pid_path.exists():
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
        if intermediary_pid_path.exists():
            intermediary_pid = int(intermediary_pid_path.read_text(encoding="utf-8"))
        for pid in (child_pid, intermediary_pid):
            if pid is None:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if launcher.poll() is None:
            launcher.kill()
            launcher.wait(timeout=2.0)
        if worker.poll() is None:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.wait(timeout=2.0)

    ask = result["ask"]
    assert result["exit_code"] == ABORT
    assert ask["ok"] is False
    assert ask["cancelled"] is True
    assert ask["error"] == "interrupted"
    assert ask["reason"] == "orchestrator_disappeared"
    assert ask["end_phrase"] == "orchestrator_disappeared"
    assert ask["signal"] is None
    assert "Traceback" not in result["stderr"]
    assert "KeyboardInterrupt" not in result["stderr"]
    assert result["capture_in_progress"] is (not native_abort_returns)
    assert result["mic_available"] is True
    assert result["mic_holder"] is None
    assert result["ambient_pause"] is None
    assert result["active_listen"] is None
    assert result["workers_running"] is True
    assert worker.pid in result["worker_pids"]
    expected_cleanup = ["abort", "close"] if native_abort_returns else ["abort"]
    assert cleaned.read_text(encoding="utf-8").splitlines() == expected_cleanup


@pytest.mark.skipif(shutil.which("script") is None, reason="util-linux script missing")
def test_ctrl_c_from_real_pty_returns_structured_abort(tmp_path):
    ready = tmp_path / "pty-ready"
    cleaned = tmp_path / "pty-cleaned"
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
        cleaned.write_text("sync-exit")
        while True:
            time.sleep(1.0)

    def abort(self):
        self.aborted = True
        cleaned.write_text("abort")

    def close(self):
        return None

    def read(self, _block):
        ready.write_text("reading")
        while not self.aborted:
            time.sleep(0.01)
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
    env["XDG_STATE_HOME"] = str(tmp_path / "state")

    child_command = shlex.join([sys.executable, "-c", script, str(ready), str(cleaned)])
    process = subprocess.Popen(
        ["script", "-qefc", child_command, "/dev/null"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        deadline = time.monotonic() + 5.0
        while (
            not ready.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert ready.exists(), "PTY child did not enter blocking capture"
        assert process.stdin is not None
        process.stdin.write(b"\x03")
        process.stdin.flush()
        stdout, stderr = process.communicate(timeout=5.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=2.0)

    rendered = stdout.decode(errors="replace")
    json_start = rendered.find("{")
    assert json_start >= 0, rendered
    result, _end = json.JSONDecoder().raw_decode(rendered[json_start:])
    assert process.returncode == ABORT
    assert result["cancelled"] is True
    assert result["signal"] == "SIGINT"
    assert cleaned.read_text() == "abort"
    assert "Traceback" not in rendered
    assert "KeyboardInterrupt" not in rendered
    assert b"Traceback" not in stderr

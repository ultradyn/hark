"""CLI exit must keep Python's pending-signal dispatch running (B153/B184)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys

import pytest

from hark_test_guard import guarded_subprocess_env
from hark.tts_interrupt_policy import (
    TtsSynthesisInterrupted,
    cli_tts_interrupt_scope,
    register_cli_terminal_wait,
)


_WAIT_S = float(os.environ.get("HARK_TEST_WAIT_S", "30"))


def test_nested_cli_waits_run_after_outer_result_and_release_references():
    import gc
    import weakref

    events = []

    class Wait:
        def __call__(self):
            events.append("terminal wait")

    wait = Wait()
    reference = weakref.ref(wait)
    with cli_tts_interrupt_scope():
        with cli_tts_interrupt_scope():
            assert register_cli_terminal_wait(wait) is True
        events.append("nested result")
        assert events == ["nested result"]
        events.append("outer result")
    assert events == ["nested result", "outer result", "terminal wait"]
    del wait
    gc.collect()
    assert reference() is None


def test_library_registration_retains_no_work():
    events = []
    assert register_cli_terminal_wait(lambda: events.append("unexpected wait")) is False
    with cli_tts_interrupt_scope():
        events.append("successful command")
    assert events == ["successful command"]


@pytest.mark.parametrize("has_primary", [False, True])
def test_terminal_callback_errors_do_not_skip_work_or_replace_primary(has_primary):
    events = []

    def fail():
        raise RuntimeError("terminal cleanup failed")

    expected = TtsSynthesisInterrupted if has_primary else RuntimeError
    with pytest.raises(expected):
        with cli_tts_interrupt_scope():
            register_cli_terminal_wait(fail)
            register_cli_terminal_wait(lambda: events.append("remaining cleanup"))
            if has_primary:
                raise TtsSynthesisInterrupted
    assert events == ["remaining cleanup"]
    assert register_cli_terminal_wait(lambda: None) is False


@pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(signal, "pthread_kill"),
    reason="requires targeted POSIX thread signals",
)
@pytest.mark.parametrize("command", ["tts", "ask"])
def test_worker_targeted_repeat_is_processed_after_first_cli_result(tmp_path, command):
    script = r'''
import json
import os
from pathlib import Path
import signal
import sys
import threading

import hark.cli as cli
import hark.speech as speech
from hark.config import HarkConfig

command, marker = sys.argv[1:]
main_ident = threading.get_ident()

class Store:
    def record_tts(self, **kwargs):
        return None

class Synth:
    def synthesize(self, text, *, voice):
        print("READY", flush=True)
        poll = threading.Event()
        while True:
            frame = sys._current_frames().get(main_ident)
            names = []
            top_name = frame.f_code.co_name if frame is not None else ""
            while frame is not None:
                names.append(frame.f_code.co_name)
                frame = frame.f_back
            # Inspect a state, not a marker preceding it: the main thread must
            # already be inside the old native join or the replacement wait.
            waiting = top_name in {"wait", "join"} and any(
                name in names for name in ("_python_exit", "_wait_for_cli_terminal_exit")
            )
            if waiting:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
                Path(marker).write_text("worker-targeted SIGINT")
                try:
                    # Native delivery occurs in this worker; only the main
                    # thread can later execute the Python signal handler.
                    signal.pthread_kill(threading.get_ident(), signal.SIGINT)
                except BaseException:
                    os._exit(93)
                threading.Event().wait()
            poll.wait(0.001)

speech.UsageStore = Store
speech.resolve_tts = lambda *args, **kwargs: Synth()
speech._synth_transport_factory = speech._in_process_synth_transport_factory
speech.lookup_cached_tts = lambda *args, **kwargs: None
cfg = HarkConfig()
cli.load_config = lambda *args, **kwargs: cfg
if command == "tts":
    cli.dispatch = lambda args, loaded: speech.run_tts(
        loaded, "pending signal", play=False, use_cache=False
    )
    raise SystemExit(cli.main(["providers"]))
raise SystemExit(cli.main(["ask", "--confirm", "never", "--json", "Pending signal?"]))
'''
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["XAI_API_KEY"] = "hark-test-placeholder-not-a-real-key"
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    marker = tmp_path / "second-signal"
    process = subprocess.Popen(
        [sys.executable, "-c", script, command, str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=guarded_subprocess_env(env),
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as ready:
            ready.register(process.stdout, selectors.EVENT_READ)
            assert ready.select(_WAIT_S), "synthesis worker did not become ready"
        assert process.stdout.readline().strip() == "READY"
        os.kill(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=_WAIT_S)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=_WAIT_S)

    assert marker.read_text() == "worker-targeted SIGINT"
    assert process.returncode == 130
    if command == "ask":
        # Dispatch must emit the first cancellation before the terminal wait.
        # A forced repeat exits immediately and cannot manufacture this JSON.
        result = json.loads(stdout)
        assert result["ok"] is False
        assert result["cancelled"] is True
        assert result["error"] == "interrupted"
        assert result["exit"] != 0
    else:
        assert stdout == ""
    assert "Traceback" not in stderr

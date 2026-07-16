"""B153: bounded repeated-SIGINT escape from non-cooperative TTS synth."""

from __future__ import annotations

import io
import json
import os
import selectors
import signal
import struct
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + current if current else "")
    return env


def _read_ready(proc: subprocess.Popen[str], timeout_s: float = 2.0) -> str:
    assert proc.stdout is not None
    selector = selectors.DefaultSelector()
    try:
        selector.register(proc.stdout, selectors.EVENT_READ)
        assert selector.select(timeout_s), "child did not report ready"
        return proc.stdout.readline().strip()
    finally:
        selector.close()


def _terminate(proc: subprocess.Popen[str]) -> tuple[str, str]:
    if proc.poll() is None:
        proc.kill()
    return proc.communicate(timeout=2.0)


def _hung_tts_child(
    *,
    play: bool,
    cooperative_delay_s: float | None,
    cleanup_delay_s: float = 0.0,
) -> str:
    delay = repr(cooperative_delay_s)
    cleanup_delay = repr(cleanup_delay_s)
    return textwrap.dedent(
        f"""
        import threading
        import time
        from types import SimpleNamespace

        import hark.cli as cli
        import hark.conference as conference
        import hark.speech as speech
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        class Synth:
            def synthesize(self, text, *, voice):
                print("READY", flush=True)
                delay = {delay}
                if delay is None:
                    threading.Event().wait()
                else:
                    time.sleep(delay)
                return SimpleNamespace(
                    audio=b"audio",
                    provider="fake",
                    content_type="audio/mpeg",
                    voice=voice,
                )

        class Hold:
            skipped = False
            def as_meta(self):
                return {{}}

        speech.UsageStore = Store
        speech.resolve_tts = lambda *args, **kwargs: Synth()
        speech._synth_transport_factory = speech._in_process_synth_transport_factory
        speech.lookup_cached_tts = lambda *args, **kwargs: None
        conference.apply_conference_hold = lambda *args, **kwargs: Hold()
        speech.claim_tts_play_ticket = lambda: object()
        def abandon(*args, **kwargs):
            print("ABANDON_START", flush=True)
            time.sleep({cleanup_delay})
            print("ABANDON_DONE", flush=True)

        speech.abandon_tts_play_ticket = abandon
        speech.repair_tts_mute_after_play = (
            lambda **kwargs: print("REPAIR", flush=True) or {{"repaired": False}}
        )

        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded,
            "hung",
            play={play!r},
            use_cache=False,
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _failed_then_hung_tts_child() -> str:
    return textwrap.dedent(
        """
        import threading
        from types import SimpleNamespace

        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        class Synth:
            calls = 0
            lock = threading.Lock()
            def synthesize(self, text, *, voice):
                with self.lock:
                    type(self).calls += 1
                    call = type(self).calls
                if call == 1:
                    raise RuntimeError("first synth failed")
                print("HUNG_READY", flush=True)
                threading.Event().wait()
                return SimpleNamespace(
                    audio=b"audio",
                    provider="fake",
                    content_type="audio/mpeg",
                    voice=voice,
                )

        speech.UsageStore = Store
        speech.resolve_tts = lambda *args, **kwargs: Synth()
        speech._synth_transport_factory = speech._in_process_synth_transport_factory
        cfg = HarkConfig()
        cfg.tts.chunk_chars = 5
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded,
            "alpha beta",
            play=False,
            use_cache=False,
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _gil_holding_tts_child() -> str:
    return textwrap.dedent(
        """
        import ctypes
        import os
        import sys

        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        class Synth:
            def synthesize(self, text, *, voice):
                print(f"GIL_READY {os.getpid()}", flush=True)
                ctypes.PyDLL(None).sleep(10)
                raise AssertionError("unreachable")

        speech.UsageStore = Store
        speech._synth_worker_command_factory = lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-gil-hang",
        ]
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded,
            "hung in C",
            play=False,
            use_cache=False,
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _append_failure_tts_child() -> str:
    return textwrap.dedent(
        """
        import threading
        import sys

        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        class Synth:
            def synthesize(self, text, *, voice):
                print("APPEND_READY", flush=True)
                threading.Event().wait()

        class BadList(list):
            def append(self, value):
                raise MemoryError("future tracking failed")

        RealPool = speech._InterruptibleSynthPool
        class AppendFailPool(RealPool):
            def __init__(self):
                super().__init__()
                self._futures = BadList()

        speech.UsageStore = Store
        speech._synth_worker_command_factory = lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-hang",
        ]
        speech._InterruptibleSynthPool = AppendFailPool
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded,
            "append failure",
            play=False,
            use_cache=False,
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def test_shutdown_wait_false_alone_still_hangs_at_interpreter_exit(tmp_path):
    """Prove the executor atexit join that makes wait=False insufficient."""
    child = textwrap.dedent(
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        blocker = threading.Event()
        started = threading.Event()
        def hang():
            started.set()
            blocker.wait()

        pool = ThreadPoolExecutor(max_workers=1)
        pool.submit(hang)
        started.wait()
        pool.shutdown(wait=False, cancel_futures=True)
        print("READY", flush=True)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "READY"
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=0.2)
    finally:
        _terminate(proc)


def test_sigint_during_waiting_shutdown_retains_repeated_exit_handler(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", _failed_then_hung_tts_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "HUNG_READY"
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.15)
        assert proc.poll() is None

        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert "Traceback" not in stderr
    assert "threading shutdown" not in stderr
    assert "concurrent.futures" not in stderr


def test_gil_holding_provider_has_os_independent_repeated_exit(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", _gil_holding_tts_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        ready, provider_pid_text = _read_ready(proc).split()
        assert ready == "GIL_READY"
        provider_pid = int(provider_pid_text)

        for _ in range(3):
            os.kill(proc.pid, signal.SIGINT)
            time.sleep(0.15)
        stdout, stderr = proc.communicate(timeout=1.25)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""
    with pytest.raises(ProcessLookupError):
        os.kill(provider_pid, 0)


def test_append_failure_keeps_started_worker_owned_for_repeated_exit(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", _append_failure_tts_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "APPEND_READY"
        for _ in range(3):
            os.kill(proc.pid, signal.SIGINT)
            time.sleep(0.15)
        stdout, stderr = proc.communicate(timeout=1.25)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert "Traceback" not in stderr
    assert "threading shutdown" not in stderr
    assert "concurrent.futures" not in stderr


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("--test-success", "ok"),
        ("--test-provider-error", "provider"),
        ("--test-unknown-error", "unknown"),
    ],
)
def test_exec_worker_result_and_exception_mapping(
    monkeypatch, tmp_path, mode, expected
):
    import hark.speech as speech
    from hark.config import HarkConfig
    from hark.providers.base import ProviderError
    from hark.tts_isolation import SynthWorkerError

    class Store:
        def record_tts(self, **kwargs):
            return None

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(speech, "UsageStore", Store)
    monkeypatch.setattr(
        speech,
        "_synth_worker_command_factory",
        lambda: [sys.executable, "-m", "hark.tts_worker", mode],
    )
    cfg = HarkConfig()
    cached: list[tuple[str, str, bytes]] = []
    monkeypatch.setattr(speech, "lookup_cached_tts", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        speech,
        "store_cached_tts",
        lambda voice, text, audio: cached.append((voice, text, audio)),
    )

    if expected == "provider":
        with pytest.raises(ProviderError, match="worker provider failed") as caught:
            speech.run_tts(cfg, "hello", play=False, use_cache=False)
        assert caught.value.code == 42
    elif expected == "unknown":
        with pytest.raises(SynthWorkerError, match="test.UnknownFailure"):
            speech.run_tts(cfg, "hello", play=False, use_cache=False)
    else:
        result = speech.run_tts(cfg, "hello", play=False, use_cache=True)
        assert result["ok"] is True
        assert result["provider"] == "test-worker"
        assert result["voice"] == "test-voice"
        assert cached == [("test-voice", "hello", b"test-audio")]


def _worker_frame(message, audio=b""):
    payload = json.dumps(message).encode("utf-8")
    return io.BytesIO(struct.pack("!I", len(payload)) + payload + audio)


@pytest.mark.parametrize(
    "message",
    [
        {},
        {"status": 1},
        {"status": "wat"},
        {
            "status": "ok",
            "audio_size": "1",
            "provider": "p",
            "content_type": "c",
            "voice": "v",
        },
        {
            "status": "ok",
            "audio_size": True,
            "provider": "p",
            "content_type": "c",
            "voice": "v",
        },
        {"status": "ok", "audio_size": 0, "content_type": "c", "voice": "v"},
        {
            "status": "ok",
            "audio_size": 0,
            "provider": 1,
            "content_type": "c",
            "voice": "v",
        },
        {"status": "error", "kind": "provider", "message": 1, "code": 4},
        {"status": "error", "kind": "provider", "message": "x", "code": "4"},
        {"status": "error", "kind": "provider", "message": "x", "code": -1},
        {"status": "error", "kind": "exception", "type": 1, "message": "x"},
        {"status": "error", "kind": "other", "type": "x", "message": "x"},
    ],
)
def test_worker_protocol_rejects_malformed_primitive_fields(message):
    from hark.tts_isolation import SubprocessSynthTransport, SynthWorkerError

    with pytest.raises(SynthWorkerError):
        SubprocessSynthTransport._decode(_worker_frame(message), 1)


def test_worker_protocol_preserves_empty_provider_message_and_zero_code():
    from hark.providers.base import ProviderError
    from hark.tts_isolation import SubprocessSynthTransport

    frame = _worker_frame(
        {
            "status": "error",
            "kind": "provider",
            "message": "",
            "code": 0,
            "audio_size": 0,
        }
    )
    with pytest.raises(ProviderError) as caught:
        SubprocessSynthTransport._decode(frame, 1)
    assert str(caught.value) == ""
    assert caught.value.code == 0


def _request_frame(message):
    payload = json.dumps(message).encode("utf-8")
    return io.BytesIO(struct.pack("!I", len(payload)) + payload)


@pytest.mark.parametrize(
    "message",
    [
        [],
        {},
        {"provider": 1, "voice": "v", "language": None, "text": "x"},
        {"provider": "p", "voice": 1, "language": None, "text": "x"},
        {"provider": "p", "voice": "v", "language": 1, "text": "x"},
        {"provider": "p", "voice": "v", "language": None, "text": 1},
    ],
)
def test_worker_request_protocol_rejects_malformed_primitive_fields(message):
    from hark.tts_worker import _read_request

    with pytest.raises(ValueError):
        _read_request(_request_frame(message))


def test_worker_request_protocol_is_framed_and_bounded():
    from hark.tts_worker import _read_request

    with pytest.raises(ValueError, match="oversize"):
        _read_request(io.BytesIO(struct.pack("!I", 64 * 1024 + 1)))


def test_worker_decode_does_not_replace_system_exception(monkeypatch):
    import hark.tts_isolation as isolation

    monkeypatch.setattr(
        isolation.json,
        "loads",
        lambda payload: (_ for _ in ()).throw(MemoryError("primary")),
    )
    frame = _worker_frame({"status": "ok"})
    with pytest.raises(MemoryError, match="primary"):
        isolation.SubprocessSynthTransport._decode(frame, 1)


def test_unregister_failure_does_not_replace_provider_error_or_leave_child():
    from hark.providers.base import ProviderError
    from hark.tts_isolation import SubprocessSynthTransport

    class RaisingOwner:
        process = None

        def register_synth_process(self, process):
            self.process = process

        def unregister_synth_process(self, process):
            assert process is self.process
            raise RuntimeError("unregister failed")

    owner = RaisingOwner()
    transport = SubprocessSynthTransport(
        owner,
        command_factory=lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-provider-error",
        ],
    )
    from hark.tts_isolation import SynthRequest

    with pytest.raises(ProviderError, match="worker provider failed"):
        transport.synthesize(SynthRequest("p", "v", None, "x"))
    assert owner.process is not None
    assert owner.process.poll() is not None


def test_spawn_return_failure_reaps_preclaimed_child_and_preserves_primary(tmp_path):
    child = textwrap.dedent(
        """
        import os
        import sys

        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        real_init = isolation.subprocess.Popen.__init__
        spawned = {}
        def init_then_raise(self, *args, **kwargs):
            real_init(self, *args, **kwargs)
            spawned["pid"] = self.pid
            raise MemoryError("after child creation")

        speech.UsageStore = Store
        speech._synth_worker_command_factory = lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-hang",
        ]
        isolation.subprocess.Popen.__init__ = init_then_raise
        try:
            speech.run_tts(HarkConfig(), "hello", play=False, use_cache=False)
        except MemoryError as exc:
            print(f"PRIMARY {exc}", flush=True)
        finally:
            isolation.subprocess.Popen.__init__ = real_init

        pid = spawned["pid"]
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print("CHILD_GONE", flush=True)
        else:
            raise AssertionError(f"child still alive: {pid}")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=3.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert "PRIMARY after child creation" in proc.stdout
    assert "CHILD_GONE" in proc.stdout
    assert proc.stderr == ""


def test_future_tracking_append_failure_preserves_memoryerror(monkeypatch, tmp_path):
    import hark.speech as speech
    from hark.config import HarkConfig

    class Store:
        def record_tts(self, **kwargs):
            return None

    class BadList(list):
        def append(self, value):
            raise MemoryError("future tracking failed")

    real_pool = speech._InterruptibleSynthPool

    class AppendFailPool(real_pool):
        def __init__(self):
            super().__init__()
            self._futures = BadList()

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(speech, "UsageStore", Store)
    monkeypatch.setattr(speech, "_InterruptibleSynthPool", AppendFailPool)
    monkeypatch.setattr(
        speech,
        "_synth_worker_command_factory",
        lambda: [sys.executable, "-m", "hark.tts_worker", "--test-success"],
    )

    with pytest.raises(MemoryError, match="future tracking failed"):
        speech.run_tts(HarkConfig(), "hello", play=False, use_cache=False)


@pytest.mark.parametrize("play", [False, True])
def test_repeated_sigint_hard_exits_hung_synth_without_traceback(tmp_path, play):
    proc = subprocess.Popen(
        [sys.executable, "-c", _hung_tts_child(play=play, cooperative_delay_s=None)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "READY"
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.15)
        assert proc.poll() is None

        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert "Traceback" not in stderr
    assert "threading shutdown" not in stderr
    assert "concurrent.futures" not in stderr
    if play:
        assert "ABANDON_DONE" in stdout
        assert "REPAIR" in stdout


def test_repeated_sigint_waits_for_play_cleanup_before_hard_exit(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _hung_tts_child(
                play=True,
                cooperative_delay_s=None,
                cleanup_delay_s=0.3,
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "READY"
        os.kill(proc.pid, signal.SIGINT)
        assert _read_ready(proc) == "ABANDON_START"

        # The repeat is recorded while cleanup owns the play ticket. It must
        # not terminate the process until abandonment and mute repair finish.
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert "ABANDON_DONE" in stdout
    assert "REPAIR" in stdout
    assert stderr == ""


def test_cli_scope_is_known_before_provider_finishes_during_unwind(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _hung_tts_child(
                play=True,
                cooperative_delay_s=0.05,
                cleanup_delay_s=0.3,
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "READY"
        os.kill(proc.pid, signal.SIGINT)
        assert _read_ready(proc) == "ABANDON_START"
        time.sleep(0.1)  # provider is done; CLI has not yet caught the interrupt

        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert "ABANDON_DONE" in stdout
    assert "REPAIR" in stdout
    assert stderr == ""


def test_repeated_sigint_deadline_exits_when_play_cleanup_stalls(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _hung_tts_child(
                play=True,
                cooperative_delay_s=None,
                cleanup_delay_s=10.0,
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "READY"
        os.kill(proc.pid, signal.SIGINT)
        assert _read_ready(proc) == "ABANDON_START"

        started = time.monotonic()
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert elapsed < 1.5
    assert "ABANDON_DONE" not in stdout
    assert "REPAIR" not in stdout
    assert stderr == ""


@pytest.mark.parametrize("play", [False, True])
def test_single_sigint_allows_cooperative_synth_cleanup(tmp_path, play):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _hung_tts_child(play=play, cooperative_delay_s=0.15),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "READY"
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stderr == ""
    if play:
        assert "ABANDON_DONE" in stdout
        assert "REPAIR" in stdout
    else:
        assert stdout == ""


def test_long_lived_caller_delegates_next_sigint_after_synth_finishes(tmp_path):
    child = textwrap.dedent(
        """
        import time
        from types import SimpleNamespace

        import hark.speech as speech
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        class Synth:
            def synthesize(self, text, *, voice):
                print("READY", flush=True)
                time.sleep(0.15)
                return SimpleNamespace(
                    audio=b"audio",
                    provider="fake",
                    content_type="audio/mpeg",
                    voice=voice,
                )

        speech.UsageStore = Store
        speech.resolve_tts = lambda *args, **kwargs: Synth()
        speech._synth_transport_factory = speech._in_process_synth_transport_factory
        try:
            speech.run_tts(HarkConfig(), "hello", play=False, use_cache=False)
        except speech.TtsSynthesisInterrupted:
            print("FIRST_CAUGHT", flush=True)

        time.sleep(0.3)
        print("READY_AGAIN", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("SECOND_DELEGATED", flush=True)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "READY"
        os.kill(proc.pid, signal.SIGINT)
        assert _read_ready(proc) == "FIRST_CAUGHT"
        assert _read_ready(proc) == "READY_AGAIN"

        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 0
    assert "SECOND_DELEGATED" in stdout
    assert stderr == ""


def test_pre_submit_interrupt_does_not_strand_false_running_state(tmp_path):
    child = textwrap.dedent(
        """
        import os
        import signal
        import time

        import hark.speech as speech
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        real_pthread_sigmask = signal.pthread_sigmask
        interrupt_once = True
        def interrupt_before_mask(how, mask):
            global interrupt_once
            if interrupt_once and how == signal.SIG_BLOCK:
                interrupt_once = False
                os.kill(os.getpid(), signal.SIGINT)
            return real_pthread_sigmask(how, mask)

        speech.UsageStore = Store
        signal.pthread_sigmask = interrupt_before_mask
        try:
            speech.run_tts(HarkConfig(), "hello", play=False, use_cache=False)
        except speech.TtsSynthesisInterrupted:
            print("FIRST_CAUGHT", flush=True)
        finally:
            signal.pthread_sigmask = real_pthread_sigmask

        time.sleep(0.05)
        print("READY_AGAIN", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("SECOND_DELEGATED", flush=True)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "FIRST_CAUGHT"
        assert _read_ready(proc) == "READY_AGAIN"

        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 0
    assert "SECOND_DELEGATED" in stdout
    assert stderr == ""


def test_successful_synth_restores_prior_sigint_handler(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import hark.speech as speech
    from hark.config import HarkConfig

    class Store:
        def record_tts(self, **kwargs):
            return None

    class Synth:
        def synthesize(self, text, *, voice):
            return SimpleNamespace(
                audio=b"audio",
                provider="fake",
                content_type="audio/mpeg",
                voice=voice,
            )

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(speech, "UsageStore", Store)
    monkeypatch.setattr(speech, "resolve_tts", lambda *args, **kwargs: Synth())
    monkeypatch.setattr(
        speech,
        "_synth_transport_factory",
        speech._in_process_synth_transport_factory,
    )
    previous = signal.getsignal(signal.SIGINT)

    result = speech.run_tts(HarkConfig(), "hello", play=False, use_cache=False)

    assert result["ok"] is True
    assert signal.getsignal(signal.SIGINT) is previous


def test_cli_reraises_unrelated_keyboard_interrupt(monkeypatch):
    import hark.cli as cli
    from hark.config import HarkConfig

    def unrelated_interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: HarkConfig())
    monkeypatch.setattr(cli, "dispatch", unrelated_interrupt)

    with pytest.raises(KeyboardInterrupt):
        cli.main(["providers"])

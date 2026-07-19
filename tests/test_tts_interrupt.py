"""B153: bounded repeated-SIGINT escape from non-cooperative TTS synth."""

from __future__ import annotations

import ctypes
import io
import errno
import json
import os
import selectors
import signal
import struct
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _linux_pidfd_open(pid: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.syscall(434, pid, 0)  # __NR_pidfd_open
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(fd)


def _linux_pidfd_send_signal(pidfd: int, signum: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(424, pidfd, signum, 0, 0)  # __NR_pidfd_send_signal
    if result < 0:
        err = ctypes.get_errno()
        if err == errno.ESRCH:
            raise ProcessLookupError(err, os.strerror(err))
        raise OSError(err, os.strerror(err))


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


def _rapid_gil_holding_tts_child() -> str:
    return textwrap.dedent(
        """
        import signal
        import sys

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        real_publish = speech._InterruptibleSynthPool.publish_synth_process_pidfd
        def visible_publish(self, process, pidfd):
            published = real_publish(self, process, pidfd)
            if published:
                print(f"SUPERVISOR {process.pid}", flush=True)
            return published

        real_send = isolation.SynthProcessLifecycle._send
        term_visible = False
        def visible_send(self, authority, signum):
            global term_visible
            if signum == signal.SIGTERM and not term_visible:
                term_visible = True
                print("SUPERVISOR_TERM", flush=True)
            return real_send(self, authority, signum)

        speech.UsageStore = Store
        speech._InterruptibleSynthPool.publish_synth_process_pidfd = visible_publish
        isolation.SynthProcessLifecycle._send = visible_send
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
            "rapid hung in C",
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


def _pre_main_tts_child(site_dir: Path, *, block_pidfd_open: bool) -> str:
    """Run a worker whose interpreter hangs before hark.tts_worker.main."""
    pidfd_patch = ""
    if block_pidfd_open:
        pidfd_patch = textwrap.dedent(
            """
            import threading
            real_pidfd_open = isolation.os.pidfd_open
            def blocked_pidfd_open(pid, flags=0):
                print(f"PIDFD_GAP {pid}", flush=True)
                threading.Event().wait()
                return real_pidfd_open(pid, flags)
            isolation.os.pidfd_open = blocked_pidfd_open
            """
        )
    else:
        pidfd_patch = textwrap.dedent(
            """
            real_publish = speech._InterruptibleSynthPool.publish_synth_process_pidfd
            def visible_publish(self, process, pidfd):
                published = real_publish(self, process, pidfd)
                if published:
                    print(f"PIDFD_PUBLISHED {process.pid}", flush=True)
                return published
            speech._InterruptibleSynthPool.publish_synth_process_pidfd = visible_publish
            """
        )
    return textwrap.dedent(
        f"""
        import os
        import sys
        from pathlib import Path

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        site_dir = Path({str(site_dir)!r})
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text(
            "import os, time\\n"
            "print(f'PRE_MAIN {{os.getpid()}}', flush=True)\\n"
            "time.sleep(30)\\n",
            encoding="utf-8",
        )
        os.environ["PYTHONPATH"] = str(site_dir)

        class Store:
            def record_tts(self, **kwargs):
                return None

        speech.UsageStore = Store
        {textwrap.indent(pidfd_patch, "        ").strip()}
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded,
            "pre-main hang",
            play=False,
            use_cache=False,
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _pending_group_sigint_child(site_dir: Path, release_path: Path) -> str:
    return textwrap.dedent(
        f"""
        import os
        import sys
        from pathlib import Path

        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        site_dir = Path({str(site_dir)!r})
        release_path = Path({str(release_path)!r})
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text(
            "import os, pathlib, time\\n"
            "release = pathlib.Path({str(release_path)!r})\\n"
            "if not os.environ.get('HARK_TTS_PAYLOAD'):\\n"
            " print(f'PRE_MAIN {{os.getpid()}}', flush=True)\\n"
            " while not release.exists(): time.sleep(0.01)\\n",
            encoding="utf-8",
        )
        os.environ["PYTHONPATH"] = str(site_dir)

        class Store:
            def record_tts(self, **kwargs):
                return None

        real_publish = speech._InterruptibleSynthPool.publish_synth_process_pidfd
        def visible_publish(self, process, pidfd):
            published = real_publish(self, process, pidfd)
            if published:
                print(f"PIDFD_PUBLISHED {{process.pid}}", flush=True)
            return published

        speech.UsageStore = Store
        speech._InterruptibleSynthPool.publish_synth_process_pidfd = visible_publish
        speech._synth_worker_command_factory = lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-success",
        ]
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded,
            "pending group signal",
            play=False,
            use_cache=False,
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _detached_tree_tts_child(site_dir: Path) -> str:
    detached_helper = textwrap.dedent(
        """
        import os
        import signal
        import time
        if os.fork() > 0:
            os._exit(0)
        os.setsid()
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print(f"DETACHED {os.getpid()}", flush=True)
        while True:
            time.sleep(1)
        """
    )
    sitecustomize = textwrap.dedent(
        f"""
        import os
        import subprocess
        import sys
        import threading
        from types import SimpleNamespace
        import hark.providers.resolve as provider_resolve

        class DetachedProvider:
            def synthesize(self, text, *, voice):
                intermediate = subprocess.Popen(
                    [sys.executable, "-c", {detached_helper!r}]
                )
                intermediate.wait(timeout=2.0)
                print(f"PROVIDER_HUNG {{os.getpid()}}", flush=True)
                threading.Event().wait()
                return SimpleNamespace(
                    audio=b"never",
                    provider="detached-test",
                    content_type="audio/mpeg",
                    voice=voice,
                )

        provider_resolve.resolve_tts = lambda *args, **kwargs: DetachedProvider()
        """
    )
    return textwrap.dedent(
        f"""
        import os
        import sys
        from pathlib import Path

        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        site_dir = Path({str(site_dir)!r})
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text(
            {sitecustomize!r},
            encoding="utf-8",
        )
        os.environ["PYTHONPATH"] = str(site_dir)

        class Store:
            def record_tts(self, **kwargs):
                return None

        real_publish = speech._InterruptibleSynthPool.publish_synth_process_pidfd
        def visible_publish(self, process, pidfd):
            published = real_publish(self, process, pidfd)
            if published:
                print(f"PIDFD_PUBLISHED {{process.pid}}", flush=True)
            return published

        speech.UsageStore = Store
        speech._InterruptibleSynthPool.publish_synth_process_pidfd = visible_publish
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded,
            "detached provider",
            play=False,
            use_cache=False,
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _library_hung_tts_child() -> str:
    return textwrap.dedent(
        """
        import os
        import sys
        import time

        import hark.speech as speech
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        real_publish = speech._InterruptibleSynthPool.publish_synth_process_pidfd
        def visible_publish(self, process, pidfd):
            published = real_publish(self, process, pidfd)
            if published:
                print(f"SUPERVISOR {process.pid}", flush=True)
            return published

        speech.UsageStore = Store
        speech._InterruptibleSynthPool.publish_synth_process_pidfd = visible_publish
        speech._synth_worker_command_factory = lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-gil-hang",
        ]
        try:
            speech.run_tts(HarkConfig(), "library hang", play=False, use_cache=False)
        except speech.TtsSynthesisInterrupted:
            print("FIRST_CAUGHT", flush=True)

        time.sleep(0.05)
        print("READY_SECOND", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("SECOND_CAUGHT", flush=True)
        print("HOST_ALIVE", flush=True)
        """
    )


def _library_preclaim_race_child() -> str:
    return textwrap.dedent(
        """
        import os
        import threading
        import time

        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        release = threading.Event()
        worker_started = threading.Event()
        real_spawn = isolation.SynthProcessLifecycle.spawn
        real_popen_init = isolation._SYNTH_POPEN_INIT

        def blocked_spawn(self, process, command, **kwargs):
            print("PRECLAIM_ENTER", flush=True)
            release.wait()
            return real_spawn(self, process, command, **kwargs)

        def visible_popen(self, *args, **kwargs):
            command = args[0] if args else kwargs.get("args", ())
            if "hark.tts_worker" in command:
                worker_started.set()
                print("WORKER_STARTED", flush=True)
            return real_popen_init(self, *args, **kwargs)

        speech.UsageStore = Store
        isolation.SynthProcessLifecycle.spawn = blocked_spawn
        isolation._SYNTH_POPEN_INIT = visible_popen
        try:
            speech.run_tts(HarkConfig(), "race", play=False, use_cache=False)
        except speech.TtsSynthesisInterrupted:
            print("FIRST_CAUGHT", flush=True)

        print("READY_SECOND", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("SECOND_CAUGHT", flush=True)

        release.set()
        time.sleep(0.2)
        if worker_started.is_set():
            raise AssertionError("worker spawned after terminalization")
        print("NO_WORKER_STARTED", flush=True)
        print("HOST_ALIVE", flush=True)
        """
    )


def _terminalization_window_child(site_dir: Path, stage: str) -> str:
    return textwrap.dedent(
        f"""
        import sys
        import threading
        from pathlib import Path

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        site_dir = Path({str(site_dir)!r})
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text(
            "import os, time\\n"
            "print(f'WORKER_STARTED {{os.getpid()}}', flush=True)\\n"
            "time.sleep(30)\\n",
            encoding="utf-8",
        )
        import os
        os.environ["PYTHONPATH"] = str(site_dir)

        stage = {stage!r}
        real_wait = isolation.SynthProcessLifecycle._wait_direct_child
        reached = set()

        def staged_wait(authority, timeout):
            level = authority.termination_level
            if stage == "term_wait" and level == 1 and stage not in reached:
                reached.add(stage)
                print("TERM_WAIT", flush=True)
                threading.Event().wait()
            return real_wait(authority, timeout)

        class Store:
            def record_tts(self, **kwargs):
                return None

        isolation.SynthProcessLifecycle._wait_direct_child = staticmethod(staged_wait)
        speech.UsageStore = Store
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "terminalization", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _post_real_popen_gap_child() -> str:
    return textwrap.dedent(
        """
        import sys
        import threading

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        real_init = isolation._SYNTH_POPEN_INIT
        def init_then_stall(self, *args, **kwargs):
            real_init(self, *args, **kwargs)
            command = args[0] if args else kwargs.get("args", ())
            if "hark.tts_worker" in command:
                print(f"POST_REAL_POPEN {self.pid}", flush=True)
                threading.Event().wait()

        class Store:
            def record_tts(self, **kwargs):
                return None

        isolation._SYNTH_POPEN_INIT = init_then_stall
        speech.UsageStore = Store
        def worker_command():
            command = isolation.synth_worker_command()
            command.append("--test-hang")
            return command

        speech._synth_worker_command_factory = worker_command
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "post popen gap", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _pre_real_popen_gap_child(release_file: Path) -> str:
    return textwrap.dedent(
        f"""
        import os
        import sys
        import time

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        real_init = isolation._SYNTH_POPEN_INIT
        def stall_before_init(self, *args, **kwargs):
            command = args[0] if args else kwargs.get("args", ())
            if "hark.tts_worker" in command:
                print("BEFORE_REAL_POPEN", flush=True)
                while not os.path.exists({str(release_file)!r}):
                    time.sleep(0.01)
            return real_init(self, *args, **kwargs)

        class Store:
            def record_tts(self, **kwargs):
                return None

        isolation._SYNTH_POPEN_INIT = stall_before_init
        speech.UsageStore = Store
        def worker_command():
            command = isolation.synth_worker_command()
            command.append("--test-hang")
            return command

        speech._synth_worker_command_factory = worker_command
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "pre popen gap", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _fork_without_pid_publication_child() -> str:
    return textwrap.dedent(
        """
        import sys
        import threading

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        real_init = isolation._SYNTH_POPEN_INIT
        hidden_processes = []
        def fork_then_hide_pid(self, *args, **kwargs):
            command = args[0] if args else kwargs.get("args", ())
            if "hark.tts_worker" in command:
                hidden = isolation.subprocess.Popen.__new__(isolation.subprocess.Popen)
                real_init(hidden, *args, **kwargs)
                hidden_processes.append(hidden)
                print(f"FORK_WITHOUT_PID {hidden.pid}", flush=True)
                threading.Event().wait()
            return real_init(self, *args, **kwargs)

        class Store:
            def record_tts(self, **kwargs):
                return None

        isolation._SYNTH_POPEN_INIT = fork_then_hide_pid
        speech.UsageStore = Store
        def worker_command():
            command = isolation.synth_worker_command()
            command.append("--test-hang")
            return command

        speech._synth_worker_command_factory = worker_command
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "fork gap", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _deceptive_worker_argv_gap_child(release_file: Path) -> str:
    return textwrap.dedent(
        f"""
        import os
        import signal
        import subprocess
        import time

        import hark.tts_isolation as isolation

        lifecycle = isolation.SynthProcessLifecycle()
        real_init = isolation._SYNTH_POPEN_INIT
        release_file = {str(release_file)!r}

        def fork_then_hide_pid(self, *args, **kwargs):
            hidden = isolation.subprocess.Popen.__new__(isolation.subprocess.Popen)
            real_init(hidden, *args, **kwargs)
            print(f"DECEPTIVE_CHILD {{hidden.pid}}", flush=True)
            try:
                while not os.path.exists(release_file):
                    time.sleep(0.01)
            finally:
                if hidden.poll() is None:
                    hidden.terminate()
                hidden.wait(timeout=1.0)
            raise RuntimeError("released deceptive child")

        def handle_sigint(signum, frame):
            safe = lifecycle.cancel()
            print(f"CANCEL_SAFE {{int(safe)}}", flush=True)
            if safe:
                os._exit(130)

        isolation._SYNTH_POPEN_INIT = fork_then_hide_pid
        signal.signal(signal.SIGINT, handle_sigint)
        process = subprocess.Popen.__new__(subprocess.Popen)
        try:
            lifecycle.spawn(
                process,
                ["/bin/sh", "-c", "exec sleep 30", "-m", "hark.tts_worker"],
            )
        except RuntimeError as exc:
            assert str(exc) == "released deceptive child"
        finally:
            isolation._SYNTH_POPEN_INIT = real_init
        print("DECEPTIVE_CLEAN", flush=True)
        """
    )


def _canonical_pre_main_unknown_pid_subreaper_child(
    site_dir: Path,
    publish_release: Path,
) -> str:
    hidden_file = site_dir / "hidden-pid"
    pre_main_file = site_dir / "pre-main-pid"
    target = textwrap.dedent(
        f"""
        import sys
        import time
        from pathlib import Path

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        hidden_file = Path({str(hidden_file)!r})
        publish_release = Path({str(publish_release)!r})
        real_init = isolation._SYNTH_POPEN_INIT

        def fork_then_publish_late(self, *args, **kwargs):
            command = args[0] if args else kwargs.get("args", ())
            if command[:3] == [sys.executable, "-m", "hark.tts_worker"]:
                hidden = isolation.subprocess.Popen.__new__(
                    isolation.subprocess.Popen
                )
                real_init(hidden, *args, **kwargs)
                hidden_file.write_text(str(hidden.pid), encoding="utf-8")
                while not publish_release.exists():
                    time.sleep(0.01)
                self.__dict__.update(hidden.__dict__)
                return None
            return real_init(self, *args, **kwargs)

        class Store:
            def record_tts(self, **kwargs):
                return None

        isolation._SYNTH_POPEN_INIT = fork_then_publish_late
        speech.UsageStore = Store
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded,
            "canonical pre-main unknown pid",
            play=False,
            use_cache=False,
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )
    return textwrap.dedent(
        f"""
        import ctypes
        import os
        import signal
        import subprocess
        import sys
        import time
        from pathlib import Path

        site_dir = Path({str(site_dir)!r})
        publish_release = Path({str(publish_release)!r})
        hidden_file = Path({str(hidden_file)!r})
        pre_main_file = Path({str(pre_main_file)!r})
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text(
            "import os, time\\n"
            "from pathlib import Path\\n"
            "if os.environ.get('HARK_TTS_RESULT_FD'):\\n"
            "    Path(os.environ['HARK_PREMAIN_FILE']).write_text("
            "str(os.getpid()), encoding='utf-8')\\n"
            "    time.sleep(30)\\n",
            encoding="utf-8",
        )

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER failed")

        env = os.environ.copy()
        current_path = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(site_dir) + (
            os.pathsep + current_path if current_path else ""
        )
        env["HARK_PREMAIN_FILE"] = str(pre_main_file)
        target = subprocess.Popen(
            [sys.executable, "-c", {target!r}],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        hidden_pid = None
        try:
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if hidden_file.exists() and pre_main_file.exists():
                    hidden_pid = int(hidden_file.read_text(encoding="utf-8"))
                    assert int(pre_main_file.read_text(encoding="utf-8")) == hidden_pid
                    break
                time.sleep(0.01)
            assert hidden_pid is not None, "canonical worker did not reach pre-main gap"

            os.kill(target.pid, signal.SIGINT)
            time.sleep(0.05)
            os.kill(target.pid, signal.SIGINT)
            time.sleep(0.2)
            assert target.poll() is None, (
                "unknown-PID child incorrectly authorized parent hard exit"
            )

            publish_release.touch()
            stdout, stderr = target.communicate(timeout=3.0)
            assert target.returncode == 130
            assert stdout == ""
            assert stderr == ""
            try:
                os.kill(hidden_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("pre-main canonical worker survived cleanup")
            print("CANONICAL_PRE_MAIN_FAIL_CLOSED", flush=True)
        finally:
            publish_release.touch(exist_ok=True)
            if target.poll() is None:
                target.kill()
                target.wait(timeout=1.0)
            if hidden_pid is not None:
                try:
                    os.kill(hidden_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(hidden_pid, 0)
                except ChildProcessError:
                    pass
        """
    )


def _portable_supervisor_atexit_child() -> str:
    worker = textwrap.dedent(
        """
        import atexit
        import os
        import threading
        import hark.tts_worker as worker

        def hang_at_exit():
            print(f"SUPERVISOR_ATEXIT {os.getpid()}", flush=True)
            threading.Event().wait()

        atexit.register(hang_at_exit)
        worker._write_result(
            {
                "status": "ok",
                "audio": b"test-audio",
                "provider": "test-worker",
                "content_type": "audio/mpeg",
                "voice": "test-voice",
            }
        )
        """
    )
    return textwrap.dedent(
        f"""
        import sys

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        real_unregister = speech._InterruptibleSynthPool.unregister_synth_process
        real_wait = speech._InterruptibleSynthPool.wait_and_unregister_synth_process
        def visible_unregister(self, process):
            print("AUTH_RELEASED", flush=True)
            return real_unregister(self, process)

        def visible_wait(self, process):
            print(f"WAIT_ACTIVE {{int(self._process_lifecycle.active)}}", flush=True)
            return real_wait(self, process)

        isolation.SubprocessSynthTransport._open_pidfd = staticmethod(
            lambda process: None
        )
        speech.UsageStore = Store
        speech._InterruptibleSynthPool.unregister_synth_process = visible_unregister
        speech._InterruptibleSynthPool.wait_and_unregister_synth_process = visible_wait
        speech._synth_worker_command_factory = lambda: [
            sys.executable, "-c", {worker!r}
        ]
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "atexit hang", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _hostile_cleanup_tts_child(hostile: str) -> str:
    return textwrap.dedent(
        f"""
        import contextlib
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
                threading.Event().wait()
                return SimpleNamespace(
                    audio=b"never",
                    provider="fake",
                    content_type="audio/mpeg",
                    voice=voice,
                )

        class Hold:
            skipped = False
            def as_meta(self):
                return {{}}

        def abandon(*args, **kwargs):
            print("ABANDON_{hostile.upper()}", flush=True)
            if {hostile!r} == "abandon":
                raise SystemExit("hostile abandon")

        def repair(**kwargs):
            time.sleep(0.05)
            print("REPAIR_{hostile.upper()}", flush=True)
            if {hostile!r} == "mute":
                raise SystemExit("hostile mute repair")
            return {{"repaired": False}}

        speech.UsageStore = Store
        speech.resolve_tts = lambda *args, **kwargs: Synth()
        speech._synth_transport_factory = speech._in_process_synth_transport_factory
        speech.lookup_cached_tts = lambda *args, **kwargs: None
        conference.apply_conference_hold = lambda *args, **kwargs: Hold()
        speech.claim_tts_play_ticket = lambda: object()
        speech.abandon_tts_play_ticket = abandon
        speech.repair_tts_mute_after_play = repair
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded,
            "hostile cleanup",
            play=True,
            use_cache=False,
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _portable_cancel_tts_child() -> str:
    worker = textwrap.dedent(
        """
        import hark.tts_worker as worker
        raise SystemExit(worker.main(["--test-gil-hang"]))
        """
    )
    return textwrap.dedent(
        f"""
        import sys

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        worker = {worker!r}
        real_publish = speech._InterruptibleSynthPool.publish_synth_process_pidfd
        def visible_publish(self, process, pidfd):
            published = real_publish(self, process, pidfd)
            if published:
                print(f"SUPERVISOR {{process.pid}}", flush=True)
            return published

        isolation.SubprocessSynthTransport._open_pidfd = staticmethod(
            lambda process: None
        )
        speech.UsageStore = Store
        speech._InterruptibleSynthPool.publish_synth_process_pidfd = visible_publish
        speech._synth_worker_command_factory = lambda: [
            sys.executable, "-c", worker
        ]
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "portable cancel", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _portable_detached_pipe_child(site_dir: Path) -> str:
    detached_helper = textwrap.dedent(
        """
        import os
        import signal
        import time
        if os.fork() > 0:
            os._exit(0)
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print(f"PORTABLE_DETACHED {os.getpid()}", flush=True)
        while True:
            time.sleep(1)
        """
    )
    sitecustomize = textwrap.dedent(
        f"""
        import os
        import subprocess
        import sys
        import threading
        from types import SimpleNamespace
        import hark.providers.resolve as provider_resolve

        class DetachedProvider:
            def synthesize(self, text, *, voice):
                intermediate = subprocess.Popen(
                    [sys.executable, "-c", {detached_helper!r}]
                )
                intermediate.wait(timeout=2.0)
                print(f"PORTABLE_PAYLOAD {{os.getpid()}}", flush=True)
                threading.Event().wait()
                return SimpleNamespace(
                    audio=b"never",
                    provider="portable-detached",
                    content_type="audio/mpeg",
                    voice=voice,
                )

        provider_resolve.resolve_tts = lambda *args, **kwargs: DetachedProvider()
        """
    )
    worker = textwrap.dedent(
        """
        import hark.tts_worker as worker
        raise SystemExit(worker.main())
        """
    )
    return textwrap.dedent(
        f"""
        import os
        import sys
        from pathlib import Path

        import hark.cli as cli
        import hark.speech as speech
        import hark.tts_isolation as isolation
        from hark.config import HarkConfig

        site_dir = Path({str(site_dir)!r})
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text({sitecustomize!r}, encoding="utf-8")
        os.environ["PYTHONPATH"] = str(site_dir)

        class Store:
            def record_tts(self, **kwargs):
                return None

        isolation.SubprocessSynthTransport._open_pidfd = staticmethod(
            lambda process: None
        )
        speech.UsageStore = Store
        speech._synth_worker_command_factory = lambda: [
            sys.executable, "-c", {worker!r}
        ]
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "portable detached", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _saturated_output_tts_child(
    site_dir: Path,
    supervisor_file: Path,
    payload_file: Path,
) -> str:
    sitecustomize = textwrap.dedent(
        f"""
        import os
        import threading
        from pathlib import Path
        import hark.providers.resolve as provider_resolve

        class FloodProvider:
            def synthesize(self, text, *, voice):
                Path({str(payload_file)!r}).write_text(
                    str(os.getpid()), encoding="utf-8"
                )
                chunk = b"x" * (64 * 1024)
                def flood(fd):
                    while True:
                        os.write(fd, chunk)
                threading.Thread(target=flood, args=(1,), daemon=True).start()
                threading.Thread(target=flood, args=(2,), daemon=True).start()
                threading.Event().wait()

        provider_resolve.resolve_tts = lambda *args, **kwargs: FloodProvider()
        """
    )
    return textwrap.dedent(
        f"""
        import os
        import sys
        from pathlib import Path

        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        site_dir = Path({str(site_dir)!r})
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text(
            {sitecustomize!r}, encoding="utf-8"
        )
        os.environ["PYTHONPATH"] = str(site_dir)

        class Store:
            def record_tts(self, **kwargs):
                return None

        real_publish = speech._InterruptibleSynthPool.publish_synth_process_pidfd
        def visible_publish(self, process, pidfd):
            published = real_publish(self, process, pidfd)
            if published:
                Path({str(supervisor_file)!r}).write_text(
                    str(process.pid), encoding="utf-8"
                )
            return published

        speech.UsageStore = Store
        speech._InterruptibleSynthPool.publish_synth_process_pidfd = visible_publish
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "saturated output", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )


def _read_markers(proc: subprocess.Popen[str], expected: set[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    deadline = time.monotonic() + 3.0
    while set(found) != expected:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"missing markers: {expected - set(found)}"
        marker, pid_text = _read_ready(proc, remaining).split()
        if marker in expected:
            found[marker] = int(pid_text)
    return found


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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd regression")
def test_gil_holding_provider_has_os_independent_repeated_exit(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", _gil_holding_tts_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    provider_pidfd = -1
    try:
        ready, provider_pid_text = _read_ready(proc).split()
        assert ready == "GIL_READY"
        provider_pid = int(provider_pid_text)
        provider_pidfd = _linux_pidfd_open(provider_pid)

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
    assert provider_pidfd >= 0
    try:
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(provider_pidfd, 0)
    finally:
        os.close(provider_pidfd)


@pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine not in {"x86_64", "aarch64"},
    reason="Linux pidfd rapid-repeat regression",
)
@pytest.mark.parametrize("third_delay_s", [0.0, 0.001, 0.005])
def test_rapid_third_sigint_waits_for_provider_tree_and_pipe_cleanup(
    tmp_path,
    third_delay_s,
):
    proc = subprocess.Popen(
        [sys.executable, "-c", _rapid_gil_holding_tts_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    supervisor_pidfd = None
    payload_pidfd = None
    try:
        markers = _read_markers(proc, {"SUPERVISOR", "GIL_READY"})
        supervisor_pidfd = _linux_pidfd_open(markers["SUPERVISOR"])
        payload_pidfd = _linux_pidfd_open(markers["GIL_READY"])

        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.05)
        os.kill(proc.pid, signal.SIGINT)
        assert _read_ready(proc) == "SUPERVISOR_TERM"
        time.sleep(third_delay_s)
        os.kill(proc.pid, signal.SIGINT)

        # communicate returning proves every inherited parent-facing pipe has
        # reached EOF; pidfds below prove both cleanup supervisor and provider
        # payload are gone without relying on reusable numeric PID lookups.
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)
        for pidfd in (payload_pidfd, supervisor_pidfd):
            if pidfd is None:
                continue
            try:
                _linux_pidfd_send_signal(pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""
    assert supervisor_pidfd is not None
    assert payload_pidfd is not None
    try:
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(supervisor_pidfd, 0)
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(payload_pidfd, 0)
    finally:
        os.close(payload_pidfd)
        os.close(supervisor_pidfd)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_repeated_sigint_kills_worker_hung_before_python_main_and_closes_pipes(
    tmp_path,
):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _pre_main_tts_child(tmp_path / "premain-site", block_pidfd_open=False),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    worker_pidfd = -1
    try:
        markers = _read_markers(proc, {"PRE_MAIN", "PIDFD_PUBLISHED"})
        worker_pid = markers["PRE_MAIN"]
        assert markers["PIDFD_PUBLISHED"] == worker_pid
        worker_pidfd = _linux_pidfd_open(worker_pid)

        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.15)
        assert proc.poll() is None
        os.kill(proc.pid, signal.SIGINT)
        # communicate returning is the inherited stdout/stderr pipe EOF proof.
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""
    assert worker_pidfd >= 0
    try:
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(worker_pidfd, 0)
    finally:
        os.close(worker_pidfd)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_repeated_sigint_kills_unreaped_child_in_popen_to_pidfd_gap(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _pre_main_tts_child(tmp_path / "gap-site", block_pidfd_open=True),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    worker_pidfd = -1
    try:
        markers = _read_markers(proc, {"PRE_MAIN", "PIDFD_GAP"})
        worker_pid = markers["PRE_MAIN"]
        assert markers["PIDFD_GAP"] == worker_pid
        worker_pidfd = _linux_pidfd_open(worker_pid)

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
    assert stderr == ""
    assert worker_pidfd >= 0
    try:
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(worker_pidfd, 0)
    finally:
        os.close(worker_pidfd)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_process_group_sigint_during_startup_has_no_worker_traceback(tmp_path):
    release_path = tmp_path / "release-worker"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _pending_group_sigint_child(tmp_path / "group-site", release_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
        start_new_session=True,
    )
    worker_pidfd = -1
    try:
        markers = _read_markers(proc, {"PRE_MAIN", "PIDFD_PUBLISHED"})
        worker_pid = markers["PRE_MAIN"]
        assert markers["PIDFD_PUBLISHED"] == worker_pid
        worker_pidfd = _linux_pidfd_open(worker_pid)

        os.killpg(proc.pid, signal.SIGINT)
        release_path.touch()
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        release_path.touch(exist_ok=True)
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""
    assert worker_pidfd >= 0
    try:
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(worker_pidfd, 0)
    finally:
        os.close(worker_pidfd)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper regression")
def test_repeated_sigint_reaps_setsid_double_fork_provider_tree_and_pipes(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _detached_tree_tts_child(tmp_path / "tree-site"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    process_pidfds: list[int] = []
    try:
        markers = _read_markers(
            proc,
            {"PIDFD_PUBLISHED", "PROVIDER_HUNG", "DETACHED"},
        )
        supervisor_pid = markers["PIDFD_PUBLISHED"]
        payload_pid = markers["PROVIDER_HUNG"]
        detached_pid = markers["DETACHED"]
        process_pidfds = [
            _linux_pidfd_open(pid)
            for pid in (supervisor_pid, payload_pid, detached_pid)
        ]

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
    assert stderr == ""
    assert len(process_pidfds) == 3
    try:
        for pidfd in process_pidfds:
            with pytest.raises(ProcessLookupError):
                _linux_pidfd_send_signal(pidfd, 0)
    finally:
        for pidfd in process_pidfds:
            os.close(pidfd)


def test_cli_sigint_before_pool_handler_install_is_typed_and_traceback_free(tmp_path):
    child = textwrap.dedent(
        """
        import threading
        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        real_enter = speech._InterruptibleSynthPool.__enter__
        def blocked_enter(self):
            print("BEFORE_POOL_INSTALL", flush=True)
            threading.Event().wait()
            return real_enter(self)

        speech._InterruptibleSynthPool.__enter__ = blocked_enter
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "transition", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
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
        assert _read_ready(proc) == "BEFORE_POOL_INSTALL"
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""


def test_cli_pending_sigint_during_boundary_install_is_typed_and_traceback_free(
    tmp_path,
):
    child = textwrap.dedent(
        """
        import os
        import signal
        import hark.cli as cli

        real_mask = signal.pthread_sigmask
        injected = False
        def inject_after_block(how, mask):
            global injected
            previous = real_mask(how, mask)
            if (
                not injected
                and how == signal.SIG_BLOCK
                and signal.SIGINT in mask
            ):
                injected = True
                os.kill(os.getpid(), signal.SIGINT)
            return previous

        signal.pthread_sigmask = inject_after_block
        cli.dispatch = lambda args, cfg: 0
        raise SystemExit(cli.main(["providers"]))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 130
    assert proc.stdout == ""
    assert proc.stderr == ""


def test_pool_pending_sigint_during_handler_install_restores_publication_truth(
    tmp_path,
):
    child = textwrap.dedent(
        """
        import os
        import signal
        import time
        import hark.speech as speech

        previous_handler = signal.getsignal(signal.SIGINT)
        real_mask = signal.pthread_sigmask
        real_mask(signal.SIG_UNBLOCK, {signal.SIGINT})
        injected = False
        def inject_after_block(how, mask):
            global injected
            previous = real_mask(how, mask)
            if not injected and how == signal.SIG_BLOCK and signal.SIGINT in mask:
                injected = True
                os.kill(os.getpid(), signal.SIGINT)
            return previous

        pool = speech._InterruptibleSynthPool()
        signal.pthread_sigmask = inject_after_block
        caught = False
        try:
            pool.__enter__()
            # CPython may run the Python handler either inside the unmask call
            # or at the next bytecode checkpoint after it returns.
            time.sleep(0.05)
        except speech.TtsSynthesisInterrupted:
            caught = True
        finally:
            signal.pthread_sigmask = real_mask

        assert caught is True
        if pool._signal_installed:
            assert signal.getsignal(signal.SIGINT) is pool._handler
            pool._restore_handler()
        assert pool._signal_installed is False
        assert signal.getsignal(signal.SIGINT) is previous_handler
        current_mask = real_mask(signal.SIG_BLOCK, set())
        assert signal.SIGINT not in current_mask
        pool._pool.shutdown(wait=False, cancel_futures=True)
        print("POOL_PENDING_RECOVERED")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "POOL_PENDING_RECOVERED\n"
    assert proc.stderr == ""


def test_pool_handler_install_post_effect_raise_rolls_back_actual_handler(tmp_path):
    child = textwrap.dedent(
        """
        import signal
        import hark.speech as speech

        previous_handler = signal.getsignal(signal.SIGINT)
        real_signal = signal.signal
        injected = False
        pool = speech._InterruptibleSynthPool()
        def install_then_raise(signum, handler):
            global injected
            result = real_signal(signum, handler)
            if not injected and handler is pool._handler:
                injected = True
                raise MemoryError("post-install primary")
            return result

        signal.signal = install_then_raise
        try:
            pool.__enter__()
        except MemoryError as exc:
            assert str(exc) == "post-install primary"
        else:
            raise AssertionError("post-effect install injection did not fire")
        finally:
            signal.signal = real_signal

        assert pool._signal_installed is False
        assert signal.getsignal(signal.SIGINT) is previous_handler
        pool._pool.shutdown(wait=False, cancel_futures=True)
        print("POOL_INSTALL_ROLLED_BACK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "POOL_INSTALL_ROLLED_BACK\n"
    assert proc.stderr == ""


def test_pool_restore_post_effect_raise_preserves_first_handler_exception(tmp_path):
    child = textwrap.dedent(
        """
        import signal
        import hark.speech as speech

        original_handler = signal.getsignal(signal.SIGINT)
        def first_handler(signum, frame):
            raise LookupError("first handler primary")

        signal.signal(signal.SIGINT, first_handler)
        pool = speech._InterruptibleSynthPool()
        pool.__enter__()
        real_signal = signal.signal
        injected = False
        def restore_then_raise(signum, handler):
            global injected
            result = real_signal(signum, handler)
            if not injected and handler is first_handler:
                injected = True
                raise MemoryError("post-restore secondary")
            return result

        signal.signal = restore_then_raise
        try:
            pool._handle_sigint(signal.SIGINT, None)
        except LookupError as exc:
            assert str(exc) == "first handler primary"
        else:
            raise AssertionError("first handler exception was not preserved")
        finally:
            signal.signal = real_signal

        assert pool._signal_installed is False
        assert signal.getsignal(signal.SIGINT) is first_handler
        real_signal(signal.SIGINT, original_handler)
        pool._pool.shutdown(wait=False, cancel_futures=True)
        print("POOL_RESTORE_PRIMARY_PRESERVED")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "POOL_RESTORE_PRIMARY_PRESERVED\n"
    assert proc.stderr == ""


def test_cli_handler_install_post_effect_raise_rolls_back_actual_handler(tmp_path):
    child = textwrap.dedent(
        """
        import signal
        from hark.tts_interrupt_policy import _CliSigintController

        controller = _CliSigintController()
        previous_handler = signal.getsignal(signal.SIGINT)
        real_signal = signal.signal
        injected = False
        def install_then_raise(signum, handler):
            global injected
            result = real_signal(signum, handler)
            if not injected and handler is controller._handler:
                injected = True
                raise MemoryError("post-cli-install primary")
            return result

        signal.signal = install_then_raise
        try:
            controller.activate()
        except MemoryError as exc:
            assert str(exc) == "post-cli-install primary"
        else:
            raise AssertionError("post-effect CLI injection did not fire")
        finally:
            signal.signal = real_signal

        assert controller._active_depth == 0
        assert controller._installed is False
        assert signal.getsignal(signal.SIGINT) is previous_handler
        print("CLI_INSTALL_ROLLED_BACK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "CLI_INSTALL_ROLLED_BACK\n"
    assert proc.stderr == ""


def test_cli_scope_restores_previous_sigint_handler_on_normal_exit():
    import hark.tts_interrupt_policy as policy

    controller = policy._CliSigintController()
    previous = signal.getsignal(signal.SIGINT)

    try:
        controller.activate()
        assert signal.getsignal(signal.SIGINT) is controller._handler
        controller.deactivate()

        assert controller._active_depth == 0
        assert controller._installed is False
        assert signal.getsignal(signal.SIGINT) is previous
    finally:
        signal.signal(signal.SIGINT, previous)


def test_cli_scope_preserves_live_pool_handler_on_outer_exit():
    import hark.tts_interrupt_policy as policy

    controller = policy._CliSigintController()
    previous = signal.getsignal(signal.SIGINT)

    def pool_handler(signum, frame):
        del signum, frame

    try:
        controller.activate()
        signal.signal(signal.SIGINT, pool_handler)
        controller.deactivate()

        assert controller._active_depth == 0
        assert controller._installed is False
        assert signal.getsignal(signal.SIGINT) is pool_handler
    finally:
        signal.signal(signal.SIGINT, previous)


def test_cli_outer_reactivation_repairs_externally_reset_sigint_handler(tmp_path):
    child = textwrap.dedent(
        """
        import os
        import signal

        from hark.tts_interrupt_policy import (
            TtsSynthesisInterrupted,
            _CliSigintController,
        )

        controller = _CliSigintController()
        original_handler = signal.getsignal(signal.SIGINT)
        try:
            controller.activate()
            controller.deactivate()
            signal.signal(signal.SIGINT, signal.SIG_DFL)

            controller.activate()
            assert controller._installed is True
            assert signal.getsignal(signal.SIGINT) is controller._handler
            try:
                os.kill(os.getpid(), signal.SIGINT)
            except TtsSynthesisInterrupted:
                print("CLI_REACTIVATION_TYPED", flush=True)
            except KeyboardInterrupt:
                raise AssertionError("reactivation leaked raw KeyboardInterrupt")
            else:
                raise AssertionError("reactivated SIGINT was not delivered")
            finally:
                controller.deactivate()
        finally:
            signal.signal(signal.SIGINT, original_handler)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "CLI_REACTIVATION_TYPED\n"
    assert proc.stderr == ""


@pytest.mark.parametrize("external", ["default", "custom", "dfl"])
def test_cli_reused_controller_getsignal_interrupt_uses_only_live_handler(
    tmp_path,
    external,
):
    child = textwrap.dedent(
        f"""
        import os
        import signal

        import hark.tts_interrupt_policy as policy

        class ExternalInterrupt(KeyboardInterrupt):
            pass

        def external_handler(signum, frame):
            raise ExternalInterrupt

        controller = policy._CliSigintController()
        original_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        controller.activate()
        controller.deactivate()
        selected = {{
            "default": signal.default_int_handler,
            "custom": external_handler,
            "dfl": signal.SIG_DFL,
        }}[{external!r}]
        signal.signal(signal.SIGINT, selected)
        real_getsignal = signal.getsignal
        injected = False

        def interrupt_inside_getsignal(signum):
            global injected
            current = real_getsignal(signum)
            if not injected:
                injected = True
                os.kill(os.getpid(), signal.SIGINT)
                raise AssertionError("SIGINT was not delivered inside getsignal")
            return current

        signal.getsignal = interrupt_inside_getsignal
        try:
            try:
                controller.activate()
            except policy.TtsSynthesisInterrupted:
                raise AssertionError("pre-snapshot interrupt used cached state")
            except ExternalInterrupt:
                assert {external!r} == "custom"
            except KeyboardInterrupt:
                assert {external!r} == "default"
            else:
                raise AssertionError("getsignal SIGINT did not interrupt activation")
            assert controller._active_depth == 0
            assert controller._installed is False
            print("CLI_GETSIGNAL_{external.upper()}_LIVE", flush=True)
        finally:
            signal.getsignal = real_getsignal
            signal.signal(signal.SIGINT, original_handler)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    if external == "dfl":
        assert proc.returncode == -signal.SIGINT
        assert proc.stdout == ""
        assert proc.stderr == ""
    else:
        assert proc.returncode == 0
        assert proc.stdout == f"CLI_GETSIGNAL_{external.upper()}_LIVE\n"
        assert proc.stderr == ""


@pytest.mark.parametrize("external", ["default", "custom"])
def test_cli_pre_mask_real_sigint_uses_published_live_handler_truth(
    tmp_path,
    external,
):
    child = textwrap.dedent(
        f"""
        import os
        import signal

        import hark.tts_interrupt_policy as policy

        class ExternalInterrupt(KeyboardInterrupt):
            pass

        def external_handler(signum, frame):
            raise ExternalInterrupt

        controller = policy._CliSigintController()
        original_handler = signal.getsignal(signal.SIGINT)
        selected = (
            signal.default_int_handler
            if {external!r} == "default"
            else external_handler
        )
        signal.signal(signal.SIGINT, selected)
        real_acquire = policy.SigintMaskGuard.acquire

        def interrupt_before_mask():
            os.kill(os.getpid(), signal.SIGINT)
            raise AssertionError("SIGINT was not delivered before masking")

        policy.SigintMaskGuard.acquire = staticmethod(interrupt_before_mask)
        try:
            try:
                controller.activate()
            except policy.TtsSynthesisInterrupted:
                assert {external!r} == "default"
            except ExternalInterrupt:
                assert {external!r} == "custom"
            else:
                raise AssertionError("pre-mask SIGINT did not interrupt activation")

            assert controller._active_depth == 0
            assert controller._installed is False
            assert signal.getsignal(signal.SIGINT) is selected
            print("CLI_PRE_MASK_{external.upper()}_PRESERVED", flush=True)
        finally:
            policy.SigintMaskGuard.acquire = real_acquire
            signal.signal(signal.SIGINT, original_handler)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == f"CLI_PRE_MASK_{external.upper()}_PRESERVED\n"
    assert proc.stderr == ""


def test_cli_mask_post_effect_raise_restores_mask_and_controller_truth(tmp_path):
    child = textwrap.dedent(
        """
        import signal
        from hark.tts_interrupt_policy import _CliSigintController

        controller = _CliSigintController()
        previous_handler = signal.getsignal(signal.SIGINT)
        real_mask = signal.pthread_sigmask
        injected = False
        def block_then_raise(how, mask):
            global injected
            result = real_mask(how, mask)
            if not injected and how == signal.SIG_BLOCK and signal.SIGINT in mask:
                injected = True
                raise MemoryError("post-mask primary")
            return result

        signal.pthread_sigmask = block_then_raise
        try:
            controller.activate()
        except MemoryError as exc:
            assert str(exc) == "post-mask primary"
        else:
            raise AssertionError("mask injection did not fire")
        finally:
            signal.pthread_sigmask = real_mask

        current_mask = real_mask(signal.SIG_BLOCK, set())
        assert signal.SIGINT not in current_mask
        assert controller._active_depth == 0
        assert controller._installed is False
        assert signal.getsignal(signal.SIGINT) is previous_handler
        print("CLI_MASK_RECOVERED")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "CLI_MASK_RECOVERED\n"
    assert proc.stderr == ""


def test_pool_restore_mask_post_effect_raise_keeps_handler_truth(tmp_path):
    child = textwrap.dedent(
        """
        import signal
        import hark.speech as speech

        pool = speech._InterruptibleSynthPool()
        pool.__enter__()
        assert pool._signal_installed is True
        real_mask = signal.pthread_sigmask
        injected = False
        def block_then_raise(how, mask):
            global injected
            result = real_mask(how, mask)
            if not injected and how == signal.SIG_BLOCK and signal.SIGINT in mask:
                injected = True
                raise MemoryError("restore post-mask primary")
            return result

        signal.pthread_sigmask = block_then_raise
        try:
            pool._restore_handler()
        except MemoryError as exc:
            assert str(exc) == "restore post-mask primary"
        else:
            raise AssertionError("mask injection did not fire")
        finally:
            signal.pthread_sigmask = real_mask

        current_mask = real_mask(signal.SIG_BLOCK, set())
        assert signal.SIGINT not in current_mask
        assert pool._signal_installed is True
        assert signal.getsignal(signal.SIGINT) is pool._handler

        pool._restore_handler()
        assert pool._signal_installed is False
        pool._pool.shutdown(wait=False, cancel_futures=True)
        print("POOL_MASK_RECOVERED")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=2.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "POOL_MASK_RECOVERED\n"
    assert proc.stderr == ""


def test_cli_sigint_after_pool_handler_restore_is_typed_and_traceback_free(tmp_path):
    child = textwrap.dedent(
        """
        import threading
        import sys
        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        real_restore = speech._InterruptibleSynthPool._restore_handler
        def blocked_restore(self):
            real_restore(self)
            print("AFTER_POOL_RESTORE", flush=True)
            threading.Event().wait()

        speech.UsageStore = Store
        speech._InterruptibleSynthPool._restore_handler = blocked_restore
        speech._synth_worker_command_factory = lambda: [
            sys.executable, "-m", "hark.tts_worker", "--test-success"
        ]
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "transition", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
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
        assert _read_ready(proc) == "AFTER_POOL_RESTORE"
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""


def test_hung_library_caller_repeat_cancels_without_process_death(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", _library_hung_tts_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        markers = _read_markers(proc, {"SUPERVISOR", "GIL_READY"})
        supervisor_pid = markers["SUPERVISOR"]
        payload_pid = markers["GIL_READY"]
        os.kill(proc.pid, signal.SIGINT)
        assert _read_ready(proc) == "FIRST_CAUGHT"
        assert _read_ready(proc) == "READY_SECOND"

        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 0
    assert stdout == "SECOND_CAUGHT\nHOST_ALIVE\n"
    assert stderr == ""
    for pid in (supervisor_pid, payload_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_library_terminalization_rejects_preclaim_race_without_spawning(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", _library_preclaim_race_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "PRECLAIM_ENTER"
        os.kill(proc.pid, signal.SIGINT)
        assert _read_ready(proc) == "FIRST_CAUGHT"
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY_SECOND"
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 0
    assert stdout == "SECOND_CAUGHT\nNO_WORKER_STARTED\nHOST_ALIVE\n"
    assert stderr == ""


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_nested_sigint_resumes_term_wait_until_worker_reaped(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _terminalization_window_child(tmp_path / "term_wait", "term_wait"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        ready, worker_pid_text = _read_ready(proc).split()
        assert ready == "WORKER_STARTED"
        worker_pid = int(worker_pid_text)
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.1)
        os.kill(proc.pid, signal.SIGINT)
        assert _read_ready(proc, timeout_s=1.5) == "TERM_WAIT"
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)


def test_repeated_sigint_does_not_wait_on_post_real_popen_spawn_lock(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", _post_real_popen_gap_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        marker, worker_pid_text = _read_ready(proc).split()
        assert marker == "POST_REAL_POPEN"
        worker_pid = int(worker_pid_text)
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.1)
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""
    with pytest.raises(ProcessLookupError):
        os.kill(worker_pid, 0)


def test_repeated_sigint_fails_closed_before_real_popen_initialization(tmp_path):
    release_file = tmp_path / "release-pre-real-popen"
    proc = subprocess.Popen(
        [sys.executable, "-c", _pre_real_popen_gap_child(release_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "BEFORE_REAL_POPEN"
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.1)
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.2)
        assert proc.poll() is None
        release_file.touch()
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        release_file.touch(exist_ok=True)
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""


@pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine not in {"x86_64", "aarch64"},
    reason="Linux pidfd parent-death regression",
)
def test_exact_pre_main_worker_under_subreaper_fails_closed_without_pid(tmp_path):
    child = _canonical_pre_main_unknown_pid_subreaper_child(
        tmp_path / "canonical-pre-main-site",
        tmp_path / "publish-hidden-pid",
    )
    proc = subprocess.run(
        [sys.executable, "-c", child],
        capture_output=True,
        text=True,
        timeout=6.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "CANONICAL_PRE_MAIN_FAIL_CLOSED\n"
    assert proc.stderr == ""


@pytest.mark.skipif(
    sys.platform != "linux" or os.uname().machine not in {"x86_64", "aarch64"},
    reason="Linux pidfd deceptive-command regression",
)
def test_deceptive_worker_argv_cannot_authorize_unknown_pid_hard_exit(tmp_path):
    release_file = tmp_path / "release-deceptive-worker"
    proc = subprocess.Popen(
        [sys.executable, "-c", _deceptive_worker_argv_gap_child(release_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    hidden_pidfd = None
    completed = False
    try:
        marker, hidden_pid_text = _read_ready(proc).split()
        assert marker == "DECEPTIVE_CHILD"
        hidden_pidfd = _linux_pidfd_open(int(hidden_pid_text))

        for _ in range(2):
            os.kill(proc.pid, signal.SIGINT)
            assert _read_ready(proc) == "CANCEL_SAFE 0"
            assert proc.poll() is None

        release_file.touch()
        stdout, stderr = proc.communicate(timeout=2.0)
        completed = True
    finally:
        release_file.touch(exist_ok=True)
        if proc.poll() is None:
            _terminate(proc)
        if not completed and hidden_pidfd is not None:
            try:
                _linux_pidfd_send_signal(hidden_pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert proc.returncode == 0
    assert stdout == "DECEPTIVE_CLEAN\n"
    assert stderr == ""
    assert hidden_pidfd is not None
    try:
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(hidden_pidfd, 0)
    finally:
        os.close(hidden_pidfd)


def test_parent_without_pidfd_repeated_cancel_is_bounded_and_clean(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", _portable_cancel_tts_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        markers = _read_markers(proc, {"SUPERVISOR", "GIL_READY"})
        supervisor_pid = markers["SUPERVISOR"]
        payload_pid = markers["GIL_READY"]
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
    assert stderr == ""
    for pid in (supervisor_pid, payload_pid):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_portable_result_eof_retains_authority_through_supervisor_atexit(tmp_path):
    supervisor_pid = None
    proc = subprocess.Popen(
        [sys.executable, "-c", _portable_supervisor_atexit_child()],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        first = _read_ready(proc)
        assert proc.stdout is not None
        second = proc.stdout.readline().strip()
        markers = {
            marker: int(value)
            for marker, value in (line.split() for line in (first, second))
        }
        assert set(markers) == {"SUPERVISOR_ATEXIT", "WAIT_ACTIVE"}
        supervisor_pid = markers["SUPERVISOR_ATEXIT"]
        assert markers["WAIT_ACTIVE"] == 1
        assert proc.stdout is not None
        selector = selectors.DefaultSelector()
        try:
            selector.register(proc.stdout, selectors.EVENT_READ)
            assert not selector.select(0.1), "authority released at result EOF"
        finally:
            selector.close()

        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.1)
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            if supervisor_pid is not None:
                try:
                    os.kill(supervisor_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout in ("", "AUTH_RELEASED\n")
    assert stderr == ""
    with pytest.raises(ProcessLookupError):
        os.kill(supervisor_pid, 0)


def test_portable_detached_descendant_is_killed_and_cannot_hold_pipes(tmp_path):
    detached_pid = None
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _portable_detached_pipe_child(tmp_path / "portable-tree-site"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        markers = _read_markers(proc, {"PORTABLE_DETACHED", "PORTABLE_PAYLOAD"})
        detached_pid = markers["PORTABLE_DETACHED"]
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.1)
        started = time.monotonic()
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=1.5)
        elapsed = time.monotonic() - started

        assert proc.returncode == 130
        assert elapsed < 1.0
        assert stdout == ""
        assert stderr == ""

        with pytest.raises(ProcessLookupError):
            os.kill(detached_pid, 0)
    finally:
        if proc.poll() is None:
            _terminate(proc)
        if detached_pid is not None:
            try:
                os.kill(detached_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper regression")
def test_saturated_stdout_stderr_cannot_block_repeated_interrupt_cleanup(tmp_path):
    supervisor_file = tmp_path / "saturated-supervisor"
    payload_file = tmp_path / "saturated-payload"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _saturated_output_tts_child(
                tmp_path / "saturated-site",
                supervisor_file,
                payload_file,
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_isolated_env(tmp_path),
    )
    supervisor_pidfd = None
    payload_pidfd = None
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if supervisor_file.exists() and payload_file.exists():
                break
            time.sleep(0.01)
        else:
            pytest.fail("saturated provider did not publish process authority")

        supervisor_pidfd = _linux_pidfd_open(
            int(supervisor_file.read_text(encoding="utf-8"))
        )
        payload_pidfd = _linux_pidfd_open(int(payload_file.read_text(encoding="utf-8")))
        time.sleep(0.2)
        started = time.monotonic()
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.1)
        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.02)
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=1.5)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            _terminate(proc)
        for pidfd in (payload_pidfd, supervisor_pidfd):
            if pidfd is None:
                continue
            try:
                _linux_pidfd_send_signal(pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass

    assert proc.returncode == 130
    assert elapsed < 1.25
    assert stdout
    assert stderr
    assert supervisor_pidfd is not None
    assert payload_pidfd is not None
    try:
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(supervisor_pidfd, 0)
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(payload_pidfd, 0)
    finally:
        os.close(payload_pidfd)
        os.close(supervisor_pidfd)


def test_output_relay_appends_to_existing_redirected_file(tmp_path):
    import hark.tts_worker as worker

    output_path = tmp_path / "provider-output.log"
    with output_path.open("w+b") as inherited_output:
        inherited_output.write(b"parent-prefix\n")
        inherited_output.flush()
        relay_fd = worker._open_output_relay_fd(inherited_output.fileno())
        try:
            assert relay_fd >= 0
            worker._write_output_nowait(relay_fd, b"provider-diagnostic\n")
        finally:
            if relay_fd >= 0:
                os.close(relay_fd)

        inherited_output.seek(0)
        assert inherited_output.read() == (
            b"parent-prefix\nprovider-diagnostic\n"
        )


def test_output_relay_uses_private_nonblocking_open_description():
    import hark.tts_worker as worker

    read_fd, inherited_write_fd = os.pipe()
    relay_fd = -1
    try:
        relay_fd = worker._open_output_relay_fd(inherited_write_fd)
        assert relay_fd >= 0
        assert relay_fd != inherited_write_fd
        assert os.get_blocking(inherited_write_fd) is True
        assert os.get_blocking(relay_fd) is False

        worker._write_output_nowait(relay_fd, b"diagnostic")

        assert os.get_blocking(inherited_write_fd) is True
        assert os.read(read_fd, len(b"diagnostic")) == b"diagnostic"
    finally:
        if relay_fd >= 0:
            os.close(relay_fd)
        os.close(inherited_write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("failure_effect", ["pre", "post"])
def test_output_relay_restores_blocking_after_transition_failure(
    monkeypatch,
    failure_effect,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    real_set_blocking = os.set_blocking
    transitions: list[bool] = []

    def fail_transition(fd, blocking):
        transitions.append(blocking)
        if not blocking:
            if failure_effect == "post":
                real_set_blocking(fd, False)
            raise OSError(f"{failure_effect}-effect transition failure")
        real_set_blocking(fd, True)

    monkeypatch.setattr(worker.os, "set_blocking", fail_transition)
    try:
        worker._write_output_nowait(write_fd, b"diagnostic")

        assert transitions == [False, True]
        assert os.get_blocking(write_fd) is True
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_output_relay_restore_failure_preserves_transition_primary(monkeypatch):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    real_set_blocking = os.set_blocking

    def post_effect_failures(fd, blocking):
        real_set_blocking(fd, blocking)
        if not blocking:
            raise MemoryError("transition primary")
        raise OSError("restoration secondary")

    monkeypatch.setattr(worker.os, "set_blocking", post_effect_failures)
    try:
        with pytest.raises(MemoryError, match="transition primary"):
            worker._write_output_nowait(write_fd, b"diagnostic")

        assert os.get_blocking(write_fd) is True
    finally:
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("failure_site", ["transition", "write"])
@pytest.mark.parametrize("failure_effect", ["pre", "post"])
@pytest.mark.parametrize("restore_effect", ["pre", "post"])
def test_output_relay_baseexception_matrix_preserves_first_primary_and_mode(
    monkeypatch,
    failure_site,
    failure_effect,
    restore_effect,
):
    import hark.tts_worker as worker

    class InjectedInterrupt(BaseException):
        pass

    read_fd, write_fd = os.pipe()
    real_set_blocking = os.set_blocking
    real_write = os.write
    primary = InjectedInterrupt(f"{failure_site} primary")
    restore_failure = MemoryError("restore secondary")
    restore_attempts = 0

    def injected_set_blocking(fd, blocking):
        nonlocal restore_attempts
        if not blocking:
            if failure_site != "transition":
                real_set_blocking(fd, False)
                return
            if failure_effect == "post":
                real_set_blocking(fd, False)
            raise primary
        restore_attempts += 1
        if restore_attempts == 1:
            if restore_effect == "post":
                real_set_blocking(fd, True)
            raise restore_failure
        real_set_blocking(fd, True)

    def injected_write(fd, data):
        if failure_site != "write":
            return real_write(fd, data)
        if failure_effect == "post":
            real_write(fd, data)
        raise primary

    monkeypatch.setattr(worker.os, "set_blocking", injected_set_blocking)
    monkeypatch.setattr(worker.os, "write", injected_write)
    try:
        with pytest.raises(InjectedInterrupt) as raised:
            worker._write_output_nowait(write_fd, b"diagnostic")

        assert raised.value is primary
        assert os.get_blocking(write_fd) is True
        assert 1 <= restore_attempts <= worker._BLOCKING_RESTORE_ATTEMPTS
    finally:
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("restore_effect", ["pre", "post"])
def test_output_relay_restore_baseexception_is_primary_after_mode_reconciliation(
    monkeypatch,
    restore_effect,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    real_set_blocking = os.set_blocking
    restore_failure = MemoryError("restore primary")
    restore_attempts = 0

    def injected_set_blocking(fd, blocking):
        nonlocal restore_attempts
        if not blocking:
            real_set_blocking(fd, False)
            return
        restore_attempts += 1
        if restore_attempts == 1:
            if restore_effect == "post":
                real_set_blocking(fd, True)
            raise restore_failure
        real_set_blocking(fd, True)

    monkeypatch.setattr(worker.os, "set_blocking", injected_set_blocking)
    try:
        with pytest.raises(MemoryError) as raised:
            worker._write_output_nowait(write_fd, b"diagnostic")

        assert raised.value is restore_failure
        assert os.get_blocking(write_fd) is True
        assert 1 <= restore_attempts <= worker._BLOCKING_RESTORE_ATTEMPTS
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_output_relay_persistent_restore_failures_are_bounded_and_reconciled(
    monkeypatch,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    real_get_blocking = os.get_blocking
    real_set_blocking = os.set_blocking
    get_failure = MemoryError("restore get primary")
    get_calls = 0
    restore_attempts = 0

    def injected_get_blocking(fd):
        nonlocal get_calls
        get_calls += 1
        if get_calls > 1:
            raise get_failure
        return real_get_blocking(fd)

    def injected_set_blocking(fd, blocking):
        nonlocal restore_attempts
        if blocking:
            restore_attempts += 1
            raise OSError("pre-effect restore failure")
        real_set_blocking(fd, False)

    monkeypatch.setattr(worker.os, "get_blocking", injected_get_blocking)
    monkeypatch.setattr(worker.os, "set_blocking", injected_set_blocking)
    try:
        with pytest.raises(OSError, match="pre-effect restore failure"):
            worker._write_output_nowait(write_fd, b"diagnostic")

        assert real_get_blocking(write_fd) is True
        assert restore_attempts == worker._BLOCKING_RESTORE_ATTEMPTS
        assert get_calls == 1 + worker._BLOCKING_RESTORE_ATTEMPTS
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_output_relay_restore_get_failure_becomes_primary_after_reconciliation(
    monkeypatch,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    real_get_blocking = os.get_blocking
    get_failure = MemoryError("restore get primary")
    get_calls = 0

    def injected_get_blocking(fd):
        nonlocal get_calls
        get_calls += 1
        if get_calls == 2:
            raise get_failure
        return real_get_blocking(fd)

    monkeypatch.setattr(worker.os, "get_blocking", injected_get_blocking)
    try:
        with pytest.raises(MemoryError) as raised:
            worker._write_output_nowait(write_fd, b"diagnostic")

        assert raised.value is get_failure
        assert real_get_blocking(write_fd) is True
        assert get_calls == 3
    finally:
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize(
    ("failure", "propagates"),
    [(OSError("initial get unavailable"), False), (MemoryError("initial get"), True)],
)
def test_output_relay_initial_get_failure_never_changes_mode(
    monkeypatch,
    failure,
    propagates,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    real_get_blocking = os.get_blocking
    writes = 0

    def fail_get_blocking(fd):
        raise failure

    def count_write(fd, data):
        nonlocal writes
        writes += 1

    monkeypatch.setattr(worker.os, "get_blocking", fail_get_blocking)
    monkeypatch.setattr(worker.os, "write", count_write)
    try:
        if propagates:
            with pytest.raises(type(failure)) as raised:
                worker._write_output_nowait(write_fd, b"diagnostic")
            assert raised.value is failure
        else:
            worker._write_output_nowait(write_fd, b"diagnostic")

        assert writes == 0
        assert real_get_blocking(write_fd) is True
    finally:
        os.close(write_fd)
        os.close(read_fd)


@pytest.mark.parametrize("write_effect", ["partial", "eagain"])
def test_output_relay_write_outcomes_restore_original_blocking_mode(
    monkeypatch,
    write_effect,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()

    def injected_write(fd, data):
        assert os.get_blocking(fd) is False
        if write_effect == "partial":
            return 1
        raise BlockingIOError(errno.EAGAIN, "relay pipe full")

    monkeypatch.setattr(worker.os, "write", injected_write)
    try:
        worker._write_output_nowait(write_fd, b"diagnostic")

        assert os.get_blocking(write_fd) is True
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_output_relay_real_full_pipe_is_bounded_and_restores_blocking():
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    try:
        os.set_blocking(write_fd, False)
        chunk = b"x" * (64 * 1024)
        with pytest.raises(BlockingIOError):
            while True:
                os.write(write_fd, chunk)
        os.set_blocking(write_fd, True)

        started = time.monotonic()
        worker._write_output_nowait(write_fd, b"blocked diagnostic")

        assert time.monotonic() - started < 0.1
        assert os.get_blocking(write_fd) is True
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_supervisor_relinquishes_adopted_result_fd_before_forward_failure(
    monkeypatch,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    reused_fd = None

    def close_reuse_then_raise(payload_result, forwarding_owner):
        nonlocal reused_fd
        forwarding_fd = forwarding_owner.fd
        forwarding_owner.close_if_owned()
        reused_fd = os.open(os.devnull, os.O_WRONLY)
        assert reused_fd == forwarding_fd
        raise MemoryError("post-adoption forward failure")

    monkeypatch.setenv("HARK_TTS_RESULT_FD", str(write_fd))
    monkeypatch.setattr(worker, "_stage_payload_request", lambda request: None)
    monkeypatch.setattr(worker, "_forward_payload_result", close_reuse_then_raise)
    try:
        with pytest.raises(MemoryError, match="post-adoption forward failure"):
            worker._supervise_payload(["--test-success"], subreaper=True)

        assert reused_fd is not None
        os.fstat(reused_fd)
        assert os.read(read_fd, 1) == b""
    finally:
        if reused_fd is not None:
            try:
                os.close(reused_fd)
            except OSError:
                pass
        else:
            try:
                os.close(write_fd)
            except OSError:
                pass
        os.close(read_fd)


@pytest.mark.parametrize("failure_site", ["flush", "seek", "fdopen"])
def test_result_fd_guard_closes_original_once_on_pre_adoption_baseexception(
    monkeypatch,
    failure_site,
):
    import hark.tts_worker as worker

    class FailingPayload(io.BytesIO):
        def flush(self):
            if failure_site == "flush":
                raise MemoryError("pre-adoption primary")
            return super().flush()

        def seek(self, *args):
            if failure_site == "seek":
                raise MemoryError("pre-adoption primary")
            return super().seek(*args)

    read_fd, write_fd = os.pipe()
    real_close = worker._RESULT_CLOSE
    close_calls: list[int] = []

    def tracking_close(fd, state):
        close_calls.append(fd)
        real_close(fd, state)

    def fail_fdopen(fd, state, *args, **kwargs):
        raise MemoryError("pre-adoption primary")

    monkeypatch.setattr(worker, "_RESULT_CLOSE", tracking_close)
    if failure_site == "fdopen":
        monkeypatch.setattr(worker, "_RESULT_FDOPEN", fail_fdopen)
    try:
        with pytest.raises(MemoryError, match="pre-adoption primary"):
            worker._forward_payload_result(FailingPayload(b"result"), write_fd)

        assert close_calls == [write_fd]
        with pytest.raises(OSError):
            os.fstat(write_fd)
        assert os.read(read_fd, 1) == b""
    finally:
        try:
            os.close(write_fd)
        except OSError:
            pass
        os.close(read_fd)


def test_result_fd_guard_disarms_after_fdopen_close_reuse_failure(monkeypatch):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    source_fd = os.open(os.devnull, os.O_WRONLY)
    source_identity = worker._result_fd_identity(source_fd)
    real_fdopen = worker._RESULT_FDOPEN
    close_calls: list[int] = []

    def adopt_close_reuse_then_raise(fd, state, *args, **kwargs):
        adopted = real_fdopen(fd, state, *args, **kwargs)
        adopted.close()
        os.dup2(source_fd, fd)
        raise MemoryError("post-adoption primary")

    def tracking_close(fd, state):
        del state
        close_calls.append(fd)
        os.close(fd)

    monkeypatch.setattr(worker, "_RESULT_FDOPEN", adopt_close_reuse_then_raise)
    monkeypatch.setattr(worker, "_RESULT_CLOSE", tracking_close)
    try:
        with pytest.raises(MemoryError, match="post-adoption primary"):
            worker._forward_payload_result(io.BytesIO(b"result"), write_fd)

        assert close_calls == []
        assert worker._result_fd_identity(write_fd) == source_identity
        assert os.read(read_fd, 1) == b""
    finally:
        os.close(write_fd)
        os.close(source_fd)
        os.close(read_fd)


def test_result_fd_guard_disarms_before_adopted_context_can_reuse_fd(monkeypatch):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    source_fd = os.open(os.devnull, os.O_WRONLY)
    source_identity = worker._result_fd_identity(source_fd)
    real_fdopen = worker._RESULT_FDOPEN
    close_calls: list[int] = []

    class PostAdoptionFailure:
        def __init__(self, adopted):
            self.adopted = adopted

        def __enter__(self):
            self.adopted.close()
            os.dup2(source_fd, write_fd)
            raise MemoryError("post-adoption enter primary")

        def __exit__(self, *args):
            return False

    def return_adopted_context(fd, state, *args, **kwargs):
        return PostAdoptionFailure(real_fdopen(fd, state, *args, **kwargs))

    def tracking_close(fd, state):
        del state
        close_calls.append(fd)
        os.close(fd)

    monkeypatch.setattr(worker, "_RESULT_FDOPEN", return_adopted_context)
    monkeypatch.setattr(worker, "_RESULT_CLOSE", tracking_close)
    try:
        with pytest.raises(MemoryError, match="post-adoption enter primary"):
            worker._forward_payload_result(io.BytesIO(b"result"), write_fd)

        assert close_calls == []
        assert worker._result_fd_identity(write_fd) == source_identity
        assert os.read(read_fd, 1) == b""
    finally:
        os.close(write_fd)
        os.close(source_fd)
        os.close(read_fd)


def test_result_fd_guard_close_reuse_failure_preserves_pre_adoption_primary(
    monkeypatch,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    source_fd = os.open(os.devnull, os.O_WRONLY)
    source_identity = worker._result_fd_identity(source_fd)
    payload_primary = MemoryError("payload primary")
    close_calls = 0

    class FailingPayload(io.BytesIO):
        def flush(self):
            raise payload_primary

    def close_reuse_then_raise(fd, state):
        nonlocal close_calls
        state.effect_started = True
        close_calls += 1
        os.close(fd)
        os.dup2(source_fd, fd)
        raise KeyboardInterrupt("close secondary")

    monkeypatch.setattr(worker, "_RESULT_CLOSE", close_reuse_then_raise)
    try:
        with pytest.raises(MemoryError) as raised:
            worker._forward_payload_result(FailingPayload(), write_fd)

        assert raised.value is payload_primary
        assert close_calls == 1
        assert worker._result_fd_identity(write_fd) == source_identity
        assert os.read(read_fd, 1) == b""
    finally:
        os.close(write_fd)
        os.close(source_fd)
        os.close(read_fd)


@pytest.mark.parametrize("hostile", ["abandon", "mute"])
def test_cleanup_baseexception_preserves_first_interrupt_and_terminal_gate(
    tmp_path, hostile
):
    proc = subprocess.Popen(
        [sys.executable, "-c", _hostile_cleanup_tts_child(hostile)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    try:
        assert _read_ready(proc) == "READY"
        os.kill(proc.pid, signal.SIGINT)
        expected = "ABANDON_ABANDON" if hostile == "abandon" else "ABANDON_MUTE"
        assert _read_ready(proc) == expected
        if hostile == "abandon":
            assert _read_ready(proc) == "REPAIR_ABANDON"
        else:
            assert _read_ready(proc) == "REPAIR_MUTE"
        os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)
    finally:
        if proc.poll() is None:
            _terminate(proc)

    assert proc.returncode == 130
    assert stdout == ""
    assert stderr == ""


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


def test_worker_without_linux_prctl_fails_closed_before_synthesis(monkeypatch):
    import hark.speech as speech
    from hark.config import HarkConfig
    from hark.tts_isolation import SynthWorkerError

    class Store:
        def record_tts(self, **kwargs):
            return None

    worker = textwrap.dedent(
        """
        import hark.tts_worker as worker
        class NoPrctl:
            pass
        worker.ctypes.CDLL = lambda *args, **kwargs: NoPrctl()
        raise SystemExit(worker.main(["--test-success"]))
        """
    )
    monkeypatch.setattr(speech, "UsageStore", Store)
    monkeypatch.setattr(
        speech,
        "_synth_worker_command_factory",
        lambda: [sys.executable, "-c", worker],
    )

    with pytest.raises(
        SynthWorkerError,
        match="isolated TTS requires exact descendant cleanup authority on this host",
    ):
        speech.run_tts(HarkConfig(), "portable", play=False, use_cache=False)


def test_parent_without_pidfd_uses_portable_supervisor_authority(monkeypatch):
    import hark.speech as speech
    from hark.tts_isolation import SubprocessSynthTransport, SynthRequest

    owner = speech._InterruptibleSynthPool()
    transport = SubprocessSynthTransport(
        owner,
        command_factory=lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-success",
        ],
    )
    monkeypatch.setattr(transport, "_open_pidfd", lambda process: None)
    try:
        response = transport.synthesize(SynthRequest("p", "v", None, "x"))
        assert response.audio == b"test-audio"
        assert response.provider == "test-worker"
        assert owner._process_lifecycle.active is False
    finally:
        owner._pool.shutdown(wait=False, cancel_futures=True)


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
        identity = None

        def spawn_synth_process(self, process, command, **kwargs):
            self.process = process
            subprocess.Popen.__init__(process, command, **kwargs)

        def publish_synth_process_pidfd(self, process, identity):
            assert process is self.process
            self.identity = identity
            return True

        def unregister_synth_process(self, process):
            assert process is self.process
            if self.identity is not None:
                self.identity.request_close()
            raise RuntimeError("unregister failed")

        def wait_and_unregister_synth_process(self, process):
            returncode = process.wait()
            self.unregister_synth_process(process)
            return returncode

        def cancel_synth_process(self, process):
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=1.0)
            return True

        def close_synth_identity_if_unowned(self, process, identity):
            if self.identity is identity:
                return
            identity.request_close()

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


def test_pipe_write_close_post_effect_failure_does_not_close_reused_fd(monkeypatch):
    import hark.speech as speech
    import hark.tts_isolation as isolation

    source_fd = os.open(os.devnull, os.O_RDONLY)
    source_stat = os.fstat(source_fd)
    reused_fd = None
    real_close = isolation._PIPE_CLOSE
    injected = False

    def close_reuse_then_raise(fd, state):
        nonlocal injected, reused_fd
        if not injected:
            injected = True
            reused_fd = fd
            real_close(fd, state)
            os.dup2(source_fd, fd)
            raise MemoryError("pipe close post-effect primary")
        real_close(fd, state)

    def worker_command():
        command = isolation.synth_worker_command()
        command.append("--test-hang")
        return command

    owner = speech._InterruptibleSynthPool()
    transport = isolation.SubprocessSynthTransport(
        owner,
        command_factory=worker_command,
    )
    monkeypatch.setattr(isolation, "_PIPE_CLOSE", close_reuse_then_raise)
    try:
        with pytest.raises(MemoryError, match="pipe close post-effect primary"):
            transport.synthesize(isolation.SynthRequest("p", "v", None, "x"))

        assert reused_fd is not None
        reused_stat = os.fstat(reused_fd)
        assert (reused_stat.st_dev, reused_stat.st_ino) == (
            source_stat.st_dev,
            source_stat.st_ino,
        )
    finally:
        owner._pool.shutdown(wait=False, cancel_futures=True)
        if reused_fd is not None:
            try:
                os.close(reused_fd)
            except OSError:
                pass
        os.close(source_fd)


def test_pipe_fdopen_post_adoption_failure_does_not_close_reused_fd(monkeypatch):
    import hark.speech as speech
    import hark.tts_isolation as isolation

    source_fd = os.open(os.devnull, os.O_RDONLY)
    source_stat = os.fstat(source_fd)
    reused_fd = None
    real_fdopen = isolation._PIPE_FDOPEN

    def adopt_close_reuse_then_raise(fd, state, *args, **kwargs):
        nonlocal reused_fd
        adopted = real_fdopen(fd, state, *args, **kwargs)
        adopted.close()
        os.dup2(source_fd, fd)
        reused_fd = fd
        raise MemoryError("pipe fdopen post-adoption primary")

    def worker_command():
        command = isolation.synth_worker_command()
        command.append("--test-success")
        return command

    owner = speech._InterruptibleSynthPool()
    transport = isolation.SubprocessSynthTransport(
        owner,
        command_factory=worker_command,
    )
    monkeypatch.setattr(isolation, "_PIPE_FDOPEN", adopt_close_reuse_then_raise)
    try:
        with pytest.raises(MemoryError, match="pipe fdopen post-adoption primary"):
            transport.synthesize(isolation.SynthRequest("p", "v", None, "x"))

        assert reused_fd is not None
        reused_stat = os.fstat(reused_fd)
        assert (reused_stat.st_dev, reused_stat.st_ino) == (
            source_stat.st_dev,
            source_stat.st_ino,
        )
    finally:
        owner._pool.shutdown(wait=False, cancel_futures=True)
        if reused_fd is not None:
            try:
                os.close(reused_fd)
            except OSError:
                pass
        os.close(source_fd)


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("transfer", ["close", "fdopen"])
@pytest.mark.parametrize("phase", ["pre", "post-reuse"])
def test_pipe_transfer_baseexception_ownership_matrix(
    monkeypatch,
    failure_type,
    transfer,
    phase,
):
    import hark.speech as speech
    import hark.tts_isolation as isolation

    source_fd = os.open(os.devnull, os.O_RDONLY)
    source_stat = os.fstat(source_fd)
    transferred_fd = None
    primary = failure_type(f"{transfer}-{phase}-primary")
    real_close = isolation._PIPE_CLOSE
    real_fdopen = isolation._PIPE_FDOPEN

    def fail_close(fd, state):
        nonlocal transferred_fd
        transferred_fd = fd
        if phase == "post-reuse":
            real_close(fd, state)
            os.dup2(source_fd, fd)
        raise primary

    def fail_fdopen(fd, state, *args, **kwargs):
        nonlocal transferred_fd
        transferred_fd = fd
        if phase == "post-reuse":
            adopted = real_fdopen(fd, state, *args, **kwargs)
            adopted.close()
            os.dup2(source_fd, fd)
        raise primary

    def worker_command():
        return isolation.synth_worker_command() + ["--test-success"]

    owner = speech._InterruptibleSynthPool()
    transport = isolation.SubprocessSynthTransport(
        owner,
        command_factory=worker_command,
    )
    monkeypatch.setattr(
        isolation,
        "_PIPE_CLOSE" if transfer == "close" else "_PIPE_FDOPEN",
        fail_close if transfer == "close" else fail_fdopen,
    )
    try:
        with pytest.raises(failure_type) as raised:
            transport.synthesize(isolation.SynthRequest("p", "v", None, "x"))

        assert raised.value is primary
        assert transferred_fd is not None
        assert owner._process_lifecycle.active is False
        if phase == "pre":
            with pytest.raises(OSError):
                os.fstat(transferred_fd)
        else:
            reused_stat = os.fstat(transferred_fd)
            assert (reused_stat.st_dev, reused_stat.st_ino) == (
                source_stat.st_dev,
                source_stat.st_ino,
            )
    finally:
        owner._pool.shutdown(wait=False, cancel_futures=True)
        if phase == "post-reuse" and transferred_fd is not None:
            try:
                os.close(transferred_fd)
            except OSError:
                pass
        os.close(source_fd)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_pidfd_open_failure_reaps_worker_and_preserves_primary(monkeypatch):
    import hark.speech as speech
    import hark.tts_isolation as isolation
    from hark.tts_isolation import SubprocessSynthTransport, SynthRequest
    from hark.tts_isolation import SynthWorkerError

    spawned = []
    real_init = isolation._SYNTH_POPEN_INIT

    def capture_init(process, *args, **kwargs):
        real_init(process, *args, **kwargs)
        authority = owner._process_lifecycle._authority
        if authority is not None and authority.process is process:
            spawned.append(process)

    owner = speech._InterruptibleSynthPool()
    transport = SubprocessSynthTransport(
        owner,
        command_factory=lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-hang",
        ],
    )
    monkeypatch.setattr(
        isolation.os,
        "pidfd_open",
        lambda pid, flags=0: (_ for _ in ()).throw(OSError(errno.EIO, "boom")),
    )
    monkeypatch.setattr(isolation, "_SYNTH_POPEN_INIT", capture_init)

    try:
        with pytest.raises(SynthWorkerError, match="could not claim"):
            transport.synthesize(SynthRequest("p", "v", None, "x"))

        assert owner._process_lifecycle.active is False
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
    finally:
        owner._pool.shutdown(wait=False, cancel_futures=True)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_worker_exit_before_pidfd_open_fails_closed_without_foreign_kill(monkeypatch):
    import hark.speech as speech
    import hark.tts_isolation as isolation
    from hark.tts_isolation import SubprocessSynthTransport, SynthRequest
    from hark.tts_isolation import SynthWorkerError

    real_pidfd_open = isolation.os.pidfd_open
    spawned = []
    real_init = isolation._SYNTH_POPEN_INIT

    def capture_init(process, *args, **kwargs):
        real_init(process, *args, **kwargs)
        authority = owner._process_lifecycle._authority
        if authority is not None and authority.process is process:
            spawned.append(process)

    def reap_before_open(pid, flags=0):
        waited, _ = os.waitpid(pid, 0)
        assert waited == pid
        return real_pidfd_open(pid, flags)

    owner = speech._InterruptibleSynthPool()
    transport = SubprocessSynthTransport(
        owner,
        command_factory=lambda: [sys.executable, "-c", "pass"],
    )
    monkeypatch.setattr(isolation.os, "pidfd_open", reap_before_open)
    monkeypatch.setattr(isolation, "_SYNTH_POPEN_INIT", capture_init)

    try:
        with pytest.raises(SynthWorkerError, match="could not claim"):
            transport.synthesize(SynthRequest("p", "v", None, "x"))
        assert owner._process_lifecycle.active is False
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
    finally:
        owner._pool.shutdown(wait=False, cancel_futures=True)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
@pytest.mark.parametrize("mode", ["withdraw", "raise"])
def test_pidfd_publication_failure_closes_fd_and_unregisters_worker(monkeypatch, mode):
    import hark.speech as speech
    import hark.tts_isolation as isolation
    from hark.tts_isolation import SubprocessSynthTransport, SynthRequest
    from hark.tts_isolation import SynthWorkerError

    owner = speech._InterruptibleSynthPool()
    opened = []
    spawned = []
    real_open = isolation.os.pidfd_open
    real_init = isolation._SYNTH_POPEN_INIT

    def visible_open(pid, flags=0):
        fd = real_open(pid, flags)
        opened.append(fd)
        return fd

    def capture_init(process, *args, **kwargs):
        real_init(process, *args, **kwargs)
        authority = owner._process_lifecycle._authority
        if authority is not None and authority.process is process:
            spawned.append(process)

    def fail_publish(process, identity):
        if mode == "raise":
            raise MemoryError("publish primary")
        return False

    monkeypatch.setattr(isolation.os, "pidfd_open", visible_open)
    monkeypatch.setattr(isolation, "_SYNTH_POPEN_INIT", capture_init)
    monkeypatch.setattr(owner, "publish_synth_process_pidfd", fail_publish)
    transport = SubprocessSynthTransport(
        owner,
        command_factory=lambda: [
            sys.executable,
            "-m",
            "hark.tts_worker",
            "--test-hang",
        ],
    )

    try:
        if mode == "raise":
            with pytest.raises(MemoryError, match="publish primary"):
                transport.synthesize(SynthRequest("p", "v", None, "x"))
        else:
            with pytest.raises(SynthWorkerError, match="ownership was withdrawn"):
                transport.synthesize(SynthRequest("p", "v", None, "x"))

        assert owner._process_lifecycle.active is False
        assert len(spawned) == 1
        assert spawned[0].poll() is not None
        assert len(opened) == 1
        with pytest.raises(OSError) as caught:
            os.fstat(opened[0])
        assert caught.value.errno == errno.EBADF
    finally:
        owner._pool.shutdown(wait=False, cancel_futures=True)


def test_from_raw_post_commit_failure_has_one_close_owner_after_fd_reuse(monkeypatch):
    import hark.tts_isolation as isolation

    source_fd = os.open(os.devnull, os.O_RDONLY)
    raw_pidfd = os.dup(source_fd)
    real_close = isolation._OS_CLOSE
    close_calls = []

    def close_then_reuse(fd):
        assert fd == raw_pidfd
        close_calls.append(fd)
        real_close(fd)
        os.dup2(source_fd, fd)

    def fail_after_commit(self):
        raise MemoryError("post-commit primary")

    monkeypatch.setattr(isolation, "_OS_CLOSE", close_then_reuse)
    monkeypatch.setattr(isolation._OwnedPidfd, "_after_commit", fail_after_commit)
    with pytest.raises(MemoryError, match="post-commit primary"):
        isolation._OwnedPidfd.from_raw(raw_pidfd)

    # The except path and destructor share one committed owner; destruction
    # cannot close the descriptor integer after the close hook reuses it.
    assert close_calls == [raw_pidfd]
    os.fstat(raw_pidfd)
    os.close(raw_pidfd)
    os.close(source_fd)


def test_from_raw_pre_adoption_failure_closes_unowned_descriptor(monkeypatch):
    import hark.tts_isolation as isolation

    source_fd = os.open(os.devnull, os.O_RDONLY)
    raw_pidfd = os.dup(source_fd)

    def fail_authority_construction():
        raise MemoryError("pre-adoption primary")

    monkeypatch.setattr(isolation, "_BorrowAuthority", fail_authority_construction)
    with pytest.raises(MemoryError, match="pre-adoption primary"):
        isolation._OwnedPidfd.from_raw(raw_pidfd)

    with pytest.raises(OSError) as caught:
        os.fstat(raw_pidfd)
    assert caught.value.errno == errno.EBADF
    os.close(source_fd)


def test_raii_pidfd_closes_when_identity_construction_never_returns(monkeypatch):
    import hark.tts_isolation as isolation

    source_fd = os.open(os.devnull, os.O_RDONLY)
    raw_pidfd = os.dup(source_fd)

    def fail_identity(owner):
        assert owner.fd == raw_pidfd
        raise MemoryError("identity construction primary")

    monkeypatch.setattr(isolation, "_IdentityToken", fail_identity)
    with pytest.raises(MemoryError, match="identity construction primary"):
        fail_identity(isolation._OwnedPidfd.from_raw(raw_pidfd))

    with pytest.raises(OSError) as caught:
        os.fstat(raw_pidfd)
    assert caught.value.errno == errno.EBADF
    os.dup2(source_fd, raw_pidfd)
    os.fstat(raw_pidfd)
    os.close(raw_pidfd)
    os.close(source_fd)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_terminal_pidfd_cleanup_is_idempotent_and_closes_descriptor():
    import hark.speech as speech
    import hark.tts_isolation as isolation

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    pool = speech._InterruptibleSynthPool()
    pidfd = os.pidfd_open(process.pid, 0)
    identity = isolation._IdentityToken(isolation._OwnedPidfd.from_raw(pidfd))
    try:
        pool.register_synth_process(process)
        assert pool.publish_synth_process_pidfd(process, identity) is True

        pool._terminate_synth_process_for_exit()
        pool._terminate_synth_process_for_exit()

        with pytest.raises(OSError) as caught:
            os.fstat(pidfd)
        assert caught.value.errno == errno.EBADF
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)
        pool._pool.shutdown(wait=False, cancel_futures=True)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_pidfd_close_handoff_survives_nested_concurrent_finish_and_fd_reuse(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    lifecycle = isolation.SynthProcessLifecycle()
    lifecycle.preclaim(process)
    pidfd = os.pidfd_open(process.pid, 0)
    identity = isolation._IdentityToken(isolation._OwnedPidfd.from_raw(pidfd))
    assert lifecycle.publish(process, identity) is True
    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    authority = lifecycle._authority
    assert authority is not None

    source_fd = os.open(os.devnull, os.O_RDONLY)
    close_calls: list[int] = []
    start = threading.Barrier(3)

    def close_then_reuse(fd):
        close_calls.append(fd)
        os.close(fd)
        os.dup2(source_fd, fd)
        # Re-enter after the original integer has already been reused. A
        # second close would now corrupt this unrelated descriptor.
        assert lifecycle.cancel() is True
        os.fstat(fd)

    monkeypatch.setattr(isolation, "_OS_CLOSE", close_then_reuse)

    outcomes = []

    def cancel():
        start.wait()
        outcomes.append(("cancel", lifecycle.cancel()))

    def wait():
        start.wait()
        outcomes.append(("wait", lifecycle.wait_and_release(process)))

    threads = [threading.Thread(target=cancel), threading.Thread(target=wait)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=1.0)
        assert not thread.is_alive()

    try:
        assert close_calls == [pidfd]
        assert {name for name, _ in outcomes} == {"cancel", "wait"}
        os.fstat(pidfd)
    finally:
        os.close(pidfd)
        os.close(source_fd)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_pidfd_publication_interruption_has_one_close_owner_after_fd_reuse():
    import hark.tts_isolation as isolation

    class RaiseAfterIdentity(isolation._ProcessAuthority):
        armed = False

        def __setattr__(self, name, value):
            super().__setattr__(name, value)
            if name == "identity" and self.armed and value is not None:
                raise MemoryError("interrupted after identity publication")

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    lifecycle = isolation.SynthProcessLifecycle()
    authority = RaiseAfterIdentity(process, False)
    lifecycle._authority = authority
    pidfd = os.pidfd_open(process.pid, 0)
    identity = isolation._IdentityToken(isolation._OwnedPidfd.from_raw(pidfd))
    source_fd = os.open(os.devnull, os.O_RDONLY)

    try:
        authority.armed = True
        with pytest.raises(MemoryError, match="after identity publication"):
            lifecycle.publish(process, identity)
        authority.armed = False

        # The single published token already contains both the signalling mode
        # and descriptor ownership, even though publication was interrupted.
        assert authority.identity is identity
        assert identity.pidfd_mode is True
        assert lifecycle.cancel() is True
        with pytest.raises(OSError) as caught:
            os.fstat(pidfd)
        assert caught.value.errno == errno.EBADF

        # Emulate transport-finally cleanup after the descriptor integer has
        # been reused. Since lifecycle cleanup already consumed the token,
        # caller cleanup must not close this unrelated descriptor.
        os.dup2(source_fd, pidfd)
        lifecycle.close_identity_if_unowned(process, identity)
        os.fstat(pidfd)
    finally:
        try:
            os.close(pidfd)
        except OSError:
            pass
        os.close(source_fd)
        if process.returncode is None:
            process.kill()
        process.wait(timeout=1.0)


def test_borrow_cleanup_trace_interruption_cannot_wedge_authority():
    import inspect

    import hark.tts_isolation as isolation

    authority = isolation._BorrowAuthority()
    source, start_line = inspect.getsourcelines(isolation._BorrowAuthority.use)
    cleanup_line = max(
        start_line + offset
        for offset, line in enumerate(source)
        if "dict.pop(self._borrower_slot" in line
    )
    injected = False

    def interrupt_cleanup(frame, event, arg):
        nonlocal injected
        if (
            not injected
            and frame.f_code is isolation._BorrowAuthority.use.__code__
            and event == "line"
            and frame.f_lineno == cleanup_line
        ):
            injected = True
            raise MemoryError("cleanup trace primary")
        return interrupt_cleanup

    sys.settrace(interrupt_cleanup)
    try:
        with pytest.raises(MemoryError, match="cleanup trace primary"):
            authority.use(lambda: "first")
    finally:
        sys.settrace(None)

    assert injected is True
    assert authority.borrowed is False
    assert authority.use(lambda: "recovered") == "recovered"


def test_borrow_operation_primary_skips_fallible_eager_cleanup():
    import inspect

    import hark.tts_isolation as isolation

    authority = isolation._BorrowAuthority()
    source, start_line = inspect.getsourcelines(isolation._BorrowAuthority.use)
    cleanup_line = max(
        start_line + offset
        for offset, line in enumerate(source)
        if "dict.pop(self._borrower_slot" in line
    )
    cleanup_reached = False

    def fail_if_cleanup_runs(frame, event, arg):
        nonlocal cleanup_reached
        if (
            frame.f_code is isolation._BorrowAuthority.use.__code__
            and event == "line"
            and frame.f_lineno == cleanup_line
        ):
            cleanup_reached = True
            raise RuntimeError("cleanup secondary")
        return fail_if_cleanup_runs

    def fail_operation():
        raise MemoryError("operation primary")

    sys.settrace(fail_if_cleanup_runs)
    try:
        with pytest.raises(MemoryError, match="operation primary"):
            authority.use(fail_operation)
    finally:
        sys.settrace(None)

    assert cleanup_reached is False
    assert authority.borrowed is False
    assert authority.use(lambda: "recovered") == "recovered"


def test_post_mask_acquisition_failure_restores_sigint_and_close_retries(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    if not hasattr(signal, "pthread_sigmask"):
        pytest.skip("pthread_sigmask unavailable")
    real_mask = signal.pthread_sigmask
    previous = real_mask(signal.SIG_BLOCK, set())
    source_fd = os.open(os.devnull, os.O_RDONLY)
    raw_pidfd = os.dup(source_fd)
    owner = isolation._OwnedPidfd.from_raw(raw_pidfd)
    injected = False

    def block_then_raise(how, mask):
        nonlocal injected
        result = real_mask(how, mask)
        if not injected and how == signal.SIG_BLOCK and signal.SIGINT in mask:
            injected = True
            raise MemoryError("post-mask primary")
        return result

    monkeypatch.setattr(isolation, "_PTHREAD_SIGMASK", block_then_raise)
    owner.request_close()

    assert injected is True
    assert real_mask(signal.SIG_BLOCK, set()) == previous
    with pytest.raises(OSError) as caught:
        os.fstat(raw_pidfd)
    assert caught.value.errno == errno.EBADF
    os.close(source_fd)


def test_borrow_authority_does_not_depend_on_sigint_mask(monkeypatch):
    import hark.tts_isolation as isolation

    monkeypatch.setattr(
        isolation,
        "_PTHREAD_SIGMASK",
        lambda *args: (_ for _ in ()).throw(MemoryError("must not mask")),
    )
    authority = isolation._BorrowAuthority()
    assert authority.use(lambda: "borrowed") == "borrowed"


def test_two_thread_close_race_has_one_winner_after_forced_fd_reuse(monkeypatch):
    import hark.tts_isolation as isolation

    source_fd = os.open(os.devnull, os.O_RDONLY)
    raw_pidfd = os.dup(source_fd)
    owner = isolation._OwnedPidfd.from_raw(raw_pidfd)
    real_close = isolation._OS_CLOSE
    close_entered = threading.Event()
    release_close = threading.Event()
    close_calls = []

    def held_close(fd):
        close_calls.append(fd)
        real_close(fd)
        os.dup2(source_fd, fd)
        close_entered.set()
        assert release_close.wait(timeout=1.0)

    monkeypatch.setattr(isolation, "_OS_CLOSE", held_close)
    threads = [threading.Thread(target=owner.request_close) for _ in range(2)]
    threads[0].start()
    assert close_entered.wait(timeout=1.0)
    threads[1].start()
    threads[1].join(timeout=1.0)
    assert not threads[1].is_alive()
    release_close.set()
    threads[0].join(timeout=1.0)
    assert not threads[0].is_alive()

    assert close_calls == [raw_pidfd]
    os.fstat(raw_pidfd)
    owner.request_close()
    os.fstat(raw_pidfd)
    os.close(raw_pidfd)
    os.close(source_fd)


def test_rejected_borrower_cannot_release_true_borrower_or_close_reused_fd(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    original_read, original_write = os.pipe()
    foreign_read, foreign_write = os.pipe()
    original_inode = os.fstat(original_read).st_ino
    foreign_inode = os.fstat(foreign_read).st_ino
    assert original_inode != foreign_inode
    owner = isolation._OwnedPidfd.from_raw(original_read)
    real_close = isolation._OS_CLOSE
    borrower_entered = threading.Event()
    inspect_after_rejection = threading.Event()
    close_calls = []
    observed_inodes = []

    def close_then_reuse(fd):
        close_calls.append(fd)
        real_close(fd)
        os.dup2(foreign_read, fd)

    def true_borrower(fd, signum):
        borrower_entered.set()
        assert inspect_after_rejection.wait(timeout=1.0)
        observed_inodes.append(os.fstat(fd).st_ino)

    monkeypatch.setattr(isolation, "_OS_CLOSE", close_then_reuse)
    borrower = threading.Thread(
        target=owner.use,
        args=(true_borrower, signal.SIGCONT),
    )
    borrower.start()
    assert borrower_entered.wait(timeout=1.0)

    # A owns the published lease. Closing retires future borrows but must defer
    # until A releases. B's rejected temporary lease has no release authority.
    owner.request_close()
    rejected = owner.use(lambda fd, signum: pytest.fail("rejected borrow ran"), 0)
    assert rejected is isolation._BORROW_BUSY
    assert close_calls == []

    inspect_after_rejection.set()
    borrower.join(timeout=1.0)
    assert not borrower.is_alive()
    assert observed_inodes == [original_inode]
    assert close_calls == [original_read]
    assert os.fstat(original_read).st_ino == foreign_inode

    owner.request_close()
    assert os.fstat(original_read).st_ino == foreign_inode
    os.close(original_read)
    os.close(original_write)
    os.close(foreign_read)
    os.close(foreign_write)


def test_retire_post_effect_interrupt_rejects_late_borrow_after_fd_reuse(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    original_read, original_write = os.pipe()
    foreign_read, foreign_write = os.pipe()
    foreign_inode = os.fstat(foreign_read).st_ino
    owner = isolation._OwnedPidfd.from_raw(original_read)
    real_close = isolation._OS_CLOSE
    close_calls = []

    class InterruptAfterRetire(isolation._BorrowAuthority):
        armed = False

        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            if name == "retired" and value is True and self.armed:
                self.armed = False
                raise MemoryError("retire post-effect")

    borrow = InterruptAfterRetire()
    borrow.armed = True
    owner._borrow = borrow

    def close_then_reuse(fd):
        close_calls.append(fd)
        real_close(fd)
        os.dup2(foreign_read, fd)

    monkeypatch.setattr(isolation, "_OS_CLOSE", close_then_reuse)
    with pytest.raises(MemoryError, match="retire post-effect"):
        owner.request_close()

    assert borrow.retired is True
    assert close_calls == [original_read]
    assert os.fstat(original_read).st_ino == foreign_inode
    ran = []
    result = owner.use(lambda fd, signum: ran.append(fd), signal.SIGCONT)
    assert result is isolation._BORROW_BUSY
    assert ran == []

    owner.request_close()
    assert close_calls == [original_read]
    assert os.fstat(original_read).st_ino == foreign_inode
    os.close(original_read)
    os.close(original_write)
    os.close(foreign_read)
    os.close(foreign_write)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_borrowed_pidfd_preserves_primary_and_never_closes_reused_fd(monkeypatch):
    import hark.tts_isolation as isolation

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    raw_pidfd = os.pidfd_open(process.pid, 0)
    owner = isolation._OwnedPidfd.from_raw(raw_pidfd)
    source_fd = os.open(os.devnull, os.O_RDONLY)
    real_close = isolation._OS_CLOSE

    def close_reuse_then_raise(fd):
        assert fd == raw_pidfd
        real_close(fd)
        os.dup2(source_fd, fd)
        raise RuntimeError("cleanup secondary")

    def request_close_then_fail(fd, signum):
        assert fd == raw_pidfd
        owner.request_close()
        raise MemoryError("borrow primary")

    monkeypatch.setattr(isolation, "_OS_CLOSE", close_reuse_then_raise)
    try:
        with pytest.raises(MemoryError, match="borrow primary"):
            owner.use(request_close_then_fail, signal.SIGTERM)

        # Cleanup's post-close exception did not replace the primary, and the
        # owner invalidated its integer before the close/reuse boundary.
        os.fstat(raw_pidfd)
        owner.request_close()
        os.fstat(raw_pidfd)
    finally:
        os.close(raw_pidfd)
        os.close(source_fd)
        process.kill()
        process.wait(timeout=1.0)


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_pidfd_error_after_concurrent_reap_never_falls_back_to_numeric_pid(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    lifecycle = isolation.SynthProcessLifecycle()
    lifecycle.preclaim(process)
    pidfd = os.pidfd_open(process.pid, 0)
    identity = isolation._IdentityToken(isolation._OwnedPidfd.from_raw(pidfd))
    assert lifecycle.publish(process, identity) is True
    authority = lifecycle._authority
    assert authority is not None
    numeric_sends = []

    def reap_then_fail(fd, signum):
        assert fd == pidfd
        assert lifecycle._wait_direct_child(authority, 1.0) is True
        lifecycle._finish_reaped(authority)
        # The old numeric PID is now unfenced and may already identify a
        # foreign process. Returning OSError must fail closed.
        raise OSError(errno.EIO, "pidfd failure after reap")

    monkeypatch.setattr(isolation.signal, "pidfd_send_signal", reap_then_fail)
    monkeypatch.setattr(
        isolation.SynthProcessLifecycle,
        "_send_pid",
        staticmethod(lambda pid, signum: numeric_sends.append((pid, signum))),
    )

    lifecycle._send(authority, signal.SIGTERM)

    assert numeric_sends == []
    assert lifecycle.active is False
    with pytest.raises(OSError) as caught:
        os.fstat(pidfd)
    assert caught.value.errno == errno.EBADF


def test_waitpid_post_effect_failure_permanently_fences_numeric_pid_send(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    lifecycle = isolation.SynthProcessLifecycle()
    lifecycle.preclaim(process)
    identity = isolation._IdentityToken(None)
    assert lifecycle.publish(process, identity) is True
    authority = lifecycle._authority
    assert authority is not None
    real_waitpid = isolation.os.waitpid
    injected = False

    def reap_then_raise(pid, options):
        nonlocal injected
        if not injected:
            injected = True
            waited, _ = real_waitpid(pid, 0)
            assert waited == pid
            raise MemoryError("waitpid post-effect")
        return real_waitpid(pid, options)

    monkeypatch.setattr(isolation.os, "waitpid", reap_then_raise)
    with pytest.raises(MemoryError, match="waitpid post-effect"):
        lifecycle._wait_direct_child(authority, 1.0)

    assert authority.numeric_send_fenced is True
    assert authority.reaped is False
    numeric_sends = []
    monkeypatch.setattr(
        isolation.SynthProcessLifecycle,
        "_send_pid",
        staticmethod(lambda pid, signum: numeric_sends.append((pid, signum))),
    )
    lifecycle._send(authority, signal.SIGKILL)
    assert numeric_sends == []

    # A second wait observes that the direct child is already gone and closes
    # lifecycle authority without ever reopening the numeric PID path.
    assert lifecycle._wait_direct_child(authority, 0.1) is True
    assert authority.reaped is True
    assert authority.numeric_send_fenced is True
    lifecycle._finish_reaped(authority)
    assert lifecycle.active is False
    process.returncode = 0


def test_portable_send_without_reap_token_fails_closed_before_pid_reuse(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    lifecycle = isolation.SynthProcessLifecycle()
    lifecycle.preclaim(process)
    identity = isolation._IdentityToken(None)
    assert lifecycle.publish(process, identity) is True
    authority = lifecycle._authority
    assert authority is not None
    authority.reap.retired = True
    numeric_sends = []
    monkeypatch.setattr(
        isolation.SynthProcessLifecycle,
        "_send_pid",
        staticmethod(lambda pid, signum: numeric_sends.append((pid, signum))),
    )

    # A concurrent waiter owns the only PID-reuse fence. The sender must not
    # signal, even though process.pid still contains a numeric value.
    lifecycle._send(authority, signal.SIGTERM)
    assert numeric_sends == []

    waited, status = os.waitpid(process.pid, 0)
    assert waited == process.pid
    process.returncode = os.waitstatus_to_exitcode(status)
    authority.reaped = True
    lifecycle._finish_reaped(authority)

    # The token is consumed by reap; a later sender still cannot touch the now
    # reusable numeric PID.
    lifecycle._send(authority, signal.SIGKILL)
    assert numeric_sends == []
    assert lifecycle.active is False


def test_portable_sender_baseexception_keeps_reap_authority_recoverable(monkeypatch):
    import hark.tts_isolation as isolation

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    lifecycle = isolation.SynthProcessLifecycle()
    lifecycle.preclaim(process)
    identity = isolation._IdentityToken(None)
    assert lifecycle.publish(process, identity) is True
    authority = lifecycle._authority
    assert authority is not None
    real_send = isolation.SynthProcessLifecycle._send_pid

    def send_then_raise(pid, signum):
        os.kill(pid, signal.SIGCONT)
        raise MemoryError("numeric send primary")

    monkeypatch.setattr(
        isolation.SynthProcessLifecycle,
        "_send_pid",
        staticmethod(send_then_raise),
    )
    with pytest.raises(MemoryError, match="numeric send primary"):
        lifecycle._send(authority, signal.SIGCONT)

    # The durable authority was never popped. Borrow-marker cleanup ran in the
    # primary exception region, so a later terminalization can still signal and
    # reap the same live child.
    assert authority.reap.borrowed is False
    assert authority.reap.retired is False
    monkeypatch.setattr(
        isolation.SynthProcessLifecycle,
        "_send_pid",
        staticmethod(real_send),
    )
    assert lifecycle.cancel() is True
    assert lifecycle.active is False
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)


def test_transport_wait_cannot_reap_while_portable_sender_owns_pid_fence(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    lifecycle = isolation.SynthProcessLifecycle()
    lifecycle.preclaim(process)
    identity = isolation._IdentityToken(None)
    assert lifecycle.publish(process, identity) is True
    authority = lifecycle._authority
    assert authority is not None
    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)

    sender_entered = threading.Event()
    release_sender = threading.Event()
    waiter_started = threading.Event()
    numeric_sends = []
    outcomes = []

    def held_send(pid, signum):
        numeric_sends.append((pid, signum))
        sender_entered.set()
        assert release_sender.wait(timeout=1.0)

    monkeypatch.setattr(
        isolation.SynthProcessLifecycle,
        "_send_pid",
        staticmethod(held_send),
    )

    sender = threading.Thread(target=lifecycle._send, args=(authority, signal.SIGCONT))

    def wait_transport_path():
        waiter_started.set()
        outcomes.append(lifecycle.wait_and_release(process))

    waiter = threading.Thread(target=wait_transport_path)
    sender.start()
    assert sender_entered.wait(timeout=1.0)
    waiter.start()
    assert waiter_started.wait(timeout=1.0)
    time.sleep(0.05)

    # This is the only transport wait/reap path. It cannot consume the child
    # while the numeric sender holds the PID-reuse fence.
    assert waiter.is_alive()
    assert authority.reaped is False
    assert process.returncode is None

    release_sender.set()
    sender.join(timeout=1.0)
    waiter.join(timeout=1.0)
    assert not sender.is_alive()
    assert not waiter.is_alive()
    assert outcomes == [0]
    assert lifecycle.active is False

    # After reap, even a stale authority cannot signal the now-reusable PID.
    lifecycle._send(authority, signal.SIGKILL)
    assert numeric_sends == [(process.pid, signal.SIGCONT)]


@pytest.mark.skipif(not hasattr(os, "pidfd_open"), reason="Linux pidfd regression")
def test_cleanup_baseexception_retains_authority_until_retry_confirms_reap(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ]
    )
    lifecycle = isolation.SynthProcessLifecycle()
    lifecycle.preclaim(process)
    pidfd = os.pidfd_open(process.pid, 0)
    identity = isolation._IdentityToken(isolation._OwnedPidfd.from_raw(pidfd))
    assert lifecycle.publish(process, identity) is True
    real_wait = lifecycle._wait_direct_child
    monkeypatch.setattr(
        lifecycle,
        "_wait_direct_child",
        lambda authority, timeout: (_ for _ in ()).throw(MemoryError("cleanup")),
    )
    try:
        assert lifecycle.cancel() is False
        assert lifecycle.active is True
        os.fstat(pidfd)

        monkeypatch.setattr(lifecycle, "_wait_direct_child", real_wait)
        assert lifecycle.cancel() is True
        assert lifecycle.active is False
        with pytest.raises(OSError) as caught:
            os.fstat(pidfd)
        assert caught.value.errno == errno.EBADF
        with pytest.raises(ProcessLookupError):
            os.kill(process.pid, 0)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=1.0)


def test_preinit_popen_failure_releases_partial_authority_without_attribute_error(
    monkeypatch,
):
    import hark.tts_isolation as isolation

    lifecycle = isolation.SynthProcessLifecycle()
    process = subprocess.Popen.__new__(subprocess.Popen)

    def fail_before_initialization():
        raise MemoryError("before child creation")

    monkeypatch.setattr(lifecycle, "_before_popen_init", fail_before_initialization)
    with pytest.raises(MemoryError, match="before child creation"):
        lifecycle.spawn(process, [sys.executable, "-c", "pass"])

    assert getattr(process, "pid", None) is None
    assert getattr(process, "returncode", None) is None
    assert lifecycle.active is False
    assert lifecycle.cancel() is True
    lifecycle.release(process)


def test_popen_entry_without_pid_retains_uncertain_child_authority(monkeypatch):
    import hark.tts_isolation as isolation

    lifecycle = isolation.SynthProcessLifecycle()
    process = subprocess.Popen.__new__(subprocess.Popen)
    monkeypatch.setattr(
        isolation,
        "_SYNTH_POPEN_INIT",
        lambda self, *args, **kwargs: None,
    )

    with pytest.raises(isolation.SynthWorkerError, match="has no process id"):
        lifecycle.spawn(process, [sys.executable, "-c", "pass"])

    assert lifecycle.active is True
    authority = lifecycle._authority
    assert authority is not None
    assert authority.spawn_state is isolation._SpawnState.CHILD_CREATION_UNCERTAIN
    assert lifecycle.cancel() is False


def test_cancel_preinitialized_popen_records_no_child_without_dropping_authority():
    import hark.tts_isolation as isolation

    lifecycle = isolation.SynthProcessLifecycle()
    process = subprocess.Popen.__new__(subprocess.Popen)
    lifecycle.preclaim(process)

    assert lifecycle.cancel() is True
    assert lifecycle.active is True
    authority = lifecycle._authority
    assert authority is not None
    assert authority.spawn_state is isolation._SpawnState.CLAIMED_NOT_ENTERED

    lifecycle.release(process)
    assert lifecycle.active is True
    assert lifecycle.cancel() is True


@pytest.mark.skipif(
    not Path("/usr/bin/python3").exists(),
    reason="system Python pidfd gate unavailable",
)
def test_system_python_pidfd_authority_constructor_and_signal_gate(tmp_path):
    child = textwrap.dedent(
        """
        import os
        import signal
        import subprocess
        import sys

        import hark.tts_isolation as isolation

        assert hasattr(os, "pidfd_open")
        assert hasattr(signal, "pidfd_send_signal")
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        raw_pidfd = os.pidfd_open(process.pid, 0)
        authority = isolation._ProcessAuthority(process, False)
        assert authority.spawn_state is isolation._SpawnState.CHILD
        identity = isolation._IdentityToken(isolation._OwnedPidfd.from_raw(raw_pidfd))
        authority.identity = identity
        try:
            result = identity.pidfd.use(signal.pidfd_send_signal, signal.SIGTERM)
            assert result is not isolation._BORROW_BUSY
            process.wait(timeout=1.0)
            identity.request_close()
            try:
                os.fstat(raw_pidfd)
            except OSError:
                pass
            else:
                raise AssertionError("pidfd owner did not close descriptor")
            print("SYSTEM_PIDFD_GATE_OK", flush=True)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=1.0)
            identity.request_close()
        """
    )
    proc = subprocess.run(
        ["/usr/bin/python3", "-c", child],
        capture_output=True,
        text=True,
        timeout=3.0,
        env=_isolated_env(tmp_path),
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == "SYSTEM_PIDFD_GATE_OK\n"
    assert proc.stderr == ""


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

        real_init = isolation._SYNTH_POPEN_INIT
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
        isolation._SYNTH_POPEN_INIT = init_then_raise
        try:
            speech.run_tts(HarkConfig(), "hello", play=False, use_cache=False)
        except MemoryError as exc:
            print(f"PRIMARY {exc}", flush=True)
        finally:
            isolation._SYNTH_POPEN_INIT = real_init

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


@pytest.mark.parametrize("platform", ["darwin", "freebsd13"])
def test_true_portable_host_rejects_isolation_before_payload_spawn(
    monkeypatch,
    platform,
):
    import hark.tts_worker as worker

    popen_called = False

    def unexpected_popen(*args, **kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("payload Popen must not be entered without tree authority")

    monkeypatch.setattr(worker.sys, "platform", platform)
    monkeypatch.setattr(worker, "_PAYLOAD_POPEN_INIT", unexpected_popen)

    with pytest.raises(RuntimeError, match="exact descendant cleanup authority"):
        worker._supervise_payload([], subreaper=False)

    assert popen_called is False


def test_linux_without_subreaper_rejects_isolation_before_payload_spawn(monkeypatch):
    import hark.tts_worker as worker

    popen_called = False

    def unexpected_popen(*args, **kwargs):
        nonlocal popen_called
        popen_called = True
        raise AssertionError("payload Popen must not precede subreaper authority")

    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker, "_PAYLOAD_POPEN_INIT", unexpected_popen)

    with pytest.raises(RuntimeError, match="exact descendant cleanup authority"):
        worker._supervise_payload([], subreaper=False)

    assert popen_called is False


def test_persistent_subreaper_enumeration_failure_is_bounded_and_conservative(
    monkeypatch,
):
    import hark.tts_worker as worker

    payload = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    enumerations = 0

    def child_once_then_fail():
        nonlocal enumerations
        enumerations += 1
        if enumerations == 1:
            return {payload.pid}
        raise OSError("persistent procfs failure")

    monkeypatch.setattr(worker, "_linux_direct_children", child_once_then_fail)
    started = time.monotonic()
    try:
        # The exact unreaped payload remains signal-safe even after procfs
        # disappears. ECHILD then proves that this direct-only tree is gone.
        assert worker._cleanup_process_tree(payload, subreaper=True) is True
        assert time.monotonic() - started < 1.0
        with pytest.raises(ProcessLookupError):
            os.kill(payload.pid, 0)
    finally:
        if payload.poll() is None:
            _terminate(payload)


def test_unsupported_isolation_is_one_stable_structured_worker_failure(monkeypatch):
    import hark.tts_worker as worker

    messages = []
    monkeypatch.setattr(worker, "_install_parent_death_signal", lambda: False)
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    monkeypatch.setattr(worker, "_write_result", messages.append)
    monkeypatch.setattr(
        worker.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("unsupported isolation spawned a payload"),
    )

    assert worker.main([]) == 1
    assert messages == [
        {
            "status": "error",
            "kind": "exception",
            "type": "builtins.RuntimeError",
            "message": (
                "isolated TTS requires exact descendant cleanup authority on this host"
            ),
        }
    ]


@pytest.mark.parametrize(
    "failure_site",
    [
        "cdll_oserror",
        "pdeath_return",
        "pdeath_oserror",
        "subreaper_return",
        "subreaper_valueerror",
        "proc_oserror",
        "proc_valueerror",
    ],
)
def test_authority_acquisition_failures_share_one_pre_supervisor_contract(
    monkeypatch,
    failure_site,
):
    import hark.tts_worker as worker

    messages = []
    prctl_calls = 0

    class FakeLibc:
        def prctl(self, option, *args):
            nonlocal prctl_calls
            prctl_calls += 1
            if failure_site == "pdeath_oserror" and prctl_calls == 1:
                raise OSError("pdeath syscall unavailable")
            if failure_site == "subreaper_valueerror" and prctl_calls == 2:
                raise ValueError("subreaper argument rejected")
            if failure_site == "pdeath_return" and prctl_calls == 1:
                return -1
            if failure_site == "subreaper_return" and prctl_calls == 2:
                return -1
            return 0

    def fake_cdll(*args, **kwargs):
        if failure_site == "cdll_oserror":
            raise OSError("libc unavailable")
        return FakeLibc()

    def initial_children():
        if failure_site == "proc_oserror":
            raise OSError("procfs unavailable")
        if failure_site == "proc_valueerror":
            raise ValueError("malformed child list")
        return set()

    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.os, "getppid", lambda: 4242)
    monkeypatch.setattr(worker.ctypes, "CDLL", fake_cdll)
    monkeypatch.setattr(worker, "_linux_direct_children", initial_children)
    monkeypatch.setattr(worker, "_write_result", messages.append)
    monkeypatch.setattr(
        worker,
        "_supervise_payload",
        lambda *a, **k: pytest.fail("failed authority entered supervision"),
    )
    monkeypatch.setattr(
        worker,
        "_PAYLOAD_POPEN_INIT",
        lambda *a, **k: pytest.fail("failed authority spawned a payload"),
    )

    assert worker.main([]) == 1
    assert messages == [
        {
            "status": "error",
            "kind": "exception",
            "type": "builtins.RuntimeError",
            "message": (
                "isolated TTS requires exact descendant cleanup authority on this host"
            ),
        }
    ]


def test_successful_authority_acquisition_enters_supervisor_once(monkeypatch):
    import hark.tts_worker as worker

    supervisor_calls = []
    prctl_options = []
    procfs_probes = []

    class FakeLibc:
        def prctl(self, option, *args):
            prctl_options.append(option)
            return 0

    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(worker.os, "getppid", lambda: 4242)
    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *a, **k: FakeLibc())
    monkeypatch.setattr(
        worker,
        "_linux_direct_children",
        lambda: procfs_probes.append(True) or set(),
    )
    monkeypatch.setattr(
        worker,
        "_supervise_payload",
        lambda args, *, subreaper, result_handoff=None: (
            supervisor_calls.append((args, subreaper)) or 23
        ),
    )

    assert worker.main(["--test-success"]) == 23
    assert prctl_options == [worker._PR_SET_PDEATHSIG, worker._PR_SET_CHILD_SUBREAPER]
    assert procfs_probes == [True]
    assert supervisor_calls == [(["--test-success"], True)]


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("transfer", ["close", "fdopen"])
def test_pipe_transfer_post_return_trace_never_owns_reused_fd(
    monkeypatch,
    failure_type,
    transfer,
):
    import hark.tts_isolation as isolation

    read_fd, owned_fd = os.pipe()
    source_fd = os.open(os.devnull, os.O_RDONLY)
    source_identity = os.fstat(source_fd)
    guard = isolation._RawPipeFdGuard(owned_fd)
    primary = failure_type(f"{transfer} post-return primary")
    effect_returned = False
    adopted = None
    injected = False

    real_close = isolation._PIPE_CLOSE
    real_fdopen = isolation._PIPE_FDOPEN

    def close_then_return(fd, state):
        nonlocal effect_returned
        real_close(fd, state)
        effect_returned = True

    def adopt_then_return(fd, state, *args, **kwargs):
        nonlocal adopted, effect_returned
        adopted = real_fdopen(fd, state, *args, **kwargs)
        effect_returned = True
        return adopted

    target_code = (
        isolation._RawPipeFdGuard.close.__code__
        if transfer == "close"
        else isolation._RawPipeFdGuard.adopt.__code__
    )

    def trace(frame, event, arg):
        nonlocal injected
        if (
            frame.f_code is target_code
            and event in {"line", "return"}
            and effect_returned
            and not injected
        ):
            injected = True
            if adopted is not None:
                adopted.close()
            os.dup2(source_fd, owned_fd)
            raise primary
        return trace

    monkeypatch.setattr(
        isolation,
        "_PIPE_CLOSE" if transfer == "close" else "_PIPE_FDOPEN",
        close_then_return if transfer == "close" else adopt_then_return,
    )
    try:
        sys.settrace(trace)
        with pytest.raises(failure_type) as raised:
            if transfer == "close":
                guard.close()
            else:
                guard.adopt("rb", closefd=True)
        assert raised.value is primary
    finally:
        sys.settrace(None)

    try:
        assert guard.fd == -1
        guard.close_if_owned()
        reused_identity = os.fstat(owned_fd)
        assert (reused_identity.st_dev, reused_identity.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        )
    finally:
        for fd in (read_fd, owned_fd, source_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("transfer", ["close", "fdopen", "raw-close"])
def test_pipe_transfer_post_effect_never_reclaims_same_ofd_alias(
    monkeypatch,
    failure_type,
    transfer,
):
    import hark.tts_isolation as isolation

    read_fd, owned_fd = os.pipe()
    same_ofd_alias = os.dup(owned_fd)
    guard = isolation._RawPipeFdGuard(owned_fd)
    primary = failure_type(f"{transfer} same-ofd post-effect primary")
    real_close = isolation._PIPE_CLOSE
    real_fdopen = isolation._PIPE_FDOPEN
    real_raw_close = isolation._PIPE_RAW_CLOSE

    def close_reuse_then_raise(fd, state):
        real_close(fd, state)
        os.dup2(same_ofd_alias, fd)
        raise primary

    def fdopen_reuse_then_raise(fd, state, *args, **kwargs):
        adopted = real_fdopen(fd, state, *args, **kwargs)
        adopted.close()
        os.dup2(same_ofd_alias, fd)
        raise primary

    def raw_close_reuse_then_raise(fd, state):
        real_raw_close(fd, state)
        os.dup2(same_ofd_alias, fd)
        raise primary

    seam, replacement = {
        "close": ("_PIPE_CLOSE", close_reuse_then_raise),
        "fdopen": ("_PIPE_FDOPEN", fdopen_reuse_then_raise),
        "raw-close": ("_PIPE_RAW_CLOSE", raw_close_reuse_then_raise),
    }[transfer]
    monkeypatch.setattr(isolation, seam, replacement)

    try:
        with pytest.raises(failure_type) as raised:
            if transfer == "close":
                guard.close()
            elif transfer == "fdopen":
                guard.adopt("wb", closefd=True)
            else:
                guard.close_if_owned()
        assert raised.value is primary
        assert guard.fd == -1

        # The replacement has the same stat/resource identity as the original
        # descriptor. Explicit effect state, not identity inference, must keep
        # the guard disarmed and preserve this foreign alias.
        guard.close_if_owned()
        reused_identity = os.fstat(owned_fd)
        alias_identity = os.fstat(same_ofd_alias)
        assert (
            reused_identity.st_dev,
            reused_identity.st_ino,
            reused_identity.st_mode,
            reused_identity.st_rdev,
        ) == (
            alias_identity.st_dev,
            alias_identity.st_ino,
            alias_identity.st_mode,
            alias_identity.st_rdev,
        )
    finally:
        for fd in (read_fd, owned_fd, same_ofd_alias):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("transfer", ["close", "fdopen", "raw-close"])
def test_pipe_transfer_disarm_interruption_reclaims_for_retry(
    failure_type,
    transfer,
):
    import hark.tts_isolation as isolation

    read_fd, owned_fd = os.pipe()
    guard = isolation._RawPipeFdGuard(owned_fd)
    primary = failure_type(f"{transfer} disarm primary")
    target = {
        "close": isolation._RawPipeFdGuard.close,
        "fdopen": isolation._RawPipeFdGuard.adopt,
        "raw-close": isolation._RawPipeFdGuard.close_if_owned,
    }[transfer]
    injected = False

    def trace(frame, event, arg):
        nonlocal injected
        state = frame.f_locals.get("state")
        if (
            frame.f_code is target.__code__
            and event == "line"
            and not injected
            and guard.fd == -1
            and state is not None
            and not state.effect_started
        ):
            injected = True
            raise primary
        return trace

    try:
        sys.settrace(trace)
        with pytest.raises(failure_type) as raised:
            if transfer == "close":
                guard.close()
            elif transfer == "fdopen":
                guard.adopt("rb", closefd=True)
            else:
                guard.close_if_owned()
        assert raised.value is primary
    finally:
        sys.settrace(None)

    try:
        assert injected is True
        assert guard.fd == owned_fd
        os.fstat(owned_fd)
        guard.close_if_owned()
        with pytest.raises(OSError):
            os.fstat(owned_fd)
    finally:
        for fd in (read_fd, owned_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("phase", ["before", "at", "after"])
@pytest.mark.parametrize("transfer", ["close", "fdopen", "raw-close"])
def test_pipe_transfer_actual_effect_trace_boundary(
    monkeypatch,
    failure_type,
    phase,
    transfer,
):
    import hark.tts_isolation as isolation

    read_fd, owned_fd = os.pipe()
    source_fd = os.open(os.devnull, os.O_RDONLY)
    source_identity = os.fstat(source_fd)
    guard = isolation._RawPipeFdGuard(owned_fd)
    primary = failure_type(f"{transfer} {phase}-effect primary")
    target_code = isolation._run_fd_transfer.__code__
    real_os_close = isolation._OS_CLOSE
    real_fdopen = os.fdopen
    effect_called = False
    effect_complete = False
    injected = False
    adopted = None

    def close_effect(fd):
        nonlocal effect_called, effect_complete
        effect_called = True
        assert sys.gettrace() is None
        real_os_close(fd)
        if phase == "after":
            os.dup2(source_fd, fd)
        effect_complete = True

    def fdopen_effect(fd, *args, **kwargs):
        nonlocal effect_called, effect_complete
        effect_called = True
        assert sys.gettrace() is None
        result = real_fdopen(fd, *args, **kwargs)
        if phase == "after":
            result.close()
            os.dup2(source_fd, fd)
        effect_complete = True
        return result

    def trace(frame, event, arg):
        nonlocal injected
        if frame.f_code is not target_code:
            return trace
        frame.f_trace_opcodes = True
        state = frame.f_locals.get("state")
        if state is None or injected:
            return trace
        should_inject = (
            (phase == "before" and guard.fd == -1 and not state.effect_started)
            or (phase == "at" and state.effect_started and not effect_called)
            or (phase == "after" and state.effect_started and effect_complete)
        )
        if should_inject:
            injected = True
            raise primary
        return trace

    if transfer == "fdopen":
        monkeypatch.setattr(os, "fdopen", fdopen_effect)
    else:
        monkeypatch.setattr(isolation, "_OS_CLOSE", close_effect)

    try:
        sys.settrace(trace)
        if phase in {"before", "after"}:
            with pytest.raises(failure_type) as raised:
                if transfer == "close":
                    guard.close()
                elif transfer == "fdopen":
                    guard.adopt("rb", closefd=True)
                else:
                    guard.close_if_owned()
            assert raised.value is primary
        elif transfer == "close":
            guard.close()
        elif transfer == "fdopen":
            adopted = guard.adopt("rb", closefd=True)
        else:
            guard.close_if_owned()
    finally:
        sys.settrace(None)

    try:
        if phase == "before":
            assert injected is True
            assert effect_called is False
            assert guard.fd == owned_fd
            os.fstat(owned_fd)
            guard.close_if_owned()
        elif phase == "at":
            # No callback can run between the authoritative marker and the
            # actual effect. The operation therefore completes normally.
            assert injected is False
            assert effect_called is True
            assert effect_complete is True
            assert guard.fd == -1
            if adopted is not None:
                adopted.close()
            with pytest.raises(OSError):
                os.fstat(owned_fd)
        else:
            assert injected is True
            assert effect_complete is True
            assert guard.fd == -1
            guard.close_if_owned()
            reused_identity = os.fstat(owned_fd)
            assert (
                reused_identity.st_dev,
                reused_identity.st_ino,
                reused_identity.st_mode,
                reused_identity.st_rdev,
            ) == (
                source_identity.st_dev,
                source_identity.st_ino,
                source_identity.st_mode,
                source_identity.st_rdev,
            )
    finally:
        if adopted is not None:
            adopted.close()
        for fd in (read_fd, owned_fd, source_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("phase", ["before", "effect"])
@pytest.mark.parametrize("transfer", ["close", "fdopen", "raw-close"])
def test_pipe_transfer_actual_effect_sigint_boundary(
    monkeypatch,
    phase,
    transfer,
):
    import hark.tts_isolation as isolation

    if getattr(signal, "pthread_sigmask", None) is None:
        pytest.skip("requires pthread signal masking")

    read_fd, owned_fd = os.pipe()
    source_fd = os.open(os.devnull, os.O_RDONLY)
    source_identity = os.fstat(source_fd)
    guard = isolation._RawPipeFdGuard(owned_fd)
    real_os_close = isolation._OS_CLOSE
    real_fdopen = os.fdopen
    real_mask = signal.pthread_sigmask
    effect_called = False
    delivered = False

    def interrupt_before(*args, **kwargs):
        raise KeyboardInterrupt("SIGINT before transfer")

    def deliver_on_restore(how, mask):
        nonlocal delivered
        result = real_mask(how, mask)
        if effect_called and not delivered and how == signal.SIG_SETMASK:
            delivered = True
            raise KeyboardInterrupt("SIGINT delivered after transfer effect")
        return result

    def close_during_effect(fd):
        nonlocal effect_called
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert signal.SIGINT in blocked
        effect_called = True
        real_os_close(fd)
        os.dup2(source_fd, fd)

    def fdopen_during_effect(fd, *args, **kwargs):
        nonlocal effect_called
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert signal.SIGINT in blocked
        effect_called = True
        return real_fdopen(fd, *args, **kwargs)

    if phase == "before":
        seam = {
            "close": "_PIPE_CLOSE",
            "fdopen": "_PIPE_FDOPEN",
            "raw-close": "_PIPE_RAW_CLOSE",
        }[transfer]
        monkeypatch.setattr(isolation, seam, interrupt_before)
    else:
        monkeypatch.setattr(isolation, "_PTHREAD_SIGMASK", deliver_on_restore)
        if transfer == "fdopen":
            monkeypatch.setattr(os, "fdopen", fdopen_during_effect)
        else:
            monkeypatch.setattr(isolation, "_OS_CLOSE", close_during_effect)

    def transfer_once():
        if transfer == "close":
            guard.close()
        elif transfer == "fdopen":
            guard.adopt("rb", closefd=True)
        else:
            guard.close_if_owned()

    try:
        # An ambient handled exception must not be mistaken for this
        # transfer's primary and suppress the newly delivered SIGINT.
        try:
            raise LookupError("ambient exception context")
        except LookupError:
            with pytest.raises(KeyboardInterrupt):
                transfer_once()

        if phase == "before":
            assert effect_called is False
            assert delivered is False
            assert guard.fd == owned_fd
            os.fstat(owned_fd)
            monkeypatch.setattr(isolation, "_PIPE_CLOSE", isolation._pipe_close)
            monkeypatch.setattr(isolation, "_PIPE_FDOPEN", isolation._pipe_fdopen)
            monkeypatch.setattr(isolation, "_PIPE_RAW_CLOSE", isolation._pipe_close)
            guard.close_if_owned()
        else:
            assert effect_called is True
            assert delivered is True
            assert guard.fd == -1
            guard.close_if_owned()
            if transfer == "fdopen":
                # The pending signal interrupted publication of the adopted
                # file object. Transfer-state cleanup must close that object.
                with pytest.raises(OSError):
                    os.fstat(owned_fd)
                return
            reused_identity = os.fstat(owned_fd)
            assert (
                reused_identity.st_dev,
                reused_identity.st_ino,
                reused_identity.st_mode,
                reused_identity.st_rdev,
            ) == (
                source_identity.st_dev,
                source_identity.st_ino,
                source_identity.st_mode,
                source_identity.st_rdev,
            )
    finally:
        for fd in (read_fd, owned_fd, source_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, SystemExit, GeneratorExit])
def test_result_fd_adoption_post_return_trace_never_owns_reused_fd(
    monkeypatch,
    failure_type,
):
    import hark.tts_worker as worker

    read_fd, owned_fd = os.pipe()
    source_fd = os.open(os.devnull, os.O_RDONLY)
    source_identity = os.fstat(source_fd)
    guard = worker._RawResultFdGuard(owned_fd)
    primary = failure_type("result fd post-return primary")
    effect_returned = False
    adopted = None
    injected = False
    real_fdopen = worker._RESULT_FDOPEN

    def adopt_then_return(fd, state, *args, **kwargs):
        nonlocal adopted, effect_returned
        adopted = real_fdopen(fd, state, *args, **kwargs)
        effect_returned = True
        return adopted

    def trace(frame, event, arg):
        nonlocal injected
        if (
            frame.f_code is worker._RawResultFdGuard.adopt.__code__
            and event == "line"
            and effect_returned
            and not injected
        ):
            injected = True
            assert adopted is not None
            adopted.close()
            os.dup2(source_fd, owned_fd)
            raise primary
        return trace

    monkeypatch.setattr(worker, "_RESULT_FDOPEN", adopt_then_return)
    try:
        sys.settrace(trace)
        with pytest.raises(failure_type) as raised:
            guard.adopt()
        assert raised.value is primary
    finally:
        sys.settrace(None)

    try:
        assert guard._fd == -1
        guard.close_if_owned()
        reused_identity = os.fstat(owned_fd)
        assert (reused_identity.st_dev, reused_identity.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        )
    finally:
        for fd in (read_fd, owned_fd, source_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("owner_kind", ["pidfd", "pidfd-raw", "result"])
def test_owned_fd_close_suppresses_profile_c_call_before_kernel_effect(owner_kind):
    import hark.tts_isolation as isolation
    import hark.tts_worker as worker

    read_fd, owned_fd = os.pipe()
    attempted = False

    def profile(frame, event, arg):
        nonlocal attempted
        if event == "c_call" and arg is os.close:
            if frame.f_code in {
                isolation._run_fd_transfer.__code__,
                worker._run_fd_transfer.__code__,
            }:
                attempted = True
                raise MemoryError("profile entered fd close")
        return profile

    try:
        sys.setprofile(profile)
        if owner_kind == "pidfd":
            owner = isolation._OwnedPidfd.from_raw(owned_fd)
            owner.request_close()
            assert owner.fd is None
        elif owner_kind == "pidfd-raw":
            isolation._OwnedPidfd._close_raw(owned_fd)
        else:
            guard = worker._RawResultFdGuard(owned_fd)
            guard.close_if_owned()
            assert guard._fd == -1
        assert sys.getprofile() is profile
    finally:
        sys.setprofile(None)

    try:
        assert attempted is False
        with pytest.raises(OSError):
            os.fstat(owned_fd)
    finally:
        for fd in (owned_fd, read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_result_fd_adopt_suppresses_profile_c_call_before_fdopen():
    import hark.tts_worker as worker

    read_fd, owned_fd = os.pipe()
    guard = worker._RawResultFdGuard(owned_fd)
    attempted = False
    adopted = None

    def profile(frame, event, arg):
        nonlocal attempted
        if (
            frame.f_code is worker._run_fd_transfer.__code__
            and event == "c_call"
            and arg is os.fdopen
        ):
            attempted = True
            raise MemoryError("profile entered fdopen")
        return profile

    try:
        sys.setprofile(profile)
        adopted = guard.adopt()
        assert sys.getprofile() is profile
    finally:
        sys.setprofile(None)

    try:
        assert attempted is False
        assert guard.fd == -1
        assert adopted is not None
        adopted.close()
        with pytest.raises(OSError):
            os.fstat(owned_fd)
    finally:
        if adopted is not None:
            adopted.close()
        for fd in (owned_fd, read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_profile_restoration_after_fdopen_never_closes_reused_result_fd(
    monkeypatch,
):
    import hark.tts_worker as worker

    read_fd, owned_fd = os.pipe()
    source_fd = os.open(os.devnull, os.O_RDONLY)
    source_identity = os.fstat(source_fd)
    guard = worker._RawResultFdGuard(owned_fd)
    real_fdopen = os.fdopen
    effect_complete = False
    injected = False

    def fdopen_and_reuse(fd, *args, **kwargs):
        nonlocal effect_complete
        adopted = real_fdopen(fd, *args, **kwargs)
        adopted.close()
        os.dup2(source_fd, fd)
        effect_complete = True
        return adopted

    def profile(frame, event, arg):
        nonlocal injected
        del frame, event, arg
        if effect_complete and not injected:
            injected = True
            raise MemoryError("profile restoration after fdopen")
        return profile

    monkeypatch.setattr(worker.os, "fdopen", fdopen_and_reuse)
    try:
        sys.setprofile(profile)
        with pytest.raises(MemoryError, match="profile restoration after fdopen"):
            guard.adopt()
    finally:
        sys.setprofile(None)

    try:
        assert effect_complete is True
        assert injected is True
        assert guard.fd == -1
        guard.close_if_owned()
        reused_identity = os.fstat(owned_fd)
        assert (reused_identity.st_dev, reused_identity.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        )
    finally:
        for fd in (owned_fd, source_fd, read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("owner_kind", ["pidfd", "result"])
def test_owned_fd_close_retries_profile_disable_post_effect_failure(
    monkeypatch,
    owner_kind,
):
    import hark.tts_isolation as isolation
    import hark.tts_worker as worker

    read_fd, owned_fd = os.pipe()
    real_setprofile = sys.setprofile
    injected = False

    def profile(frame, event, arg):
        del frame, event, arg

    def disable_then_raise(callback):
        nonlocal injected
        real_setprofile(callback)
        if callback is None and not injected:
            injected = True
            raise MemoryError("profile disable post-effect primary")

    real_setprofile(profile)
    monkeypatch.setattr(sys, "setprofile", disable_then_raise)
    try:
        if owner_kind == "pidfd":
            owner = isolation._OwnedPidfd.from_raw(owned_fd)
            owner.request_close()
            assert owner.fd is None
        else:
            guard = worker._RawResultFdGuard(owned_fd)
            with pytest.raises(MemoryError, match="profile disable post-effect"):
                guard.close_if_owned()
            assert guard._fd == owned_fd
            os.fstat(owned_fd)
            monkeypatch.setattr(sys, "setprofile", real_setprofile)
            guard.close_if_owned()
            assert guard._fd == -1
        assert injected is True
        assert sys.getprofile() is profile
    finally:
        monkeypatch.setattr(sys, "setprofile", real_setprofile)
        real_setprofile(None)

    try:
        with pytest.raises(OSError):
            os.fstat(owned_fd)
    finally:
        for fd in (owned_fd, read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("owner_kind", ["pidfd", "result"])
def test_profile_restoration_failure_after_close_never_closes_reused_fd(
    monkeypatch,
    owner_kind,
):
    import hark.tts_isolation as isolation
    import hark.tts_worker as worker

    read_fd, owned_fd = os.pipe()
    source_fd = os.open(os.devnull, os.O_RDONLY)
    source_identity = os.fstat(source_fd)
    effect_complete = False
    injected = False
    real_close = os.close

    def close_and_reuse(fd):
        nonlocal effect_complete
        real_close(fd)
        os.dup2(source_fd, fd)
        effect_complete = True

    def profile(frame, event, arg):
        nonlocal injected
        del frame, event, arg
        if effect_complete and not injected:
            injected = True
            raise MemoryError("profile restoration post-effect primary")
        return profile

    if owner_kind == "pidfd":
        monkeypatch.setattr(isolation, "_OS_CLOSE", close_and_reuse)
        owner = isolation._OwnedPidfd.from_raw(owned_fd)
    else:
        monkeypatch.setattr(worker.os, "close", close_and_reuse)
        guard = worker._RawResultFdGuard(owned_fd)

    try:
        sys.setprofile(profile)
        if owner_kind == "pidfd":
            owner.request_close()
            assert owner.fd is None
        else:
            with pytest.raises(MemoryError, match="profile restoration post-effect"):
                guard.close_if_owned()
            assert guard._fd == -1
    finally:
        sys.setprofile(None)

    try:
        assert effect_complete is True
        assert injected is True
        reused_identity = os.fstat(owned_fd)
        assert (reused_identity.st_dev, reused_identity.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        )
    finally:
        for fd in (owned_fd, source_fd, read_fd):
            try:
                real_close(fd)
            except OSError:
                pass


@pytest.mark.skipif(
    getattr(signal, "pthread_sigmask", None) is None,
    reason="requires pthread signal masking",
)
@pytest.mark.parametrize("owner_kind", ["pidfd", "result"])
def test_pending_sigint_after_owned_fd_close_restores_profile_and_disarms(
    monkeypatch,
    owner_kind,
):
    import hark.tts_isolation as isolation
    import hark.tts_worker as worker

    read_fd, owned_fd = os.pipe()
    real_mask = signal.pthread_sigmask
    effect_complete = False
    delivered = False
    cleanup_close = os.close

    def profile(frame, event, arg):
        del frame, event, arg

    def deliver_on_restore(how, mask):
        nonlocal delivered
        result = real_mask(how, mask)
        if effect_complete and not delivered and how == signal.SIG_SETMASK:
            delivered = True
            raise KeyboardInterrupt("pending SIGINT after close")
        return result

    if owner_kind == "pidfd":
        real_close = isolation._OS_CLOSE

        def close_and_mark(fd):
            nonlocal effect_complete
            real_close(fd)
            effect_complete = True

        monkeypatch.setattr(isolation, "_OS_CLOSE", close_and_mark)
        monkeypatch.setattr(isolation, "_PTHREAD_SIGMASK", deliver_on_restore)
        owner = isolation._OwnedPidfd.from_raw(owned_fd)
    else:
        real_os_close = os.close

        def close_and_mark(fd):
            nonlocal effect_complete
            real_os_close(fd)
            effect_complete = True

        monkeypatch.setattr(worker.os, "close", close_and_mark)
        monkeypatch.setattr(worker, "_PTHREAD_SIGMASK", deliver_on_restore)
        guard = worker._RawResultFdGuard(owned_fd)

    try:
        sys.setprofile(profile)
        if owner_kind == "pidfd":
            owner.request_close()
            assert owner.fd is None
        else:
            with pytest.raises(KeyboardInterrupt, match="pending SIGINT after close"):
                guard.close_if_owned()
            assert guard._fd == -1
        assert sys.getprofile() is profile
    finally:
        sys.setprofile(None)

    try:
        assert effect_complete is True
        assert delivered is True
        with pytest.raises(OSError):
            os.fstat(owned_fd)
    finally:
        for fd in (owned_fd, read_fd):
            try:
                cleanup_close(fd)
            except OSError:
                pass


def test_successful_run_tts_propagates_delivered_sigint_from_mute_repair(tmp_path):
    child = textwrap.dedent(
        """
        import contextlib
        import os
        import signal
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
                return SimpleNamespace(
                    audio=b"audio",
                    provider="fake",
                    content_type="audio/mpeg",
                    voice=voice,
                )

        class State:
            skipped = False
            applied = False
            was_muted = False
            def as_meta(self):
                return {}

        speech.UsageStore = Store
        speech.resolve_tts = lambda *args, **kwargs: Synth()
        speech._synth_transport_factory = speech._in_process_synth_transport_factory
        speech.claim_tts_play_ticket = lambda: object()
        speech.exclusive_playback = lambda **kwargs: contextlib.nullcontext()
        speech.mic_muted_during_tts = (
            lambda **kwargs: contextlib.nullcontext(State())
        )
        speech.duck_media = lambda *args, **kwargs: contextlib.nullcontext(State())
        speech.play_wav_bytes = lambda *args, **kwargs: SimpleNamespace(duration_ms=1)
        conference.apply_conference_hold = lambda *args, **kwargs: State()

        def repair(**kwargs):
            print("REPAIR_SIGINT", flush=True)
            os.kill(os.getpid(), signal.SIGINT)
            raise AssertionError("delivered SIGINT returned")

        speech.repair_tts_mute_after_play = repair
        cfg = HarkConfig()
        cfg.audio.defer_tts_while_listening = False
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "success", play=True, use_cache=False
        )
        rc = cli.main(["providers"])
        print(f"RC {rc}", flush=True)
        raise SystemExit(rc)
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

    assert proc.returncode == 130
    assert proc.stdout == "REPAIR_SIGINT\nRC 130\n"
    assert proc.stderr == ""


def test_mute_repair_baseexception_cannot_replace_run_tts_primary(
    monkeypatch, tmp_path
):
    import hark.conference as conference
    import hark.speech as speech
    from hark.config import HarkConfig
    from hark.tts_interrupt_policy import TtsSynthesisInterrupted

    primary = MemoryError("synthesis primary")

    class Store:
        def record_tts(self, **kwargs):
            return None

    class Synth:
        def synthesize(self, text, *, voice):
            raise primary

    class Hold:
        skipped = False

        def as_meta(self):
            return {}

    def repair(**kwargs):
        raise TtsSynthesisInterrupted

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(speech, "UsageStore", Store)
    monkeypatch.setattr(speech, "resolve_tts", lambda *args, **kwargs: Synth())
    monkeypatch.setattr(
        speech,
        "_synth_transport_factory",
        speech._in_process_synth_transport_factory,
    )
    monkeypatch.setattr(speech, "claim_tts_play_ticket", lambda: object())
    monkeypatch.setattr(speech, "abandon_tts_play_ticket", lambda ticket: None)
    monkeypatch.setattr(speech, "repair_tts_mute_after_play", repair)
    monkeypatch.setattr(conference, "apply_conference_hold", lambda *a, **k: Hold())

    with pytest.raises(MemoryError) as raised:
        speech.run_tts(HarkConfig(), "failure", play=True, use_cache=False)

    assert raised.value is primary


def test_inner_payload_init_return_failure_reaps_exact_child_and_preserves_primary(
    monkeypatch,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    spawned_pid = None
    real_init = worker._PAYLOAD_POPEN_INIT

    def init_then_raise(self, *args, **kwargs):
        nonlocal spawned_pid
        real_init(self, *args, **kwargs)
        spawned_pid = self.pid
        raise MemoryError("inner wrapper after child creation")

    monkeypatch.setenv("HARK_TTS_RESULT_FD", str(write_fd))
    monkeypatch.setattr(worker, "_stage_payload_request", lambda request: None)
    monkeypatch.setattr(worker, "_PAYLOAD_POPEN_INIT", init_then_raise)
    try:
        with pytest.raises(MemoryError, match="inner wrapper after child creation"):
            worker._supervise_payload(["--test-hang"], subreaper=True)

        assert spawned_pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(spawned_pid, 0)
        assert os.read(read_fd, 1) == b""
    finally:
        for fd in (write_fd, read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_supervisor_retries_settle_boundary_fault_until_exact_child_is_reaped(
    monkeypatch,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    spawned_pid = None
    settle_calls = 0
    primary = MemoryError("inner wrapper primary")
    real_init = worker._PAYLOAD_POPEN_INIT
    real_settle = worker._settle_process_tree

    def init_then_raise(self, *args, **kwargs):
        nonlocal spawned_pid
        real_init(self, *args, **kwargs)
        spawned_pid = self.pid
        raise primary

    def interrupt_settle_once(*args, **kwargs):
        nonlocal settle_calls
        settle_calls += 1
        if settle_calls == 1:
            raise SystemExit("settle boundary secondary")
        return real_settle(*args, **kwargs)

    monkeypatch.setenv("HARK_TTS_RESULT_FD", str(write_fd))
    monkeypatch.setattr(worker, "_stage_payload_request", lambda request: None)
    monkeypatch.setattr(worker, "_PAYLOAD_POPEN_INIT", init_then_raise)
    monkeypatch.setattr(worker, "_settle_process_tree", interrupt_settle_once)
    try:
        with pytest.raises(MemoryError) as raised:
            worker._supervise_payload(["--test-hang"], subreaper=True)

        assert raised.value is primary
        assert settle_calls == 2
        assert spawned_pid is not None
        with pytest.raises(ProcessLookupError):
            os.kill(spawned_pid, 0)
        assert os.read(read_fd, 1) == b""
    finally:
        for fd in (write_fd, read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("body_failure", [False, True])
def test_supervisor_finalization_preserves_primary_and_cleans_after_selector_close(
    monkeypatch,
    body_failure,
):
    import hark.tts_worker as worker

    read_fd, write_fd = os.pipe()
    events: list[str] = []
    settled_payloads = []
    primary = MemoryError("forwarding primary")
    real_selector = worker.selectors.DefaultSelector
    real_settle = worker._settle_process_tree

    class CloseFaultSelector:
        def __init__(self):
            self._inner = real_selector()

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            self._inner.close()
            events.append("selector-close")
            raise SystemExit("selector close secondary")

    def track_settle(payload, **kwargs):
        events.append("settle")
        settled_payloads.append(payload)
        return real_settle(payload, **kwargs)

    def fail_forward(payload_result, forwarding_owner):
        forwarding_owner.close_if_owned()
        raise primary

    monkeypatch.setenv("HARK_TTS_RESULT_FD", str(write_fd))
    monkeypatch.setenv(
        "PYTHONPATH",
        str(ROOT / "src")
        + (
            os.pathsep + os.environ["PYTHONPATH"]
            if os.environ.get("PYTHONPATH")
            else ""
        ),
    )
    monkeypatch.setattr(worker, "_stage_payload_request", lambda request: None)
    monkeypatch.setattr(worker.selectors, "DefaultSelector", CloseFaultSelector)
    monkeypatch.setattr(worker, "_settle_process_tree", track_settle)
    if body_failure:
        monkeypatch.setattr(worker, "_forward_payload_result", fail_forward)

    try:
        if body_failure:
            with pytest.raises(MemoryError) as raised:
                worker._supervise_payload(["--test-success"], subreaper=True)
            assert raised.value is primary
        else:
            with pytest.raises(SystemExit, match="selector close secondary"):
                worker._supervise_payload(["--test-success"], subreaper=True)

        assert events[-2:] == ["selector-close", "settle"]
        assert settled_payloads[-1].returncode == 0
        assert os.read(read_fd, 1) in {b"", b"\x00"}
    finally:
        for fd in (write_fd, read_fd):
            try:
                os.close(fd)
            except OSError:
                pass


def test_settle_process_tree_retries_false_cleanup_until_proven(monkeypatch):
    import hark.tts_worker as worker

    outcomes = iter([False, False, True])
    calls = 0

    def cleanup(*args, **kwargs):
        nonlocal calls
        calls += 1
        return next(outcomes)

    monkeypatch.setattr(worker, "_cleanup_process_tree", cleanup)

    assert (
        worker._settle_process_tree(
            object(),
            subreaper=True,
            payload_pidfd=None,
        )
        is None
    )
    assert calls == 3


def test_public_interrupt_is_bounded_when_procfs_fails_after_payload_spawn(tmp_path):
    supervisor = textwrap.dedent(
        """
        import hark.tts_worker as worker

        subreaper = worker._claim_descendant_cleanup_authority()
        def unavailable():
            raise OSError("persistent procfs failure")
        worker._linux_direct_children = unavailable
        raise SystemExit(
            worker._supervise_payload(["--test-gil-hang"], subreaper=subreaper)
        )
        """
    )
    child = textwrap.dedent(
        f"""
        import sys

        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        class Store:
            def record_tts(self, **kwargs):
                return None

        speech.UsageStore = Store
        speech._synth_worker_command_factory = lambda: [
            sys.executable, "-c", {supervisor!r}
        ]
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "hung", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    payload_pidfd = None
    try:
        marker = _read_ready(proc)
        assert marker.startswith("GIL_READY ")
        payload_pid = int(marker.split()[1])
        payload_pidfd = _linux_pidfd_open(payload_pid)

        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.01)
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)

        assert proc.returncode == 130
        assert stdout == ""
        assert "Traceback" not in stderr
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(payload_pidfd, 0)
        assert unrelated.poll() is None
    finally:
        if payload_pidfd is not None:
            os.close(payload_pidfd)
        if proc.poll() is None:
            _terminate(proc)
        _terminate(unrelated)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper regression")
def test_procfs_loss_with_adopted_descendant_is_bounded_and_orphan_free(tmp_path):
    detached_helper = textwrap.dedent(
        """
        import os
        import signal
        import time
        if os.fork() > 0:
            while True:
                time.sleep(1)
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print(f"PROCFS_LOST_DESCENDANT {os.getpid()}", flush=True)
        while True:
            time.sleep(1)
        """
    )
    sitecustomize = textwrap.dedent(
        f"""
        import subprocess
        import sys
        import hark.providers.resolve as provider_resolve

        class DetachedProvider:
            def synthesize(self, text, *, voice):
                child = subprocess.Popen([sys.executable, "-c", {detached_helper!r}])
                child.wait()

        provider_resolve.resolve_tts = lambda *args, **kwargs: DetachedProvider()
        """
    )
    supervisor = textwrap.dedent(
        """
        import hark.tts_worker as worker

        subreaper = worker._claim_descendant_cleanup_authority()
        worker._linux_direct_children = lambda: (_ for _ in ()).throw(
            OSError("persistent procfs path failure")
        )
        raise SystemExit(worker._supervise_payload([], subreaper=subreaper))
        """
    )
    child = textwrap.dedent(
        f"""
        import os
        import sys
        from pathlib import Path

        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        site_dir = Path({str(tmp_path / "procfs-loss-site")!r})
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text(
            {sitecustomize!r}, encoding="utf-8"
        )
        os.environ["PYTHONPATH"] = str(site_dir) + os.pathsep + {str(ROOT / "src")!r}

        class Store:
            def record_tts(self, **kwargs):
                return None

        speech.UsageStore = Store
        speech._synth_worker_command_factory = lambda: [
            sys.executable, "-c", {supervisor!r}
        ]
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "procfs lost", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    descendant_pidfd = None
    try:
        marker = _read_ready(proc)
        assert marker.startswith("PROCFS_LOST_DESCENDANT ")
        descendant_pidfd = _linux_pidfd_open(int(marker.split()[1]))

        os.kill(proc.pid, signal.SIGINT)
        time.sleep(0.01)
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGINT)
        stdout, stderr = proc.communicate(timeout=2.0)

        assert proc.returncode == 130
        assert stdout == ""
        assert "Traceback" not in stderr
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(descendant_pidfd, 0)
        assert unrelated.poll() is None
    finally:
        if descendant_pidfd is not None:
            try:
                _linux_pidfd_send_signal(descendant_pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(descendant_pidfd)
        if proc.poll() is None:
            _terminate(proc)
        _terminate(unrelated)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper regression")
def test_already_exited_payload_still_cleans_adopted_descendant(tmp_path):
    detached_helper = textwrap.dedent(
        """
        import os
        import signal
        import time
        if os.fork() > 0:
            os._exit(0)
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print(f"EXITED_PAYLOAD_DESCENDANT {os.getpid()}", flush=True)
        while True:
            time.sleep(1)
        """
    )
    sitecustomize = textwrap.dedent(
        f"""
        import os
        import subprocess
        import sys
        import hark.providers.resolve as provider_resolve

        class ExitAfterForkProvider:
            def synthesize(self, text, *, voice):
                intermediate = subprocess.Popen(
                    [sys.executable, "-c", {detached_helper!r}]
                )
                intermediate.wait(timeout=2.0)
                os._exit(0)

        provider_resolve.resolve_tts = lambda *args, **kwargs: ExitAfterForkProvider()
        """
    )
    supervisor = textwrap.dedent(
        """
        import hark.tts_worker as worker
        raise SystemExit(worker.main())
        """
    )
    child = textwrap.dedent(
        f"""
        import os
        import sys
        from pathlib import Path

        import hark.cli as cli
        import hark.speech as speech
        from hark.config import HarkConfig

        site_dir = Path({str(tmp_path / "exited-payload-site")!r})
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "sitecustomize.py").write_text(
            {sitecustomize!r}, encoding="utf-8"
        )
        os.environ["PYTHONPATH"] = (
            str(site_dir) + os.pathsep + {str(ROOT / "src")!r}
        )

        class Store:
            def record_tts(self, **kwargs):
                return None

        speech.UsageStore = Store
        speech._synth_worker_command_factory = lambda: [
            sys.executable, "-c", {supervisor!r}
        ]
        cfg = HarkConfig()
        cli.load_config = lambda *args, **kwargs: cfg
        cli.dispatch = lambda args, loaded: speech.run_tts(
            loaded, "exit after fork", play=False, use_cache=False
        )
        raise SystemExit(cli.main(["providers"]))
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_isolated_env(tmp_path),
    )
    descendant_pidfd = None
    try:
        marker = _read_ready(proc)
        assert marker.startswith("EXITED_PAYLOAD_DESCENDANT ")
        descendant_pidfd = _linux_pidfd_open(int(marker.split()[1]))
        stdout, stderr = proc.communicate(timeout=3.0)

        assert proc.returncode != 0
        assert stdout == ""
        assert "Traceback" not in stderr
        with pytest.raises(ProcessLookupError):
            _linux_pidfd_send_signal(descendant_pidfd, 0)
    finally:
        if descendant_pidfd is not None:
            try:
                _linux_pidfd_send_signal(descendant_pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.close(descendant_pidfd)
        if proc.poll() is None:
            _terminate(proc)


@pytest.mark.parametrize("stage", ["pre-publication", "guard-published"])
@pytest.mark.parametrize(
    "failure_type", [MemoryError, KeyboardInterrupt, SystemExit, GeneratorExit]
)
def test_raw_pipe_claim_has_one_close_owner_under_fault_and_fd_reuse(
    monkeypatch,
    stage,
    failure_type,
):
    import hark.tts_isolation as isolation

    raw_fds = list(os.pipe())
    owned_fd = raw_fds[1]
    source_fd = os.dup(owned_fd)
    source_identity = os.fstat(source_fd)
    primary = failure_type(f"{stage} primary")
    close_calls = 0
    injected = False
    real_close = isolation._PIPE_RAW_CLOSE
    target_code = isolation._RawPipeFdGuard.claim.__func__.__code__

    def close_reuse_then_raise(fd, state):
        nonlocal close_calls
        close_calls += 1
        real_close(fd, state)
        os.dup2(source_fd, fd)
        raise RuntimeError("raw close secondary")

    def trace(frame, event, arg):
        nonlocal injected
        if frame.f_code is not target_code or injected:
            return trace
        guard = frame.f_locals.get("guard")
        slots = frame.f_locals.get("slots")
        index = frame.f_locals.get("index")
        if guard is None or slots is None or index is None:
            return trace
        at_stage = (
            stage == "pre-publication"
            and event == "line"
            and slots[index] == owned_fd
            and getattr(guard, "_fd", None) == owned_fd
            and not getattr(guard, "_committed", False)
        ) or (
            stage == "guard-published"
            and event == "return"
            and slots[index] is guard
            and getattr(guard, "_committed", False)
            and getattr(guard, "_fd", None) == owned_fd
        )
        if at_stage:
            injected = True
            raise primary
        return trace

    monkeypatch.setattr(isolation, "_PIPE_RAW_CLOSE", close_reuse_then_raise)
    raw_slot_cleaned = False
    try:
        sys.settrace(trace)
        with pytest.raises(failure_type) as raised:
            isolation._RawPipeFdGuard.claim(raw_fds, 1)
        assert raised.value is primary
    finally:
        sys.settrace(None)

    raw_owner = raw_fds[1]
    if isinstance(raw_owner, isolation._RawPipeFdGuard):
        raw_fds[1] = -1
        try:
            raw_owner.close_if_owned()
        except RuntimeError as exc:
            assert str(exc) == "raw close secondary"
        raw_slot_cleaned = True
    elif isinstance(raw_owner, int) and raw_owner >= 0:
        raw_fds[1] = -1
        try:
            isolation._PIPE_RAW_CLOSE(raw_owner, isolation._FdTransferState())
        except RuntimeError as exc:
            assert str(exc) == "raw close secondary"
        raw_slot_cleaned = True

    try:
        assert injected is True
        assert raw_fds[1] == -1
        assert close_calls == 1
        assert raw_slot_cleaned is True
        reused_identity = os.fstat(owned_fd)
        assert (reused_identity.st_dev, reused_identity.st_ino) == (
            source_identity.st_dev,
            source_identity.st_ino,
        )
    finally:
        for fd in (raw_fds[0], owned_fd, source_fd):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize(
    "failure_type", [MemoryError, KeyboardInterrupt, SystemExit, GeneratorExit]
)
def test_raw_pipe_claim_retains_owner_when_cleanup_close_fails_before_effect(
    monkeypatch,
    failure_type,
):
    import hark.tts_isolation as isolation

    raw_fds = list(os.pipe())
    owned_fd = raw_fds[1]
    primary = failure_type("publication primary")
    close_primary = OSError("raw close failed before effect")
    injected = False
    close_calls = 0
    real_close = isolation._PIPE_RAW_CLOSE
    target_code = isolation._RawPipeFdGuard.claim.__func__.__code__

    def fail_before_close(fd, state):
        nonlocal close_calls
        close_calls += 1
        raise close_primary

    def trace(frame, event, arg):
        nonlocal injected
        if frame.f_code is not target_code or injected:
            return trace
        guard = frame.f_locals.get("guard")
        slots = frame.f_locals.get("slots")
        index = frame.f_locals.get("index")
        if (
            guard is not None
            and slots is not None
            and index is not None
            and event == "return"
            and slots[index] is guard
            and guard._committed
            and guard._fd == owned_fd
        ):
            injected = True
            raise primary
        return trace

    monkeypatch.setattr(isolation, "_PIPE_RAW_CLOSE", fail_before_close)
    try:
        sys.settrace(trace)
        with pytest.raises(failure_type) as raised:
            isolation._RawPipeFdGuard.claim(raw_fds, 1)
        assert raised.value is primary
    finally:
        sys.settrace(None)

    owner = raw_fds[1]
    assert isinstance(owner, isolation._RawPipeFdGuard)
    assert owner.fd == owned_fd
    assert close_calls == 0
    os.fstat(owned_fd)

    raw_fds[1] = -1
    with pytest.raises(OSError) as raised:
        owner.close_if_owned()
    assert raised.value is close_primary
    assert close_calls == 1
    assert owner.fd == owned_fd
    monkeypatch.setattr(isolation, "_PIPE_RAW_CLOSE", real_close)
    owner.close_if_owned()
    with pytest.raises(OSError):
        os.fstat(owned_fd)
    os.close(raw_fds[0])


def test_supervisor_selector_setup_failure_transfers_one_result_owner(monkeypatch):
    import hark.tts_worker as worker

    result_read, result_write = os.pipe()
    foreign_fds: list[int] = []
    previous_term = signal.getsignal(signal.SIGTERM)
    real_write_to_owner = worker._write_result_to_owner

    class SelectorFailure(MemoryError):
        pass

    def fail_selector():
        raise SelectorFailure("selector construction primary")

    def write_then_reuse(message, owner):
        real_write_to_owner(message, owner)
        foreign_fds.extend(os.pipe())
        assert result_write in foreign_fds

    monkeypatch.setenv("HARK_TTS_RESULT_FD", str(result_write))
    monkeypatch.setattr(worker, "_claim_descendant_cleanup_authority", lambda: True)
    monkeypatch.setattr(
        worker,
        "_require_descendant_cleanup_authority",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(worker.selectors, "DefaultSelector", fail_selector)
    monkeypatch.setattr(worker, "_write_result_to_owner", write_then_reuse)

    try:
        assert worker.main(["--selector-failure"]) == 1
        assert signal.getsignal(signal.SIGTERM) is previous_term
        assert all(os.fstat(fd) for fd in foreign_fds)
        frame = os.read(result_read, 64 * 1024)
        size = struct.unpack("!I", frame[:4])[0]
        message = json.loads(frame[4 : 4 + size])
        assert message["status"] == "error"
        assert message["kind"] == "exception"
        assert message["type"].endswith(".<locals>.SelectorFailure")
        assert message["message"] == "selector construction primary"
        assert message["audio_size"] == 0
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        for fd in (result_read, *foreign_fds):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("callback_kind", ["trace", "profile"])
@pytest.mark.parametrize("stage", ["pre-publication", "post-publication"])
def test_result_owner_claim_fault_still_serializes_structured_primary(
    monkeypatch,
    callback_kind,
    stage,
):
    import hark.tts_worker as worker

    result_read, result_write = os.pipe()
    primary = MemoryError(f"{callback_kind} {stage} result claim primary")
    claim_code = worker._RawResultFdGuard.claim.__func__.__code__
    transfer_code = worker._run_fd_transfer.__code__
    injected = False
    previous_term = signal.getsignal(signal.SIGTERM)

    def claim_stage(frame, event):
        guard = frame.f_locals.get("guard")
        slots = frame.f_locals.get("slots")
        index = frame.f_locals.get("index")
        if guard is None or slots is None or index is None:
            return False
        if stage == "pre-publication":
            return (
                event == "line"
                and slots[index] == result_write
                and getattr(guard, "_fd", None) == result_write
                and not getattr(guard, "_committed", False)
            )
        return (
            event == "return"
            and slots[index] is guard
            and getattr(guard, "_fd", None) == result_write
            and getattr(guard, "_committed", False)
        )

    def trace(frame, event, arg):
        nonlocal injected
        del arg
        if not injected and frame.f_code is claim_code and claim_stage(frame, event):
            injected = True
            raise primary
        return trace

    def profile(frame, event, arg):
        nonlocal injected
        del arg
        if injected:
            return
        if stage == "pre-publication" and frame.f_code is transfer_code:
            caller = frame.f_back
            if caller is not None and caller.f_code is claim_code:
                injected = True
                raise primary
        if stage == "post-publication" and frame.f_code is claim_code:
            if claim_stage(frame, event):
                injected = True
                raise primary

    monkeypatch.setenv("HARK_TTS_RESULT_FD", str(result_write))
    monkeypatch.setattr(worker, "_claim_descendant_cleanup_authority", lambda: True)
    monkeypatch.setattr(
        worker,
        "_require_descendant_cleanup_authority",
        lambda **kwargs: None,
    )
    try:
        if callback_kind == "trace":
            sys.settrace(trace)
        else:
            sys.setprofile(profile)
        assert worker.main(["--claim-failure"]) == 1
    finally:
        sys.settrace(None)
        sys.setprofile(None)
        signal.signal(signal.SIGTERM, previous_term)

    try:
        assert injected is True
        frame = os.read(result_read, 64 * 1024)
        size = struct.unpack("!I", frame[:4])[0]
        message = json.loads(frame[4 : 4 + size])
        assert message["status"] == "error"
        assert message["kind"] == "exception"
        assert message["type"] == "builtins.MemoryError"
        assert message["message"] == str(primary)
        assert message["audio_size"] == 0
        assert signal.getsignal(signal.SIGTERM) is previous_term
    finally:
        for fd in (result_read, result_write):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.mark.parametrize("failure_phase", ["pre-effect", "post-effect"])
def test_supervisor_setup_retries_sigterm_restoration_and_preserves_primary(
    monkeypatch,
    failure_phase,
):
    import hark.tts_worker as worker

    previous_term = signal.getsignal(signal.SIGTERM)
    real_signal = signal.signal
    calls = 0
    restore_failure = MemoryError(f"SIGTERM restore {failure_phase}")

    def fail_restore_once(signum, handler):
        nonlocal calls
        calls += 1
        if calls == 2:
            if failure_phase == "post-effect":
                real_signal(signum, handler)
            raise restore_failure
        return real_signal(signum, handler)

    monkeypatch.setenv("HARK_TTS_RESULT_FD", "not-an-integer")
    monkeypatch.setattr(
        worker,
        "_require_descendant_cleanup_authority",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(worker.signal, "signal", fail_restore_once)
    try:
        with pytest.raises(ValueError):
            worker._supervise_payload([], subreaper=True)
        assert signal.getsignal(signal.SIGTERM) is previous_term
        assert calls == (3 if failure_phase == "pre-effect" else 2)
    finally:
        real_signal(signal.SIGTERM, previous_term)


@pytest.mark.parametrize("failure_phase", ["pre-effect", "post-effect"])
def test_direct_children_close_retains_authority_until_effect(
    monkeypatch,
    failure_phase,
):
    import hark.tts_worker as worker

    authority_fd, writer = os.pipe()
    os.close(writer)
    real_close = worker._DIRECT_CHILDREN_CLOSE
    primary = MemoryError(f"authority close {failure_phase}")
    calls = 0

    def fail_once(fd, state, retire):
        nonlocal calls
        calls += 1
        if calls == 1 and failure_phase == "pre-effect":
            raise primary
        real_close(fd, state, retire)
        if calls == 1 and failure_phase == "post-effect":
            raise primary

    monkeypatch.setattr(worker, "_DIRECT_CHILDREN_AUTHORITY_FD", authority_fd)
    monkeypatch.setattr(worker, "_DIRECT_CHILDREN_AUTHORITY_PID", os.getpid())
    monkeypatch.setattr(worker, "_DIRECT_CHILDREN_CLOSE", fail_once)

    with pytest.raises(MemoryError) as raised:
        worker._close_direct_children_authority()

    assert raised.value is primary
    assert calls == (2 if failure_phase == "pre-effect" else 1)
    assert worker._DIRECT_CHILDREN_AUTHORITY_FD == -1
    assert worker._DIRECT_CHILDREN_AUTHORITY_PID == -1
    with pytest.raises(OSError):
        os.fstat(authority_fd)


@pytest.mark.parametrize("failure_phase", ["pre-effect", "post-effect"])
def test_payload_pidfd_close_retains_slot_until_effect(monkeypatch, failure_phase):
    import hark.tts_worker as worker

    pidfd, writer = os.pipe()
    os.close(writer)
    owner = [pidfd]
    real_close = worker._PIDFD_CLOSE
    primary = MemoryError(f"pidfd close {failure_phase}")
    calls = 0

    def fail_once(fd, state, retire):
        nonlocal calls
        calls += 1
        if calls == 1 and failure_phase == "pre-effect":
            assert owner[0] == pidfd
            raise primary
        real_close(fd, state, retire)
        if calls == 1 and failure_phase == "post-effect":
            assert owner[0] is None
            raise primary

    monkeypatch.setattr(worker, "_PIDFD_CLOSE", fail_once)
    close_error = worker._close_pidfd_owner(owner)

    assert close_error is not None
    assert close_error[0] is primary
    assert calls == (2 if failure_phase == "pre-effect" else 1)
    assert owner == [None]
    with pytest.raises(OSError):
        os.fstat(pidfd)


@pytest.mark.parametrize("result_fd_text", [None, "not-an-integer"])
def test_supervisor_invalid_setup_always_restores_sigterm(
    monkeypatch,
    result_fd_text,
):
    import hark.tts_worker as worker

    previous_term = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(
        worker,
        "_require_descendant_cleanup_authority",
        lambda **kwargs: None,
    )
    if result_fd_text is None:
        monkeypatch.delenv("HARK_TTS_RESULT_FD", raising=False)
    else:
        monkeypatch.setenv("HARK_TTS_RESULT_FD", result_fd_text)

    with pytest.raises((RuntimeError, ValueError)):
        worker._supervise_payload([], subreaper=True)
    assert signal.getsignal(signal.SIGTERM) is previous_term


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="requires POSIX signal masks",
)
@pytest.mark.parametrize(
    "failure_mode",
    ["pending-sigint", "restore-failure", "pre-effect-failure"],
)
def test_main_authority_cleanup_preserves_structured_primary(
    monkeypatch,
    failure_mode,
):
    import hark.tts_worker as worker

    result_read, result_write = os.pipe()
    authority_read, authority_write = os.pipe()
    os.close(authority_write)
    primary = MemoryError("supervisor entry primary")
    real_close = os.close
    real_mask = signal.pthread_sigmask
    close_sent = False
    restore_calls = 0
    block_calls = 0

    def fail_entry():
        raise primary

    def close_with_pending_sigint(fd):
        nonlocal close_sent
        if fd == authority_read and failure_mode == "pending-sigint" and not close_sent:
            close_sent = True
            os.kill(os.getpid(), signal.SIGINT)
        return real_close(fd)

    def fail_first_restore(how, values):
        nonlocal block_calls, restore_calls
        if how == signal.SIG_BLOCK:
            block_calls += 1
            if failure_mode == "pre-effect-failure" and block_calls == 2:
                raise GeneratorExit("mask acquisition secondary")
        if how == signal.SIG_SETMASK:
            restore_calls += 1
            if failure_mode == "restore-failure" and restore_calls == 1:
                raise SystemExit("mask restoration secondary")
        return real_mask(how, values)

    monkeypatch.setenv("HARK_TTS_RESULT_FD", str(result_write))
    monkeypatch.setattr(worker, "_claim_descendant_cleanup_authority", fail_entry)
    monkeypatch.setattr(worker, "_DIRECT_CHILDREN_AUTHORITY_FD", authority_read)
    monkeypatch.setattr(worker, "_DIRECT_CHILDREN_AUTHORITY_PID", os.getpid())
    monkeypatch.setattr(worker, "_OS_CLOSE", close_with_pending_sigint)
    monkeypatch.setattr(worker, "_PTHREAD_SIGMASK", fail_first_restore)

    try:
        assert worker.main(["--cleanup-primary"]) == 1
        with pytest.raises(OSError):
            os.fstat(authority_read)
        frame = os.read(result_read, 64 * 1024)
        size = struct.unpack("!I", frame[:4])[0]
        message = json.loads(frame[4 : 4 + size])
        assert message["type"] == "builtins.MemoryError"
        assert message["message"] == "supervisor entry primary"
        assert message["audio_size"] == 0
        if failure_mode == "pending-sigint":
            assert close_sent is True
        elif failure_mode == "restore-failure":
            assert restore_calls == 2
        else:
            assert block_calls >= 4
    finally:
        for fd in (result_read, authority_read):
            try:
                real_close(fd)
            except OSError:
                pass


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="requires POSIX signal masks",
)
def test_pipe_creation_publishes_both_fds_before_delivered_sigint(monkeypatch):
    import hark.tts_isolation as isolation

    real_pipe = os.pipe
    opened: list[int] = []

    def pipe_then_sigint():
        fds = real_pipe()
        opened.extend(fds)
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert signal.SIGINT in blocked
        os.kill(os.getpid(), signal.SIGINT)
        return fds

    monkeypatch.setattr(isolation.os, "pipe", pipe_then_sigint)
    transport = isolation.SubprocessSynthTransport(object())
    previous_sigint = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            transport.synthesize(isolation.SynthRequest("p", "v", None, "x"))
    finally:
        signal.signal(signal.SIGINT, previous_sigint)

    assert len(opened) == 2
    for fd in opened:
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd regression")
@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="requires POSIX signal masks",
)
def test_outer_pidfd_is_owned_before_delivered_sigint(monkeypatch):
    import hark.tts_isolation as isolation

    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    opened: list[int] = []

    def pidfd_then_sigint(pid, flags=0):
        assert flags == 0
        fd = _linux_pidfd_open(pid)
        opened.append(fd)
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert signal.SIGINT in blocked
        os.kill(os.getpid(), signal.SIGINT)
        return fd

    monkeypatch.setattr(isolation.os, "pidfd_open", pidfd_then_sigint, raising=False)
    monkeypatch.setattr(
        isolation.signal,
        "pidfd_send_signal",
        lambda *args: None,
        raising=False,
    )
    previous_sigint = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            isolation.SubprocessSynthTransport._open_pidfd(process)
        assert len(opened) == 1
        with pytest.raises(OSError) as caught:
            os.fstat(opened[0])
        assert caught.value.errno == errno.EBADF
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=1.0)


@pytest.mark.skipif(sys.platform != "linux", reason="requires fork_exec")
def test_outer_popen_suppresses_profile_c_return_until_pid_publication():
    import hark.tts_isolation as isolation

    lifecycle = isolation.SynthProcessLifecycle()
    process = subprocess.Popen.__new__(subprocess.Popen)
    passed_read, passed_write = os.pipe()
    attempted = False

    def profile(frame, event, arg):
        nonlocal attempted
        del frame
        if event == "c_return" and getattr(arg, "__name__", "") == "fork_exec":
            attempted = True
            raise MemoryError("profile after supervisor fork_exec")

    try:
        sys.setprofile(profile)
        lifecycle.spawn(
            process,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            pass_fds=(passed_write,),
        )
        assert sys.getprofile() is profile
    finally:
        sys.setprofile(None)
        for fd in (passed_read, passed_write):
            os.close(fd)

    assert attempted is False
    assert isinstance(process.pid, int)
    assert lifecycle.cancel() is True
    assert lifecycle.active is False
    assert process.poll() is not None


@pytest.mark.skipif(sys.platform != "linux", reason="requires fork_exec")
def test_outer_popen_suppresses_opcode_trace_until_pid_publication():
    import dis

    import hark.tts_isolation as isolation

    lifecycle = isolation.SynthProcessLifecycle()
    process = subprocess.Popen.__new__(subprocess.Popen)
    passed_read, passed_write = os.pipe()
    instructions = {
        instruction.offset: instruction
        for instruction in dis.get_instructions(subprocess.Popen._execute_child)
    }
    assert any(
        instruction.opname == "STORE_ATTR" and instruction.argval == "pid"
        for instruction in instructions.values()
    )
    attempted = False

    def trace(frame, event, arg):
        nonlocal attempted
        del arg
        if frame.f_code is subprocess.Popen._execute_child.__code__:
            frame.f_trace_opcodes = True
            instruction = instructions.get(frame.f_lasti)
            if (
                event == "opcode"
                and instruction is not None
                and instruction.opname == "STORE_ATTR"
                and instruction.argval == "pid"
            ):
                attempted = True
                raise MemoryError("opcode before supervisor pid publication")
        return trace

    try:
        sys.settrace(trace)
        lifecycle.spawn(
            process,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            pass_fds=(passed_write,),
        )
        assert sys.gettrace() is trace
    finally:
        sys.settrace(None)
        for fd in (passed_read, passed_write):
            os.close(fd)

    assert attempted is False
    assert isinstance(process.pid, int)
    assert lifecycle.cancel() is True
    assert lifecycle.active is False
    assert process.poll() is not None


def test_outer_popen_restore_fault_retains_cancellable_pid(monkeypatch):
    import hark.tts_isolation as isolation

    lifecycle = isolation.SynthProcessLifecycle()
    process = subprocess.Popen.__new__(subprocess.Popen)
    passed_read, passed_write = os.pipe()
    primary = MemoryError("profile restoration after supervisor fork")
    real_restore = isolation._restore_runtime_callback
    real_init = isolation._SYNTH_POPEN_INIT
    armed = False

    def init_then_arm(process, *args, **kwargs):
        nonlocal armed
        armed = True
        real_init(process, *args, **kwargs)

    def restore_then_raise(setter, callback):
        real_restore(setter, callback)
        if armed and setter is sys.setprofile:
            raise primary

    monkeypatch.setattr(isolation, "_SYNTH_POPEN_INIT", init_then_arm)
    monkeypatch.setattr(isolation, "_restore_runtime_callback", restore_then_raise)
    try:
        sys.setprofile(lambda frame, event, arg: None)
        with pytest.raises(MemoryError) as raised:
            lifecycle.spawn(
                process,
                [sys.executable, "-c", "import time; time.sleep(30)"],
                pass_fds=(passed_write,),
            )
        assert raised.value is primary
    finally:
        sys.setprofile(None)
        for fd in (passed_read, passed_write):
            os.close(fd)

    assert isinstance(process.pid, int)
    assert lifecycle.cancel() is True
    assert lifecycle.active is False
    assert process.poll() is not None


@pytest.mark.skipif(sys.platform != "linux", reason="requires fork_exec")
def test_inner_popen_suppresses_profile_c_return_until_pid_publication(monkeypatch):
    import hark.tts_worker as worker

    result_read, result_write = os.pipe()
    request = struct.pack("!I", 2) + b"{}"
    attempted = False

    class Stdin:
        buffer = io.BytesIO(request)

    def profile(frame, event, arg):
        nonlocal attempted
        del frame
        if event == "c_return" and getattr(arg, "__name__", "") == "fork_exec":
            attempted = True
            raise MemoryError("profile after payload fork_exec")

    monkeypatch.setenv("HARK_TTS_RESULT_FD", str(result_write))
    monkeypatch.setattr(worker.sys, "stdin", Stdin())
    monkeypatch.setattr(
        worker,
        "_require_descendant_cleanup_authority",
        lambda **kwargs: None,
    )
    previous_term = signal.getsignal(signal.SIGTERM)
    try:
        sys.setprofile(profile)
        assert worker._supervise_payload(["--test-success"], subreaper=True) == 0
        assert sys.getprofile() is profile
    finally:
        sys.setprofile(None)
        signal.signal(signal.SIGTERM, previous_term)
        try:
            os.close(result_write)
        except OSError:
            pass

    assert attempted is False
    frame = os.read(result_read, 64 * 1024)
    os.close(result_read)
    size = struct.unpack("!I", frame[:4])[0]
    message = json.loads(frame[4 : 4 + size])
    assert message["status"] == "ok"


def test_inner_popen_restore_fault_reaps_published_payload(monkeypatch):
    import hark.tts_worker as worker

    result_read, result_write = os.pipe()
    request = struct.pack("!I", 2) + b"{}"
    primary = MemoryError("profile restoration after payload fork")
    real_restore = worker._restore_runtime_callback
    real_init = worker._PAYLOAD_POPEN_INIT
    payloads = []
    armed = False
    injected = False

    class Stdin:
        buffer = io.BytesIO(request)

    def init_then_arm(process, *args, **kwargs):
        nonlocal armed
        armed = True
        try:
            real_init(process, *args, **kwargs)
        finally:
            if isinstance(getattr(process, "pid", None), int):
                payloads.append(process)

    def restore_then_raise(setter, callback):
        nonlocal injected
        real_restore(setter, callback)
        if armed and not injected and setter is sys.setprofile:
            injected = True
            raise primary

    monkeypatch.setenv("HARK_TTS_RESULT_FD", str(result_write))
    monkeypatch.setattr(worker.sys, "stdin", Stdin())
    monkeypatch.setattr(
        worker,
        "_require_descendant_cleanup_authority",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(worker, "_PAYLOAD_POPEN_INIT", init_then_arm)
    monkeypatch.setattr(worker, "_restore_runtime_callback", restore_then_raise)
    previous_term = signal.getsignal(signal.SIGTERM)
    try:
        sys.setprofile(lambda frame, event, arg: None)
        with pytest.raises(MemoryError) as raised:
            worker._supervise_payload(["--test-hang"], subreaper=True)
        assert raised.value is primary
    finally:
        sys.setprofile(None)
        signal.signal(signal.SIGTERM, previous_term)
        for fd in (result_read, result_write):
            try:
                os.close(fd)
            except OSError:
                pass

    assert injected is True
    assert len(payloads) == 1
    assert isinstance(payloads[0].pid, int)
    assert payloads[0].poll() is not None


@pytest.mark.skipif(
    not hasattr(signal, "pthread_sigmask"),
    reason="requires POSIX signal masks",
)
def test_direct_children_open_publishes_before_delivered_sigint(monkeypatch):
    import hark.tts_worker as worker

    real_open = os.open
    opened: list[int] = []

    class Libc:
        @staticmethod
        def prctl(*args):
            return 0

    def open_then_sigint(path, flags):
        fd = real_open(path, flags)
        opened.append(fd)
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert signal.SIGINT in blocked
        os.kill(os.getpid(), signal.SIGINT)
        return fd

    monkeypatch.setattr(worker.ctypes, "CDLL", lambda *args, **kwargs: Libc())
    monkeypatch.setattr(worker, "_linux_direct_children", lambda: set())
    monkeypatch.setattr(worker.os, "open", open_then_sigint)
    monkeypatch.setattr(worker, "_DIRECT_CHILDREN_AUTHORITY_FD", -1)
    monkeypatch.setattr(worker, "_DIRECT_CHILDREN_AUTHORITY_PID", -1)
    previous_sigint = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            worker._install_parent_death_signal()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)

    assert len(opened) == 1
    assert worker._DIRECT_CHILDREN_AUTHORITY_FD == -1
    assert worker._DIRECT_CHILDREN_AUTHORITY_PID == -1
    with pytest.raises(OSError) as caught:
        os.fstat(opened[0])
    assert caught.value.errno == errno.EBADF

import argparse
import threading
import time

import hark.cli as cli
import hark.speak_then_listen.handoff as handoff_mod
import pytest
from hark.config import HarkConfig, load_config
from hark.exitcodes import OK
from hark.speech import ListenResult, speak_and_listen


def test_tts_listen_flag_chains_listen(monkeypatch, capsys):
    calls = {"speak": 0}

    def fake_speak(cfg, text, **kwargs):
        calls["speak"] += 1
        assert kwargs.get("provider") is None
        return (
            {
                "ok": True,
                "provider": "xai",
                "voice": "eve",
                "mic_muted": True,
            },
            ListenResult(
                text="option one",
                provider="xai",
                duration_ms=500,
                end_mode="silence",
                stream_id="stest",
            ),
        )

    monkeypatch.setattr("hark.speech.speak_and_listen", fake_speak)

    args = argparse.Namespace(
        text=["hello", "there"],
        provider=None,
        voice=None,
        no_play=False,
        out=None,
        json=True,
        listen=True,
        end_mode=None,
    )

    code = cli.cmd_tts(args, HarkConfig())
    assert code == OK
    assert calls["speak"] == 1
    out = capsys.readouterr().out
    assert "option one" in out
    assert "tts" in out


def test_tts_without_listen_skips_listen(monkeypatch, capsys):
    calls = {"listen": 0}

    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *a, **k: {"ok": True, "provider": "xai"},
    )

    def boom(*a, **k):
        calls["listen"] += 1
        raise AssertionError("listen should not run")

    monkeypatch.setattr("hark.speech.speak_and_listen", boom)
    monkeypatch.setattr("hark.speech.run_listen", boom)

    args = argparse.Namespace(
        text=["hi"],
        provider=None,
        voice=None,
        no_play=False,
        out=None,
        json=False,
        listen=False,
        end_mode=None,
    )

    assert cli.cmd_tts(args, HarkConfig()) == OK
    assert calls["listen"] == 0


def test_overlap_prearm_config_defaults_and_load(tmp_path):
    cfg = HarkConfig()
    assert cfg.audio.overlap_prearm is False
    assert cfg.audio.overlap_discard_ms == 150

    path = tmp_path / "config.toml"
    path.write_text(
        """
[audio]
overlap_prearm = true
overlap_discard_ms = 200
listen_pre_arm_ms = 250
""",
        encoding="utf-8",
    )
    loaded = load_config(path)
    assert loaded.audio.overlap_prearm is True
    assert loaded.audio.overlap_discard_ms == 200
    assert loaded.audio.listen_pre_arm_ms == 250


def test_half_duplex_default_listen_after_tts(monkeypatch):
    """Default: capture starts only after run_tts returns (no concurrent thread)."""
    order: list[str] = []
    cfg = HarkConfig()
    assert cfg.audio.overlap_prearm is False
    cfg.audio.listen_pre_arm_ms = 50

    def fake_tts(cfg, text, **kwargs):
        order.append("tts_start")
        on_near = kwargs.get("on_near_end")
        if on_near:
            on_near()  # signal only — must not start listen yet
            order.append("near_end")
        time.sleep(0.02)
        order.append("tts_done")
        return {"ok": True, "provider": "mock", "voice": "eve", "mic_muted": True}

    def fake_listen(cfg, **kwargs):
        order.append("listen")
        assert kwargs.get("already_armed") is True
        assert kwargs.get("audio_ok_after") is None
        return ListenResult(
            text="hello",
            provider="mock",
            duration_ms=100,
            end_mode="silence",
            stream_id="s1",
        )

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    tts_info, listened = speak_and_listen(cfg, "prompt?")
    assert tts_info["ok"]
    assert listened.text == "hello"
    assert order == ["tts_start", "near_end", "tts_done", "listen"]


def test_overlap_prearm_starts_listen_on_near_end(monkeypatch):
    """overlap_prearm: listen thread starts at near-end; discards until TTS ends."""
    order: list[str] = []
    order_lock = threading.Lock()
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    cfg.audio.overlap_discard_ms = 40

    listen_started = threading.Event()
    tts_may_finish = threading.Event()

    def fake_tts(cfg, text, **kwargs):
        with order_lock:
            order.append("tts_start")
        on_near = kwargs.get("on_near_end")
        assert on_near is not None
        with order_lock:
            order.append("near_end")
        on_near()  # starts overlap listen thread
        # Wait until listen worker has started (true overlap)
        assert listen_started.wait(timeout=2.0)
        with order_lock:
            order.append("tts_tail")
        tts_may_finish.set()
        time.sleep(0.03)
        with order_lock:
            order.append("tts_done")
        return {"ok": True, "provider": "mock", "voice": "eve", "mic_muted": True}

    def fake_listen(cfg, **kwargs):
        listen_started.set()
        with order_lock:
            order.append("listen_start")
        assert kwargs.get("already_armed") is True
        assert kwargs.get("post_tts_guard_s") == 0.0
        ok_after = kwargs.get("audio_ok_after")
        assert callable(ok_after)
        # While TTS still playing, audio is not yet OK
        assert ok_after() is None
        assert tts_may_finish.wait(timeout=2.0)
        # After speak_and_listen marks tts_done_at, deadline is set
        deadline = None
        for _ in range(50):
            deadline = ok_after()
            if deadline is not None:
                break
            time.sleep(0.01)
        assert deadline is not None
        # Deadline is ~discard_ms after TTS end
        assert deadline > time.monotonic() - 1.0
        with order_lock:
            order.append("listen_done")
        return ListenResult(
            text="overlapped",
            provider="mock",
            duration_ms=200,
            end_mode="silence",
            stream_id="s2",
        )

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    tts_info, listened = speak_and_listen(cfg, "prompt?")
    assert tts_info["ok"]
    assert listened.text == "overlapped"
    with order_lock:
        snap = list(order)
    assert snap.index("near_end") < snap.index("listen_start")
    assert snap.index("listen_start") < snap.index("tts_done")
    assert "listen_done" in snap


def test_overlap_prearm_waits_for_inflight_near_end_decision(monkeypatch):
    """Do not choose sequential listen while the timer callback is publishing."""
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80

    real_thread = threading.Thread
    callback_in_constructor = threading.Event()
    allow_callback_to_publish = threading.Event()
    call_returned = threading.Event()
    callback_threads: list[threading.Thread] = []
    listen_calls: list[dict[str, object]] = []
    result_box: dict[str, object] = {}

    def controlled_thread(*args, **kwargs):
        if kwargs.get("name") == "hark-overlap-listen":
            callback_in_constructor.set()
            assert allow_callback_to_publish.wait(timeout=2.0)
        return real_thread(*args, **kwargs)

    def fake_tts(cfg, text, **kwargs):
        on_near = kwargs.get("on_near_end")
        assert on_near is not None
        callback_thread = real_thread(target=on_near, name="fake-near-end-timer")
        callback_threads.append(callback_thread)
        callback_thread.start()
        assert callback_in_constructor.wait(timeout=2.0)
        return {"ok": True, "provider": "mock", "voice": "eve", "mic_muted": True}

    def fake_listen(cfg, **kwargs):
        listen_calls.append(dict(kwargs))
        return ListenResult(
            text="settled",
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            stream_id="settled-1",
        )

    def call_handoff() -> None:
        try:
            result_box["result"] = speak_and_listen(cfg, "prompt?")
        except BaseException as exc:  # noqa: BLE001 - surface from worker thread
            result_box["error"] = exc
        finally:
            call_returned.set()

    monkeypatch.setattr(threading, "Thread", controlled_thread)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    caller = real_thread(target=call_handoff, name="test-handoff-caller")
    caller.start()
    try:
        assert callback_in_constructor.wait(timeout=2.0)
        assert not call_returned.wait(timeout=0.1)
    finally:
        allow_callback_to_publish.set()
        caller.join(timeout=2.0)
        for callback_thread in callback_threads:
            callback_thread.join(timeout=2.0)

    assert not caller.is_alive()
    assert all(not callback_thread.is_alive() for callback_thread in callback_threads)
    assert "error" not in result_box
    assert len(listen_calls) == 1
    assert listen_calls[0]["already_armed"] is True
    assert callable(listen_calls[0]["audio_ok_after"])


def test_overlap_prearm_stalled_callback_is_cancelled_before_sequential(monkeypatch):
    """A callback stalled after publication cannot later start capture."""
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    real_thread = threading.Thread
    callback_in_constructor = threading.Event()
    allow_constructor = threading.Event()
    callback_done = threading.Event()
    overlap_capture = threading.Event()
    calls: list[str] = []

    def controlled_thread(*args, **kwargs):
        if kwargs.get("name") == "hark-overlap-listen":
            callback_in_constructor.set()
            assert allow_constructor.wait(timeout=2.0)
        return real_thread(*args, **kwargs)

    def fake_tts(cfg, text, **kwargs):
        callback = kwargs["on_near_end"]

        def invoke_callback():
            callback()
            callback_done.set()

        real_thread(target=invoke_callback, name="stalled-near-end").start()
        assert callback_in_constructor.wait(timeout=2.0)
        return {"ok": True, "provider": "mock", "voice": "eve"}

    def fake_listen(cfg, **kwargs):
        if kwargs.get("audio_ok_after") is not None:
            overlap_capture.set()
        calls.append("listen")
        return ListenResult(
            text="sequential",
            provider="mock",
            duration_ms=1,
            end_mode="silence",
            stream_id="bounded-sequential",
        )

    monkeypatch.setattr(threading, "Thread", controlled_thread)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    started = time.monotonic()
    _, listened = speak_and_listen(cfg, "prompt?")
    elapsed = time.monotonic() - started
    assert listened.text == "sequential"
    assert elapsed < 1.0
    assert calls == ["listen"]

    allow_constructor.set()
    assert callback_done.wait(timeout=2.0)
    assert not overlap_capture.wait(timeout=0.1)
    assert calls == ["listen"]


def test_overlap_prearm_started_thread_must_ack_target_entry(monkeypatch):
    """Thread.start success cannot deadlock before the target's first instruction."""
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    real_thread = threading.Thread
    target_delayed = threading.Event()
    allow_target_entry = threading.Event()
    call_returned = threading.Event()
    calls: list[str] = []
    result_box: dict[str, object] = {}
    worker_threads: list[threading.Thread] = []

    class DelayedTargetThread:
        def __init__(self, *, target, name, daemon):
            assert name == "hark-overlap-listen"
            assert daemon is True
            self._target = target

        def start(self) -> None:
            def delayed_bootstrap() -> None:
                target_delayed.set()
                assert allow_target_entry.wait(timeout=2.0)
                self._target()

            worker = real_thread(target=delayed_bootstrap, daemon=True)
            worker_threads.append(worker)
            worker.start()

    def fake_tts(cfg, text, **kwargs):
        kwargs["on_near_end"]()
        assert target_delayed.wait(timeout=2.0)
        return {"ok": True, "provider": "mock", "voice": "eve"}

    def fake_listen(cfg, **kwargs):
        overlap_call = kwargs.get("audio_ok_after") is not None
        calls.append("overlap" if overlap_call else "sequential")
        return ListenResult(
            text="overlap" if overlap_call else "sequential",
            provider="mock",
            duration_ms=1,
            end_mode="silence",
            stream_id="target-entry-ack",
        )

    def invoke() -> None:
        try:
            result_box["result"] = speak_and_listen(cfg, "prompt?")
        except BaseException as exc:  # noqa: BLE001 - surface from caller
            result_box["error"] = exc
        finally:
            call_returned.set()

    monkeypatch.setattr(threading, "Thread", DelayedTargetThread)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    caller = real_thread(target=invoke, name="target-entry-owner")
    caller.start()
    try:
        assert target_delayed.wait(timeout=2.0)
        assert call_returned.wait(timeout=0.8)
        assert "error" not in result_box
        assert calls == ["sequential"]
    finally:
        allow_target_entry.set()
        caller.join(timeout=2.0)
        for worker in worker_threads:
            worker.join(timeout=2.0)

    assert not caller.is_alive()
    assert calls == ["sequential"]


def test_overlap_prearm_settlement_interrupt_retries_cancellation(monkeypatch):
    """An interrupted ownership transition cannot permit post-return capture."""
    import hark.audio.capture as capture_mod

    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    real_thread = threading.Thread
    callback_in_registration = threading.Event()
    allow_registration = threading.Event()
    callback_done = threading.Event()
    late_capture = threading.Event()
    interruption = KeyboardInterrupt("cancel transition interrupted")
    cancel_calls = 0
    finish_calls = 0
    real_cancel = handoff_mod._OverlapAttempt.cancel
    real_finish = handoff_mod._OverlapAttempt.finish_cancelled_before_run
    real_register = capture_mod.register_capture_attempt

    def interrupt_first_cancel(owned):
        nonlocal cancel_calls
        cancel_calls += 1
        if cancel_calls == 1:
            raise interruption
        return real_cancel(owned)

    def interrupt_first_finish(owned):
        nonlocal finish_calls
        finish_calls += 1
        if finish_calls == 1:
            raise MemoryError("terminal transition interrupted")
        return real_finish(owned)

    def blocked_register():
        callback_in_registration.set()
        assert allow_registration.wait(timeout=2.0)
        return real_register()

    def fake_tts(cfg, text, **kwargs):
        callback = kwargs["on_near_end"]

        def invoke_callback() -> None:
            try:
                callback()
            finally:
                callback_done.set()

        real_thread(target=invoke_callback, daemon=True).start()
        assert callback_in_registration.wait(timeout=2.0)
        return {"ok": True, "provider": "mock", "voice": "eve"}

    def fake_listen(cfg, **kwargs):
        if kwargs.get("audio_ok_after") is not None:
            late_capture.set()
        return ListenResult(
            text="late",
            provider="mock",
            duration_ms=1,
            end_mode="silence",
            stream_id="interrupted-settlement",
        )

    monkeypatch.setattr(handoff_mod._OverlapAttempt, "cancel", interrupt_first_cancel)
    monkeypatch.setattr(
        handoff_mod._OverlapAttempt,
        "finish_cancelled_before_run",
        interrupt_first_finish,
    )
    monkeypatch.setattr(capture_mod, "register_capture_attempt", blocked_register)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    try:
        with pytest.raises(KeyboardInterrupt) as raised:
            speak_and_listen(cfg, "prompt?")
        assert raised.value is interruption
    finally:
        allow_registration.set()

    assert callback_done.wait(timeout=2.0)
    assert cancel_calls >= 2
    assert finish_calls >= 2
    assert not late_capture.wait(timeout=0.1)


def test_overlap_prearm_terminal_waits_for_capture_lease_release(monkeypatch):
    """Terminal acknowledgement follows, rather than precedes, lease release."""
    import hark.audio.capture as capture_mod

    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    release_entered = threading.Event()
    allow_release = threading.Event()
    call_returned = threading.Event()
    result_box: dict[str, object] = {}
    real_release = capture_mod.release_capture_attempt

    def blocked_release(state):
        release_entered.set()
        assert allow_release.wait(timeout=2.0)
        return real_release(state)

    def fake_tts(cfg, text, **kwargs):
        kwargs["on_near_end"]()
        return {"ok": True, "provider": "mock", "voice": "eve"}

    def fake_listen(cfg, **kwargs):
        return ListenResult(
            text="settled",
            provider="mock",
            duration_ms=1,
            end_mode="silence",
            stream_id="lease-release",
        )

    def invoke() -> None:
        try:
            result_box["result"] = speak_and_listen(cfg, "prompt?")
        except BaseException as exc:  # noqa: BLE001 - surface from caller
            result_box["error"] = exc
        finally:
            call_returned.set()

    monkeypatch.setattr(capture_mod, "release_capture_attempt", blocked_release)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    caller = threading.Thread(target=invoke, daemon=True)
    with capture_mod.capture_interrupt_signals():
        caller.start()
        try:
            assert release_entered.wait(timeout=2.0)
            assert capture_mod.capture_in_progress() is True
            assert not call_returned.wait(timeout=0.1)
        finally:
            allow_release.set()
            caller.join(timeout=2.0)
        assert capture_mod.capture_in_progress() is False

    assert not caller.is_alive()
    assert "error" not in result_box


def test_overlap_prearm_interrupted_lease_release_retries_before_terminal(monkeypatch):
    """A lease-release interruption is retained and cannot leak ownership."""
    import hark.audio.capture as capture_mod

    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    interruption = KeyboardInterrupt("lease release interrupted")
    release_calls = 0
    real_release = capture_mod.release_capture_attempt

    def interrupted_release(state):
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise interruption
        return real_release(state)

    def fake_tts(cfg, text, **kwargs):
        kwargs["on_near_end"]()
        return {"ok": True, "provider": "mock", "voice": "eve"}

    def fake_listen(cfg, **kwargs):
        return ListenResult(
            text="settled",
            provider="mock",
            duration_ms=1,
            end_mode="silence",
            stream_id="lease-interrupt",
        )

    monkeypatch.setattr(capture_mod, "release_capture_attempt", interrupted_release)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    with pytest.raises(KeyboardInterrupt) as raised:
        speak_and_listen(cfg, "prompt?")

    assert raised.value is interruption
    assert release_calls >= 2
    assert capture_mod.capture_in_progress() is False


def test_overlap_prearm_noncooperative_failure_is_bounded_and_owned(monkeypatch):
    """A running target cannot deadlock failure cleanup or lose ownership."""
    import hark.speak_then_listen.handoff as handoff_mod

    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    primary = RuntimeError("playback failed")
    listen_started = threading.Event()
    allow_terminal = threading.Event()
    listener_terminal = threading.Event()
    quarantine_calls = 0
    real_quarantine = handoff_mod._OverlapAttempt.quarantine

    def interrupt_first_quarantine(owned):
        nonlocal quarantine_calls
        quarantine_calls += 1
        if quarantine_calls == 1:
            raise KeyboardInterrupt("quarantine transition interrupted")
        return real_quarantine(owned)

    def fake_tts(cfg, text, **kwargs):
        kwargs["on_near_end"]()
        assert listen_started.wait(timeout=2.0)
        raise primary

    def fake_listen(cfg, **kwargs):
        listen_started.set()
        assert allow_terminal.wait(timeout=2.0)
        listener_terminal.set()
        return ListenResult(
            text="owned",
            provider="mock",
            duration_ms=1,
            end_mode="silence",
            stream_id="hostile-owned",
        )

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)
    monkeypatch.setattr(
        handoff_mod._OverlapAttempt, "quarantine", interrupt_first_quarantine
    )
    monkeypatch.setattr(handoff_mod, "_OVERLAP_CANCEL_JOIN_S", 0.05)

    quarantine_before = len(handoff_mod._QUARANTINED_ATTEMPTS)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as raised:
            speak_and_listen(cfg, "prompt?")
        assert raised.value is primary
        assert time.monotonic() - started < 1.0
        assert not listener_terminal.is_set()
        assert len(handoff_mod._QUARANTINED_ATTEMPTS) == quarantine_before + 1
    finally:
        allow_terminal.set()

    assert quarantine_calls >= 2
    assert listener_terminal.wait(timeout=2.0)
    for _ in range(100):
        if len(handoff_mod._QUARANTINED_ATTEMPTS) == quarantine_before:
            break
        time.sleep(0.01)
    assert len(handoff_mod._QUARANTINED_ATTEMPTS) == quarantine_before


def test_overlap_prearm_rejects_callback_after_handoff_returns(monkeypatch):
    """A cancelled timer callback cannot create capture after sequential handoff."""
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    delayed_callback: list[object] = []
    listen_calls = 0
    late_listen = threading.Event()

    def fake_tts(cfg, text, **kwargs):
        delayed_callback.append(kwargs["on_near_end"])
        return {"ok": True, "provider": "mock", "voice": "eve", "mic_muted": True}

    def fake_listen(cfg, **kwargs):
        nonlocal listen_calls
        listen_calls += 1
        if listen_calls > 1:
            late_listen.set()
        return ListenResult(
            text="sequential",
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            stream_id="sequential-1",
        )

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    _, listened = speak_and_listen(cfg, "prompt?")
    assert listened.text == "sequential"
    assert listen_calls == 1

    callback = delayed_callback[0]
    assert callable(callback)
    callback()
    assert not late_listen.wait(timeout=0.1)
    assert listen_calls == 1


def test_overlap_prearm_tts_failure_joins_worker_and_preserves_primary(monkeypatch):
    """An owned overlap worker settles without replacing the TTS failure."""
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    primary = RuntimeError("playback failed")
    listen_started = threading.Event()
    worker_settled = threading.Event()

    def fake_tts(cfg, text, **kwargs):
        callback = kwargs["on_near_end"]
        callback()
        assert listen_started.wait(timeout=2.0)
        raise primary

    def fake_listen(cfg, **kwargs):
        listen_started.set()
        audio_ok_after = kwargs["audio_ok_after"]
        for _ in range(100):
            if audio_ok_after() is not None:
                worker_settled.set()
                break
            time.sleep(0.01)
        raise ValueError("secondary listen failure")

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    with pytest.raises(RuntimeError) as raised:
        speak_and_listen(cfg, "prompt?")

    assert raised.value is primary
    assert worker_settled.is_set()


def test_overlap_prearm_owns_worker_when_start_raises_after_launch(monkeypatch):
    """A post-launch start failure cannot orphan the overlap capture."""
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = True
    cfg.audio.listen_pre_arm_ms = 80
    real_thread = threading.Thread
    start_failure = RuntimeError("start wrapper failed after launch")
    worker_started = threading.Event()
    allow_worker_to_finish = threading.Event()
    worker_finished = threading.Event()
    call_returned = threading.Event()
    result_box: dict[str, object] = {}

    class StartThenRaiseThread(real_thread):
        def start(self) -> None:
            super().start()
            raise start_failure

    def controlled_thread(*args, **kwargs):
        if kwargs.get("name") == "hark-overlap-listen":
            return StartThenRaiseThread(*args, **kwargs)
        return real_thread(*args, **kwargs)

    def fake_tts(cfg, text, **kwargs):
        kwargs["on_near_end"]()
        return {"ok": True, "provider": "mock", "voice": "eve", "mic_muted": True}

    def fake_listen(cfg, **kwargs):
        worker_started.set()
        assert allow_worker_to_finish.wait(timeout=2.0)
        worker_finished.set()
        return ListenResult(
            text="owned",
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            stream_id="owned-1",
        )

    def call_handoff() -> None:
        try:
            speak_and_listen(cfg, "prompt?")
        except BaseException as exc:  # noqa: BLE001 - surface from worker thread
            result_box["error"] = exc
        finally:
            call_returned.set()

    monkeypatch.setattr(threading, "Thread", controlled_thread)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    caller = real_thread(target=call_handoff, name="test-post-launch-start-failure")
    caller.start()
    try:
        assert worker_started.wait(timeout=2.0)
        assert not call_returned.wait(timeout=0.1)
    finally:
        allow_worker_to_finish.set()
        caller.join(timeout=2.0)

    assert not caller.is_alive()
    assert worker_finished.is_set()
    assert result_box["error"] is start_failure


def test_discard_leading_skips_echo_frames(monkeypatch):
    """capture_utterance drops leading frames for discard_leading_ms / audio_ok_after."""
    from types import SimpleNamespace

    import numpy as np

    from hark.audio import capture as cap_mod

    class FakeStream:
        def __init__(self):
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, block):
            self.reads += 1
            # Real-time-ish: discard uses wall clock (~20 ms blocks)
            time.sleep(0.02)
            # reads 1–5 (~100 ms) discarded loud echo; then speech; then silence
            if self.reads <= 5:
                samples = np.full(block, 0.5, dtype=np.float32)
            elif self.reads <= 20:
                samples = np.full(block, 0.4, dtype=np.float32)
            else:
                samples = np.zeros(block, dtype=np.float32)
            return samples.reshape(-1, 1), False

    fake = FakeStream()
    monkeypatch.setattr(cap_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        cap_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **kw: fake),
    )

    t0 = time.monotonic()
    # Fixed discard: ~100ms = 5 blocks of 20ms
    result = cap_mod.capture_utterance(
        max_s=2.0,
        end_silence_s=0.1,
        min_speech_s=0.05,
        open_confirm_blocks=2,
        initial_timeout_s=2.0,
        discard_leading_ms=100,
        post_tts_guard_s=0,
    )
    assert result.duration_ms > 0
    # Stream opened and discarded some frames before gate
    assert fake.reads > 5
    assert time.monotonic() - t0 < 5.0


def test_audio_ok_after_none_holds_discard(monkeypatch):
    from types import SimpleNamespace

    import numpy as np

    from hark.audio import capture as cap_mod

    class FakeStream:
        def __init__(self):
            self.reads = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, block):
            self.reads += 1
            samples = np.zeros(block, dtype=np.float32)
            return samples.reshape(-1, 1), False

    fake = FakeStream()
    monkeypatch.setattr(cap_mod, "_require_sd", lambda: None)
    monkeypatch.setattr(
        cap_mod,
        "sd",
        SimpleNamespace(InputStream=lambda **kw: fake),
    )

    release_at = time.monotonic() + 0.08
    state = {"done": None}

    def ok_after():
        if time.monotonic() < release_at:
            return None
        if state["done"] is None:
            state["done"] = time.monotonic()
        return state["done"]  # deadline = now → stop discarding immediately

    # Will hang in discard until ok_after returns a past/now deadline, then
    # timeout on no speech (zeros). That's fine — we only assert discard held.
    try:
        cap_mod.capture_utterance(
            max_s=0.5,
            end_silence_s=0.05,
            min_speech_s=0.02,
            initial_timeout_s=0.15,
            audio_ok_after=ok_after,
            post_tts_guard_s=0,
        )
    except TimeoutError:
        pass
    assert fake.reads >= 3  # discarded frames while ok_after was None

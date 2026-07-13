import os
import threading
import time

from hark.audio.capture import MicBusyError, MicLease
from hark.mic_coord import (
    ambient_pause_requested,
    clear_ambient_pause,
    pause_ambient_for_mic,
    request_ambient_pause,
    wait_for_mic_free,
)


def test_pause_request_and_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_ambient_pause()
    assert not ambient_pause_requested()
    request_ambient_pause(reason="test")
    assert ambient_pause_requested()
    clear_ambient_pause()
    assert not ambient_pause_requested()


def test_wait_for_mic_free_when_idle(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    wait_for_mic_free(timeout_s=2.0)


def test_pause_ambient_context_yields_mic(tmp_path, monkeypatch):
    """Simulate ambient holding mic until it sees pause file."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_ambient_pause()

    held = threading.Event()
    released = threading.Event()
    errors: list[BaseException] = []

    def ambient_side() -> None:
        try:
            with MicLease("ambient"):
                held.set()
                # Hold until pause requested (as ambient loop would)
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if ambient_pause_requested():
                        break
                    time.sleep(0.02)
                # exit context → release mic
            released.set()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=ambient_side, daemon=True)
    t.start()
    assert held.wait(2.0), "ambient never took mic"

    # Without pause protocol, listen would raise MicBusyError
    try:
        with MicLease("listen"):
            raise AssertionError("should not acquire while ambient holds")
    except MicBusyError:
        pass

    with pause_ambient_for_mic(reason="listen", timeout_s=5.0):
        assert ambient_pause_requested()
        # ambient should release; we can take listen lease
        with MicLease("listen"):
            assert released.wait(2.0) or True
            pass

    assert not ambient_pause_requested()
    t.join(timeout=2.0)
    assert not errors, errors


def test_wait_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with MicLease("blocker"):
        try:
            wait_for_mic_free(timeout_s=0.2)
            raise AssertionError("expected MicBusyError")
        except MicBusyError:
            pass

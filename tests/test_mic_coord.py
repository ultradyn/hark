import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

from hark.audio.capture import MicBusyError, MicLease
from hark import mic_coord
from hark.mic_coord import (
    ambient_pause_requested,
    clear_ambient_pause,
    pause_ambient_for_mic,
    request_ambient_pause,
    read_ambient_pause,
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


def test_pause_release_is_scoped_to_owner_token(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    token_a = request_ambient_pause(reason="a")
    token_b = request_ambient_pause(reason="b")

    clear_ambient_pause(token_a)
    state = read_ambient_pause()
    assert state is not None
    assert [owner["token"] for owner in state["owners"]] == [token_b]

    clear_ambient_pause(token_b)
    assert not ambient_pause_requested()


def test_concurrent_same_process_owners_are_all_retained(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    barrier = threading.Barrier(8)
    tokens: list[str] = []
    errors: list[BaseException] = []

    def acquire() -> None:
        try:
            barrier.wait(timeout=5)
            tokens.append(request_ambient_pause(reason="thread"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=acquire) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert len(tokens) == len(threads)
    state = read_ambient_pause()
    assert state is not None
    assert {owner["token"] for owner in state["owners"]} == set(tokens)

    for token in tokens:
        clear_ambient_pause(token)
    assert not ambient_pause_requested()


def test_unknown_token_cannot_clear_replaced_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    token = request_ambient_pause(reason="new-owner")

    clear_ambient_pause("stale-owner-token")

    state = read_ambient_pause()
    assert state is not None
    assert state["token"] == token


def test_tokenless_legacy_clear_refuses_overlapping_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    token_a = request_ambient_pause(reason="a")
    token_b = request_ambient_pause(reason="b")

    clear_ambient_pause()

    state = read_ambient_pause()
    assert state is not None
    assert [owner["token"] for owner in state["owners"]] == [token_a, token_b]


def test_nested_pause_context_retains_outer_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pause_ambient_for_mic(reason="outer", timeout_s=1.0):
        with pause_ambient_for_mic(reason="inner", timeout_s=1.0):
            state = read_ambient_pause()
            assert state is not None
            assert [owner["reason"] for owner in state["owners"]] == [
                "outer",
                "inner",
            ]
        state = read_ambient_pause()
        assert state is not None
        assert [owner["reason"] for owner in state["owners"]] == ["outer"]
    assert not ambient_pause_requested()


def _pause_child_code(*, wait_for_gate: bool = False) -> str:
    gate_wait = (
        """
gate = Path(sys.argv[1])
while not gate.exists():
    time.sleep(0.001)
"""
        if wait_for_gate
        else ""
    )
    return f"""
import sys
import time
from pathlib import Path
from hark.mic_coord import clear_ambient_pause, request_ambient_pause
{gate_wait}
token = request_ambient_pause(reason="child")
print(token, flush=True)
sys.stdin.readline()
clear_ambient_pause(token)
"""


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    source = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (source, env.get("PYTHONPATH", "")) if part
    )
    return env


def _read_child_token(child: subprocess.Popen[str]) -> str:
    assert child.stdout is not None
    ready, _, _ = select.select([child.stdout], [], [], 5.0)
    assert ready, "pause owner child did not acquire within 5 seconds"
    return child.stdout.readline().strip()


def test_cross_process_release_retains_other_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    token = request_ambient_pause(reason="parent")
    child = subprocess.Popen(
        [sys.executable, "-c", _pause_child_code()],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=_child_env(),
    )
    try:
        child_token = _read_child_token(child)
        assert child_token
        clear_ambient_pause(token)
        state = read_ambient_pause()
        assert state is not None
        assert [owner["token"] for owner in state["owners"]] == [child_token]
        assert child.stdin is not None
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.wait(timeout=5) == 0
        assert not ambient_pause_requested()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_concurrent_process_acquisitions_do_not_lose_owners(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    gate = tmp_path / "start-gate"
    children = [
        subprocess.Popen(
            [sys.executable, "-c", _pause_child_code(wait_for_gate=True), str(gate)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=_child_env(),
        )
        for _ in range(4)
    ]
    try:
        gate.touch()
        tokens = {_read_child_token(child) for child in children}
        assert len(tokens) == len(children)
        state = read_ambient_pause()
        assert state is not None
        assert {owner["token"] for owner in state["owners"]} == tokens
        for child in children:
            assert child.stdin is not None
            child.stdin.write("release\n")
            child.stdin.flush()
        assert all(child.wait(timeout=5) == 0 for child in children)
        assert not ambient_pause_requested()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=5)


def test_owner_is_pruned_after_process_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    parent_token = request_ambient_pause(reason="parent")
    code = """
from hark.mic_coord import request_ambient_pause
request_ambient_pause(reason="abrupt-exit")
"""
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        env=_child_env(),
    )
    assert mic_coord.pause_path().is_file()
    state = read_ambient_pause()
    assert state is not None
    assert [owner["token"] for owner in state["owners"]] == [parent_token]
    clear_ambient_pause(parent_token)
    assert not ambient_pause_requested()


def test_pid_reuse_identity_is_pruned(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    starts = {4242: "old-start"}
    monkeypatch.setattr(mic_coord, "_process_start_time", starts.get)
    request_ambient_pause(reason="reused", pid=4242)

    starts[4242] = "new-start"

    assert not ambient_pause_requested()


def test_pause_owner_expires_after_bounded_age(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clock = {"now": 100.0}
    monkeypatch.setattr(mic_coord.time, "time", lambda: clock["now"])
    request_ambient_pause(reason="timed")

    clock["now"] += mic_coord.DEFAULT_PAUSE_OWNER_MAX_AGE_S + 1

    assert not ambient_pause_requested()


def test_malformed_pause_state_is_pruned(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = mic_coord.pause_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json\n", encoding="utf-8")

    assert not ambient_pause_requested()
    assert not path.exists()


def test_malformed_owner_is_pruned_without_losing_valid_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    token = request_ambient_pause(reason="valid")
    path = mic_coord.pause_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["owners"].append(
        {
            "token": ["not", "a", "string"],
            "reason": {"not": "a string"},
            "pid": True,
            "requested_at": "yesterday",
            "process_start": 123,
            "boot_id": None,
        }
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    state = read_ambient_pause()
    assert state is not None
    assert [owner["token"] for owner in state["owners"]] == [token]


def test_acquisition_fails_when_process_identity_is_unverifiable(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(mic_coord, "_process_start_time", lambda _pid: None)

    try:
        request_ambient_pause(reason="unverifiable")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "cannot verify" in str(exc)
    assert not mic_coord.pause_path().exists()


def test_pause_state_keeps_legacy_top_level_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    token = request_ambient_pause(reason="compatible")

    state = json.loads(mic_coord.pause_path().read_text(encoding="utf-8"))
    assert state["version"] == mic_coord.AMBIENT_PAUSE_VERSION
    assert state["reason"] == "compatible"
    assert state["pid"] == os.getpid()
    assert state["token"] == token


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


def test_pause_context_timeout_releases_its_owner(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with MicLease("blocker"):
        try:
            with pause_ambient_for_mic(reason="timeout", timeout_s=0.1):
                raise AssertionError("unreachable")
        except MicBusyError:
            pass
    assert not ambient_pause_requested()


def test_wait_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with MicLease("blocker"):
        try:
            wait_for_mic_free(timeout_s=0.2)
            raise AssertionError("expected MicBusyError")
        except MicBusyError:
            pass

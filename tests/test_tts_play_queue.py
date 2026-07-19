"""B092: exclusive playback lock + pipelined synth. B099: abandoned ticket heal."""

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

import hark.speech as speech_mod
from hark.audio import playback as pb
from hark.config import HarkConfig
from hark.exitcodes import TIMEOUT
from hark.providers.base import ProviderError
from hark.speech import run_tts


def _queue_paths(tmp_path, monkeypatch):
    lock = tmp_path / "tts_play.lock"
    queue = tmp_path / "tts_play_queue.json"
    monkeypatch.setattr(pb, "tts_play_lock_path", lambda: lock)
    monkeypatch.setattr(pb, "tts_play_queue_path", lambda: queue)
    return lock, queue


def _flock_holder(lock, *, env):
    code = """
import fcntl
import os
import sys

fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
print("READY", flush=True)
sys.stdin.read()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(lock)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "READY"
    return proc


def _patch_run_tts_dependencies(monkeypatch):
    class FakeTts:
        def synthesize(self, text, voice=None):
            return SimpleNamespace(
                audio=b"ID3fake",
                provider="fake",
                content_type="audio/mpeg",
                voice=voice or "eve",
            )

    class FakeUsage:
        def record_tts(self, **_kwargs):
            return None

    monkeypatch.setattr("hark.speech.resolve_tts", lambda *a, **k: FakeTts())
    # B153: default transport is subprocess isolation; unit tests patch
    # resolve_tts in-process and need the injectable transport to honor it.
    monkeypatch.setattr(
        "hark.speech._synth_transport_factory",
        speech_mod._in_process_synth_transport_factory,
    )
    monkeypatch.setattr("hark.speech.UsageStore", FakeUsage)
    monkeypatch.setattr(
        "hark.speech.repair_tts_mute_after_play",
        lambda **k: {"repaired": False},
    )
    monkeypatch.setattr(
        "hark.conference.apply_conference_hold",
        lambda *a, **k: SimpleNamespace(skipped=False, as_meta=lambda: {"held": False}),
    )


def _tts_config():
    cfg = HarkConfig()
    cfg.audio.defer_tts_while_listening = False
    cfg.audio.hold_during_conference = False
    return cfg


def test_claim_lock_timeout_reports_owner_and_queue(tmp_path):
    """A wedged playback flock must fail boundedly with actionable state (B146)."""
    state = tmp_path / "state"
    state.mkdir()
    lock = state / "hark" / "tts_play.lock"
    lock.parent.mkdir()
    queue = state / "hark" / "tts_play_queue.json"
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(state)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    holder = _flock_holder(lock, env=env)
    queue.write_text(
        json.dumps(
            {
                "next": 8,
                "serving": 7,
                "cancelled": [],
                "holders": {"7": {"pid": holder.pid, "claimed_at": time.time() - 2}},
            }
        ),
        encoding="utf-8",
    )
    code = """
import json
from hark.audio import playback as pb

pb._PLAY_LOCK_ACQUIRE_TIMEOUT_S = 0.15
try:
    pb.claim_tts_play_ticket()
except TimeoutError as exc:
    print(json.dumps({
        "type": type(exc).__name__,
        "operation": getattr(exc, "operation", None),
        "lock_owner_pid": getattr(exc, "lock_owner_pid", None),
        "serving": getattr(exc, "queue_state", {}).get("serving"),
        "queue_owner_pid": getattr(exc, "queue_owner_pid", None),
        "message": str(exc),
    }))
else:
    raise SystemExit("claim unexpectedly succeeded")
"""
    contender = subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = contender.communicate(timeout=2.0)
        assert contender.returncode == 0, stderr
        report = json.loads(stdout)
        assert report["type"] == "TtsPlayLockTimeout"
        assert report["operation"] == "claim"
        assert report["lock_owner_pid"] == holder.pid
        assert report["serving"] == 7
        assert report["queue_owner_pid"] == holder.pid
        assert "serving=7" in report["message"]
    finally:
        if contender.poll() is None:
            contender.kill()
            contender.wait()
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)


def test_abandon_lock_timeout_reports_owner_and_preserves_ticket(
    tmp_path, monkeypatch
):
    """Abandonment is bounded and leaves a recoverable tracked ticket."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())
    try:
        with pytest.raises(pb.TtsPlayLockTimeout) as caught:
            pb.abandon_tts_play_ticket(ticket, lock_timeout_s=0.12)
        assert caught.value.operation == "abandon"
        assert caught.value.ticket == ticket
        assert caught.value.lock_owner_pid == holder.pid
        assert caught.value.queue_state["serving"] == ticket
        assert ticket in pb._our_tickets
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)
        pb.abandon_tts_play_ticket(ticket)


def test_heal_lock_timeout_returns_structured_owner_and_queue_diagnostics(
    tmp_path, monkeypatch
):
    """The non-raising healer still exposes typed lock-timeout details."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.12)
    try:
        report = pb.heal_tts_play_queue()
        assert report["ok"] is False
        assert report["error_type"] == "tts_play_lock_timeout"
        assert report["tts_play_lock"]["operation"] == "heal"
        assert report["tts_play_lock"]["lock_owner_pid"] == holder.pid
        assert report["tts_play_lock"]["queue"]["serving"] == ticket
        assert report["tts_play_lock"]["queue"]["next"] == ticket + 1
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)
        pb.abandon_tts_play_ticket(ticket)


def test_process_cleanup_never_waits_a_full_lock_timeout(tmp_path, monkeypatch):
    """Signal/atexit cleanup is best-effort and cannot itself look hung."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.5)
    try:
        started = time.monotonic()
        pb._abandon_our_tickets()
        assert time.monotonic() - started < 0.1
        assert ticket in pb._our_tickets
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)
        pb.abandon_tts_play_ticket(ticket)


def test_exclusive_lock_timeout_defers_ticket_abandonment(tmp_path, monkeypatch):
    """Bounded wait lock timeout returns promptly; cleanup runs after release."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    env = os.environ.copy()
    holder = _flock_holder(lock, env=env)
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.12)
    try:
        started = time.monotonic()
        with pytest.raises(pb.TtsPlayLockTimeout) as caught:
            with pb.exclusive_playback(ticket=ticket, wait_timeout_s=0.12):
                raise AssertionError("must not enter playback")
        assert time.monotonic() - started < 1.0
        assert caught.value.operation == "wait"
        assert caught.value.ticket == ticket
        assert ticket in pb._our_tickets
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    deadline = time.monotonic() + 2.0
    while ticket in pb._our_tickets and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ticket not in pb._our_tickets
    assert pb.inspect_tts_play_queue()["serving"] == ticket + 1


def test_unbounded_exclusive_wait_retries_lock_without_abandoning(
    tmp_path, monkeypatch
):
    """Live players hold the flock for whole utterances; waiters must not bail."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.08)
    result: dict[str, object] = {}

    def waiter() -> None:
        with pb.exclusive_playback(ticket=ticket):
            result["played"] = True

    thread = threading.Thread(target=waiter, name="b146-unbounded-waiter")
    thread.start()
    try:
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            if ticket in pb._deferred_abandons:
                raise AssertionError("unbounded waiter abandoned during lock hold")
            time.sleep(0.02)
        assert thread.is_alive()
        assert ticket in pb._our_tickets
        assert "played" not in result
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert result.get("played") is True
    assert ticket not in pb._our_tickets
    assert pb.inspect_tts_play_queue()["serving"] == ticket + 1


def test_deferred_abandonment_uses_one_reaper_for_many_tickets(
    tmp_path, monkeypatch
):
    """A permanent wedge must not leak one retrying daemon per ticket."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    tickets = [pb.claim_tts_play_ticket() for _ in range(2)]
    holder = _flock_holder(lock, env=os.environ.copy())
    real_start = threading.Thread.start
    started_reapers = []

    def record_start(thread):
        if thread.name.startswith("hark-tts-abandon-"):
            started_reapers.append(thread.name)
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", record_start)
    try:
        assert pb.defer_tts_play_ticket_abandon(tickets[0]) is True
        assert pb.defer_tts_play_ticket_abandon(tickets[1]) is True
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    deadline = time.monotonic() + 2.0
    while any(ticket in pb._our_tickets for ticket in tickets) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert all(ticket not in pb._our_tickets for ticket in tickets)
    assert started_reapers == ["hark-tts-abandon-reaper"]


def test_exclusive_lock_timeout_recovers_if_deferred_thread_start_fails_once(
    tmp_path, monkeypatch
):
    """A failed first cleanup-worker publication must not orphan the ticket."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.12)
    real_start = threading.Thread.start
    starts = 0

    def fail_first_start(thread):
        nonlocal starts
        if thread.name.startswith("hark-tts-abandon-"):
            starts += 1
            if starts == 1:
                raise RuntimeError("thread publication failed")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_first_start)
    try:
        with pytest.raises(pb.TtsPlayLockTimeout):
            with pb.exclusive_playback(ticket=ticket, wait_timeout_s=0.12):
                raise AssertionError("must not enter playback")
        assert starts == 2
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    deadline = time.monotonic() + 2.0
    while ticket in pb._our_tickets and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ticket not in pb._our_tickets
    assert pb.inspect_tts_play_queue()["serving"] == ticket + 1


def test_exclusive_cleanup_preserves_primary_exception(tmp_path, monkeypatch):
    """Queue-cleanup failures must not replace the playback/body failure."""
    _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    original_write = pb._queue_write

    class PrimaryFailure(BaseException):
        pass

    def fail_write(*_args, **_kwargs):
        raise RuntimeError("cleanup write failed")

    try:
        with pytest.raises(PrimaryFailure, match="body failed"):
            with pb.exclusive_playback(ticket=ticket):
                monkeypatch.setattr(pb, "_queue_write", fail_write)
                raise PrimaryFailure("body failed")
    finally:
        monkeypatch.setattr(pb, "_queue_write", original_write)
        pb.abandon_tts_play_ticket(ticket)


def test_run_tts_configured_wait_timeout_is_total_and_keeps_lock_details(
    tmp_path, monkeypatch
):
    """run_tts cleanup must not add a second lock wait after delegation."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.5)
    _patch_run_tts_dependencies(monkeypatch)

    def delayed_claim(**kwargs):
        assert kwargs["lock_timeout_s"] == pytest.approx(0.12)
        time.sleep(0.07)
        return ticket

    monkeypatch.setattr("hark.speech.claim_tts_play_ticket", delayed_claim)
    cfg = _tts_config()
    started = time.monotonic()
    try:
        with pytest.raises(pb.TtsPlayLockTimeout) as caught:
            run_tts(
                cfg,
                "bounded playback",
                play=True,
                mute_mic=False,
                conference_policy="force",
                use_cache=False,
                play_wait_timeout_s=0.12,
            )
        elapsed = time.monotonic() - started
        # One queue budget (~0.12s) plus synth/setup; must not restart a second
        # full lock acquisition bound (0.5s) after the timeout.
        assert 0.1 <= elapsed < 0.45
        assert caught.value.operation == "wait"
        assert caught.value.lock_owner_pid == holder.pid
        assert caught.value.queue_owner_pid == os.getpid()
        assert caught.value.queue_state["next"] == ticket + 1
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    deadline = time.monotonic() + 2.0
    while ticket in pb._our_tickets and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ticket not in pb._our_tickets


def test_run_tts_lock_timeout_with_failed_reaper_does_not_restart_budget(
    tmp_path, monkeypatch
):
    """Permanent reaper publication failure cannot add a second full wait."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())
    _patch_run_tts_dependencies(monkeypatch)
    monkeypatch.setattr("hark.speech.claim_tts_play_ticket", lambda **_kwargs: ticket)
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.5)
    real_start = threading.Thread.start

    def fail_reaper_start(thread):
        if thread.name == "hark-tts-abandon-reaper":
            raise RuntimeError("threads unavailable")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_reaper_start)
    started = time.monotonic()
    try:
        with pytest.raises(pb.TtsPlayLockTimeout) as caught:
            run_tts(
                _tts_config(),
                "bounded even without reaper",
                play=True,
                mute_mic=False,
                conference_policy="force",
                use_cache=False,
                play_wait_timeout_s=0.12,
            )
        assert caught.value.operation == "wait"
        assert time.monotonic() - started < 0.45
        assert ticket in pb._deferred_abandons
        assert ticket in pb._our_tickets
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    monkeypatch.undo()
    monkeypatch.setattr(pb, "tts_play_lock_path", lambda: lock)
    monkeypatch.setattr(pb, "tts_play_queue_path", lambda: _queue)
    next_ticket = pb.claim_tts_play_ticket()
    assert ticket not in pb._deferred_abandons
    assert ticket not in pb._our_tickets
    pb.abandon_tts_play_ticket(next_ticket)


def test_run_tts_synth_failure_cleanup_respects_total_lock_budget(
    tmp_path, monkeypatch
):
    """A synth failure cannot trigger a fresh full lock wait during cleanup."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    holder_box = {}
    real_claim = pb.claim_tts_play_ticket

    def claim_then_block(**kwargs):
        ticket = real_claim(**kwargs)
        holder_box["ticket"] = ticket
        holder_box["holder"] = _flock_holder(lock, env=os.environ.copy())
        return ticket

    class FailedTts:
        def synthesize(self, text, voice=None):
            raise ProviderError("synth failed")

    class FakeUsage:
        def record_tts(self, **_kwargs):
            return None

    monkeypatch.setattr("hark.speech.claim_tts_play_ticket", claim_then_block)
    monkeypatch.setattr("hark.speech.resolve_tts", lambda *a, **k: FailedTts())
    monkeypatch.setattr(
        "hark.speech._synth_transport_factory",
        speech_mod._in_process_synth_transport_factory,
    )
    monkeypatch.setattr("hark.speech.UsageStore", FakeUsage)
    monkeypatch.setattr(
        "hark.conference.apply_conference_hold",
        lambda *a, **k: SimpleNamespace(skipped=False, as_meta=lambda: {"held": False}),
    )
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.5)
    real_start = threading.Thread.start
    reaper_starts = 0

    def fail_first_reaper_start(thread):
        nonlocal reaper_starts
        if thread.name == "hark-tts-abandon-reaper":
            reaper_starts += 1
            if reaper_starts == 1:
                raise RuntimeError("thread publication failed")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_first_reaper_start)
    cfg = _tts_config()
    started = time.monotonic()
    try:
        with pytest.raises(ProviderError, match="synth failed"):
            run_tts(
                cfg,
                "failed synthesis",
                play=True,
                mute_mic=False,
                conference_policy="force",
                use_cache=False,
                play_wait_timeout_s=0.12,
            )
        assert time.monotonic() - started < 0.3
        assert holder_box["ticket"] in pb._our_tickets
        assert reaper_starts == 2
    finally:
        holder = holder_box["holder"]
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    ticket = holder_box["ticket"]
    deadline = time.monotonic() + 2.0
    while ticket in pb._our_tickets and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ticket not in pb._our_tickets


def test_failed_reaper_publication_is_retained_for_next_lock_transaction(
    tmp_path, monkeypatch
):
    """Repeated thread-start failure leaves recoverable in-process cleanup state."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    abandoned = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())

    def fail_start(_thread):
        raise RuntimeError("threads unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    try:
        assert pb.defer_tts_play_ticket_abandon(abandoned) is False
        assert abandoned in pb._deferred_abandons
        assert abandoned in pb._our_tickets
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    monkeypatch.undo()
    monkeypatch.setattr(pb, "tts_play_lock_path", lambda: lock)
    monkeypatch.setattr(pb, "tts_play_queue_path", lambda: _queue)
    next_ticket = pb.claim_tts_play_ticket()
    state = pb.inspect_tts_play_queue()
    assert abandoned not in pb._deferred_abandons
    assert abandoned not in pb._our_tickets
    assert state["serving"] == next_ticket
    pb.abandon_tts_play_ticket(next_ticket)


def test_recovered_cleanup_stays_pending_until_queue_write_commits(
    tmp_path, monkeypatch
):
    """A failed recovery transaction must retain its cleanup ownership state."""
    lock, queue = _queue_paths(tmp_path, monkeypatch)
    abandoned = pb.claim_tts_play_ticket()
    real_start = threading.Thread.start

    def fail_start(_thread):
        raise RuntimeError("threads unavailable")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    assert pb.defer_tts_play_ticket_abandon(abandoned) is False
    monkeypatch.setattr(threading.Thread, "start", real_start)

    real_write = pb._queue_write
    writes = 0

    def fail_first_write(path, state):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("queue write failed")
        return real_write(path, state)

    monkeypatch.setattr(pb, "_queue_write", fail_first_write)
    with pytest.raises(OSError, match="queue write failed"):
        pb.claim_tts_play_ticket()
    assert abandoned in pb._deferred_abandons
    assert abandoned in pb._our_tickets
    on_disk = json.loads(queue.read_text(encoding="utf-8"))
    assert on_disk["serving"] == abandoned

    next_ticket = pb.claim_tts_play_ticket()
    assert abandoned not in pb._deferred_abandons
    assert abandoned not in pb._our_tickets
    assert pb.inspect_tts_play_queue()["serving"] == next_ticket
    pb.abandon_tts_play_ticket(next_ticket)


def test_deferred_reaper_retries_transient_cleanup_errors(tmp_path, monkeypatch):
    """A transient queue I/O error cannot discard the only cleanup request."""
    _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    real_abandon = pb._abandon_tts_play_ticket_now
    attempts = 0

    def fail_once(ticket_arg, *, lock_timeout_s=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient queue write failure")
        return real_abandon(ticket_arg, lock_timeout_s=lock_timeout_s)

    monkeypatch.setattr(pb, "_abandon_tts_play_ticket_now", fail_once)
    assert pb.defer_tts_play_ticket_abandon(ticket) is True
    deadline = time.monotonic() + 2.0
    while ticket in pb._our_tickets and time.monotonic() < deadline:
        time.sleep(0.02)
    assert attempts >= 2
    assert ticket not in pb._our_tickets
    assert ticket not in pb._deferred_abandons


def test_run_tts_configured_wait_timeout_bounds_initial_claim(tmp_path, monkeypatch):
    """The configured queue budget applies before synth at claim time too."""
    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    holder = _flock_holder(lock, env=os.environ.copy())
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.5)
    _patch_run_tts_dependencies(monkeypatch)
    started = time.monotonic()
    try:
        with pytest.raises(pb.TtsPlayLockTimeout) as caught:
            run_tts(
                _tts_config(),
                "bounded initial claim",
                play=True,
                mute_mic=False,
                conference_policy="force",
                use_cache=False,
                play_wait_timeout_s=0.12,
            )
        assert time.monotonic() - started < 0.4
        assert caught.value.operation == "claim"
        assert caught.value.lock_owner_pid == holder.pid
        assert caught.value.ticket is None
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)


def test_cli_configured_wait_timeout_prints_owner_diagnostics(
    tmp_path, monkeypatch, capsys
):
    """The CLI timeout surface retains the typed lock diagnostics (B146)."""
    from hark import cli

    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.12)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    def blocked_dispatch(_args, _cfg):
        with pb.exclusive_playback(ticket=ticket, wait_timeout_s=0.12):
            raise AssertionError("must not enter playback")

    monkeypatch.setattr(cli, "dispatch", blocked_dispatch)
    try:
        assert cli.main(["doctor"]) == cli.TIMEOUT
        stderr = capsys.readouterr().err
        assert "hark: timeout:" in stderr
        assert "operation=wait" in stderr
        assert f"lock_owner_pid={holder.pid}" in stderr
        assert f"queue_owner_pid={os.getpid()}" in stderr
        assert f"next={ticket + 1}" in stderr
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    deadline = time.monotonic() + 2.0
    while ticket in pb._our_tickets and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ticket not in pb._our_tickets


def test_cmd_ask_serializes_typed_play_lock_timeout(tmp_path, monkeypatch, capsys):
    """A real held flock becomes actionable ask JSON and exit TIMEOUT."""
    from hark import cli

    lock, _queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    holder = _flock_holder(lock, env=os.environ.copy())
    monkeypatch.setattr(pb, "_PLAY_LOCK_ACQUIRE_TIMEOUT_S", 0.12)

    def blocked_speak_and_listen(*_args, **_kwargs):
        with pb.exclusive_playback(ticket=ticket, wait_timeout_s=0.12):
            raise AssertionError("must not enter playback")

    monkeypatch.setattr("hark.speech.speak_and_listen", blocked_speak_and_listen)
    args = SimpleNamespace(
        text=["Will", "this", "return?"],
        confirm="never",
        end_mode=None,
        provider=None,
        json=True,
        event_id="event-B146",
    )
    try:
        assert cli.cmd_ask(args, _tts_config()) == TIMEOUT
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["exit"] == TIMEOUT
        assert payload["error_type"] == "tts_play_lock_timeout"
        assert payload["tts_play_lock"]["operation"] == "wait"
        assert payload["tts_play_lock"]["ticket"] == ticket
        assert payload["tts_play_lock"]["lock_owner_pid"] == holder.pid
        assert payload["tts_play_lock"]["queue"]["serving"] == ticket
        assert payload["tts_play_lock"]["queue"]["next"] == ticket + 1
        assert payload["for_event"] == "event-B146"
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=2.0)

    deadline = time.monotonic() + 2.0
    while ticket in pb._our_tickets and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ticket not in pb._our_tickets


def test_cmd_ask_serializes_typed_queue_turn_timeout(
    tmp_path, monkeypatch, capsys
):
    """Queue-head expiry retains typed head-owner diagnostics in ask JSON."""
    from hark import cli

    _lock, _queue = _queue_paths(tmp_path, monkeypatch)
    head = pb.claim_tts_play_ticket()
    waiter = pb.claim_tts_play_ticket()

    def blocked_speak_and_listen(*_args, **_kwargs):
        with pb.exclusive_playback(ticket=waiter, wait_timeout_s=0.12):
            raise AssertionError("must not enter playback")

    monkeypatch.setattr("hark.speech.speak_and_listen", blocked_speak_and_listen)
    args = SimpleNamespace(
        text=["Will", "the", "queue", "return?"],
        confirm="never",
        end_mode=None,
        provider=None,
        json=True,
        event_id="event-B146-queue",
    )
    try:
        assert cli.cmd_ask(args, _tts_config()) == TIMEOUT
        payload = json.loads(capsys.readouterr().out)
        assert payload["error_type"] == "tts_play_queue_timeout"
        assert payload["tts_play_lock"]["operation"] == "queue_wait"
        assert payload["tts_play_lock"]["ticket"] == waiter
        assert payload["tts_play_lock"]["queue_owner_pid"] == os.getpid()
        assert payload["tts_play_lock"]["queue"]["serving"] == head
        assert payload["tts_play_lock"]["queue"]["next"] == waiter + 1
    finally:
        pb.abandon_tts_play_ticket(head)


def test_exclusive_playback_serializes_fifo(tmp_path, monkeypatch):
    _queue_paths(tmp_path, monkeypatch)
    order: list[str] = []

    def worker(name: str, hold: float, delay_before: float = 0.0) -> None:
        if delay_before:
            time.sleep(delay_before)
        ticket = pb.claim_tts_play_ticket()
        # Simulate slower "synth" for earlier tickets so late claimers finish first
        with pb.exclusive_playback(ticket=ticket):
            order.append(f"{name}:in")
            time.sleep(hold)
            order.append(f"{name}:out")

    # a claims ticket first; b starts slightly later but finishes "work" faster
    t1 = threading.Thread(target=worker, args=("a", 0.15))
    t2 = threading.Thread(
        target=worker, args=("b", 0.05), kwargs={"delay_before": 0.03}
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    # FIFO by ticket claim order (a before b), not finish order
    assert order == ["a:in", "a:out", "b:in", "b:out"]


def test_five_tickets_play_in_launch_order(tmp_path, monkeypatch):
    """5 concurrent jobs: reverse synth finish order must still play 0..4."""
    _queue_paths(tmp_path, monkeypatch)
    play_order: list[int] = []
    # Claim in launch order (main thread), like sequential process starts
    tickets = [pb.claim_tts_play_ticket() for _ in range(5)]
    assert tickets == [0, 1, 2, 3, 4] or tickets == list(
        range(tickets[0], tickets[0] + 5)
    )

    def worker(i: int) -> None:
        # Higher i "synths" faster — would race ahead without FIFO tickets
        time.sleep(0.05 * (4 - i))
        with pb.exclusive_playback(ticket=tickets[i]):
            play_order.append(i)
            time.sleep(0.02)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert play_order == [0, 1, 2, 3, 4]


def test_exclusive_playback_reentrant(tmp_path, monkeypatch):
    _queue_paths(tmp_path, monkeypatch)
    with pb.exclusive_playback():
        with pb.exclusive_playback():
            pass  # no deadlock


def test_claim_records_holder_pid(tmp_path, monkeypatch):
    _, queue = _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    st = json.loads(queue.read_text(encoding="utf-8"))
    holders = st.get("holders") or {}
    h = holders.get(str(ticket))
    assert h is not None
    assert int(h["pid"]) == __import__("os").getpid()
    assert float(h["claimed_at"]) > 0
    pb.abandon_tts_play_ticket(ticket)


def test_heal_dead_pid_advances_serving(tmp_path, monkeypatch):
    """Dogfood case: serving < next with dead holder PIDs → heal advances."""
    _, queue = _queue_paths(tmp_path, monkeypatch)
    # Simulate abandoned tickets 75, 76 (dead PIDs) like the real incident
    queue.write_text(
        json.dumps(
            {
                "next": 77,
                "serving": 75,
                "cancelled": [],
                "holders": {
                    "75": {"pid": 999_999_991, "claimed_at": time.time() - 60},
                    "76": {"pid": 999_999_992, "claimed_at": time.time() - 50},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pb, "_pid_alive", lambda pid: False)
    report = pb.heal_tts_play_queue(missing_as_abandoned=False)
    assert report["healed_count"] == 2
    assert report["serving"] == 77
    assert report["next"] == 77
    assert report["pending"] == 0
    st = json.loads(queue.read_text(encoding="utf-8"))
    assert st["serving"] == 77


def test_heal_missing_holders_legacy_queue(tmp_path, monkeypatch):
    """Pre-B099 queue JSON without holders still heals when missing_as_abandoned."""
    _, queue = _queue_paths(tmp_path, monkeypatch)
    queue.write_text(
        json.dumps({"next": 77, "serving": 75, "cancelled": []}),
        encoding="utf-8",
    )
    report = pb.heal_tts_play_queue(missing_as_abandoned=True)
    assert report["healed_count"] >= 2
    assert report["serving"] == 77


def test_heal_preserves_live_holder(tmp_path, monkeypatch):
    _queue_paths(tmp_path, monkeypatch)
    t0 = pb.claim_tts_play_ticket()
    t1 = pb.claim_tts_play_ticket()
    assert t1 == t0 + 1
    report = pb.heal_tts_play_queue(missing_as_abandoned=True)
    # Our live tickets must not be skipped
    assert report["serving"] == t0
    assert report["healed_count"] == 0
    pb.abandon_tts_play_ticket(t0)
    pb.abandon_tts_play_ticket(t1)


def test_exclusive_playback_skips_dead_head(tmp_path, monkeypatch):
    """Waiter auto-advances past a dead-PID head without waiting forever."""
    _, queue = _queue_paths(tmp_path, monkeypatch)
    dead_pid = 999_999_993
    queue.write_text(
        json.dumps(
            {
                "next": 1,
                "serving": 0,
                "cancelled": [],
                "holders": {
                    "0": {"pid": dead_pid, "claimed_at": time.time() - 30},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pb, "_pid_alive", lambda pid: pid != dead_pid and pid > 0)
    # Claim ticket 1; exclusive should heal past 0 and play
    ticket = pb.claim_tts_play_ticket()
    assert ticket == 1
    t0 = time.monotonic()
    with pb.exclusive_playback(ticket=ticket, wait_timeout_s=3.0):
        pass
    assert time.monotonic() - t0 < 2.0
    st = json.loads(queue.read_text(encoding="utf-8"))
    assert st["serving"] == 2  # advanced past our ticket after play


def test_exclusive_playback_wait_timeout_abandons(tmp_path, monkeypatch):
    """wait_timeout_s raises and abandons so the queue does not stall (boot path)."""
    _queue_paths(tmp_path, monkeypatch)
    # Live "other" holder blocks us
    head = pb.claim_tts_play_ticket()
    waiter = pb.claim_tts_play_ticket()
    # Pretend head holder is still alive forever (default pid_alive for us)
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        with pb.exclusive_playback(ticket=waiter, wait_timeout_s=0.25):
            raise AssertionError("should not enter play")
    assert time.monotonic() - t0 < 1.5
    # Waiter abandoned → cancelled or skipped; head still serving
    st = pb.inspect_tts_play_queue()
    assert st["serving"] == head
    assert waiter in st["cancelled"] or st["serving"] > waiter
    pb.abandon_tts_play_ticket(head)


def test_abandon_on_exit_hook_clears_ticket(tmp_path, monkeypatch):
    _queue_paths(tmp_path, monkeypatch)
    ticket = pb.claim_tts_play_ticket()
    assert ticket in pb._our_tickets
    pb._abandon_our_tickets()
    st = pb.inspect_tts_play_queue()
    assert st["serving"] == ticket + 1 or ticket in st["cancelled"]
    assert ticket not in pb._our_tickets


def test_run_tts_pipelines_next_chunk_synth(monkeypatch):
    """While chunk i plays, chunk i+1 synth should already have started."""
    synth_starts: list[tuple[float, str]] = []
    play_starts: list[float] = []
    t0 = time.monotonic()

    class FakeDuck:
        def __enter__(self):
            return SimpleNamespace(as_meta=lambda: {"media_ducked": False})

        def __exit__(self, *a):
            return False

    class FakeMute:
        def __enter__(self):
            return SimpleNamespace(applied=False)

        def __exit__(self, *a):
            return False

    def fake_lookup(*a, **k):
        return None

    def fake_resolve(*a, **k):
        class T:
            def synthesize(self, text, voice=None):
                synth_starts.append((time.monotonic() - t0, text[:20]))
                time.sleep(0.08)  # simulate network
                return SimpleNamespace(
                    audio=b"AUD" + str(len(synth_starts)).encode(),
                    provider="xai",
                    content_type="audio/mpeg",
                    voice=voice or "eve",
                )

        return T()

    def fake_play(audio, **k):
        play_starts.append(time.monotonic() - t0)
        time.sleep(0.12)  # simulate play longer than synth
        return SimpleNamespace(duration_ms=120)

    monkeypatch.setattr("hark.speech.lookup_cached_tts", fake_lookup)
    monkeypatch.setattr("hark.speech.store_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.resolve_tts", fake_resolve)
    monkeypatch.setattr(
        "hark.speech._synth_transport_factory",
        speech_mod._in_process_synth_transport_factory,
    )
    monkeypatch.setattr("hark.speech.play_wav_bytes", fake_play)
    monkeypatch.setattr("hark.speech.duck_media", lambda *a, **k: FakeDuck())
    monkeypatch.setattr("hark.speech.mic_muted_during_tts", lambda **k: FakeMute())
    monkeypatch.setattr(
        "hark.speech.repair_tts_mute_after_play",
        lambda **k: {"repaired": False},
    )
    monkeypatch.setattr(
        "hark.conference.apply_conference_hold",
        lambda *a, **k: SimpleNamespace(skipped=False, as_meta=lambda: {"held": False}),
    )
    # exclusive_playback identity for unit test (no flock races with parallel suites)
    from contextlib import contextmanager

    @contextmanager
    def _fake_exclusive(ticket=None, *, wait_timeout_s=None):
        yield

    monkeypatch.setattr("hark.speech.exclusive_playback", _fake_exclusive)
    monkeypatch.setattr("hark.speech.claim_tts_play_ticket", lambda: 0)
    monkeypatch.setattr("hark.speech.abandon_tts_play_ticket", lambda t: None)

    long = ("Sentence number one with padding words here. " * 20) + (
        "Sentence number two with more padding. " * 20
    )
    cfg = HarkConfig()
    cfg.tts.max_chars = 0
    cfg.tts.chunk_chars = 200
    cfg.audio.hold_during_conference = False
    out = run_tts(cfg, long, play=True, conference_policy="force", use_cache=False)
    assert out["ok"] and out["chunked"] and out["chunks"] >= 2
    assert len(synth_starts) >= 2
    assert len(play_starts) >= 2
    # Second synth should start before first play ends (pipeline):
    # play0 starts at play_starts[0], lasts 0.12s; synth1 should start < play0+0.12
    synth1_t = synth_starts[1][0]
    play0_t = play_starts[0]
    assert synth1_t < play0_t + 0.12, (
        f"synth1 at {synth1_t:.3f} not overlapping play0 at {play0_t:.3f}"
    )

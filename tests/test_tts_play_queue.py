"""B092: exclusive playback lock + pipelined synth. B099: abandoned ticket heal."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from hark.audio import playback as pb
from hark.config import HarkConfig
from hark.speech import run_tts


def _queue_paths(tmp_path, monkeypatch):
    lock = tmp_path / "tts_play.lock"
    queue = tmp_path / "tts_play_queue.json"
    monkeypatch.setattr(pb, "tts_play_lock_path", lambda: lock)
    monkeypatch.setattr(pb, "tts_play_queue_path", lambda: queue)
    return lock, queue


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
    t2 = threading.Thread(target=worker, args=("b", 0.05), kwargs={"delay_before": 0.03})
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
    assert tickets == [0, 1, 2, 3, 4] or tickets == list(range(tickets[0], tickets[0] + 5))

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
    monkeypatch.setattr(
        pb, "_pid_alive", lambda pid: pid != dead_pid and pid > 0
    )
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
    monkeypatch.setattr("hark.speech.play_wav_bytes", fake_play)
    monkeypatch.setattr("hark.speech.duck_media", lambda *a, **k: FakeDuck())
    monkeypatch.setattr("hark.speech.mic_muted_during_tts", lambda **k: FakeMute())
    monkeypatch.setattr(
        "hark.speech.repair_tts_mute_after_play",
        lambda **k: {"repaired": False},
    )
    monkeypatch.setattr(
        "hark.conference.apply_conference_hold",
        lambda *a, **k: SimpleNamespace(
            skipped=False, as_meta=lambda: {"held": False}
        ),
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

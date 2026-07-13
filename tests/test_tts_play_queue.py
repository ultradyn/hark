"""B092: exclusive playback lock + pipelined synth."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from hark.audio import playback as pb
from hark.config import HarkConfig
from hark.speech import run_tts


def test_exclusive_playback_serializes(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "tts_play_lock_path", lambda: tmp_path / "tts_play.lock")
    order: list[str] = []

    def worker(name: str, hold: float) -> None:
        with pb.exclusive_playback():
            order.append(f"{name}:in")
            time.sleep(hold)
            order.append(f"{name}:out")

    t1 = threading.Thread(target=worker, args=("a", 0.15))
    t2 = threading.Thread(target=worker, args=("b", 0.05))
    t1.start()
    time.sleep(0.03)
    t2.start()
    t1.join()
    t2.join()
    # a must fully finish before b enters
    assert order == ["a:in", "a:out", "b:in", "b:out"]


def test_exclusive_playback_reentrant(tmp_path, monkeypatch):
    monkeypatch.setattr(pb, "tts_play_lock_path", lambda: tmp_path / "tts_play.lock")
    with pb.exclusive_playback():
        with pb.exclusive_playback():
            pass  # no deadlock


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
    from contextlib import nullcontext

    monkeypatch.setattr("hark.speech.exclusive_playback", nullcontext)

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

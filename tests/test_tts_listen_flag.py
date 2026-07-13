import argparse
import threading
import time

import hark.cli as cli
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

"""B097: defer TTS play/mute while operator listen/radio is capturing."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace

from hark.config import HarkConfig, load_config
from hark.listen_control import clear_active_listen, register_active_listen
from hark.mic_coord import (
    clear_ambient_pause,
    request_ambient_pause,
    user_capture_active,
    wait_until_user_capture_idle,
)
from hark.speech import run_tts


def test_user_capture_active_from_listen_active(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    clear_ambient_pause()
    assert not user_capture_active(ignore_own_pid=False).active

    # Foreign PID that is alive (use our pid but do not ignore it)
    register_active_listen("sdefer1", mode="radio")
    # active.json stores our pid; ignore_own_pid=True would hide it
    st = user_capture_active(ignore_own_pid=False)
    assert st.active
    assert "listen.active" in st.sources
    assert st.stream_id == "sdefer1"
    assert st.mode == "radio"

    # Same process should not block itself (nudge path)
    st_own = user_capture_active(ignore_own_pid=True)
    assert not st_own.active

    clear_active_listen("sdefer1")
    assert not user_capture_active(ignore_own_pid=False).active


def test_user_capture_active_from_ambient_pause_listen(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    clear_ambient_pause()
    request_ambient_pause(reason="listen", pid=os.getpid())
    st = user_capture_active(ignore_own_pid=False)
    assert st.active
    assert "ambient.pause" in st.sources
    assert not user_capture_active(ignore_own_pid=True).active
    clear_ambient_pause()


def test_user_capture_ignores_non_listen_pause_reason(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    clear_ambient_pause()
    request_ambient_pause(reason="wake-enroll", pid=1)
    st = user_capture_active(ignore_own_pid=False)
    assert not st.active
    clear_ambient_pause()


def test_user_capture_ignores_dead_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    clear_ambient_pause()
    # PID unlikely to exist
    dead = 2_147_483_646
    try:
        os.kill(dead, 0)
        # if somehow alive, skip assertion path by using another candidate
        dead = 2_147_483_645
    except ProcessLookupError:
        pass
    except PermissionError:
        pass

    from hark.listen_control import active_path, listen_control_dir

    listen_control_dir().mkdir(parents=True, exist_ok=True)
    active_path().write_text(
        json.dumps(
            {
                "stream_id": "sstale",
                "mode": "radio",
                "pid": dead,
                "started_at": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    st = user_capture_active(ignore_own_pid=False)
    # dead pid → fail open
    if not _pid_might_be_alive(dead):
        assert not st.active
    clear_active_listen()


def _pid_might_be_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def test_wait_until_idle_immediate_when_free(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    clear_ambient_pause()
    sleeps: list[float] = []
    result = wait_until_user_capture_idle(
        max_wait_s=5.0,
        poll_ms=50,
        quiet_ms=0,
        sleep_fn=sleeps.append,
    )
    assert result.deferred is False
    assert result.wait_ms == 0
    assert sleeps == []


def test_wait_until_idle_polls_then_proceeds():
    """Fake clock + probe: active for two polls, then idle."""
    clock = {"t": 0.0}
    probes = [
        SimpleNamespace(
            active=True,
            reason="listen:radio:s1",
            sources=("listen.active",),
            stream_id="s1",
            mode="radio",
            pid=999,
        ),
        SimpleNamespace(
            active=True,
            reason="listen:radio:s1",
            sources=("listen.active",),
            stream_id="s1",
            mode="radio",
            pid=999,
        ),
        SimpleNamespace(
            active=False,
            reason=None,
            sources=(),
            stream_id=None,
            mode=None,
            pid=None,
        ),
    ]
    idx = {"i": 0}

    def probe():
        i = min(idx["i"], len(probes) - 1)
        idx["i"] += 1
        return probes[i]

    def sleep(s: float) -> None:
        clock["t"] += s

    result = wait_until_user_capture_idle(
        max_wait_s=10.0,
        poll_ms=100,
        quiet_ms=0,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock["t"],
        probe_fn=probe,
    )
    assert result.deferred is True
    assert result.timed_out is False
    assert result.reason == "listen:radio:s1"
    assert result.wait_ms >= 100  # at least one poll sleep


def test_wait_until_idle_times_out():
    clock = {"t": 0.0}

    def probe():
        return SimpleNamespace(
            active=True,
            reason="listen:radio:stuck",
            sources=("listen.active",),
            stream_id="stuck",
            mode="radio",
            pid=1,
        )

    def sleep(s: float) -> None:
        clock["t"] += s

    result = wait_until_user_capture_idle(
        max_wait_s=0.25,
        poll_ms=50,
        quiet_ms=0,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock["t"],
        probe_fn=probe,
    )
    assert result.deferred is True
    assert result.timed_out is True
    assert result.wait_ms >= 250


def test_wait_quiet_pad_after_clear():
    clock = {"t": 0.0}
    # First probe active, then idle; quiet pad should advance clock
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        if calls["n"] == 1:
            return SimpleNamespace(
                active=True,
                reason="listen:silence:q1",
                sources=("listen.active",),
                stream_id="q1",
                mode="silence",
                pid=1,
            )
        return SimpleNamespace(
            active=False,
            reason=None,
            sources=(),
            stream_id=None,
            mode=None,
            pid=None,
        )

    def sleep(s: float) -> None:
        clock["t"] += s

    result = wait_until_user_capture_idle(
        max_wait_s=5.0,
        poll_ms=50,
        quiet_ms=200,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock["t"],
        probe_fn=probe,
    )
    assert result.deferred is True
    assert result.timed_out is False
    assert result.wait_ms >= 200


def test_config_defer_tts_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[audio]
defer_tts_while_listening = false
defer_tts_max_wait_s = 12.5
defer_tts_poll_ms = 50
defer_tts_quiet_ms = 100
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.audio.defer_tts_while_listening is False
    assert cfg.audio.defer_tts_max_wait_s == 12.5
    assert cfg.audio.defer_tts_poll_ms == 50
    assert cfg.audio.defer_tts_quiet_ms == 100
    # defaults
    cfg2 = HarkConfig()
    assert cfg2.audio.defer_tts_while_listening is True
    assert cfg2.audio.defer_tts_max_wait_s == 45.0


def test_run_tts_defers_play_until_capture_clears(monkeypatch, tmp_path):
    """Play/mute must not run while foreign listen is active."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    clear_ambient_pause()

    # Simulate foreign listen: active.json with a different live PID (1 = init)
    from hark.listen_control import active_path, listen_control_dir

    listen_control_dir().mkdir(parents=True, exist_ok=True)
    foreign_pid = 1
    active_path().write_text(
        json.dumps(
            {
                "stream_id": "sforeign",
                "mode": "radio",
                "pid": foreign_pid,
                "started_at": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    play_calls: list[float] = []
    mute_entered: list[bool] = []
    clock = {"t": 0.0}
    cleared = {"done": False}

    def fake_wait(**kwargs):
        # Clear capture mid-wait, then return deferred meta
        clear_active_listen()
        cleared["done"] = True
        from hark.mic_coord import DeferResult

        return DeferResult(
            deferred=True,
            wait_ms=150,
            timed_out=False,
            reason="listen:radio:sforeign",
            sources=["listen.active"],
        )

    class FakeMute:
        def __enter__(self):
            assert cleared["done"], "mute must not apply before defer completes"
            mute_entered.append(True)
            return SimpleNamespace(applied=True)

        def __exit__(self, *a):
            return False

    class FakeDuck:
        def __enter__(self):
            return SimpleNamespace(
                as_meta=lambda: {
                    "media_ducked": False,
                    "duck_level": 0.15,
                    "duck_count": 0,
                    "duck_indices": [],
                    "mpris_paused": [],
                    "duck_error": None,
                    "duck_nested": False,
                }
            )

        def __exit__(self, *a):
            return False

    def fake_play(audio, **k):
        assert cleared["done"]
        play_calls.append(1.0)
        return SimpleNamespace(duration_ms=10)

    class FakeTts:
        def synthesize(self, text, voice=None):
            return SimpleNamespace(
                audio=b"ID3fake",
                provider="xai",
                content_type="audio/mpeg",
                voice=voice or "eve",
            )

    @contextmanager
    def _fake_exclusive(ticket=None, wait_timeout_s=None, **_kw):
        yield

    monkeypatch.setattr(
        "hark.speech.wait_until_tts_play_allowed", fake_wait
    )
    monkeypatch.setattr("hark.speech.resolve_tts", lambda *a, **k: FakeTts())
    monkeypatch.setattr("hark.speech.lookup_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.store_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.play_wav_bytes", fake_play)
    monkeypatch.setattr("hark.speech.mic_muted_during_tts", lambda **k: FakeMute())
    monkeypatch.setattr("hark.speech.duck_media", lambda *a, **k: FakeDuck())
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
    monkeypatch.setattr("hark.speech.exclusive_playback", _fake_exclusive)
    monkeypatch.setattr("hark.speech.claim_tts_play_ticket", lambda: 0)
    monkeypatch.setattr("hark.speech.abandon_tts_play_ticket", lambda t: None)

    class U:
        def record_tts(self, **k):
            return None

    monkeypatch.setattr("hark.speech.UsageStore", U)

    cfg = HarkConfig()
    cfg.audio.defer_tts_while_listening = True
    cfg.audio.hold_during_conference = False
    out = run_tts(cfg, "Hello operator", play=True, conference_policy="force")
    assert out["ok"] is True
    assert play_calls == [1.0]
    assert mute_entered == [True]
    assert out.get("listen_defer", {}).get("deferred") is True
    assert out["listen_defer"]["wait_ms"] == 150


def test_run_tts_skips_defer_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    defer_called = {"n": 0}

    def fake_wait(**kwargs):
        defer_called["n"] += 1
        from hark.mic_coord import DeferResult

        return DeferResult()

    class FakeMute:
        def __enter__(self):
            return SimpleNamespace(applied=False)

        def __exit__(self, *a):
            return False

    class FakeDuck:
        def __enter__(self):
            return SimpleNamespace(as_meta=lambda: {"media_ducked": False})

        def __exit__(self, *a):
            return False

    class FakeTts:
        def synthesize(self, text, voice=None):
            return SimpleNamespace(
                audio=b"ID3fake",
                provider="xai",
                content_type="audio/mpeg",
                voice=voice or "eve",
            )

    @contextmanager
    def _fake_exclusive(ticket=None, wait_timeout_s=None, **_kw):
        yield

    monkeypatch.setattr("hark.speech.wait_until_tts_play_allowed", fake_wait)
    monkeypatch.setattr("hark.speech.resolve_tts", lambda *a, **k: FakeTts())
    monkeypatch.setattr("hark.speech.lookup_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.store_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr(
        "hark.speech.play_wav_bytes",
        lambda *a, **k: SimpleNamespace(duration_ms=5),
    )
    monkeypatch.setattr("hark.speech.mic_muted_during_tts", lambda **k: FakeMute())
    monkeypatch.setattr("hark.speech.duck_media", lambda *a, **k: FakeDuck())
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
    monkeypatch.setattr("hark.speech.exclusive_playback", _fake_exclusive)
    monkeypatch.setattr("hark.speech.claim_tts_play_ticket", lambda: 0)
    monkeypatch.setattr("hark.speech.abandon_tts_play_ticket", lambda t: None)

    class U:
        def record_tts(self, **k):
            return None

    monkeypatch.setattr("hark.speech.UsageStore", U)

    cfg = HarkConfig()
    cfg.audio.defer_tts_while_listening = False
    out = run_tts(cfg, "Go now", play=True, conference_policy="force")
    assert out["ok"]
    assert defer_called["n"] == 0
    assert "listen_defer" not in out

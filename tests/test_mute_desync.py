"""B086: TTS mute hold lifecycle — no stuck depth / Pulse mute after play."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hark.audio import mic_mute as mm
from hark.config import HarkConfig


@pytest.fixture(autouse=True)
def _reset_mute_state():
    mm._depth = 0
    mm._saved = None
    mm._user_unmuted_override = False
    yield
    mm._depth = 0
    mm._saved = None
    mm._user_unmuted_override = False


def test_force_clear_zeros_depth_and_unmutes(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(
        mm, "set_source_mute", lambda src, mute: calls.append(("pulse", src, mute)) or True
    )
    monkeypatch.setattr(
        mm, "set_alsa_mic_capture", lambda on, card=None: calls.append(("alsa", on)) or True
    )
    monkeypatch.setattr(mm, "find_wave_alsa_card", lambda: ("Wave3", 1))
    monkeypatch.setattr(mm, "default_source", lambda: "src0")

    mm._depth = 2
    mm._saved = mm.MuteState(source="src0", was_muted=False, applied=True)

    out = mm.force_clear_tts_mute_hold(reason="test")
    assert out["cleared"] is True
    assert mm.tts_mute_depth() == 0
    assert mm.tts_mute_hold_active() is False
    assert ("pulse", "src0", False) in calls
    assert ("alsa", True) in calls


def test_release_tts_mute_hold_clears_depth(monkeypatch):
    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(mm, "set_source_mute", lambda *a, **k: True)
    monkeypatch.setattr(mm, "set_alsa_mic_capture", lambda *a, **k: True)
    monkeypatch.setattr(mm, "find_wave_alsa_card", lambda: None)
    monkeypatch.setattr(mm, "default_source", lambda: "src0")

    mm._depth = 1
    mm._saved = mm.MuteState(source="src0", was_muted=False, applied=True)
    assert mm.release_tts_mute_hold() is True
    assert mm.tts_mute_depth() == 0
    assert mm._saved is None
    assert mm.release_tts_mute_hold() is False


def test_ensure_unmuted_clears_stuck_hold(monkeypatch):
    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(mm, "set_source_mute", lambda *a, **k: True)
    monkeypatch.setattr(mm, "set_alsa_mic_capture", lambda *a, **k: True)
    monkeypatch.setattr(mm, "find_wave_alsa_card", lambda: None)
    monkeypatch.setattr(mm, "default_source", lambda: "src0")

    mm._depth = 1
    mm._saved = mm.MuteState(source="src0", was_muted=False, applied=True)
    result = mm.ensure_unmuted()
    assert result["released_hark_hold"] is True
    assert mm.tts_mute_depth() == 0
    assert mm.tts_mute_hold_active() is False


def test_nested_mute_restores_once(monkeypatch):
    pulse: dict[str, bool] = {"src0": False}
    alsa = {"on": True}

    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(mm, "default_source", lambda: "src0")
    monkeypatch.setattr(
        mm, "source_is_muted", lambda s: pulse.get(s, False)
    )
    monkeypatch.setattr(
        mm,
        "set_source_mute",
        lambda s, mute: pulse.__setitem__(s, mute) or True,
    )
    monkeypatch.setattr(
        mm,
        "set_alsa_mic_capture",
        lambda on, card=None: alsa.__setitem__("on", on) or True,
    )
    monkeypatch.setattr(mm, "find_wave_alsa_card", lambda: ("Wave3", 1))

    with mm.mic_muted_during_tts(enabled=True) as outer:
        assert outer.applied is True
        assert pulse["src0"] is True
        assert mm.tts_mute_depth() == 1
        with mm.mic_muted_during_tts(enabled=True) as inner:
            assert inner.applied is True
            assert mm.tts_mute_depth() == 2
        assert mm.tts_mute_depth() == 1
        assert pulse["src0"] is True  # still held by outer
    assert mm.tts_mute_depth() == 0
    assert pulse["src0"] is False
    assert alsa["on"] is True


def test_outer_exit_unmutes_alsa(monkeypatch):
    """Normal TTS end must restore ALSA, not only Pulse."""
    alsa_calls: list[bool] = []
    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(mm, "default_source", lambda: "src0")
    monkeypatch.setattr(mm, "source_is_muted", lambda s: False)
    monkeypatch.setattr(mm, "set_source_mute", lambda *a, **k: True)
    monkeypatch.setattr(
        mm,
        "set_alsa_mic_capture",
        lambda on, card=None: alsa_calls.append(on) or True,
    )
    monkeypatch.setattr(mm, "find_wave_alsa_card", lambda: ("Wave3", 1))

    with mm.mic_muted_during_tts(enabled=True):
        pass
    assert True in alsa_calls  # unmute path


def test_repair_post_tts_clears_stuck_depth(monkeypatch):
    logs: list[tuple] = []
    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(mm, "set_source_mute", lambda *a, **k: True)
    monkeypatch.setattr(mm, "set_alsa_mic_capture", lambda *a, **k: True)
    monkeypatch.setattr(mm, "find_wave_alsa_card", lambda: None)
    monkeypatch.setattr(mm, "default_source", lambda: "src0")
    monkeypatch.setattr(mm, "source_is_muted", lambda s: False)

    def fake_log(kind, **kw):
        logs.append((kind, kw))

    monkeypatch.setattr("hark.syslog.log", fake_log)

    mm._depth = 1
    mm._saved = mm.MuteState(source="src0", was_muted=False, applied=True)
    rep = mm.repair_tts_mute_after_play(mute_was_enabled=True, mute_applied=True)
    assert rep["repaired"] is True
    assert "depth_nonzero" in rep["reasons"]
    assert mm.tts_mute_depth() == 0
    assert any(k == "mic.mute_desync" for k, _ in logs)


def test_repair_post_tts_fixes_source_still_muted(monkeypatch):
    pulse = {"src0": True}
    logs: list[str] = []
    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(mm, "default_source", lambda: "src0")
    monkeypatch.setattr(mm, "source_is_muted", lambda s: pulse[s])
    monkeypatch.setattr(
        mm,
        "set_source_mute",
        lambda s, mute: pulse.__setitem__(s, mute) or True,
    )
    monkeypatch.setattr(mm, "set_alsa_mic_capture", lambda *a, **k: True)
    monkeypatch.setattr(mm, "find_wave_alsa_card", lambda: None)
    monkeypatch.setattr(
        "hark.syslog.log", lambda kind, **kw: logs.append(kind)
    )

    rep = mm.repair_tts_mute_after_play(mute_was_enabled=True, mute_applied=True)
    assert rep["repaired"] is True
    assert "source_still_muted" in rep["reasons"]
    assert pulse["src0"] is False
    assert "mic.mute_desync" in logs


def test_repair_noop_when_clean(monkeypatch):
    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(mm, "default_source", lambda: "src0")
    monkeypatch.setattr(mm, "source_is_muted", lambda s: False)
    rep = mm.repair_tts_mute_after_play(mute_was_enabled=True, mute_applied=True)
    assert rep["repaired"] is False
    assert rep["reasons"] == []


def test_run_tts_repairs_after_play(monkeypatch):
    """run_tts finally path calls repair even if play succeeds."""
    from hark.speech import run_tts

    repairs: list[dict] = []

    class FakeDuck:
        def __enter__(self):
            return SimpleNamespace(as_meta=lambda: {"media_ducked": False})

        def __exit__(self, *a):
            return False

    class FakeMute:
        def __enter__(self):
            return mm.MuteState(source="src0", was_muted=False, applied=True)

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "hark.speech.lookup_cached_tts", lambda *a, **k: b"fake-audio"
    )
    monkeypatch.setattr(
        "hark.speech.play_wav_bytes",
        lambda *a, **k: SimpleNamespace(duration_ms=100),
    )
    monkeypatch.setattr("hark.speech.duck_media", lambda *a, **k: FakeDuck())
    monkeypatch.setattr("hark.speech.mic_muted_during_tts", lambda **k: FakeMute())
    monkeypatch.setattr(
        "hark.speech.repair_tts_mute_after_play",
        lambda **k: repairs.append(k) or {"repaired": False, "reasons": []},
    )
    monkeypatch.setattr(
        "hark.conference.apply_conference_hold",
        lambda *a, **k: SimpleNamespace(
            skipped=False, as_meta=lambda: {"held": False}
        ),
    )
    # Usage store may hit disk — allow real or stub
    cfg = HarkConfig()
    cfg.audio.mute_mic_during_tts = True
    cfg.audio.hold_during_conference = False
    out = run_tts(cfg, "hello", play=True, conference_policy="force", use_cache=True)
    assert out["ok"] is True
    assert len(repairs) == 1
    assert repairs[0]["mute_was_enabled"] is True
    assert repairs[0]["mute_applied"] is True

"""B105: pause-gated streaming TTS — min operator quiet before live acks play."""

from __future__ import annotations

import json
from types import SimpleNamespace

from hark.config import AmbientConfig, HarkConfig, config_to_dict, load_config
from hark.listen_control import (
    clear_active_listen,
    clear_voice_activity,
    operator_quiet_s,
    read_voice_activity,
    register_active_listen,
    touch_voice_activity,
)
from hark.mic_coord import (
    clear_ambient_pause,
    wait_until_tts_play_allowed,
)
from hark.monitor_feed import compact_mode_a_event
from hark.partial import make_partial_event


def test_streaming_ack_min_quiet_config_default():
    assert AmbientConfig().streaming_ack_min_quiet_s == 2.0
    assert HarkConfig().ambient.streaming_ack_min_quiet_s == 2.0
    d = config_to_dict(HarkConfig())
    assert d["ambient"]["streaming_ack_min_quiet_s"] == 2.0


def test_streaming_ack_min_quiet_loads(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
version = 1
[ambient]
streaming = true
streaming_ack_min_quiet_s = 2.5
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.ambient.streaming is True
    assert cfg.ambient.streaming_ack_min_quiet_s == 2.5


def test_voice_activity_touch_and_quiet(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    clear_voice_activity()
    assert operator_quiet_s() is None

    register_active_listen("svoice1", mode="radio")
    # No voice yet → quiet from started_at
    q0 = operator_quiet_s()
    assert q0 is not None
    assert q0 >= 0.0

    # Touch with fake clock
    touch_voice_activity(stream_id="svoice1")
    voice = read_voice_activity()
    assert voice is not None
    assert voice.get("stream_id") == "svoice1"
    assert "last_voice_at" in voice

    # Quiet immediately after touch is ~0
    q1 = operator_quiet_s(now=float(voice["last_voice_at"]) + 0.05)
    assert q1 is not None
    assert q1 < 0.2

    # After 2.5s of wall time since last voice
    q2 = operator_quiet_s(now=float(voice["last_voice_at"]) + 2.5)
    assert q2 is not None
    assert q2 >= 2.4

    clear_active_listen("svoice1")
    assert operator_quiet_s() is None
    assert read_voice_activity() is None


def test_wait_streaming_holds_until_min_quiet():
    """Continuous talk (quiet < min) → hold; then quiet ≥ min → allow."""
    clock = {"t": 0.0}
    quiet = {"s": 0.3}
    active = {"on": True}

    def probe():
        if not active["on"]:
            return SimpleNamespace(
                active=False,
                reason=None,
                sources=(),
                stream_id=None,
                mode=None,
                pid=None,
            )
        return SimpleNamespace(
            active=True,
            reason="listen:radio:s1",
            sources=("listen.active",),
            stream_id="s1",
            mode="radio",
            pid=999,
        )

    def sleep(s: float) -> None:
        clock["t"] += s
        # After 0.5s of wait, operator becomes quiet enough
        if clock["t"] >= 0.5:
            quiet["s"] = 2.2

    result = wait_until_tts_play_allowed(
        streaming=True,
        min_quiet_s=2.0,
        max_wait_s=10.0,
        poll_ms=100,
        quiet_ms=0,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock["t"],
        time_fn=lambda: clock["t"],
        probe_fn=probe,
        quiet_fn=lambda: quiet["s"],
    )
    assert result.deferred is True
    assert result.timed_out is False
    assert result.gate == "quiet"
    assert result.quiet_s is not None and result.quiet_s >= 2.0
    assert result.min_quiet_s == 2.0
    assert result.wait_ms >= 500


def test_wait_streaming_holds_during_continuous_speech_until_timeout():
    clock = {"t": 0.0}

    def probe():
        return SimpleNamespace(
            active=True,
            reason="listen:radio:busy",
            sources=("listen.active",),
            stream_id="busy",
            mode="radio",
            pid=1,
        )

    def sleep(s: float) -> None:
        clock["t"] += s

    result = wait_until_tts_play_allowed(
        streaming=True,
        min_quiet_s=2.0,
        max_wait_s=0.3,
        poll_ms=50,
        quiet_ms=0,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock["t"],
        probe_fn=probe,
        quiet_fn=lambda: 0.1,  # never reaches 2s quiet
    )
    assert result.deferred is True
    assert result.timed_out is True
    assert result.gate == "quiet"
    assert result.wait_ms >= 300


def test_wait_streaming_plays_when_capture_ends_before_quiet():
    clock = {"t": 0.0}
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        if calls["n"] <= 2:
            return SimpleNamespace(
                active=True,
                reason="listen:radio:endsoon",
                sources=("listen.active",),
                stream_id="endsoon",
                mode="radio",
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

    result = wait_until_tts_play_allowed(
        streaming=True,
        min_quiet_s=2.0,
        max_wait_s=5.0,
        poll_ms=50,
        quiet_ms=0,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock["t"],
        probe_fn=probe,
        quiet_fn=lambda: 0.2,
    )
    assert result.deferred is True
    assert result.timed_out is False
    assert result.gate == "quiet"


def test_wait_non_streaming_still_waits_for_idle():
    """streaming=false keeps B097 idle-until-capture-ends behavior."""
    clock = {"t": 0.0}
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        if calls["n"] <= 2:
            return SimpleNamespace(
                active=True,
                reason="listen:radio:hold",
                sources=("listen.active",),
                stream_id="hold",
                mode="radio",
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

    result = wait_until_tts_play_allowed(
        streaming=False,
        min_quiet_s=2.0,
        max_wait_s=5.0,
        poll_ms=50,
        quiet_ms=0,
        sleep_fn=sleep,
        monotonic_fn=lambda: clock["t"],
        probe_fn=probe,
        quiet_fn=lambda: 99.0,  # quiet alone must not release HOLD mode
    )
    assert result.deferred is True
    assert result.timed_out is False
    assert result.gate == "idle"
    assert calls["n"] >= 3


def test_wait_immediate_when_no_capture():
    result = wait_until_tts_play_allowed(
        streaming=True,
        min_quiet_s=2.0,
        max_wait_s=5.0,
        probe_fn=lambda: SimpleNamespace(
            active=False,
            reason=None,
            sources=(),
            stream_id=None,
            mode=None,
            pid=None,
        ),
    )
    assert result.deferred is False
    assert result.wait_ms == 0


def test_partial_event_includes_ack_min_quiet_when_streaming():
    hold = make_partial_event(stream_id="s1", seq=1, text="hey", streaming=False)
    live = make_partial_event(stream_id="s1", seq=2, text="hey", streaming=True)
    custom = make_partial_event(
        stream_id="s1", seq=3, text="hey", streaming=True, ack_min_quiet_s=1.5
    )
    assert "ack_min_quiet_s" not in hold
    assert live["ack_min_quiet_s"] == 2.0
    assert custom["ack_min_quiet_s"] == 1.5
    assert "quiet" in live["instructions"].lower() or "2s" in live["instructions"]
    assert "HOLD" in live["instructions"] or "continuous" in live["instructions"].lower()


def test_monitor_compact_includes_ack_min_quiet():
    base = {
        "kind": "ambient.partial",
        "event_id": "e1",
        "stream_id": "s1",
        "seq": 1,
        "text": "please ship it",
        "fragment": "ship it",
        "streaming": True,
        "ack_min_quiet_s": 2.0,
    }
    live = compact_mode_a_event(base)
    assert live["streaming"] is True
    assert live["ack_min_quiet_s"] == 2.0
    hold = compact_mode_a_event({**base, "streaming": False})
    assert "ack_min_quiet_s" not in hold


def test_register_clears_stale_voice(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    clear_active_listen()
    register_active_listen("old", mode="radio")
    touch_voice_activity(stream_id="old")
    assert read_voice_activity() is not None
    register_active_listen("new", mode="radio")
    # New register clears voice so quiet is measured from capture start
    assert read_voice_activity() is None
    clear_active_listen()
    clear_ambient_pause()

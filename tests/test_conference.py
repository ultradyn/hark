"""Conference detection and TTS hold (B017) — no real Zoom."""

from __future__ import annotations

import json

import pytest

from hark.config import AudioConfig, HarkConfig, load_config
from hark.conference import (
    ConferenceMatch,
    apply_conference_hold,
    detect_conference,
    is_conference_active,
    make_announce_held_event,
    wait_until_conference_free,
)


def test_detect_zoom_process():
    entries = [
        ("1", "systemd", "/sbin/init"),
        ("42", "zoom", "/opt/zoom/ZoomLauncher"),
    ]
    match = detect_conference(
        proc_entries=entries,
        stream_blobs=[],
        check_audio=True,
        fail_open=True,
    )
    assert match.active is True
    assert any("zoom" in m for m in match.matched)
    assert "process" in match.sources


def test_detect_meet_cmdline():
    entries = [
        (
            "99",
            "chrome",
            "/usr/bin/chrome --type=renderer https://meet.google.com/abc-defg-hij",
        ),
    ]
    match = detect_conference(proc_entries=entries, stream_blobs=[], check_audio=False)
    assert match.active is True
    assert any("meet.google.com" in m for m in match.matched)


def test_detect_teams_stream_name():
    blob = """
Sink Input #12
    application.name = \"Microsoft Teams\"
    media.name = \"playback\"
"""
    match = detect_conference(
        proc_entries=[("1", "bash", "bash")],
        stream_blobs=[blob],
        check_audio=True,
    )
    assert match.active is True
    assert any("teams" in m for m in match.matched)
    assert "audio" in match.sources


def test_no_false_positive_on_normal_desktop():
    entries = [
        ("1", "systemd", "/sbin/init"),
        ("2", "fish", "fish"),
        ("3", "code", "/usr/share/code/code"),
        ("4", "firefox", "/usr/lib/firefox/firefox"),
    ]
    match = detect_conference(
        proc_entries=entries,
        stream_blobs=["application.name = \"Firefox\"\nmedia.name = \"Audio\""],
        check_audio=True,
    )
    assert match.active is False


def test_fail_open_when_no_sources(monkeypatch, tmp_path):
    # Missing proc root + no streams + fail_open → free
    missing = tmp_path / "no_proc"
    match = detect_conference(
        proc_root=missing,
        stream_blobs=[],
        check_audio=False,
        fail_open=True,
    )
    assert match.active is False


def test_fail_closed_when_configured(tmp_path):
    missing = tmp_path / "no_proc"
    match = detect_conference(
        proc_root=missing,
        stream_blobs=[],
        check_audio=False,
        fail_open=False,
    )
    assert match.active is True
    assert match.error


def test_is_conference_active_respects_config_names():
    cfg = HarkConfig(
        audio=AudioConfig(conference_process_names=["only-this-app"])
    )
    entries = [("1", "zoom", "zoom"), ("2", "only-this-app", "only-this-app")]
    assert (
        is_conference_active(
            cfg,
            proc_entries=entries,
            stream_blobs=[],
            check_audio=False,
        )
        is True
    )
    # With only zoom (not in custom list) and no cmdline meet markers:
    assert (
        is_conference_active(
            cfg,
            proc_entries=[("1", "zoom", "zoom")],
            stream_blobs=[],
            check_audio=False,
        )
        is False
    )


def test_hold_waits_then_resumes(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = HarkConfig(
        audio=AudioConfig(
            hold_during_conference=True,
            conference_chime_only=True,
            conference_poll_ms=10,
            conference_max_hold_s=5,
        )
    )
    state = {"n": 0}

    def active() -> bool:
        state["n"] += 1
        return state["n"] < 3  # free on third check

    chimes = {"n": 0}

    result = apply_conference_hold(
        cfg,
        "Allow deploy to production?",
        policy="hold",
        detect_match=ConferenceMatch(
            active=True, sources=("process",), matched=("proc:zoom",)
        ),
        is_active=active,
        play_chime=lambda: chimes.__setitem__("n", chimes["n"] + 1) or True,
        sleep_fn=lambda _s: None,
    )
    assert result.held is True
    assert result.skipped is False
    assert result.chime_played is True
    assert result.timed_out is False
    assert result.queue_id
    assert chimes["n"] == 1
    assert result.event is not None
    assert result.event["kind"] == "announce.held"

    qpath = tmp_path / "state" / "hark" / "announce_hold_queue.jsonl"
    assert qpath.is_file()
    lines = [json.loads(ln) for ln in qpath.read_text().splitlines() if ln.strip()]
    statuses = [ln["status"] for ln in lines]
    assert "held" in statuses
    assert "resumed" in statuses


def test_hold_skip_policy_does_not_wait():
    cfg = HarkConfig(audio=AudioConfig(hold_during_conference=True))
    slept = {"n": 0}
    result = apply_conference_hold(
        cfg,
        "Hark ambient is listening.",
        policy="skip",
        detect_match=ConferenceMatch(active=True, matched=("proc:teams",)),
        sleep_fn=lambda _s: slept.__setitem__("n", slept["n"] + 1),
    )
    assert result.held is True
    assert result.skipped is True
    assert slept["n"] == 0


def test_hold_force_or_disabled_never_holds():
    cfg = HarkConfig(audio=AudioConfig(hold_during_conference=False))
    r1 = apply_conference_hold(
        cfg,
        "hello",
        policy="hold",
        detect_match=ConferenceMatch(active=True, matched=("proc:zoom",)),
    )
    assert r1.held is False

    cfg2 = HarkConfig(audio=AudioConfig(hold_during_conference=True))
    r2 = apply_conference_hold(
        cfg2,
        "hello",
        policy="force",
        detect_match=ConferenceMatch(active=True, matched=("proc:zoom",)),
    )
    assert r2.held is False


def test_hold_timeout_resumes_anyway(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = HarkConfig(
        audio=AudioConfig(
            hold_during_conference=True,
            conference_chime_only=False,
            conference_poll_ms=10,
            conference_max_hold_s=0.05,
        )
    )
    clock = {"t": 0.0}

    def mono() -> float:
        return clock["t"]

    def sleep(s: float) -> None:
        clock["t"] += s

    result = apply_conference_hold(
        cfg,
        "Blocked question",
        policy="hold",
        detect_match=ConferenceMatch(active=True, matched=("proc:zoom",)),
        is_active=lambda: True,
        play_chime=lambda: True,
        sleep_fn=sleep,
        monotonic=mono,
    )
    assert result.held is True
    assert result.timed_out is True
    assert result.chime_played is False  # chime_only false


def test_wait_until_free_immediate():
    free, ms = wait_until_conference_free(
        is_active=lambda: False,
        poll_s=0.01,
        max_hold_s=1,
        sleep_fn=lambda _s: None,
    )
    assert free is True
    assert ms >= 0


def test_announce_held_event_shape():
    ev = make_announce_held_event(
        "Allow rm -rf?",
        matched=["proc:zoom"],
        sources=["process"],
        chime=True,
        queue_id="abc",
    )
    assert ev["kind"] == "announce.held"
    assert ev["reason"] == "conference"
    assert ev["schema"] == "hark.event.v1"
    assert ev["queue_id"] == "abc"
    assert "Allow" in (ev.get("text_preview") or "")


def test_config_defaults_hold_on(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("version = 1\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.audio.hold_during_conference is True
    assert cfg.audio.conference_chime_only is True
    assert "zoom" in cfg.audio.conference_process_names
    assert cfg.audio.conference_fail_open is True


def test_config_loads_conference_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[audio]
hold_during_conference = false
conference_chime_only = false
conference_process_names = ["zoom", "custom-meet"]
conference_poll_ms = 500
conference_max_hold_s = 30
conference_fail_open = false
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.audio.hold_during_conference is False
    assert cfg.audio.conference_chime_only is False
    assert cfg.audio.conference_process_names == ["zoom", "custom-meet"]
    assert cfg.audio.conference_poll_ms == 500
    assert cfg.audio.conference_max_hold_s == 30.0
    assert cfg.audio.conference_fail_open is False


def test_run_tts_skips_when_conference_policy_skip(monkeypatch):
    from hark.speech import run_tts
    from hark.conference import HoldResult

    def fake_hold(cfg, text, **kwargs):
        assert kwargs.get("policy") == "skip"
        return HoldResult(held=True, skipped=True, matched=["proc:zoom"])

    monkeypatch.setattr("hark.conference.apply_conference_hold", fake_hold)
    # Ensure synthesize is never called
    monkeypatch.setattr(
        "hark.speech.resolve_tts",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should skip")),
    )

    out = run_tts(
        HarkConfig(),
        "Hark ambient is listening.",
        play=True,
        conference_policy="skip",
    )
    assert out["ok"] is True
    assert out.get("skipped") is True
    assert out["provider"] == "skipped"


def test_run_tts_holds_then_speaks(monkeypatch):
    from hark.speech import run_tts
    from hark.conference import HoldResult

    calls = {"synth": 0, "play": 0}

    def fake_hold(cfg, text, **kwargs):
        return HoldResult(
            held=True,
            skipped=False,
            chime_played=True,
            wait_ms=100,
            matched=["proc:zoom"],
            queue_id="q1",
        )

    class FakeTts:
        provider = "xai"
        voice = "eve"
        content_type = "audio/mpeg"

        def synthesize(self, text, voice=None):
            calls["synth"] += 1
            from hark.providers.base import SynthResult

            return SynthResult(
                audio=b"ID3fake",
                provider="xai",
                voice=voice or "eve",
                content_type="audio/mpeg",
            )

    monkeypatch.setattr("hark.conference.apply_conference_hold", fake_hold)
    monkeypatch.setattr("hark.speech.resolve_tts", lambda *a, **k: FakeTts())
    monkeypatch.setattr("hark.speech.lookup_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr(
        "hark.speech.play_wav_bytes",
        lambda *a, **k: type("R", (), {"duration_ms": 10})(),
    )
    monkeypatch.setattr(
        "hark.speech.mic_muted_during_tts",
        lambda **k: __import__("contextlib").nullcontext(
            type("M", (), {"applied": False})()
        ),
    )
    monkeypatch.setattr(
        "hark.speech.duck_media",
        lambda *a, **k: __import__("contextlib").nullcontext(
            type(
                "D",
                (),
                {
                    "applied": False,
                    "as_meta": lambda self: {
                        "media_ducked": False,
                        "duck_level": 0.15,
                        "duck_count": 0,
                        "duck_indices": [],
                        "mpris_paused": [],
                        "duck_error": None,
                        "duck_nested": False,
                    },
                },
            )()
        ),
    )
    monkeypatch.setattr("hark.speech.store_cached_tts", lambda *a, **k: None)

    # UsageStore.record_tts no-op
    class U:
        def record_tts(self, **k):
            return None

    monkeypatch.setattr("hark.speech.UsageStore", U)

    out = run_tts(HarkConfig(), "Allow this?", play=True, conference_policy="hold")
    assert out["ok"] is True
    assert calls["synth"] == 1
    assert out["conference"]["held"] is True
    assert out["conference"]["queue_id"] == "q1"


def test_idle_discord_process_not_active_when_audio_scanned():
    """Resident Discord without conference streams must not hold TTS."""
    entries = [
        ("1", "systemd", "/sbin/init"),
        ("50", "Discord", "/opt/discord/Discord"),
    ]
    blob = """
Sink Input #3
    application.name = "Firefox"
    media.name = "Audio"
"""
    match = detect_conference(
        proc_entries=entries,
        stream_blobs=[blob],
        check_audio=True,
    )
    assert match.active is False
    assert any("discord" in m for m in match.matched)
    assert "audio" in match.sources


def test_discord_active_when_stream_present():
    entries = [("50", "Discord", "/opt/discord/Discord")]
    blob = """
Sink Input #9
    application.name = "Discord"
    media.name = "Voice Call"
"""
    match = detect_conference(
        proc_entries=entries,
        stream_blobs=[blob],
        check_audio=True,
    )
    assert match.active is True
    assert any(m.startswith("stream:") for m in match.matched)


# ---------------------------------------------------------------------------
# B118 / B117: browser Teams / Chromium VC (Playback + RecordStream)
# ---------------------------------------------------------------------------

# Dogfood-shaped pactl dumps (2026-07-15 teams-vc-check): browser Teams only
# exposes application.name "Chromium" / "Chromium input" — no teams markers.

_CHROMIUM_PLAYBACK_BLOB = """
Sink Input #401
	Driver: PipeWire
	Owner Module: n/a
	Client: 400
	Sink: 42
	Sample Specification: float32le 2ch 48000Hz
	Corked: no
	Mute: no
	Properties:
		application.name = "Chromium"
		media.name = "Playback"
		media.class = "Stream/Output/Audio"
		application.process.id = "3174230"
"""

_CHROMIUM_PLAYBACK_BLOB_2 = """
Sink Input #402
	Driver: PipeWire
	Corked: no
	Mute: no
	Properties:
		application.name = "Chromium"
		media.name = "Playback"
		application.process.id = "3174230"
"""

_CHROMIUM_INPUT_RECORD_BLOB = """
Source Output #510
	Driver: PipeWire
	Owner Module: n/a
	Client: 509
	Source: 43
	Sample Specification: float32le 1ch 48000Hz
	Corked: no
	Mute: no
	Properties:
		application.name = "Chromium input"
		media.name = "RecordStream"
		media.class = "Stream/Input/Audio"
		application.process.id = "3174230"
"""


def test_browser_teams_chromium_playback_plus_recordstream_active():
    """B118 dogfood: Chromium Playback sink-inputs + Chromium input RecordStream."""
    match = detect_conference(
        proc_entries=[
            ("1", "systemd", "/sbin/init"),
            ("3174230", "chromium", "/usr/lib/chromium/chromium"),
        ],
        stream_blobs=[
            _CHROMIUM_PLAYBACK_BLOB + _CHROMIUM_PLAYBACK_BLOB_2,
            _CHROMIUM_INPUT_RECORD_BLOB,
        ],
        check_audio=True,
    )
    assert match.active is True
    assert any("browser-av" in m for m in match.matched)
    assert "audio" in match.sources


def test_browser_chromium_playback_only_stays_free():
    """Casual browser media (YouTube etc.) must not hold TTS."""
    match = detect_conference(
        proc_entries=[("3174230", "chromium", "/usr/lib/chromium/chromium")],
        stream_blobs=[_CHROMIUM_PLAYBACK_BLOB],
        check_audio=True,
    )
    assert match.active is False
    assert not any("browser-av" in m for m in match.matched)


def test_browser_chromium_recordstream_only_stays_free():
    """Mic capture alone without conference-like playback stays free."""
    match = detect_conference(
        proc_entries=[("3174230", "chromium", "/usr/lib/chromium/chromium")],
        stream_blobs=[_CHROMIUM_INPUT_RECORD_BLOB],
        check_audio=True,
    )
    assert match.active is False
    assert not any("browser-av" in m for m in match.matched)


def test_browser_chrome_and_firefox_av_heuristic():
    chrome_play = """
Sink Input #10
    Properties:
        application.name = "Google Chrome"
        media.name = "Playback"
"""
    chrome_rec = """
Source Output #11
    Properties:
        application.name = "Chrome input"
        media.name = "RecordStream"
"""
    match = detect_conference(
        proc_entries=[("1", "bash", "bash")],
        stream_blobs=[chrome_play, chrome_rec],
        check_audio=True,
    )
    assert match.active is True
    assert any("browser-av" in m for m in match.matched)

    ff_play = """
Sink Input #20
    Properties:
        application.name = "Firefox"
        media.name = "Playback"
"""
    ff_rec = """
Source Output #21
    Properties:
        application.name = "Firefox"
        media.name = "RecordStream"
"""
    match_ff = detect_conference(
        proc_entries=[("1", "bash", "bash")],
        stream_blobs=[ff_play, ff_rec],
        check_audio=True,
    )
    assert match_ff.active is True
    assert any("browser-av" in m for m in match_ff.matched)


def test_browser_av_heuristic_disabled_via_flag():
    """Optional conference_browser_av_heuristic=false keeps prior behavior."""
    match = detect_conference(
        proc_entries=[("1", "bash", "bash")],
        stream_blobs=[_CHROMIUM_PLAYBACK_BLOB, _CHROMIUM_INPUT_RECORD_BLOB],
        check_audio=True,
        browser_av_heuristic=False,
    )
    assert match.active is False
    assert not any("browser-av" in m for m in match.matched)


def test_browser_av_heuristic_config_default_on(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("version = 1\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.audio.conference_browser_av_heuristic is True

    path2 = tmp_path / "config2.toml"
    path2.write_text(
        """
[audio]
conference_browser_av_heuristic = false
""",
        encoding="utf-8",
    )
    cfg2 = load_config(path2)
    assert cfg2.audio.conference_browser_av_heuristic is False

    # is_conference_active respects config
    assert (
        is_conference_active(
            cfg,
            proc_entries=[("1", "bash", "bash")],
            stream_blobs=[_CHROMIUM_PLAYBACK_BLOB, _CHROMIUM_INPUT_RECORD_BLOB],
            check_audio=True,
        )
        is True
    )
    assert (
        is_conference_active(
            cfg2,
            proc_entries=[("1", "bash", "bash")],
            stream_blobs=[_CHROMIUM_PLAYBACK_BLOB, _CHROMIUM_INPUT_RECORD_BLOB],
            check_audio=True,
        )
        is False
    )


def test_browser_av_ignores_non_browser_playback_plus_chromium_mic():
    """Non-browser music + Chromium mic alone is not a conference."""
    music = """
Sink Input #7
    Properties:
        application.name = "Spotify"
        media.name = "playback"
"""
    match = detect_conference(
        proc_entries=[("1", "bash", "bash")],
        stream_blobs=[music, _CHROMIUM_INPUT_RECORD_BLOB],
        check_audio=True,
    )
    assert match.active is False


def test_browser_av_four_playback_streams_dogfood_shape():
    """Multiple Chromium Playback streams + one RecordStream (Teams dogfood)."""
    plays = "".join(
        f"""
Sink Input #{400 + i}
    Corked: no
    Properties:
        application.name = "Chromium"
        media.name = "Playback"
        application.process.id = "3174230"
"""
        for i in range(4)
    )
    match = detect_conference(
        proc_entries=[
            ("3174230", "chromium", "/usr/lib/chromium/chromium --type=renderer"),
        ],
        stream_blobs=[plays, _CHROMIUM_INPUT_RECORD_BLOB],
        check_audio=True,
    )
    assert match.active is True
    assert any("browser-av" in m for m in match.matched)

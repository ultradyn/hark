"""Active media detection + ducking (B044/B045) — fixture pactl blobs, no live audio."""

from __future__ import annotations

from hark.audio.media import (
    DuckState,
    MediaMatch,
    SinkInputInfo,
    detect_media,
    duck_media,
    duckable_indices_and_volumes,
    filter_duckable,
    is_hark_owned,
    is_media_active,
    parse_sink_inputs,
    pause_mpris_players,
    probe_mpris_playing,
    resume_mpris_players,
    set_sink_input_volume,
)

# ---------------------------------------------------------------------------
# Fixtures: realistic pactl list sink-inputs fragments
# ---------------------------------------------------------------------------

SPOTIFY_PLAYING = """
Sink Input #42
	Driver: PipeWire
	Owner Module: n/a
	Client: 40
	Sink: 61
	Sample Specification: s16le 2ch 44100Hz
	Channel Map: front-left,front-right
	Format: pcm, format.sample_format = "\\"s16le\\""  format.rate = "44100"  format.channels = "2"
	Corked: no
	Mute: no
	Volume: front-left: 52428 /  80% / -5.81 dB,   front-right: 52428 /  80% / -5.81 dB
	        balance 0.00
	Buffer Latency: 0 usec
	Sink Latency: 0 usec
	Resample method: PipeWire
	Properties:
		application.name = "Spotify"
		media.name = "Die With A Smile"
		application.process.id = "12345"
		application.process.binary = "spotify"
		node.name = "spotify"
		media.class = "Stream/Output/Audio"
"""

CORKED_BROWSER = """
Sink Input #7
	Driver: PipeWire
	Corked: yes
	Mute: no
	Volume: mono: 65536 / 100% / 0.00 dB
	Properties:
		application.name = "Firefox"
		media.name = "AudioStream"
		application.process.binary = "firefox"
"""

MUTED_VLC = """
Sink Input #9
	Driver: protocol-native.c
	State: RUNNING
	Corked: no
	Mute: yes
	Volume: front-left: 65536 / 100% / 0.00 dB,   front-right: 65536 / 100% / 0.00 dB
	Properties:
		application.name = "VLC media player"
		media.name = "audio stream"
		application.process.binary = "vlc"
"""

HARK_PAPLAY = """
Sink Input #2772116
	Driver: PipeWire
	Corked: no
	Mute: no
	Volume: mono: 65536 / 100% / 0.00 dB
	Properties:
		media.format = "WAV (Microsoft)"
		application.name = "paplay"
		media.name = "/usr/share/sounds/alsa/Front_Center.wav"
		application.process.binary = "pacat"
		node.name = "paplay"
"""

HARK_FFPLAY = """
Sink Input #100
	Driver: PipeWire
	Corked: no
	Mute: no
	Volume: mono: 32768 /  50% / -18.06 dB
	Properties:
		application.name = "ffplay"
		media.name = "hark-tts.mp3"
		application.process.binary = "ffplay"
"""

CLASSIC_PULSE_RUNNING = """
Sink Input #5
	Driver: protocol-native.c
	Owner Module: 12
	Client: 15
	Sink: 0
	Sample Specification: s16le 2ch 44100Hz
	Channel Map: front-left,front-right
	Format: pcm
	Corked: no
	Mute: no
	Volume: front-left: 32768 /  50% / -18.06 dB,   front-right: 32768 /  50% / -18.06 dB
	        balance 0.00
	Buffer Latency: 46321 usec
	Sink Latency: 20195 usec
	Resample method: speex-float-1
	Properties:
		media.name = "Spotify"
		application.name = "Spotify"
		native-protocol.peer = "UNIX socket client"
		application.process.id = "2222"
		application.process.binary = "spotify"
"""

STATE_IDLE = """
Sink Input #3
	Driver: PipeWire
	State: IDLE
	Corked: no
	Mute: no
	Volume: mono: 65536 / 100% / 0.00 dB
	Properties:
		application.name = "mpv"
		media.name = "track.flac"
"""

ZOOM_PLAYING = """
Sink Input #12
	Driver: PipeWire
	Corked: no
	Mute: no
	Volume: mono: 65536 / 100% / 0.00 dB
	Properties:
		application.name = "ZOOM VoiceEngine"
		media.name = "playback"
		application.process.binary = "zoom"
"""

MULTI_MIXED = (
    SPOTIFY_PLAYING
    + "\n"
    + CORKED_BROWSER
    + "\n"
    + HARK_PAPLAY
    + "\n"
    + ZOOM_PLAYING
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_spotify_playing():
    rows = parse_sink_inputs(SPOTIFY_PLAYING)
    assert len(rows) == 1
    s = rows[0]
    assert s.index == 42
    assert s.corked is False
    assert s.mute is False
    assert s.is_playing is True
    assert s.application_name == "Spotify"
    assert s.volume_pct == 80.0
    assert s.volume_raw == "52428"
    assert s.binary == "spotify"


def test_parse_corked_not_playing():
    rows = parse_sink_inputs(CORKED_BROWSER)
    assert len(rows) == 1
    assert rows[0].corked is True
    assert rows[0].is_playing is False


def test_parse_muted_not_playing():
    rows = parse_sink_inputs(MUTED_VLC)
    assert rows[0].mute is True
    assert rows[0].is_playing is False


def test_parse_empty_blob():
    assert parse_sink_inputs("") == []
    assert parse_sink_inputs("   \n") == []
    assert parse_sink_inputs("no sink inputs here") == []


def test_parse_classic_pulse_volume():
    rows = parse_sink_inputs(CLASSIC_PULSE_RUNNING)
    assert rows[0].index == 5
    assert rows[0].volume_pct == 50.0
    assert rows[0].volume_raw == "32768"
    assert rows[0].is_playing is True


def test_parse_state_idle_not_playing():
    rows = parse_sink_inputs(STATE_IDLE)
    assert rows[0].state and rows[0].state.upper() == "IDLE"
    assert rows[0].is_playing is False


def test_parse_multi_mixed_count():
    rows = parse_sink_inputs(MULTI_MIXED)
    assert len(rows) == 4
    assert [r.index for r in rows] == [42, 7, 2772116, 12]


# ---------------------------------------------------------------------------
# Hark / conference exclusion
# ---------------------------------------------------------------------------


def test_hark_owned_paplay_and_ffplay():
    paplay = parse_sink_inputs(HARK_PAPLAY)[0]
    ffplay = parse_sink_inputs(HARK_FFPLAY)[0]
    spotify = parse_sink_inputs(SPOTIFY_PLAYING)[0]
    assert is_hark_owned(paplay)
    assert is_hark_owned(ffplay)
    assert not is_hark_owned(spotify)


def test_filter_duckable_excludes_hark_and_corked():
    rows = parse_sink_inputs(MULTI_MIXED)
    duckable = filter_duckable(rows)
    # Spotify + Zoom (corked firefox + paplay excluded)
    assert {s.index for s in duckable} == {42, 12}


def test_filter_duckable_exclude_conference():
    rows = parse_sink_inputs(MULTI_MIXED)
    duckable = filter_duckable(rows, exclude_conference=True)
    assert [s.index for s in duckable] == [42]
    assert duckable[0].application_name == "Spotify"


def test_duckable_indices_and_volumes_helper():
    triples = duckable_indices_and_volumes(blob=MULTI_MIXED, exclude_conference=True)
    assert triples == [(42, 80.0, "52428")]


# ---------------------------------------------------------------------------
# detect_media / is_media_active
# ---------------------------------------------------------------------------


def test_detect_playing_media_active():
    match = detect_media(
        sink_input_blob=SPOTIFY_PLAYING,
        check_mpris=False,
    )
    assert match.active is True
    assert "sink-input" in match.sources
    assert match.indices == (42,)
    assert "Spotify" in match.app_names
    assert match.volumes == (80.0,)
    assert match.volume_raw == ("52428",)


def test_detect_corked_inactive():
    match = detect_media(sink_input_blob=CORKED_BROWSER, check_mpris=False)
    assert match.active is False
    assert match.indices == ()


def test_detect_empty_inactive():
    match = detect_media(sink_input_blob="", check_mpris=False)
    assert match.active is False
    assert match.detail


def test_detect_hark_only_inactive():
    match = detect_media(
        sink_input_blob=HARK_PAPLAY + "\n" + HARK_FFPLAY,
        check_mpris=False,
    )
    assert match.active is False
    assert match.indices == ()


def test_detect_mixed_excludes_hark():
    match = detect_media(sink_input_blob=MULTI_MIXED, check_mpris=False)
    assert match.active is True
    assert 2772116 not in match.indices
    assert 7 not in match.indices  # corked
    assert 42 in match.indices
    assert 12 in match.indices  # zoom still *seen*; callers prefer B017 hold


def test_is_media_active_returns_match():
    m = is_media_active(sink_input_blob=SPOTIFY_PLAYING, check_mpris=False)
    assert isinstance(m, MediaMatch)
    assert m.active is True


def test_fail_open_when_pactl_missing(monkeypatch):
    import hark.audio.media as media_mod

    monkeypatch.setattr(media_mod, "which", lambda _name: None)
    # No blob, no pactl on PATH → inactive (fail-open)
    match = detect_media(check_mpris=False, fail_open=True)
    assert match.active is False


def test_fail_open_run_capture_empty():
    match = detect_media(
        check_mpris=False,
        run_capture=lambda _args: "",
        fail_open=True,
    )
    assert match.active is False


# ---------------------------------------------------------------------------
# MPRIS (mocked)
# ---------------------------------------------------------------------------


def test_mpris_playing_via_metadata_format():
    def fake(args: list[str]) -> str:
        if "metadata" in args:
            return "spotify|Playing\nchromium.instance1|Stopped\n"
        return ""

    players = probe_mpris_playing(run_capture=fake, which_fn=lambda _: "/usr/bin/playerctl")
    assert players == ["spotify"]


def test_mpris_fallback_list_and_status():
    def fake(args: list[str]) -> str:
        if args[:2] == ["playerctl", "-a"] and "metadata" in args:
            return ""
        if args == ["playerctl", "-l"]:
            return "vlc\nmpv\n"
        if args == ["playerctl", "-p", "vlc", "status"]:
            return "Paused\n"
        if args == ["playerctl", "-p", "mpv", "status"]:
            return "Playing\n"
        return ""

    players = probe_mpris_playing(run_capture=fake, which_fn=lambda _: "/bin/playerctl")
    assert players == ["mpv"]


def test_mpris_missing_tool():
    players = probe_mpris_playing(
        run_capture=lambda _a: "should-not-matter",
        which_fn=lambda _: None,
    )
    assert players == []


def test_detect_media_mpris_only():
    match = detect_media(
        sink_input_blob="",
        mpris_players=["spotify"],
        check_mpris=True,
    )
    assert match.active is True
    assert "mpris" in match.sources
    assert match.mpris_players == ("spotify",)
    assert match.indices == ()


def test_detect_media_mpris_mocked_probe():
    def fake(args: list[str]) -> str:
        if "metadata" in args:
            return "plasma-browser-integration|Playing\n"
        if args == ["pactl", "list", "sink-inputs"]:
            return ""
        return ""

    match = detect_media(check_mpris=True, run_capture=fake)
    assert match.active is True
    assert match.mpris_players == ("plasma-browser-integration",)


def test_media_match_as_meta():
    m = detect_media(sink_input_blob=SPOTIFY_PLAYING, check_mpris=False)
    meta = m.as_meta()
    assert meta["active"] is True
    assert meta["indices"] == [42]
    assert "Spotify" in meta["app_names"]


def test_sink_input_info_injected():
    info = SinkInputInfo(
        index=1,
        volume_pct=25.0,
        volume_raw="16384",
        mute=False,
        corked=False,
        application_name="mpv",
    )
    match = detect_media(sink_inputs=[info], check_mpris=False)
    assert match.active is True
    assert match.volumes == (25.0,)


# ---------------------------------------------------------------------------
# Duck / restore (B045) — mocked pactl, no live audio
# ---------------------------------------------------------------------------


def test_set_sink_input_volume_percent_and_raw():
    calls: list[list[str]] = []

    def fake(args: list[str]) -> bool:
        calls.append(args)
        return True

    assert set_sink_input_volume(42, 12, run_cmd=fake)
    assert set_sink_input_volume(42, 12.4, run_cmd=fake)
    assert set_sink_input_volume(42, "52428", run_cmd=fake)
    assert set_sink_input_volume(42, 52428, run_cmd=fake)
    assert calls[0] == ["pactl", "set-sink-input-volume", "42", "12%"]
    assert calls[1] == ["pactl", "set-sink-input-volume", "42", "12%"]
    assert calls[2] == ["pactl", "set-sink-input-volume", "42", "52428"]
    assert calls[3] == ["pactl", "set-sink-input-volume", "42", "52428"]


def test_duck_media_sets_and_restores_volume():
    """Spotify at 80% → duck 0.15 → 12%; restore raw 52428."""
    cmds: list[list[str]] = []

    def run_cmd(args: list[str]) -> bool:
        cmds.append(list(args))
        return True

    with duck_media(
        enabled=True,
        level=0.15,
        pause_players=False,
        exclude_conference=True,
        sink_input_blob=SPOTIFY_PLAYING,
        run_cmd=run_cmd,
        check_mpris=False,
    ) as state:
        assert state.applied is True
        assert state.indices == (42,)
        assert state.level == 0.15
        assert state.snapshots[0].volume_pct == 80.0
        assert state.snapshots[0].ducked_pct == 12.0  # 80 * 0.15
        assert state.snapshots[0].set_ok is True
        meta = state.as_meta()
        assert meta["media_ducked"] is True
        assert meta["duck_count"] == 1
        assert meta["duck_indices"] == [42]

    # set then restore
    assert any(
        c == ["pactl", "set-sink-input-volume", "42", "12%"] for c in cmds
    )
    assert any(
        c == ["pactl", "set-sink-input-volume", "42", "52428"] for c in cmds
    )
    # Restore after context exit
    assert state.snapshots[0].restore_ok is True


def test_duck_media_restores_on_exception():
    cmds: list[list[str]] = []

    def run_cmd(args: list[str]) -> bool:
        cmds.append(list(args))
        return True

    raised = False
    try:
        with duck_media(
            enabled=True,
            level=0.15,
            pause_players=False,
            sink_input_blob=SPOTIFY_PLAYING,
            run_cmd=run_cmd,
            check_mpris=False,
        ):
            raise RuntimeError("tts boom")
    except RuntimeError:
        raised = True
    assert raised
    restores = [
        c for c in cmds if c[:3] == ["pactl", "set-sink-input-volume", "42"]
    ]
    assert len(restores) >= 2  # duck + restore
    assert restores[-1][-1] == "52428"


def test_duck_media_kill_switch_disabled():
    cmds: list[list[str]] = []

    with duck_media(
        enabled=False,
        sink_input_blob=SPOTIFY_PLAYING,
        run_cmd=lambda a: cmds.append(a) or True,
    ) as state:
        assert state.enabled is False
        assert state.applied is False
        assert state.indices == ()
    assert cmds == []


def test_duck_media_excludes_conference_and_hark():
    cmds: list[list[str]] = []

    def run_cmd(args: list[str]) -> bool:
        cmds.append(list(args))
        return True

    with duck_media(
        enabled=True,
        level=0.15,
        pause_players=False,
        exclude_conference=True,
        sink_input_blob=MULTI_MIXED,
        run_cmd=run_cmd,
        check_mpris=False,
    ) as state:
        # Only Spotify (42); zoom excluded, paplay/corked excluded
        assert state.indices == (42,)

    indices_touched = {
        c[2] for c in cmds if c[:2] == ["pactl", "set-sink-input-volume"]
    }
    assert indices_touched == {"42"}


def test_duck_media_fail_open_when_set_fails():
    def run_cmd(_args: list[str]) -> bool:
        return False  # all pactl sets fail

    with duck_media(
        enabled=True,
        level=0.15,
        pause_players=False,
        sink_input_blob=SPOTIFY_PLAYING,
        run_cmd=run_cmd,
        check_mpris=False,
    ) as state:
        # Volume set failed → not applied via volume; still yield (fail-open)
        assert state.indices == ()
        assert state.applied is False
        assert all(not s.set_ok for s in state.snapshots)


def test_pause_and_resume_mpris_mocked():
    cmds: list[list[str]] = []

    def run_cmd(args: list[str]) -> bool:
        cmds.append(list(args))
        return True

    paused = pause_mpris_players(
        ["spotify", "vlc"],
        run_cmd=run_cmd,
        which_fn=lambda _: "/usr/bin/playerctl",
    )
    assert paused == ["spotify", "vlc"]
    resumed = resume_mpris_players(["spotify", "vlc"], run_cmd=run_cmd)
    assert resumed == ["spotify", "vlc"]
    assert ["playerctl", "-p", "spotify", "pause"] in cmds
    assert ["playerctl", "-p", "vlc", "play"] in cmds


def test_duck_media_pause_media_during_tts():
    """pause_players=True pauses MPRIS Playing + ducks sink-inputs."""
    cmds: list[list[str]] = []

    def run_cmd(args: list[str]) -> bool:
        cmds.append(list(args))
        return True

    def run_capture(args: list[str]) -> str:
        if "metadata" in args:
            return "spotify|Playing\n"
        return ""

    with duck_media(
        enabled=True,
        level=0.15,
        pause_players=True,
        check_mpris=True,
        sink_input_blob=SPOTIFY_PLAYING,
        run_cmd=run_cmd,
        run_capture=run_capture,
        which_fn=lambda _: "/usr/bin/playerctl",
    ) as state:
        assert "spotify" in state.paused_players
        assert state.applied is True
        assert 42 in state.indices

    assert ["playerctl", "-p", "spotify", "pause"] in cmds
    assert ["playerctl", "-p", "spotify", "play"] in cmds  # resume
    assert any(
        c == ["pactl", "set-sink-input-volume", "42", "12%"] for c in cmds
    )


def test_duck_media_respects_cfg_fields():
    from hark.config import AudioConfig

    cmds: list[list[str]] = []
    cfg = AudioConfig(
        duck_media_during_tts=True,
        pause_media_during_tts=False,
        duck_level=0.5,
        duck_exclude_apps=["spotify"],
    )
    with duck_media(
        cfg,
        sink_input_blob=SPOTIFY_PLAYING,
        run_cmd=lambda a: cmds.append(list(a)) or True,
        check_mpris=False,
    ) as state:
        # spotify excluded via duck_exclude_apps
        assert state.applied is False
        assert state.level == 0.5
        assert state.indices == ()
    assert cmds == []


def test_duck_state_as_meta_fields():
    st = DuckState(
        enabled=True,
        applied=True,
        level=0.15,
        indices=(1, 2),
        paused_players=["spotify"],
    )
    meta = st.as_meta()
    assert meta["media_ducked"] is True
    assert meta["duck_level"] == 0.15
    assert meta["duck_count"] == 2
    assert meta["mpris_paused"] == ["spotify"]


def test_config_duck_keys_load(tmp_path):
    from hark.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(
        """
[audio]
duck_media_during_tts = false
pause_media_during_tts = true
duck_level = 0.2
duck_exclude_apps = ["easyeffects", "helvum"]
media_check_mpris = false
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.audio.duck_media_during_tts is False
    assert cfg.audio.pause_media_during_tts is True
    assert cfg.audio.duck_level == 0.2
    assert cfg.audio.duck_exclude_apps == ["easyeffects", "helvum"]
    assert cfg.audio.media_check_mpris is False


def test_config_duck_defaults(tmp_path):
    from hark.config import load_config

    path = tmp_path / "config.toml"
    path.write_text("version = 1\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.audio.duck_media_during_tts is True
    assert cfg.audio.pause_media_during_tts is False
    assert cfg.audio.duck_level == 0.15
    assert cfg.audio.duck_exclude_apps == []
    assert cfg.audio.media_check_mpris is True


def test_run_tts_wires_duck_and_meta(monkeypatch):
    """run_tts play path uses duck_media; meta includes media_ducked."""
    from contextlib import contextmanager

    from hark.config import AudioConfig, HarkConfig
    from hark.speech import run_tts
    from hark.conference import HoldResult
    from hark.audio.media import DuckState

    duck_calls: list[dict] = []

    @contextmanager
    def fake_duck(cfg, **kwargs):
        duck_calls.append(kwargs)
        en = kwargs.get("enabled", True)
        st = DuckState(
            enabled=bool(en),
            applied=bool(en),  # simulate active duck only when enabled
            level=0.15,
            indices=(42,) if en else (),
            paused_players=[],
        )
        yield st

    class FakeTts:
        provider = "xai"
        voice = "eve"
        content_type = "audio/mpeg"

        def synthesize(self, text, voice=None):
            from hark.providers.base import SynthResult

            return SynthResult(
                audio=b"ID3fake",
                provider="xai",
                voice=voice or "eve",
                content_type="audio/mpeg",
            )

    monkeypatch.setattr(
        "hark.conference.apply_conference_hold",
        lambda *a, **k: HoldResult(held=False, skipped=False),
    )
    monkeypatch.setattr("hark.speech.resolve_tts", lambda *a, **k: FakeTts())
    monkeypatch.setattr("hark.speech.lookup_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.store_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr(
        "hark.speech.play_wav_bytes",
        lambda *a, **k: type("R", (), {"duration_ms": 5})(),
    )
    monkeypatch.setattr(
        "hark.speech.mic_muted_during_tts",
        lambda **k: __import__("contextlib").nullcontext(
            type("M", (), {"applied": False})()
        ),
    )
    monkeypatch.setattr("hark.speech.duck_media", fake_duck)

    class U:
        def record_tts(self, **k):
            return None

    monkeypatch.setattr("hark.speech.UsageStore", U)

    cfg = HarkConfig(audio=AudioConfig(duck_media_during_tts=True))
    out = run_tts(cfg, "Hello", play=True, conference_policy="force")
    assert out["ok"] is True
    assert out["media_ducked"] is True
    assert out["media_duck"]["duck_count"] == 1
    assert duck_calls and duck_calls[0].get("enabled") is True
    assert duck_calls[0].get("exclude_conference") is True

    # Kill-switch
    duck_calls.clear()
    cfg2 = HarkConfig(audio=AudioConfig(duck_media_during_tts=False))
    out2 = run_tts(cfg2, "Hello", play=True, conference_policy="force")
    assert out2["media_ducked"] is False
    assert duck_calls and duck_calls[0].get("enabled") is False


def test_run_tts_conference_skip_does_not_duck(monkeypatch):
    """Conference skip returns early — duck_media never entered."""
    from hark.config import HarkConfig
    from hark.speech import run_tts
    from hark.conference import HoldResult

    duck_entered = {"n": 0}

    def boom_duck(*a, **k):
        duck_entered["n"] += 1
        raise AssertionError("duck should not run when conference skip")

    monkeypatch.setattr(
        "hark.conference.apply_conference_hold",
        lambda *a, **k: HoldResult(held=True, skipped=True, matched=["proc:zoom"]),
    )
    monkeypatch.setattr("hark.speech.duck_media", boom_duck)
    monkeypatch.setattr(
        "hark.speech.resolve_tts",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no synth")),
    )

    out = run_tts(
        HarkConfig(),
        "Hark ambient is listening.",
        play=True,
        conference_policy="skip",
    )
    assert out.get("skipped") is True
    assert duck_entered["n"] == 0

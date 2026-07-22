"""B161: desktop notification with full TTS text + Skip action while TTS plays."""

from __future__ import annotations

import io
from types import SimpleNamespace

import hark.speech as speech_mod
import hark.tts_notify as tts_notify
from hark.audio import playback
from hark.config import HarkConfig, config_to_dict, load_config
from hark.speech import run_tts


class FakeNotifyProc:
    """Minimal Popen stand-in for notify-send with scripted stdout."""

    def __init__(self, args, stdout_lines, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = io.StringIO("".join(f"{ln}\n" for ln in stdout_lines))
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _patch_notify_env(monkeypatch, procs, stdout_lines):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/fake/bus")
    monkeypatch.setattr(
        tts_notify,
        "_which",
        lambda name: f"/usr/bin/{name}" if name in {"notify-send", "gdbus"} else None,
    )
    monkeypatch.setattr(
        tts_notify,
        "_popen",
        lambda args, **kwargs: (
            procs.append(FakeNotifyProc(args, stdout_lines, **kwargs)) or procs[-1]
        ),
    )
    # Dismiss path defaults to a no-op; tests that care re-mock with recording.
    monkeypatch.setattr(
        tts_notify, "_run", lambda args, **kwargs: SimpleNamespace(returncode=0)
    )


def test_start_spawns_notify_send_with_full_text_and_skip_action(monkeypatch):
    procs: list[FakeNotifyProc] = []
    full_text = "1. Local only\n2. Remote SSH\n3. Both"
    _patch_notify_env(monkeypatch, procs, ["42"])

    note = tts_notify.TtsSkipNotification(full_text)
    assert note.start() is True
    try:
        assert len(procs) == 1
        argv = procs[0].args
        assert argv[0].endswith("notify-send")
        assert "--print-id" in argv
        assert "--action=skip=Skip" in argv
        # Entire TTS text/question is the notification body.
        assert argv[-1] == full_text
        note._id_ready.wait(1.0)
        assert note._notification_id == "42"
    finally:
        note.close()


def test_start_disabled_without_notify_send(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/fake/bus")
    monkeypatch.setattr(tts_notify, "_which", lambda name: None)
    note = tts_notify.TtsSkipNotification("hello")
    assert note.start() is False
    note.close()  # close without start is a safe no-op


def test_start_disabled_without_session_bus(monkeypatch):
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    monkeypatch.setattr(tts_notify, "_which", lambda name: "/usr/bin/notify-send")
    note = tts_notify.TtsSkipNotification("hello")
    assert note.start() is False
    note.close()


def test_skip_action_invokes_callback(monkeypatch):
    procs: list[FakeNotifyProc] = []
    _patch_notify_env(monkeypatch, procs, ["7", "skip"])
    skipped: list[bool] = []

    note = tts_notify.TtsSkipNotification("body", on_skip=lambda: skipped.append(True))
    assert note.start() is True
    note._reader.join(timeout=2.0)
    assert skipped == [True]
    note.close()


def test_close_dismisses_via_gdbus_and_terminates(monkeypatch):
    procs: list[FakeNotifyProc] = []
    runs: list[list[str]] = []
    _patch_notify_env(monkeypatch, procs, ["55"])
    monkeypatch.setattr(
        tts_notify,
        "_run",
        lambda args, **kwargs: runs.append(list(args)) or SimpleNamespace(returncode=0),
    )

    note = tts_notify.TtsSkipNotification("body")
    assert note.start() is True
    note.close()

    assert procs[0].terminated is True
    gdbus_calls = [r for r in runs if r[0].endswith("gdbus")]
    assert len(gdbus_calls) == 1
    call = gdbus_calls[0]
    assert "org.freedesktop.Notifications.CloseNotification" in call
    assert call[-1] == "55"


def test_close_is_idempotent(monkeypatch):
    procs: list[FakeNotifyProc] = []
    _patch_notify_env(monkeypatch, procs, ["9"])
    note = tts_notify.TtsSkipNotification("body")
    assert note.start() is True
    note.close()
    note.close()


def test_body_escapes_pango_markup(monkeypatch):
    procs: list[FakeNotifyProc] = []
    _patch_notify_env(monkeypatch, procs, ["11"])
    note = tts_notify.TtsSkipNotification("Use <b>bold</b> & stuff")
    assert note.start() is True
    try:
        assert procs[0].args[-1] == "Use &lt;b&gt;bold&lt;/b&gt; &amp; stuff"
    finally:
        note.close()


def test_close_falls_back_to_replace_id_when_gdbus_missing(monkeypatch):
    procs: list[FakeNotifyProc] = []
    runs: list[list[str]] = []
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/fake/bus")
    monkeypatch.setattr(
        tts_notify,
        "_which",
        lambda name: "/usr/bin/notify-send" if name == "notify-send" else None,
    )
    monkeypatch.setattr(
        tts_notify,
        "_popen",
        lambda args, **kwargs: (
            procs.append(FakeNotifyProc(args, ["21"], **kwargs)) or procs[-1]
        ),
    )
    monkeypatch.setattr(
        tts_notify,
        "_run",
        lambda args, **kwargs: runs.append(list(args)) or SimpleNamespace(returncode=0),
    )

    note = tts_notify.TtsSkipNotification("body")
    assert note.start() is True
    note.close()

    assert len(runs) == 1
    call = runs[0]
    assert call[0].endswith("notify-send")
    assert "--replace-id=21" in call
    assert "--expire-time=1" in call


def test_context_manager_respects_config_flag(monkeypatch):
    procs: list[FakeNotifyProc] = []
    _patch_notify_env(monkeypatch, procs, ["3"])

    cfg = HarkConfig()
    cfg.tts.notify_skip = False
    with tts_notify.tts_skip_notification(cfg, "quiet"):
        pass
    assert procs == []

    cfg.tts.notify_skip = True
    with tts_notify.tts_skip_notification(cfg, "loud"):
        pass
    assert len(procs) == 1
    assert procs[0].terminated is True


def test_notify_skip_config_default_and_toml(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.tts.notify_skip is True
    d = config_to_dict(cfg)
    assert d["tts"]["notify_skip"] is True

    path = tmp_path / "config.toml"
    path.write_text("[tts]\nnotify_skip = false\n", encoding="utf-8")
    loaded = load_config(path)
    assert loaded.tts.notify_skip is False
    assert not [w for w in loaded.warnings if "notify_skip" in w]


def test_request_playback_skip_invokes_stoppers_and_bumps_generation():
    before = playback.playback_skip_generation()
    stopped: list[bool] = []
    with playback._skip_stopper(lambda: stopped.append(True)):
        assert playback.request_playback_skip() is True
    assert stopped == [True]
    assert playback.playback_skip_generation() == before + 1
    # Stopper unregistered: second skip finds nothing to stop.
    assert playback.request_playback_skip() is False
    assert playback.playback_skip_generation() == before + 2


def test_play_file_skip_does_not_fall_through_to_next_player(monkeypatch):
    """A user skip must not restart the same audio via the fallback player."""
    spawned: list[list[str]] = []

    class FakePlayer:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.returncode = None

        def terminate(self):
            self.returncode = -15

        def wait(self):
            playback.request_playback_skip()  # user clicked Skip mid-play
            self.terminate()
            return self.returncode

    monkeypatch.setattr(playback.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        playback.subprocess,
        "Popen",
        lambda cmd, **kwargs: spawned.append(list(cmd)) or FakePlayer(cmd),
    )
    from pathlib import Path

    playback._play_file(Path("/tmp/fake.mp3"))
    # ffplay was tried once; paplay/aplay must NOT replay the skipped audio.
    assert len(spawned) == 1
    assert spawned[0][0] == "ffplay"


def test_play_pcm16_skip_stops_sounddevice(monkeypatch):
    """The sounddevice path registers an sd.stop() stopper (B161)."""
    calls: list[str] = []

    class FakeSd:
        def play(self, samples, sample_rate):
            calls.append("play")

        def wait(self):
            calls.append("wait")
            playback.request_playback_skip()  # user clicked Skip mid-play

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(playback, "sd", FakeSd())
    playback._play_pcm16(b"\x01\x00" * 100, 16000)
    assert calls == ["play", "wait", "stop"]


def _patch_run_tts_fakes(monkeypatch, play_impl):
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

    def fake_resolve(*a, **k):
        class T:
            def synthesize(self, text, voice=None):
                return SimpleNamespace(
                    audio=b"AUD" + text[:8].encode(),
                    provider="xai",
                    content_type="audio/mpeg",
                    voice=voice or "eve",
                )

        return T()

    monkeypatch.setattr("hark.speech.lookup_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.store_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.resolve_tts", fake_resolve)
    monkeypatch.setattr(
        "hark.speech._synth_transport_factory",
        speech_mod._in_process_synth_transport_factory,
    )
    monkeypatch.setattr("hark.speech.play_wav_bytes", play_impl)
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


_LONG_TEXT = ("Sentence one is here with more padding words. " * 15) + (
    "Final sentence ends here."
)


def test_run_tts_notification_span_and_user_skip(monkeypatch):
    """Skip action mid-play stops remaining chunks; result flags user_skipped."""
    plays: list[int] = []
    spans: list[str] = []
    closes: list[bool] = []

    class FakeNote:
        def __init__(self, text, *, on_skip=None):
            self.text = text
            self.on_skip = on_skip

        def start(self):
            spans.append(self.text)
            return True

        def close(self):
            closes.append(True)

    def play_impl(audio, **k):
        plays.append(len(audio))
        if len(plays) == 1:
            # Simulate the notification's Skip click during first chunk.
            playback.request_playback_skip()
        return SimpleNamespace(duration_ms=100)

    _patch_run_tts_fakes(monkeypatch, play_impl)
    monkeypatch.setattr("hark.tts_notify.TtsSkipNotification", FakeNote)

    cfg = HarkConfig()
    cfg.tts.max_chars = 0
    cfg.tts.chunk_chars = 500  # force multi-chunk
    cfg.audio.hold_during_conference = False
    out = run_tts(
        cfg, _LONG_TEXT, play=True, conference_policy="force", use_cache=False
    )

    assert out["ok"] is True
    assert out["chunked"] is True
    assert out["user_skipped"] is True
    assert len(plays) == 1  # stopped after the first chunk
    # The notification carried the entire TTS text and was dismissed at the end.
    assert spans == [_LONG_TEXT.strip()]
    assert closes == [True]


def test_run_tts_skip_during_prefetch_stops_before_next_chunk(monkeypatch):
    """Skip landing between chunks (during prefetch wait) must not play on."""
    plays: list[int] = []

    def play_impl(audio, **k):
        plays.append(len(audio))
        return SimpleNamespace(duration_ms=100)

    _patch_run_tts_fakes(monkeypatch, play_impl)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    # Script the skip to become visible exactly at the top-of-loop check for
    # chunk 1 (calls: snapshot, top i=0, post-play i=0, top i=1, …).
    # NOTE: this breaks by design if another playback_skip_generation() probe
    # is added to the run_tts loop — update the call budget then.
    real_gen = playback.playback_skip_generation
    gen_calls = {"n": 0}

    def fake_gen() -> int:
        gen_calls["n"] += 1
        g = real_gen()
        return g if gen_calls["n"] <= 3 else g + 1

    monkeypatch.setattr("hark.speech.playback_skip_generation", fake_gen)

    cfg = HarkConfig()
    cfg.tts.max_chars = 0
    cfg.tts.chunk_chars = 500
    cfg.audio.hold_during_conference = False
    out = run_tts(
        cfg, _LONG_TEXT, play=True, conference_policy="force", use_cache=False
    )

    assert out["ok"] is True
    assert out["chunked"] is True
    assert out["user_skipped"] is True
    # Chunk 0 played; the skip seen before chunk 1 stopped it from playing.
    assert len(plays) == 1


def test_run_tts_no_skip_plays_all_chunks(monkeypatch):
    plays: list[int] = []

    def play_impl(audio, **k):
        plays.append(len(audio))
        return SimpleNamespace(duration_ms=50)

    _patch_run_tts_fakes(monkeypatch, play_impl)
    # No desktop bus in test: notifier silently disables itself.
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

    cfg = HarkConfig()
    cfg.tts.max_chars = 0
    cfg.tts.chunk_chars = 500
    cfg.audio.hold_during_conference = False
    out = run_tts(
        cfg, _LONG_TEXT, play=True, conference_policy="force", use_cache=False
    )

    assert out["ok"] is True
    assert "user_skipped" not in out
    assert len(plays) == out["chunks"]

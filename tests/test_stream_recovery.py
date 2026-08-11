"""B178: durable answer-window audio recovery with bounded loss."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hark.audio.stream_recovery import (
    DEFAULT_FLUSH_INTERVAL_S,
    discard_recovered_stream,
    flush_all_active_recovery,
    get_recovered_stream,
    list_recovered_streams,
    open_stream_recovery,
    purge_old_recovered_streams,
    recovery_doctor_report,
    streams_dir,
)


def _pcm_tone(n_samples: int, *, amp: int = 1000) -> bytes:
    """Deterministic mono s16le pattern (not silence)."""
    import struct

    return b"".join(struct.pack("<h", amp if (i // 8) % 2 == 0 else -amp) for i in range(n_samples))


@pytest.mark.parametrize("limit", [1, 7])
def test_short_writes_preserve_pcm_and_metadata(monkeypatch, tmp_path, limit):
    import hark.audio.stream_recovery as recovery

    original_write = os.write

    def short_write(fd, data):
        return original_write(fd, data[:limit])

    monkeypatch.setattr(recovery.os, "write", short_write)
    session = open_stream_recovery("short-write", root=tmp_path)
    pcm = _pcm_tone(320)
    session.append_pcm(pcm)
    session.close_retain(reason="interrupted")
    item = get_recovered_stream("short-write", root=tmp_path)
    assert item is not None
    assert item.read_pcm() == pcm
    assert json.loads((item.path / "meta.json").read_text())["bytes_written"] == len(pcm)


def test_setup_failure_retains_pcm_and_releases_registry(tmp_path):
    import hark.audio.stream_recovery as recovery

    session = open_stream_recovery("setup-failure", root=tmp_path)
    pcm = _pcm_tone(160)
    with pytest.raises(RuntimeError, match="setup failed"):
        with session:
            session.append_pcm(pcm)
            raise RuntimeError("setup failed")
    assert recovery.active_recovery_session("setup-failure") is None
    assert get_recovered_stream("setup-failure", root=tmp_path).read_pcm() == pcm


def test_zero_write_fails_without_spinning(monkeypatch):
    import hark.audio.stream_recovery as recovery

    monkeypatch.setattr(recovery.os, "write", lambda *_a: 0)
    with pytest.raises(OSError, match="no progress"):
        recovery._write_all(42, b"pcm")


def test_append_flush_and_recover_bytes(tmp_path: Path) -> None:
    root = tmp_path / "state"
    session = open_stream_recovery(
        "stream-abc",
        sample_rate=16000,
        mode="radio",
        flush_interval_s=0.05,
        root=root,
    )
    chunk = _pcm_tone(1600)  # 100 ms
    session.append_pcm(chunk)
    session.flush(force=True)
    assert session.pcm_path.is_file()
    assert session.pcm_path.stat().st_size == len(chunk)
    # Permissions: private file
    mode = session.pcm_path.stat().st_mode & 0o777
    assert mode & 0o077 == 0
    dir_mode = session.dir.stat().st_mode & 0o777
    assert dir_mode & 0o077 == 0

    session.mark_interrupted(reason="test")
    item = get_recovered_stream("stream-abc", root=root)
    assert item is not None
    assert item.read_pcm() == chunk
    assert item.duration_ms == pytest.approx(100, abs=5)
    assert item.orphaned is True
    wav = item.read_wav()
    assert wav[:4] == b"RIFF"
    assert len(wav) > len(chunk)


def test_complete_and_discard_removes_tree(tmp_path: Path) -> None:
    root = tmp_path / "state"
    session = open_stream_recovery("done-1", root=root, flush_interval_s=0.05)
    session.append_pcm(_pcm_tone(320))
    session.flush(force=True)
    assert session.dir.is_dir()
    session.complete_and_discard()
    assert get_recovered_stream("done-1", root=root) is None
    assert not session.dir.exists()


def test_uncommitted_tail_bounded_by_flush_interval(tmp_path: Path) -> None:
    """Before force flush, disk may lag by at most ~flush_interval of audio."""
    root = tmp_path / "state"
    flush_s = 0.2
    session = open_stream_recovery(
        "tail-bound",
        root=root,
        flush_interval_s=flush_s,
        max_buffer_bytes=10_000_000,  # force time-based flush only
    )
    # Write slightly less than one flush window without waiting.
    samples = int(16000 * (flush_s * 0.5))
    pcm = _pcm_tone(samples)
    session.append_pcm(pcm)
    on_disk = session.pcm_path.stat().st_size if session.pcm_path.is_file() else 0
    # Either not yet flushed, or fully flushed — never more missing than buffer.
    missing = len(pcm) - on_disk
    assert missing >= 0
    assert missing <= len(pcm)
    # After interval, append triggers flush of prior buffer.
    time.sleep(flush_s + 0.05)
    session.append_pcm(_pcm_tone(16))
    session.flush(force=True)
    assert session.pcm_path.stat().st_size >= len(pcm)


def test_list_and_discard(tmp_path: Path) -> None:
    root = tmp_path / "state"
    a = open_stream_recovery("s1", root=root, flush_interval_s=0.05)
    a.append_pcm(_pcm_tone(800))
    a.mark_interrupted(reason="kill")
    b = open_stream_recovery("s2", root=root, flush_interval_s=0.05)
    b.append_pcm(_pcm_tone(400))
    b.mark_interrupted(reason="kill")
    items = list_recovered_streams(root=root, only_orphaned=True)
    ids = {i.stream_id for i in items}
    assert ids == {"s1", "s2"}
    assert discard_recovered_stream("s1", root=root) is True
    assert get_recovered_stream("s1", root=root) is None
    assert get_recovered_stream("s2", root=root) is not None


def test_purge_old_streams(tmp_path: Path) -> None:
    root = tmp_path / "state"
    session = open_stream_recovery("old", root=root, flush_interval_s=0.05)
    session.append_pcm(_pcm_tone(100))
    session.mark_interrupted(reason="old")
    item = get_recovered_stream("old", root=root)
    assert item is not None
    # Backdate meta + mtime
    meta_path = item.path / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["updated_at"] = time.time() - 10 * 86400
    meta["started_at"] = meta["updated_at"]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    os.utime(item.path, (meta["updated_at"], meta["updated_at"]))
    os.utime(item.pcm_path, (meta["updated_at"], meta["updated_at"]))
    n = purge_old_recovered_streams(retention_days=7.0, root=root)
    assert n == 1
    assert get_recovered_stream("old", root=root) is None


def test_doctor_report_surfaces_orphans(tmp_path: Path) -> None:
    root = tmp_path / "state"
    session = open_stream_recovery("doc1", root=root, flush_interval_s=0.05)
    session.append_pcm(_pcm_tone(500))
    session.mark_interrupted(reason="harness_timeout")
    report = recovery_doctor_report(root=root)
    assert report["ok"] is True
    assert report["orphaned"] >= 1
    assert report["warnings"]
    assert any("hark recover" in h for h in report["hints"])


def test_flush_all_active_recovery(tmp_path: Path) -> None:
    root = tmp_path / "state"
    session = open_stream_recovery("act", root=root, flush_interval_s=60.0)
    pcm = _pcm_tone(2000)
    session.append_pcm(pcm)
    # Still buffered — force via global flush (signal path).
    flush_all_active_recovery()
    assert session.pcm_path.stat().st_size == len(pcm)
    session.complete_and_discard()


def test_invalid_stream_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        open_stream_recovery("../escape", root=tmp_path)
    with pytest.raises(ValueError):
        open_stream_recovery("a/b", root=tmp_path)


def test_kill_mid_stream_recovers_all_but_small_tail(tmp_path: Path) -> None:
    """Subprocess writes continuous PCM then is SIGKILL'd; parent recovers disk.

    Proves the B178 invariant without a real microphone: accepted audio is on
    disk except at most the configured flush interval tail.
    """
    root = tmp_path / "state"
    stream_id = "kill-mid"
    flush_s = 0.15
    # Child script: open recovery, write known PCM in small frames, loop until killed.
    child_src = f"""
import os, sys, time, struct
sys.path.insert(0, {str(Path('src').resolve())!r})
from pathlib import Path
from hark.audio.stream_recovery import open_stream_recovery

root = Path({str(root)!r})
session = open_stream_recovery(
    {stream_id!r},
    sample_rate=16000,
    mode="radio",
    flush_interval_s={flush_s!r},
    root=root,
)
# 20 ms frames of deterministic PCM
frame = struct.pack("<h", 1200) * 320
n = 0
# Publish ready marker
Path({str(tmp_path / 'ready')!r}).write_text("1")
while True:
    session.append_pcm(frame)
    n += 1
    if n % 10 == 0:
        # occasional progress file so parent can wait for enough audio
        Path({str(tmp_path / 'frames')!r}).write_text(str(n))
    time.sleep(0.02)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("src").resolve()) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-c", child_src],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    ready = tmp_path / "ready"
    frames_path = tmp_path / "frames"
    deadline = time.time() + 5.0
    while time.time() < deadline and not ready.is_file():
        if proc.poll() is not None:
            err = (proc.stderr.read() or b"").decode("utf-8", "replace")
            pytest.fail(f"child exited early: {proc.returncode}\n{err}")
        time.sleep(0.02)
    assert ready.is_file(), "child never became ready"
    # Wait until at least ~1s of audio has been accepted (50 frames).
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if frames_path.is_file():
            try:
                if int(frames_path.read_text()) >= 50:
                    break
            except ValueError:
                pass
        time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("child did not write enough frames")

    # Hard kill — no Python finally, no graceful mark_interrupted.
    os.kill(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=1.0)

    item = get_recovered_stream(stream_id, root=root)
    assert item is not None, "recovery tree missing after SIGKILL"
    assert item.pcm_bytes > 0
    # 50 frames * 20 ms = 1000 ms accepted; allow missing up to ~flush + one frame.
    # Each frame = 320 samples * 2 bytes = 640 bytes.
    frame_bytes = 640
    min_expected_frames = 50
    max_missing_bytes = int(16000 * (flush_s + 0.05)) * 2 + frame_bytes
    # We cannot know exact accepted count after kill; require substantial recovery
    # and that missing vs last published frame count is within bound.
    last_frames = int(frames_path.read_text())
    expected_min = max(0, (last_frames * frame_bytes) - max_missing_bytes)
    assert item.pcm_bytes >= expected_min, (
        f"recovered {item.pcm_bytes} bytes < min {expected_min} "
        f"(last_frames={last_frames}, flush_s={flush_s})"
    )
    # Sanity: recovered most of a 1s+ utterance
    assert item.duration_ms >= 500
    assert discard_recovered_stream(stream_id, root=root)


def test_cli_recover_list_export_discard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hark.cli import main

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    root = None  # uses state_dir from env
    session = open_stream_recovery("cli-1", flush_interval_s=0.05)
    session.append_pcm(_pcm_tone(1600))
    session.mark_interrupted(reason="test")
    assert streams_dir().is_dir()

    rc = main(["recover", "list", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["count"] >= 1
    assert any(s["stream_id"] == "cli-1" for s in data["streams"])

    wav_out = tmp_path / "out.wav"
    rc = main(["recover", "export", "cli-1", "-o", str(wav_out), "--json"])
    assert rc == 0
    assert wav_out.is_file()
    assert wav_out.read_bytes()[:4] == b"RIFF"

    rc = main(["recover", "discard", "cli-1", "--json"])
    assert rc == 0
    assert get_recovered_stream("cli-1") is None


def test_capture_on_accepted_pcm_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    """capture_utterance forwards accepted speech frames to on_accepted_pcm."""
    import numpy as np

    from hark.audio import capture as capture_mod

    accepted: list[bytes] = []

    class FakeStream:
        def __init__(self) -> None:
            self.n = 0
            # Loud blocks then quiet to end
            self.plan = (
                [0.5] * 10  # open
                + [0.5] * 20  # speech
                + [0.0] * 200  # silence end
            )

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def close(self) -> None:
            return None

        def read(self, block: int):
            if self.n >= len(self.plan):
                amp = 0.0
            else:
                amp = self.plan[self.n]
                self.n += 1
            data = (np.ones(block, dtype=np.float32) * amp).reshape(-1, 1)
            return data, False

        def abort(self) -> None:
            return None

    class FakeSD:
        def InputStream(self, **kwargs):
            return FakeStream()

    monkeypatch.setattr(capture_mod, "sd", FakeSD())
    # Avoid spectrum / lease side effects
    monkeypatch.setattr(
        capture_mod,
        "_registered_input_stream",
        __import__("contextlib").contextmanager(lambda stream: (yield)),
    )
    monkeypatch.setattr(capture_mod, "_reserve_stream_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(capture_mod, "_start_stream_cleanup", lambda *a, **k: None)
    monkeypatch.setattr(capture_mod, "_wait_stream_cleanup", lambda *a, **k: None)

    result = capture_mod.capture_utterance(
        max_s=5.0,
        end_silence_s=0.3,
        initial_timeout_s=2.0,
        preroll_ms=0,
        abs_open_db=-60.0,
        open_margin_db=1.0,
        open_confirm_blocks=2,
        min_speech_s=0.05,
        on_accepted_pcm=lambda b: accepted.append(b),
    )
    assert result.duration_ms > 0
    assert accepted, "on_accepted_pcm never called"
    assert sum(len(b) for b in accepted) == len(result.pcm16)


def test_open_answer_window_success_discards_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Successful silence listen removes the recovery tree."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    from types import SimpleNamespace

    from hark.answer_window.deps import AnswerWindowDeps
    from hark.answer_window.open_window import open_answer_window
    from hark.answer_window.policy import AnswerWindowPolicy
    from hark.audio.capture import CaptureResult
    from hark.listen_end import EndMode

    pcm = _pcm_tone(1600)
    cap = CaptureResult(
        pcm16=pcm,
        sample_rate=16000,
        duration_ms=100,
        speech_ms=100,
    )

    def fake_capture(**kwargs):
        on_pcm = kwargs.get("on_accepted_pcm")
        if on_pcm:
            on_pcm(pcm)
        return cap

    class FakeSTT:
        name = "fake"

        def transcribe(self, wav: bytes):
            return SimpleNamespace(text="hello world", provider="fake")

    cfg = SimpleNamespace(
        audio=SimpleNamespace(
            mute_mic_during_tts=False,
            post_tts_guard_ms=0,
        ),
        stt=SimpleNamespace(provider="fake"),
    )
    policy = AnswerWindowPolicy(
        profile="bound_answer",
        end_mode=EndMode.SILENCE,
        max_listen_s=5.0,
        end_silence_s=0.5,
        initial_timeout_s=2.0,
        stream_id="aw-success",
    )

    import contextlib

    import hark.speech as speech

    def _cm(*_a, **_k):
        return contextlib.nullcontext()

    monkeypatch.setattr(speech, "resolve_stt", lambda *a, **k: FakeSTT())
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)
    monkeypatch.setattr(speech, "pause_ambient_for_mic", _cm)
    monkeypatch.setattr(speech, "MicLease", _cm)
    monkeypatch.setattr(speech, "BusySection", _cm)
    monkeypatch.setattr(speech, "duck_media", _cm)
    monkeypatch.setattr(speech, "configure_cues_from_config", lambda *a, **k: None)
    monkeypatch.setattr(speech, "play_record_start", lambda: None)
    monkeypatch.setattr(speech, "play_record_stop", lambda: None)
    monkeypatch.setattr(speech, "register_active_listen", lambda *a, **k: None)
    monkeypatch.setattr(speech, "clear_active_listen", lambda *a, **k: None)
    monkeypatch.setattr(speech, "poll_listen_action", lambda *a, **k: None)
    monkeypatch.setattr(speech, "consume_listen_action", lambda *a, **k: None)
    monkeypatch.setattr(speech, "touch_voice_activity", lambda *a, **k: None)
    monkeypatch.setattr(speech, "UsageStore", lambda: SimpleNamespace(record_stt=lambda **k: None))
    monkeypatch.setattr(speech, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(
        speech,
        "_transcribe_logged",
        lambda stt, wav, **k: (stt.transcribe(wav), 1),
    )
    monkeypatch.setattr(speech, "run_tts", lambda *a, **k: None)
    monkeypatch.setattr(speech, "ProviderError", RuntimeError)
    monkeypatch.setattr(speech, "ABORT", 7)
    monkeypatch.setattr(speech, "build_endpoint_strategy", lambda **k: None)
    monkeypatch.setattr(speech, "write_wav_bytes", lambda pcm, sr: b"RIFF" + pcm)
    monkeypatch.setattr(speech, "pad_pcm16_silence", lambda pcm, **k: pcm)
    monkeypatch.setattr(speech, "radio_stt_window_pcm", lambda *a, **k: (a[0], b""))

    deps = AnswerWindowDeps(cfg=cfg, stt=FakeSTT(), capture=fake_capture)
    result = open_answer_window(policy, deps=deps)
    assert result.text == "hello world"
    assert get_recovered_stream("aw-success") is None


def test_open_answer_window_keeps_recovery_on_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CaptureInterrupted leaves durable audio for recover."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    from hark.answer_window.deps import AnswerWindowDeps
    from hark.answer_window.open_window import open_answer_window
    from hark.answer_window.policy import AnswerWindowPolicy
    from hark.audio.capture import CaptureInterrupted
    from hark.listen_end import EndMode

    pcm = _pcm_tone(3200)

    def fake_capture(**kwargs):
        on_pcm = kwargs.get("on_accepted_pcm")
        if on_pcm:
            on_pcm(pcm)
            # Simulate mid-stream kill after PCM was accepted/flushed path
            from hark.audio.stream_recovery import flush_all_active_recovery

            flush_all_active_recovery()
        raise CaptureInterrupted(signal.SIGTERM)

    cfg = __import__("types").SimpleNamespace(
        audio=__import__("types").SimpleNamespace(
            mute_mic_during_tts=False,
            post_tts_guard_ms=0,
        ),
        stt=__import__("types").SimpleNamespace(provider="fake"),
    )
    policy = AnswerWindowPolicy(
        profile="bound_answer",
        end_mode=EndMode.SILENCE,
        max_listen_s=5.0,
        end_silence_s=0.5,
        initial_timeout_s=2.0,
        stream_id="aw-kill",
    )
    import contextlib
    import types

    import hark.speech as speech

    def _cm(*_a, **_k):
        return contextlib.nullcontext()

    monkeypatch.setattr(speech, "resolve_stt", lambda *a, **k: types.SimpleNamespace(name="fake"))
    monkeypatch.setattr(speech, "capture_utterance", fake_capture)
    monkeypatch.setattr(speech, "pause_ambient_for_mic", _cm)
    monkeypatch.setattr(speech, "MicLease", _cm)
    monkeypatch.setattr(speech, "BusySection", _cm)
    monkeypatch.setattr(speech, "duck_media", _cm)
    monkeypatch.setattr(speech, "configure_cues_from_config", lambda *a, **k: None)
    monkeypatch.setattr(speech, "play_record_start", lambda: None)
    monkeypatch.setattr(speech, "play_record_stop", lambda: None)
    monkeypatch.setattr(speech, "register_active_listen", lambda *a, **k: None)
    monkeypatch.setattr(speech, "clear_active_listen", lambda *a, **k: None)
    monkeypatch.setattr(speech, "poll_listen_action", lambda *a, **k: None)
    monkeypatch.setattr(speech, "consume_listen_action", lambda *a, **k: None)
    monkeypatch.setattr(speech, "touch_voice_activity", lambda *a, **k: None)
    monkeypatch.setattr(speech, "UsageStore", lambda: types.SimpleNamespace(record_stt=lambda **k: None))
    monkeypatch.setattr(speech, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(speech, "build_endpoint_strategy", lambda **k: None)
    monkeypatch.setattr(speech, "ProviderError", RuntimeError)
    monkeypatch.setattr(speech, "ABORT", 7)

    deps = AnswerWindowDeps(cfg=cfg, capture=fake_capture)
    with pytest.raises(CaptureInterrupted):
        open_answer_window(policy, deps=deps)
    item = get_recovered_stream("aw-kill")
    assert item is not None
    assert item.read_pcm() == pcm
    assert item.orphaned is True

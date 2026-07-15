"""Unified handsfree monitor feed (wake_near_miss + agent + ambient.prompt)."""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from hark.exitcodes import ERROR
from hark.monitor_feed import (
    MonitorBusyError,
    MonitorFeedLock,
    MODE_A_WAKE_KINDS,
    ambient_feed_path,
    append_ambient_jsonl,
    compact_mode_a_event,
    emit_line,
    emit_hep,
    follow_state_files,
    io_targets_path,
    probe_monitor_consumer,
    read_monitor_holder_pid,
    replay_matching,
    run_monitor,
    should_surface,
)


def test_wake_near_miss_is_mode_a_wake_kind():
    assert "ambient.wake_near_miss" in MODE_A_WAKE_KINDS
    assert "ambient.prompt" in MODE_A_WAKE_KINDS
    assert "agent.blocked" in MODE_A_WAKE_KINDS
    assert "ambient.debug" not in MODE_A_WAKE_KINDS


def test_should_surface_filters():
    assert should_surface({"kind": "ambient.wake_near_miss"}, MODE_A_WAKE_KINDS)
    assert not should_surface({"kind": "ambient.debug"}, MODE_A_WAKE_KINDS)
    assert not should_surface(
        {"kind": "ambient.prompt", "hark_provenance": "test"},
        MODE_A_WAKE_KINDS,
    )
    assert should_surface(
        {"kind": "ambient.prompt", "hark_provenance": "test"},
        MODE_A_WAKE_KINDS,
        include_test_events=True,
    )


def test_compact_wake_near_miss():
    ev = {
        "schema": "hark.event.v1",
        "kind": "ambient.wake_near_miss",
        "event_id": "e1",
        "observed_at": "2026-07-13T12:00:00Z",
        "count": 2,
        "total_near_misses": 3,
        "attempts": [
            {"text": "a clunker", "best_phrase": "ok clanker", "score": 0.7},
            {"text": "hello plank", "score": 0.8},
        ],
    }
    c = compact_mode_a_event(ev)
    assert c["kind"] == "ambient.wake_near_miss"
    assert c["attempts"] == ["a clunker", "hello plank"]
    assert "instructions" in c
    assert "Failed wake" in c["instructions"]


def test_compact_ambient_prompt_truncates():
    long = "x" * 500
    c = compact_mode_a_event(
        {
            "kind": "ambient.prompt",
            "event_id": "e2",
            "text": long,
            "phrase": "hey clanker",
        }
    )
    assert c["text"].endswith("…")
    assert len(c["text"]) <= 401
    assert c["final"] is True


def test_replay_matching_from_files(tmp_path: Path):
    amb = tmp_path / "ambient.jsonl"
    watch = tmp_path / "watch.jsonl"
    amb.write_text(
        "\n".join(
            [
                json.dumps({"kind": "ambient.debug", "event_id": "d1"}),
                json.dumps(
                    {
                        "kind": "ambient.wake_near_miss",
                        "event_id": "n1",
                        "observed_at": "2026-07-13T12:00:01Z",
                        "attempts": [{"text": "clunker"}],
                    }
                ),
                json.dumps(
                    {
                        "kind": "ambient.prompt",
                        "event_id": "p1",
                        "observed_at": "2026-07-13T12:00:02Z",
                        "text": "hi",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    watch.write_text(
        json.dumps(
            {
                "kind": "agent.blocked",
                "event_id": "b1",
                "observed_at": "2026-07-13T12:00:00Z",
                "target": {"pane_id": "w1:p1", "agent": "claude"},
                "session_id": "default",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out = StringIO()
    n = replay_matching(
        [watch, amb],
        kinds=MODE_A_WAKE_KINDS,
        limit=10,
        for_monitor=True,
        out=out,
    )
    assert n == 3
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    kinds = [line["kind"] for line in lines]
    assert kinds == [
        "agent.blocked",
        "ambient.wake_near_miss",
        "ambient.prompt",
    ]
    # near-miss came through (the bug class)
    near = next(line for line in lines if line["kind"] == "ambient.wake_near_miss")
    assert near["attempts"] == ["clunker"]
    assert all(line["monitor_delivery"]["mode"] == "replay" for line in lines)
    assert {line["monitor_delivery"]["source"] for line in lines} == {
        "watch.jsonl",
        "ambient.jsonl",
    }


def test_monitor_delivery_marks_live_source():
    out = StringIO()
    emit_line(
        {"kind": "ambient.prompt", "event_id": "live-1", "text": "hello"},
        for_monitor=False,
        out=out,
        monitor_mode="live",
        source="ambient.jsonl",
    )

    payload = json.loads(out.getvalue())
    assert payload["monitor_delivery"] == {
        "mode": "live",
        "source": "ambient.jsonl",
    }


def test_live_follow_filters_marked_test_event_without_dropping_operator_event(
    monkeypatch, tmp_path: Path
):
    class FakeFollower:
        def __init__(self, _sources):
            self.polls = 0

        def start_live(self):
            pass

        def poll(self):
            self.polls += 1
            if self.polls > 1:
                raise KeyboardInterrupt
            return iter(
                [
                    SimpleNamespace(
                        source="ambient.jsonl",
                        payload={
                            "kind": "ambient.prompt",
                            "event_id": "synthetic",
                            "hark_provenance": "test",
                        },
                    ),
                    SimpleNamespace(
                        source="ambient.jsonl",
                        payload={
                            "kind": "ambient.prompt",
                            "event_id": "operator-live",
                        },
                    ),
                ]
            )

        def close(self):
            pass

    monkeypatch.setattr("hark.monitor_feed.StateFeedFollower", FakeFollower)
    out = StringIO()

    assert (
        follow_state_files(
            [tmp_path / "ambient.jsonl"],
            kinds=MODE_A_WAKE_KINDS,
            for_monitor=False,
            out=out,
            poll_s=0,
        )
        == 0
    )
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [line["event_id"] for line in lines] == ["operator-live"]
    assert lines[0]["monitor_delivery"] == {
        "mode": "live",
        "source": "ambient.jsonl",
    }


def test_replay_filters_marked_test_events(tmp_path: Path):
    feed = tmp_path / "ambient.jsonl"
    feed.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "ambient.prompt",
                        "event_id": "synthetic",
                        "hark_provenance": "test",
                    }
                ),
                json.dumps({"kind": "ambient.prompt", "event_id": "operator-live"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out = StringIO()

    assert (
        replay_matching(
            [feed],
            kinds=MODE_A_WAKE_KINDS,
            limit=10,
            for_monitor=False,
            out=out,
        )
        == 1
    )
    assert json.loads(out.getvalue())["event_id"] == "operator-live"


def test_partial_fragment_delta():
    from hark.partial import partial_fragment

    assert partial_fragment(None, "hello") == "hello"
    assert partial_fragment("hello", "hello world") == "world"
    assert partial_fragment("hello", "goodbye") == "goodbye"


def test_compact_ambient_partial_must_listen_end_language():
    c = compact_mode_a_event(
        {
            "kind": "ambient.partial",
            "event_id": "e-partial-must",
            "stream_id": "s99",
            "seq": 1,
            "text": "ship the plan okay over",
            "fragment": "okay over",
        }
    )
    assert "MUST" in c["instructions"]
    assert "listen-end" in c["instructions"]
    assert "over" in c["instructions"].lower()
    assert c.get("streaming") is False
    assert "HOLD" in c["instructions"]


def test_compact_ambient_partial_streaming_language():
    """B098/B121: streaming=true → conversation compact (full TTS, not hard HOLD)."""
    c = compact_mode_a_event(
        {
            "kind": "ambient.partial",
            "event_id": "e-partial-stream",
            "stream_id": "s100",
            "seq": 1,
            "text": "looking that up",
            "fragment": "looking that up",
            "streaming": True,
        }
    )
    assert c["streaming"] is True
    assert "STREAMING" in c["instructions"] or "CONVERSATION" in c["instructions"]
    assert "HOLD" not in c["instructions"] or "CONVERSATION" in c["instructions"]
    assert "listen-end" in c["instructions"]
    assert "pane" in c["instructions"].lower()
    # Full TTS allowed; not radio-with-acks-only
    assert "full TTS" in c["instructions"] or "TTS reply" in c["instructions"]


def test_compact_ambient_partial_includes_fragment():
    c = compact_mode_a_event(
        {
            "kind": "ambient.partial",
            "event_id": "p1",
            "stream_id": "s1",
            "seq": 2,
            "text": "hello world more",
            "fragment": "more",
        }
    )
    assert c["fragment"] == "more"
    assert c["text"] == "hello world more"
    assert c["text_len"] == len("hello world more")


def test_compact_ambient_partial_includes_text_len():
    """B039: monitor compact partials expose text_len so agents see growth."""
    long = "prefix " + ("x" * 500)
    c = compact_mode_a_event(
        {
            "kind": "ambient.partial",
            "event_id": "e3",
            "stream_id": "s1",
            "seq": 2,
            "text": long,
        }
    )
    assert c["partial"] is True
    assert c["final"] is False
    assert c["text_len"] == len(long)
    assert c["text"].endswith("…")
    assert len(c["text"]) <= 401
    # short text: full text + matching len
    c2 = compact_mode_a_event(
        {
            "kind": "ambient.partial",
            "stream_id": "s1",
            "seq": 1,
            "text": "hello radio",
        }
    )
    assert c2["text"] == "hello radio"
    assert c2["text_len"] == len("hello radio")


# --- B102: singleflight monitor consumer lock ---


def test_monitor_feed_lock_exclusive(tmp_path: Path):
    first = MonitorFeedLock(tmp_path, pid=os.getpid())
    path = first.acquire()
    assert path.is_file()
    assert path.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert read_monitor_holder_pid(tmp_path) == os.getpid()
    probe = probe_monitor_consumer(tmp_path)
    assert probe["running"] is True
    assert probe["pid"] == os.getpid()

    second = MonitorFeedLock(tmp_path, pid=os.getpid())
    with pytest.raises(MonitorBusyError, match="already running"):
        second.acquire()

    first.release()
    assert read_monitor_holder_pid(tmp_path) is None
    # second can now take over
    second.acquire()
    assert read_monitor_holder_pid(tmp_path) == os.getpid()
    second.release()


def test_monitor_feed_lock_context_manager(tmp_path: Path):
    with MonitorFeedLock(tmp_path) as lock:
        assert lock._held is True
        with pytest.raises(MonitorBusyError):
            MonitorFeedLock(tmp_path).acquire()
    assert read_monitor_holder_pid(tmp_path) is None


def test_run_monitor_refuses_second_consumer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr("hark.monitor_feed.state_dir", lambda: tmp_path)
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)

    held = MonitorFeedLock(tmp_path)
    held.acquire()
    try:
        code = run_monitor(replay=0, allow_multiple=False, state_root=tmp_path)
        assert code == ERROR
        err = capsys.readouterr().err
        assert "already running" in err
    finally:
        held.release()


def test_run_monitor_allow_multiple_skips_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("hark.monitor_feed.state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "hark.monitor_feed.follow_state_files",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr("hark.monitor_feed.default_feed_paths", lambda: [])

    held = MonitorFeedLock(tmp_path)
    held.acquire()
    try:
        code = run_monitor(replay=0, allow_multiple=True, state_root=tmp_path, paths=[])
        assert code == 0
    finally:
        held.release()


def test_run_monitor_acquires_and_releases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("hark.monitor_feed.state_dir", lambda: tmp_path)
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)

    seen: dict[str, bool] = {"held_during": False}

    def fake_follow(*a, **k):
        seen["held_during"] = read_monitor_holder_pid(tmp_path) == os.getpid()
        return 0

    monkeypatch.setattr("hark.monitor_feed.follow_state_files", fake_follow)
    monkeypatch.setattr("hark.monitor_feed.default_feed_paths", lambda: [])

    code = run_monitor(replay=0, state_root=tmp_path, paths=[])
    assert code == 0
    assert seen["held_during"] is True
    assert read_monitor_holder_pid(tmp_path) is None


# --- B104: dual-write ambient HEP ---


def test_emit_hep_dual_writes_when_stdout_redirected(tmp_path: Path, monkeypatch):
    """B104: HEP wake events must hit ambient.jsonl even if out is a restart log."""
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)
    restart = tmp_path / "ambient-restart.log"
    feed = tmp_path / "ambient.jsonl"

    event = {
        "schema": "hark.event.v1",
        "kind": "ambient.prompt",
        "event_id": "b104-prompt-1",
        "observed_at": "2026-07-13T22:03:00.000Z",
        "text": "run the py-subagents job",
        "final": True,
        "partial": False,
    }
    with restart.open("a", encoding="utf-8") as out:
        emit_hep(event, out)

    restart_lines = restart.read_text(encoding="utf-8").splitlines()
    feed_lines = feed.read_text(encoding="utf-8").splitlines()
    assert len(restart_lines) == 1
    assert len(feed_lines) == 1
    assert json.loads(restart_lines[0])["event_id"] == "b104-prompt-1"
    assert json.loads(feed_lines[0])["event_id"] == "b104-prompt-1"
    assert json.loads(feed_lines[0])["kind"] == "ambient.prompt"


def test_emit_hep_skips_dual_write_when_out_is_ambient_jsonl(
    tmp_path: Path, monkeypatch
):
    """Workers that redirect stdout → ambient.jsonl must not double-append."""
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)
    feed = ambient_feed_path(tmp_path)
    event = {
        "schema": "hark.event.v1",
        "kind": "ambient.prompt",
        "event_id": "b104-once",
        "text": "hello",
    }
    with feed.open("a", encoding="utf-8") as out:
        assert io_targets_path(out, feed) is True
        emit_hep(event, out)

    lines = feed.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["event_id"] == "b104-once"


def test_emit_hep_direct_canonical_feed_stamps_once_and_is_filtered(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)
    feed = ambient_feed_path(tmp_path)
    event = {
        "kind": "ambient.prompt",
        "event_id": "direct-synthetic",
        "hark_provenance": "operator",
    }

    with feed.open("a", encoding="utf-8") as out:
        emit_hep(event, out)

    lines = feed.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["hark_provenance"] == "test"
    assert event["hark_provenance"] == "operator"

    replayed = StringIO()
    assert (
        replay_matching(
            [feed],
            kinds=MODE_A_WAKE_KINDS,
            limit=10,
            for_monitor=False,
            out=replayed,
        )
        == 0
    )
    assert replayed.getvalue() == ""


def test_emit_hep_dual_writes_stringio_out(tmp_path: Path, monkeypatch):
    """StringIO (tests / in-memory) still dual-writes to the feed path."""
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)
    out = StringIO()
    emit_hep(
        {
            "schema": "hark.event.v1",
            "kind": "ambient.partial",
            "event_id": "b104-partial",
            "text": "half",
            "partial": True,
            "final": False,
        },
        out,
    )
    assert "b104-partial" in out.getvalue()
    feed_text = (tmp_path / "ambient.jsonl").read_text(encoding="utf-8")
    assert "b104-partial" in feed_text
    assert "ambient.partial" in feed_text


def test_execution_provenance_overrides_caller_only_in_persisted_copy(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)
    event = {
        "kind": "ambient.prompt",
        "event_id": "reserved-provenance",
        "hark_provenance": "operator",
    }
    out = StringIO()

    emit_hep(event, out)

    assert json.loads(out.getvalue())["hark_provenance"] == "operator"
    assert event["hark_provenance"] == "operator"
    feed = tmp_path / "ambient.jsonl"
    stored = json.loads(feed.read_text(encoding="utf-8"))
    assert stored["hark_provenance"] == "test"

    replayed = StringIO()
    assert (
        replay_matching(
            [feed],
            kinds=MODE_A_WAKE_KINDS,
            limit=10,
            for_monitor=False,
            out=replayed,
        )
        == 0
    )
    assert replayed.getvalue() == ""


def test_append_ambient_jsonl_helper(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)
    ok = append_ambient_jsonl({"kind": "tts.truncated", "event_id": "t1"})
    assert ok is True
    text = (tmp_path / "ambient.jsonl").read_text(encoding="utf-8")
    assert "tts.truncated" in text
    assert json.loads(text)["hark_provenance"] == "test"


def test_default_feed_write_uses_autouse_isolated_state(
    isolated_state_home: Path,
):
    from hark.paths import state_dir

    event = {"kind": "ambient.prompt", "event_id": "isolation-regression"}
    assert append_ambient_jsonl(event) is True

    expected = isolated_state_home / "hark" / "ambient.jsonl"
    assert state_dir() == isolated_state_home / "hark"
    assert expected.is_file()
    stored = json.loads(expected.read_text(encoding="utf-8"))
    assert stored["event_id"] == "isolation-regression"
    assert stored["hark_provenance"] == "test"

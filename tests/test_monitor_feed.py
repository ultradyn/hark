"""Unified Mode A monitor feed (wake_near_miss + agent + ambient.prompt)."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from hark.monitor_feed import (
    MODE_A_WAKE_KINDS,
    compact_mode_a_event,
    replay_matching,
    should_surface,
)


def test_wake_near_miss_is_mode_a_wake_kind():
    assert "ambient.wake_near_miss" in MODE_A_WAKE_KINDS
    assert "ambient.prompt" in MODE_A_WAKE_KINDS
    assert "agent.blocked" in MODE_A_WAKE_KINDS
    assert "ambient.debug" not in MODE_A_WAKE_KINDS


def test_should_surface_filters():
    assert should_surface(
        {"kind": "ambient.wake_near_miss"}, MODE_A_WAKE_KINDS
    )
    assert not should_surface({"kind": "ambient.debug"}, MODE_A_WAKE_KINDS)


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
    lines = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    kinds = [l["kind"] for l in lines]
    assert kinds == [
        "agent.blocked",
        "ambient.wake_near_miss",
        "ambient.prompt",
    ]
    # near-miss came through (the bug class)
    near = next(l for l in lines if l["kind"] == "ambient.wake_near_miss")
    assert near["attempts"] == ["clunker"]


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

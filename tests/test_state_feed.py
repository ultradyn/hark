"""P1.M5 StateFeedFollower core + presentation unify."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hark.dashboard.tailer import MultiTailer, SourceTailer
from hark.monitor_feed import compact_mode_a_event
from hark.state_feed import (
    CursorPosition,
    FeedRecord,
    InvalidCursorPosition,
    SourceFollower,
    StateFeedFollower,
    format_cursor,
    parse_cursor,
    parse_cursor_positions,
    present_for_monitor,
)


def _write(path: Path, *objs: dict, mode: str = "a") -> None:
    with path.open(mode, encoding="utf-8") as fh:
        for obj in objs:
            fh.write(json.dumps(obj) + "\n")


def test_source_follower_is_dashboard_source_tailer_alias():
    assert SourceTailer is SourceFollower
    assert FeedRecord.__name__ == "FeedRecord"


def test_partial_and_rotation_via_core(tmp_path: Path):
    f = tmp_path / "watch.jsonl"
    f.write_text("")
    t = SourceFollower(f, source="watch")
    t.start_at_end()
    with f.open("a") as fh:
        fh.write('{"kind": "agent.blo')
    assert list(t.poll()) == []
    with f.open("a") as fh:
        fh.write('cked"}\n')
    recs = list(t.poll())
    assert [r.payload["kind"] for r in recs] == ["agent.blocked"]

    rotated = tmp_path / "watch.jsonl.1"
    f.rename(rotated)
    _write(f, {"kind": "ambient.prompt", "text": "hi"}, mode="w")
    recs = list(t.poll())
    assert [r.payload["kind"] for r in recs] == ["ambient.prompt"]
    assert recs[0].seq == 1


def test_composite_cursor_format_roundtrip():
    assert parse_cursor("watch:12,bound:3") == {"watch": 12, "bound": 3}
    assert format_cursor({"watch": 12, "bound": 3}) == "watch:12,bound:3"
    assert format_cursor([("a", 1), ("b", 2)]) == "a:1,b:2"

    cursor = format_cursor(
        {
            "watch": CursorPosition(12, "a" * 32, "b" * 32),
            "bound": CursorPosition(3, "c" * 32, "d" * 32),
        }
    )
    assert cursor == f"watch:12@{'a' * 32}~{'b' * 32},bound:3@{'c' * 32}~{'d' * 32}"
    assert parse_cursor(cursor) == {"watch": 12, "bound": 3}
    assert parse_cursor_positions(cursor)["watch"] == CursorPosition(
        12, "a" * 32, "b" * 32
    )

    # Incarnation-only B131-preview tokens remain parseable but unproven.
    assert parse_cursor_positions("watch:12@old-token")["watch"] == CursorPosition(
        12, "old-token", None
    )


@pytest.mark.parametrize(
    "key",
    ["", "Watch", "watch,ambient", "watch:ambient", "watch\nid", "watch\rid"],
)
def test_format_cursor_rejects_noncanonical_or_injectable_keys(key: str):
    with pytest.raises(ValueError):
        format_cursor({key: 1})


@pytest.mark.parametrize(
    "position",
    [
        CursorPosition(1, None, "b" * 32),
        CursorPosition(1, "", None),
        CursorPosition(1, "bad,incarnation", None),
        CursorPosition(1, "bad\nincarnation", None),
        CursorPosition(1, "a" * 31, "b" * 32),
        CursorPosition(1, "A" * 32, "b" * 32),
        CursorPosition(1, "a" * 32, "B" * 32),
        CursorPosition(1, "legacy-preview", "b" * 32),
    ],
)
def test_format_cursor_rejects_partial_or_invalid_proofs(position: CursorPosition):
    with pytest.raises(ValueError):
        format_cursor({"watch": position})


def test_format_cursor_accepts_safe_legacy_incarnation_only():
    cursor = format_cursor({"watch": CursorPosition(1, "Preview_1")})
    assert cursor == "watch:1@Preview_1"
    assert parse_cursor_positions(cursor)["watch"] == CursorPosition(1, "Preview_1")


@pytest.mark.parametrize("sequence", [-1, 10**19, True])
def test_format_cursor_rejects_out_of_grammar_sequences(sequence):
    with pytest.raises((TypeError, ValueError)):
        format_cursor({"watch": sequence})


def test_composite_cursor_rejects_sse_id_injection_key(tmp_path: Path):
    source = SourceFollower(
        tmp_path / "watch.jsonl",
        source="watch",
        cursor_key="watch\nid: injected",
    )
    follower = StateFeedFollower([source])
    with pytest.raises(ValueError):
        follower.composite_cursor()


def test_invalid_sequences_are_typed_without_integer_conversion():
    invalid = ("x", "-1", "１２", "9" * 5000)
    for raw_sequence in invalid:
        cursor = f"watch:{raw_sequence}"
        position = parse_cursor_positions(cursor)["watch"]
        assert position == InvalidCursorPosition()
        assert parse_cursor(cursor) == {}


def test_invalid_known_source_positions_replay_from_zero(tmp_path: Path):
    invalid = ("x", "-1", "１２", "9" * 5000)
    for index, raw_sequence in enumerate(invalid):
        case = tmp_path / str(index)
        case.mkdir()
        path = case / "watch.jsonl"
        _write(path, {"n": 1}, {"n": 2})
        follower = StateFeedFollower([SourceFollower(path, source="watch")])

        follower.start_from(f"watch:{raw_sequence}")

        assert [record.payload["n"] for record in follower.poll()] == [1, 2]
        follower.close()


def test_resume_replays_rotated_replacement_of_any_length(tmp_path: Path):
    for replacement_count in (2, 3, 5):
        case = tmp_path / str(replacement_count)
        case.mkdir()
        path = case / "watch.jsonl"
        _write(path, *({"old": n} for n in range(3)))
        old = SourceFollower(path, source="watch")
        old.seek_to(0)
        assert len(list(old.poll())) == 3
        stale_position = old.cursor_position
        assert len(stale_position.incarnation or "") == 32
        assert set(stale_position.incarnation or "") <= set("0123456789abcdef")
        assert len(stale_position.checkpoint or "") == 32
        old.close()

        path.rename(case / "watch.jsonl.1")
        _write(path, *({"new": n} for n in range(replacement_count)))
        resumed = SourceFollower(path, source="watch")
        resumed.seek_to(
            stale_position.seq,
            incarnation=stale_position.incarnation,
            checkpoint=stale_position.checkpoint,
        )
        records = list(resumed.poll())
        assert [record.payload["new"] for record in records] == list(
            range(replacement_count)
        )
        assert records[0].seq == 1
        assert resumed.cursor_position.incarnation != stale_position.incarnation


def test_fresh_follower_replays_same_prefix_in_place_rewrite_of_any_length(
    tmp_path: Path,
):
    for replacement_count in (2, 3, 5):
        case = tmp_path / f"rewrite-{replacement_count}"
        case.mkdir()
        path = case / "watch.jsonl"
        _write(path, {"n": "same"}, {"n": "old-1"}, {"n": "old-2"})
        old = SourceFollower(path, source="watch")
        old.seek_to(0)
        assert len(list(old.poll())) == 3
        stale_position = old.cursor_position
        old.close()

        # Path.write_text truncates the existing inode before the replacement
        # records are appended, matching state writers that reuse the path.
        path.write_text("", encoding="utf-8")
        replacement = [{"n": "same"}] + [
            {"n": f"new-{n}"} for n in range(1, replacement_count)
        ]
        _write(path, *replacement)
        resumed = SourceFollower(path, source="watch")
        resumed.seek_to(
            stale_position.seq,
            incarnation=stale_position.incarnation,
            checkpoint=stale_position.checkpoint,
        )
        records = list(resumed.poll())

        assert [record.payload["n"] for record in records] == [
            record["n"] for record in replacement
        ]
        assert records[0].seq == 1
        assert resumed.cursor_position.checkpoint != stale_position.checkpoint


def test_valid_same_incarnation_resumes_at_sequence_plus_one(tmp_path: Path):
    path = tmp_path / "watch.jsonl"
    _write(path, *({"n": n} for n in range(2)))
    source = SourceFollower(path, source="watch")
    source.seek_to(0)
    assert [record.payload["n"] for record in source.poll()] == [0, 1]
    position = source.cursor_position
    cursor = format_cursor({"watch": position})
    source.close()

    # The bounded first-record identity stays stable across ordinary appends.
    _write(path, *({"n": n} for n in range(2, 4)))

    follower = StateFeedFollower([SourceFollower(path, source="watch")])
    follower.start_from(cursor)
    records = list(follower.poll())
    assert [record.payload["n"] for record in records] == [2, 3]
    assert {record.incarnation for record in records} == {position.incarnation}
    resumed_position = follower.sources[0].cursor_position
    assert resumed_position.incarnation == position.incarnation
    assert resumed_position.checkpoint != position.checkpoint


def test_stale_cursor_waits_for_first_complete_replacement_record(tmp_path: Path):
    path = tmp_path / "watch.jsonl"
    _write(path, {"old": 1}, {"old": 2})
    original = SourceFollower(path, source="watch")
    original.start_at_end()
    stale_position = original.cursor_position
    original.close()

    path.rename(tmp_path / "watch.jsonl.1")
    path.write_text('{"new": "first', encoding="utf-8")
    resumed = SourceFollower(path, source="watch")
    resumed.seek_to(
        stale_position.seq,
        incarnation=stale_position.incarnation,
        checkpoint=stale_position.checkpoint,
    )

    assert list(resumed.poll()) == []
    with path.open("a", encoding="utf-8") as handle:
        handle.write(' complete"}\n')

    records = list(resumed.poll())
    assert [(record.seq, record.payload) for record in records] == [
        (1, {"new": "first complete"})
    ]


def test_legacy_line_only_cursor_prefers_duplicates(tmp_path: Path):
    path = tmp_path / "watch.jsonl"
    _write(path, *({"n": n} for n in range(3)))
    for cursor in ("watch:2", "watch:2@preview-incarnation"):
        follower = StateFeedFollower([SourceFollower(path, source="watch")])
        follower.start_from(cursor)
        assert [record.payload["n"] for record in follower.poll()] == [0, 1, 2]
        follower.close()


def test_legacy_cursor_replays_replacement_instead_of_skipping_it(tmp_path: Path):
    path = tmp_path / "watch.jsonl"
    _write(path, *({"old": n} for n in range(3)))

    stale_cursor = "watch:3"
    path.rename(tmp_path / "watch.jsonl.1")
    _write(path, {"new": 1}, {"new": 2})

    follower = StateFeedFollower([SourceFollower(path, source="watch")])
    follower.start_from(stale_cursor)

    assert [record.payload for record in follower.poll()] == [
        {"new": 1},
        {"new": 2},
    ]


def test_state_feed_follower_multi_source(tmp_path: Path):
    _write(tmp_path / "watch.jsonl", {"kind": "agent.blocked", "n": 1})
    _write(tmp_path / "ambient.jsonl", {"kind": "ambient.prompt", "text": "x"})
    follower = StateFeedFollower(
        [
            SourceFollower(tmp_path / "watch.jsonl", source="watch"),
            SourceFollower(tmp_path / "ambient.jsonl", source="ambient"),
        ]
    )
    follower.start_from(None, default_tail=100)
    recs = list(follower.poll())
    kinds = {r.payload["kind"] for r in recs}
    assert "agent.blocked" in kinds and "ambient.prompt" in kinds
    cur = parse_cursor(follower.composite_cursor())
    assert cur["watch"] >= 1 and cur["ambient"] >= 1
    follower.close()


def test_multitailer_uses_state_feed_core(tmp_path: Path):
    _write(tmp_path / "watch.jsonl", {"kind": "agent.blocked"})
    mt = MultiTailer(tmp_path)
    mt.start_from(None, default_tail=50)
    recs = list(mt.poll())
    assert any(r.payload.get("kind") == "agent.blocked" for r in recs)
    # resume cursor works
    cursor = mt.composite_cursor()
    mt.close()
    mt2 = MultiTailer(tmp_path)
    mt2.start_from(cursor)
    assert list(mt2.poll()) == []
    _write(tmp_path / "watch.jsonl", {"kind": "agent.needs_input"})
    more = list(mt2.poll())
    assert [r.payload["kind"] for r in more] == ["agent.needs_input"]
    mt2.close()


def test_present_for_monitor_is_compact_alias():
    assert present_for_monitor is not None
    ev = {
        "schema": "hark.event.v1",
        "kind": "ambient.wake_near_miss",
        "event_id": "e1",
        "attempts": [{"text": "clunker"}],
    }
    a = present_for_monitor(ev)
    b = compact_mode_a_event(ev)
    assert a == b
    assert a["attempts"] == ["clunker"]
    assert "instructions" in a


def test_present_agent_uses_monitor_profile_once():
    ev = {
        "schema": "hark.event.v1",
        "kind": "agent.blocked",
        "event_id": "e9",
        "session_id": "s1",
        "target": {"server_instance": "s1", "pane_id": "p1", "agent": "claude"},
        "state": {"to": "blocked"},
        "question": {"text": "Ship it?", "risk": "R1"},
    }
    c = present_for_monitor(ev)
    assert c["kind"] == "agent.blocked"
    assert c.get("question") == "Ship it?" or c.get("question")
    # re-present is stable enough for orchestrators
    c2 = present_for_monitor(c)
    assert c2["kind"] == "agent.blocked"

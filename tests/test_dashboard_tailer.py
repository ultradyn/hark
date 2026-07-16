"""B061: JSONL tailer hardening — partial lines, truncation, rotation, cursors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hark.dashboard.tailer import (
    MultiTailer,
    SourceTailer,
    parse_cursor,
    read_page,
    records_with_cursors,
)
from hark.state_feed import canonicalize_cursor


def _write(path: Path, *objs: dict, mode: str = "a") -> None:
    with path.open(mode, encoding="utf-8") as fh:
        for obj in objs:
            fh.write(json.dumps(obj) + "\n")


def test_live_tail_emits_new_records(tmp_path):
    f = tmp_path / "watch.jsonl"
    _write(f, {"kind": "old"})
    t = SourceTailer(f, source="watch")
    t.start_at_end()
    assert list(t.poll()) == []
    _write(f, {"kind": "agent.blocked"})
    recs = list(t.poll())
    assert [r.payload["kind"] for r in recs] == ["agent.blocked"]
    assert recs[0].seq == 2  # true line index, history counted


def test_partial_line_is_buffered_not_dropped(tmp_path):
    f = tmp_path / "watch.jsonl"
    f.write_text("")
    t = SourceTailer(f, source="watch")
    t.start_at_end()
    with f.open("a") as fh:
        fh.write('{"kind": "agent.blo')  # mid-append, no newline
    assert list(t.poll()) == []
    with f.open("a") as fh:
        fh.write('cked"}\n')
    recs = list(t.poll())
    assert [r.payload["kind"] for r in recs] == ["agent.blocked"]


def test_partial_line_at_start_is_not_split(tmp_path):
    f = tmp_path / "watch.jsonl"
    f.write_text('{"kind": "comp')  # partial already on disk at open time
    t = SourceTailer(f, source="watch")
    t.start_at_end()
    with f.open("a") as fh:
        fh.write('lete"}\n')
    recs = list(t.poll())
    assert [r.payload["kind"] for r in recs] == ["complete"]


def test_truncation_restarts_from_top(tmp_path):
    f = tmp_path / "watch.jsonl"
    _write(f, {"n": 1}, {"n": 2})
    t = SourceTailer(f, source="watch")
    t.start_at_end()
    f.write_text("")  # truncate in place
    _write(f, {"n": 3})
    recs = list(t.poll())
    assert [r.payload["n"] for r in recs] == [3]
    assert recs[0].seq == 1  # new incarnation


def test_rotation_by_inode_restarts(tmp_path):
    f = tmp_path / "watch.jsonl"
    _write(f, {"n": 1})
    t = SourceTailer(f, source="watch")
    t.start_at_end()
    rotated = tmp_path / "watch.jsonl.1"
    f.rename(rotated)
    _write(f, {"n": 2}, mode="w")  # new inode, same size as before
    recs = list(t.poll())
    assert [r.payload["n"] for r in recs] == [2]
    assert recs[0].seq == 1


def test_seek_to_resumes_after_cursor(tmp_path):
    f = tmp_path / "watch.jsonl"
    _write(f, {"n": 1}, {"n": 2}, {"n": 3})
    t = SourceTailer(f, source="watch")
    t.seek_to(1)
    recs = list(t.poll())
    assert [r.payload["n"] for r in recs] == [2, 3]


def test_garbage_lines_advance_seq_but_do_not_emit(tmp_path):
    f = tmp_path / "watch.jsonl"
    f.write_text('{"n": 1}\nnot json\n{"n": 2}\n')
    t = SourceTailer(f, source="watch")
    t.seek_to(0)
    recs = list(t.poll())
    assert [(r.seq, r.payload["n"]) for r in recs] == [(1, 1), (3, 2)]


def test_parse_cursor():
    assert parse_cursor("watch:12,system:9") == {"watch": 12, "system": 9}
    assert parse_cursor(None) == {}
    assert parse_cursor("bogus") == {}
    assert parse_cursor("watch:x,ambient:3") == {"ambient": 3}


def test_external_cursor_grammar_is_strict_and_canonical():
    assert canonicalize_cursor("watch:0002,ambient:03") == "watch:2,ambient:3"
    for invalid in (
        "",
        "watch:1,",
        "watch:1,,ambient:2",
        "watch:-1",
        "watch:1,watch:2",
        "watch:1\nid: watch:999",
        " watch:1",
    ):
        with pytest.raises(ValueError):
            canonicalize_cursor(invalid)


def test_multitailer_composite_cursor_and_delivery_split(tmp_path):
    _write(
        tmp_path / "events.jsonl", {"event_id": "e1", "session_id": "s", "pane_id": "p"}
    )
    _write(
        tmp_path / "deliveries.jsonl",
        {"event_id": "e1", "status": "delivered", "ts": 1.0},
    )
    mt = MultiTailer(tmp_path)
    mt.start_from(None, default_tail=100)
    recs = list(mt.poll())
    by_key = {r.cursor_key: r for r in recs}
    assert by_key["bound"].source == "delivery"
    assert by_key["bound"].payload["type"] == "bound"
    assert by_key["delivery"].payload["type"] == "outcome"
    cur = parse_cursor(mt.composite_cursor())
    assert cur["bound"] == 1 and cur["delivery"] == 1 and cur["watch"] == 0


def test_read_page_since_and_limit(tmp_path):
    _write(
        tmp_path / "watch.jsonl", *({"kind": "agent.blocked", "n": i} for i in range(5))
    )
    records, cursor, complete = read_page(tmp_path, since=None, limit=500)
    assert len(records) == 5 and complete
    records2, cursor2, _ = read_page(tmp_path, since="watch:3", limit=500)
    assert [r.payload["n"] for r in records2] == [3, 4]
    assert parse_cursor(cursor2)["watch"] == 5
    records3, _, complete3 = read_page(tmp_path, since=None, limit=2)
    assert [r.payload["n"] for r in records3] == [3, 4]
    assert not complete3

    page1, page1_cursor, page1_complete = read_page(tmp_path, since="watch:0", limit=2)
    assert [r.payload["n"] for r in page1] == [0, 1]
    assert parse_cursor(page1_cursor)["watch"] == 2
    assert not page1_complete
    page2, page2_cursor, page2_complete = read_page(
        tmp_path, since=page1_cursor, limit=2
    )
    assert [r.payload["n"] for r in page2] == [2, 3]
    assert parse_cursor(page2_cursor)["watch"] == 4
    assert not page2_complete


def test_replay_cursor_advances_only_after_each_sorted_record(tmp_path):
    _write(
        tmp_path / "watch.jsonl",
        {"kind": "agent.blocked", "n": "w1", "ts": 1.0},
        {"kind": "agent.blocked", "n": "w2", "ts": 3.0},
    )
    _write(tmp_path / "ambient.jsonl", {"kind": "ambient.prompt", "n": "a1", "ts": 2.0})

    records, page_cursor, complete = read_page(
        tmp_path, since="watch:0,ambient:0", limit=None
    )
    replay = [
        (record.payload["n"], cursor)
        for record, cursor in records_with_cursors(records, "watch:0,ambient:0")
    ]

    assert complete
    assert replay == [
        ("w1", "watch:1,ambient:0"),
        ("a1", "watch:1,ambient:1"),
        ("w2", "watch:2,ambient:1"),
    ]
    assert parse_cursor(page_cursor)["watch"] == 2
    assert parse_cursor(page_cursor)["ambient"] == 1


def test_replay_order_never_reorders_nonmonotonic_cursor_key(tmp_path):
    _write(
        tmp_path / "watch.jsonl",
        {"kind": "agent.blocked", "n": "w1", "ts": 3.0},
        {"kind": "agent.blocked", "n": "w2", "ts": 1.0},
    )
    _write(tmp_path / "ambient.jsonl", {"kind": "ambient.prompt", "n": "a1", "ts": 2.0})

    records, _, _ = read_page(tmp_path, since="watch:0,ambient:0", limit=None)
    replay = [
        (record.payload["n"], cursor)
        for record, cursor in records_with_cursors(records, "watch:0,ambient:0")
    ]

    # Ambient wins the cross-source head comparison, but w2 remains behind w1
    # even though its payload timestamp is earlier.
    assert replay == [
        ("a1", "watch:0,ambient:1"),
        ("w1", "watch:1,ambient:1"),
        ("w2", "watch:2,ambient:1"),
    ]

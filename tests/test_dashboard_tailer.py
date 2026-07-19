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
from hark.state_feed import canonicalize_cursor, format_cursor, parse_cursor_positions


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

    # Proved same-file resume skips to the next unseen record.
    same_file = SourceTailer(tmp_path / "watch.jsonl", source="watch")
    same_file.seek_to(3)
    same_file_cursor = format_cursor({"watch": same_file.cursor_position})
    same_file.close()
    records2, cursor2, _ = read_page(tmp_path, since=same_file_cursor, limit=500)
    assert [r.payload["n"] for r in records2] == [3, 4]
    assert parse_cursor(cursor2)["watch"] == 5

    # A line-only token cannot prove file identity, so duplicates beat loss (B131).
    legacy, _, _ = read_page(tmp_path, since="watch:3", limit=500)
    assert [r.payload["n"] for r in legacy] == [0, 1, 2, 3, 4]

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
        tmp_path, since="watch:0,ambient:0", limit=100
    )
    replay = [
        (record.payload["n"], cursor)
        for record, cursor in records_with_cursors(records, "watch:0,ambient:0")
    ]

    assert complete
    assert [name for name, _ in replay] == ["w1", "a1", "w2"]
    assert [parse_cursor(cursor) for _, cursor in replay] == [
        {"watch": 1, "ambient": 0},
        {"watch": 1, "ambient": 1},
        {"watch": 2, "ambient": 1},
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

    records, _, _ = read_page(tmp_path, since="watch:0,ambient:0", limit=100)
    replay = [
        (record.payload["n"], cursor)
        for record, cursor in records_with_cursors(records, "watch:0,ambient:0")
    ]

    # Ambient wins the cross-source head comparison, but w2 remains behind w1
    # even though its payload timestamp is earlier.
    assert [name for name, _ in replay] == ["a1", "w1", "w2"]
    assert [parse_cursor(cursor) for _, cursor in replay] == [
        {"watch": 0, "ambient": 1},
        {"watch": 1, "ambient": 1},
        {"watch": 2, "ambient": 1},
    ]


def test_forward_page_materializes_only_limit_plus_source_heads(tmp_path, monkeypatch):
    import hark.dashboard.tailer as dashboard_tailer

    _write(
        tmp_path / "watch.jsonl",
        *({"kind": "agent.blocked", "n": n, "ts": float(n)} for n in range(5000)),
    )
    materialized = 0
    real_record_ts = dashboard_tailer._record_ts

    def counted_record_ts(record):
        nonlocal materialized
        materialized += 1
        return real_record_ts(record)

    monkeypatch.setattr(dashboard_tailer, "_record_ts", counted_record_ts)
    records, cursor, complete = read_page(
        tmp_path, since="watch:0", sources={"watch"}, limit=2
    )

    assert [record.payload["n"] for record in records] == [0, 1]
    assert parse_cursor(cursor)["watch"] == 2
    assert complete is False
    assert materialized == 3


def test_repeated_forward_pages_do_not_rescan_the_unseen_suffix(tmp_path, monkeypatch):
    import hark.dashboard.tailer as dashboard_tailer

    _write(
        tmp_path / "watch.jsonl",
        *({"kind": "agent.blocked", "n": n, "ts": float(n)} for n in range(5)),
    )
    materialized = 0
    real_record_ts = dashboard_tailer._record_ts

    def counted_record_ts(record):
        nonlocal materialized
        materialized += 1
        return real_record_ts(record)

    monkeypatch.setattr(dashboard_tailer, "_record_ts", counted_record_ts)
    cursor = "watch:0"
    seen = []
    while True:
        records, cursor, complete = read_page(
            tmp_path, since=cursor, sources={"watch"}, limit=2
        )
        seen.extend(record.payload["n"] for record in records)
        if complete:
            break

    assert seen == list(range(5))
    assert materialized == 7  # 3 + 3 + 1, never the old 5 + 3 + 1


def test_repeated_pages_seek_from_checkpoint_instead_of_skipping_prefix(
    tmp_path, monkeypatch
):
    import hark.state_feed.source as state_source

    _write(
        tmp_path / "watch.jsonl",
        *({"kind": "agent.blocked", "n": n} for n in range(20)),
    )
    skipped_lines = 0
    raw_lines = 0
    real_skip = state_source.SourceFollower._skip_lines
    real_read = state_source.SourceFollower._read_complete_line

    def counted_skip(follower, target):
        nonlocal skipped_lines
        before = follower.seq
        result = real_skip(follower, target)
        skipped_lines += follower.seq - before
        return result

    def counted_read(follower):
        nonlocal raw_lines
        line = real_read(follower)
        if line is not None:
            raw_lines += 1
        return line

    monkeypatch.setattr(state_source.SourceFollower, "_skip_lines", counted_skip)
    monkeypatch.setattr(
        state_source.SourceFollower, "_read_complete_line", counted_read
    )
    cursor = "watch:0"
    seen = []
    while True:
        records, cursor, complete = read_page(
            tmp_path, since=cursor, sources={"watch"}, limit=2
        )
        seen.extend(record.payload["n"] for record in records)
        if complete:
            break

    assert seen == list(range(20))
    assert skipped_lines == 0  # not 0 + 2 + ... + 18 = 90
    assert raw_lines == 29  # three per incomplete page, then the final two


def test_checkpoint_mismatch_replays_rotated_file_from_zero(tmp_path):
    watch = tmp_path / "watch.jsonl"
    _write(watch, *({"kind": "agent.blocked", "n": f"old-{n}"} for n in range(4)))
    _, cursor, complete = read_page(
        tmp_path, since="watch:0", sources={"watch"}, limit=2
    )
    assert complete is False

    watch.rename(tmp_path / "watch.jsonl.1")
    _write(
        watch,
        *({"kind": "agent.blocked", "n": f"new-{n}"} for n in range(3)),
        mode="w",
    )
    records, _, _ = read_page(tmp_path, since=cursor, sources={"watch"}, limit=2)
    assert [record.payload["n"] for record in records] == ["new-0", "new-1"]


def test_checkpoint_mismatch_detects_in_place_acknowledged_rewrite(tmp_path):
    watch = tmp_path / "watch.jsonl"
    _write(watch, *({"kind": "agent.blocked", "n": f"old-{n}"} for n in range(4)))
    _, cursor, _ = read_page(tmp_path, since="watch:0", sources={"watch"}, limit=2)

    _write(
        watch,
        {"kind": "agent.blocked", "n": "old-0"},
        {"kind": "agent.blocked", "n": "new-1"},
        {"kind": "agent.blocked", "n": "new-2"},
        {"kind": "agent.blocked", "n": "new-3"},
        mode="w",
    )
    records, _, _ = read_page(tmp_path, since=cursor, sources={"watch"}, limit=2)
    assert [record.payload["n"] for record in records] == ["old-0", "new-1"]


def test_filtered_complete_page_preserves_unselected_cursor_keys(tmp_path):
    _write(tmp_path / "watch.jsonl", {"kind": "agent.blocked", "n": 1})
    _write(tmp_path / "ambient.jsonl", {"kind": "ambient.prompt", "n": 1})

    records, cursor, complete = read_page(
        tmp_path,
        since="watch:0,ambient:0",
        sources={"watch"},
        limit=100,
    )

    assert [record.source for record in records] == ["watch"]
    assert complete is True
    assert parse_cursor(cursor) == {"watch": 1, "ambient": 0}


def test_live_changed_prefix_rewrite_restarts_for_shorter_equal_and_longer(tmp_path):
    for replacement_count in (2, 3, 5):
        case = tmp_path / str(replacement_count)
        case.mkdir()
        path = case / "watch.jsonl"
        _write(path, *({"old": n} for n in range(3)))
        tailer = SourceTailer(path, source="watch")
        tailer.start_at_end()
        old_incarnation = tailer.cursor_position.incarnation

        path.write_text("", encoding="utf-8")
        _write(path, *({"new": n} for n in range(replacement_count)))
        records = list(tailer.poll())

        assert [record.payload["new"] for record in records] == list(
            range(replacement_count)
        )
        assert records[0].seq == 1
        assert records[0].incarnation != old_incarnation


def test_read_page_stale_incarnation_replays_first_new_record(tmp_path):
    path = tmp_path / "watch.jsonl"
    _write(path, {"n": "new-1"}, {"n": "new-2"})

    records, cursor, complete = read_page(
        tmp_path,
        since="watch:100@old-incarnation",
        sources={"watch"},
        limit=500,
    )

    assert complete
    assert [record.payload["n"] for record in records] == ["new-1", "new-2"]
    position = parse_cursor_positions(cursor)["watch"]
    assert position.seq == 2
    assert position.incarnation not in {None, "old-incarnation"}

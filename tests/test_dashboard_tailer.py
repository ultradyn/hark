"""B061: JSONL tailer hardening — partial lines, truncation, rotation, cursors."""

from __future__ import annotations

import json
from pathlib import Path

from hark.dashboard.tailer import MultiTailer, SourceTailer, parse_cursor, read_page
from hark.state_feed import format_cursor, parse_cursor_positions


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
    old_incarnation = t.cursor_position.incarnation
    f.write_text("")  # truncate in place
    _write(f, {"n": 3})
    recs = list(t.poll())
    assert [r.payload["n"] for r in recs] == [3]
    assert recs[0].seq == 1  # new incarnation
    assert recs[0].incarnation != old_incarnation


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


def test_multitailer_composite_cursor_and_delivery_split(tmp_path):
    _write(tmp_path / "events.jsonl", {"event_id": "e1", "session_id": "s", "pane_id": "p"})
    _write(tmp_path / "deliveries.jsonl", {"event_id": "e1", "status": "delivered", "ts": 1.0})
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
    _write(tmp_path / "watch.jsonl", *({"kind": "agent.blocked", "n": i} for i in range(5)))
    records, cursor, complete = read_page(tmp_path, since=None, limit=500)
    assert len(records) == 5 and complete

    same_file = SourceTailer(tmp_path / "watch.jsonl", source="watch")
    same_file.seek_to(3)
    same_file_cursor = format_cursor({"watch": same_file.cursor_position})
    same_file.close()
    records2, cursor2, _ = read_page(tmp_path, since=same_file_cursor, limit=500)
    assert [r.payload["n"] for r in records2] == [3, 4]
    assert parse_cursor(cursor2)["watch"] == 5

    # A line-only token cannot prove file identity, so duplicates beat loss.
    legacy, _, _ = read_page(tmp_path, since="watch:3", limit=500)
    assert [r.payload["n"] for r in legacy] == [0, 1, 2, 3, 4]

    records3, _, complete3 = read_page(tmp_path, since=None, limit=2)
    assert len(records3) == 2 and not complete3


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

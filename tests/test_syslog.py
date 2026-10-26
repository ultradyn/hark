"""Structured-log tail count contracts (B189)."""

import argparse
import json

import pytest

from hark import cli, syslog


@pytest.mark.parametrize("count", [0, -1, -100])
def test_nonpositive_tail_reads_no_records(tmp_path, count):
    path = tmp_path / "system.jsonl"
    path.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(5)))
    assert syslog.tail_lines(count, path) == []


def test_tail_preserves_positive_count(tmp_path):
    path = tmp_path / "system.jsonl"
    path.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(5)))
    assert syslog.tail_lines(2, path) == [{"i": 3}, {"i": 4}]


def test_logs_zero_emits_no_historical_records(tmp_path, monkeypatch, capsys):
    path = tmp_path / "system.jsonl"
    path.write_text('{"event":"never-print-this"}\n')
    monkeypatch.setattr(syslog, "system_log_path", lambda: path)
    args = argparse.Namespace(path=False, follow=False, lines=0, json=True)
    assert cli.cmd_logs(args) == 0
    assert capsys.readouterr().out == ""

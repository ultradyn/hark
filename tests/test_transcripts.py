"""B186 part 2: completed transcripts are recoverable from disk.

Even with ``--format text``, a caller that mangles its own stdout had no
fallback: ``usage.jsonl`` keeps only chars/words for STT and the listen state
dir keeps only ``*.cmd`` control files, so a lost answer meant asking the
operator to repeat themselves. Every turn that produced a transcript is now
appended to ``transcripts.jsonl``.
"""

from __future__ import annotations

import argparse
import builtins
import json
import stat

import pytest

from hark import cli, transcripts
from hark.config import HarkConfig
from hark.exitcodes import ABORT, OK
from hark.speech import ListenResult

ANSWER = "ship it on friday, not before the release"


def _ask_args(**over) -> argparse.Namespace:
    base = dict(
        text=["Deploy", "now?"],
        confirm=None,
        end_mode=None,
        provider=None,
        json=False,
        format="json",
        event_id=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _listen_args(**over) -> argparse.Namespace:
    base = dict(
        provider=None,
        end_mode=None,
        json=False,
        format="json",
        event_id=None,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _tts_args(**over) -> argparse.Namespace:
    base = dict(
        text=["Ready", "to", "ship?"],
        provider=None,
        voice=None,
        no_play=False,
        out=None,
        json=False,
        format="json",
        listen=True,
        end_mode=None,
        event_id=None,
        standalone=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def _records() -> list[dict]:
    path = transcripts.transcript_log_path()
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run_ask(monkeypatch, result: dict, **over) -> int:
    monkeypatch.setattr("hark.speech.run_ask", lambda *a, **k: dict(result))
    return cli.cmd_ask(_ask_args(**over), HarkConfig())


def _run_listen(monkeypatch, listened: ListenResult, **over) -> int:
    monkeypatch.setattr("hark.speech.run_listen", lambda *a, **k: listened)
    return cli.cmd_listen(_listen_args(**over), HarkConfig())


def _run_tts_listen(monkeypatch, listened: ListenResult, **over) -> int:
    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: ({"ok": True, "provider": "mock", "voice": "eve"}, listened),
    )
    return cli.cmd_tts(_tts_args(**over), HarkConfig())


def _ok_ask_result(**over) -> dict:
    base = {
        "ok": True,
        "text": ANSWER,
        "provider": "mock",
        "duration_ms": 900,
        "end_mode": "silence",
        "end_phrase": None,
        "risk": "R1",
        "tts": {"ok": True, "provider": "mock", "voice": "eve"},
        "exit": OK,
    }
    base.update(over)
    return base


# --- the recovery path -----------------------------------------------------


def test_ask_appends_the_full_transcript(monkeypatch):
    assert _run_ask(monkeypatch, _ok_ask_result(), event_id="e-1") == OK

    (rec,) = _records()
    assert rec["kind"] == "ask"
    assert rec["text"] == ANSWER
    assert rec["ok"] is True
    assert rec["provider"] == "mock"
    assert rec["for_event"] == "e-1"
    assert rec["prompt"] == "Deploy now?"
    assert rec["words"] == 8
    assert rec["chars"] == len(ANSWER)
    assert rec["iso"].endswith("Z")


def test_listen_appends_the_full_transcript(monkeypatch):
    code = _run_listen(
        monkeypatch,
        ListenResult(
            text=ANSWER,
            provider="mock",
            duration_ms=700,
            end_mode="silence",
            stream_id="s-1",
        ),
    )

    assert code == OK
    (rec,) = _records()
    assert rec["kind"] == "listen"
    assert rec["text"] == ANSWER
    assert rec["stream_id"] == "s-1"
    assert "prompt" not in rec


def test_tts_listen_records_fields_from_the_nested_listen_block(monkeypatch):
    code = _run_tts_listen(
        monkeypatch,
        ListenResult(
            text=ANSWER,
            provider="nested",
            duration_ms=800,
            end_mode="radio",
            end_phrase="over",
            stream_id="s-2",
        ),
    )

    assert code == OK
    (rec,) = _records()
    assert rec["kind"] == "tts_listen"
    assert rec["text"] == ANSWER
    assert rec["provider"] == "nested"
    assert rec["stream_id"] == "s-2"
    assert rec["end_mode"] == "radio"
    assert rec["end_phrase"] == "over"
    assert rec["prompt"] == "Ready to ship?"


@pytest.mark.parametrize("kind", ["ask", "listen", "tts_listen"])
def test_a_cancelled_turn_keeps_its_partial_transcript(monkeypatch, kind):
    """The partial answer is exactly what an operator would have to repeat."""
    partial = "well actually hold on"
    if kind == "ask":
        code = _run_ask(
            monkeypatch,
            {
                "ok": False,
                "cancelled": True,
                "text": partial,
                "end_phrase": "cancel",
                "exit": ABORT,
                "tts": None,
            },
        )
    else:
        listened = ListenResult(
            text=partial,
            provider="mock",
            duration_ms=300,
            end_mode="silence",
            end_phrase="cancel",
            cancelled=True,
        )
        code = (
            _run_listen(monkeypatch, listened)
            if kind == "listen"
            else _run_tts_listen(monkeypatch, listened)
        )

    assert code == ABORT
    (rec,) = _records()
    assert rec["text"] == partial
    assert rec["ok"] is False
    assert rec["cancelled"] is True
    assert rec["kind"] == kind


def test_a_turn_with_no_transcript_is_not_recorded(monkeypatch):
    """A provider failure before capture has nothing to recover."""
    assert _run_ask(
        monkeypatch,
        {"ok": False, "error": "provider failed", "text": "", "exit": 4},
    ) == 4

    assert _records() == []


def test_the_record_survives_a_broken_stdout(monkeypatch):
    """Persist before printing: EPIPE on stdout is the failure being fixed."""
    real_print = builtins.print

    def exploding_print(*a, **k):
        if a and not str(a[0]).startswith("hark"):
            raise BrokenPipeError("closed")
        return real_print(*a, **k)

    monkeypatch.setattr(builtins, "print", exploding_print)
    monkeypatch.setattr("hark.speech.run_ask", lambda *a, **k: _ok_ask_result())

    with pytest.raises(BrokenPipeError):
        cli.cmd_ask(_ask_args(), HarkConfig())

    assert _records()[0]["text"] == ANSWER


# --- store behaviour -------------------------------------------------------


def test_path_follows_xdg_state_and_the_env_override(monkeypatch, tmp_path):
    assert transcripts.transcript_log_path().name == "transcripts.jsonl"
    override = tmp_path / "elsewhere.jsonl"
    monkeypatch.setenv("HARK_TRANSCRIPT_LOG", str(override))
    assert transcripts.transcript_log_path() == override


def test_opt_out_env_writes_nothing(monkeypatch):
    monkeypatch.setenv("HARK_NO_TRANSCRIPT_LOG", "1")

    assert _run_ask(monkeypatch, _ok_ask_result()) == OK

    assert not transcripts.transcript_log_path().exists()


def test_the_log_is_not_group_or_world_readable(monkeypatch):
    """It holds operator speech, so it must not inherit a 0644 umask."""
    _run_ask(monkeypatch, _ok_ask_result())

    mode = transcripts.transcript_log_path().stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_rotation_bounds_the_live_file_and_keeps_one_generation(monkeypatch):
    """Unbounded growth is not acceptable; losing the newest turn is worse."""
    monkeypatch.setenv("HARK_TRANSCRIPT_MAX_BYTES", "400")
    path = transcripts.transcript_log_path()

    for i in range(12):
        transcripts.record_turn({"ok": True, "text": f"turn {i}"}, kind="listen")

    assert path.stat().st_size <= 400
    assert "turn 11" in path.read_text()
    previous = path.with_name(path.name + ".1")
    assert previous.is_file()
    assert previous.stat().st_size <= 400
    assert transcripts.tail_records(1)[0]["text"] == "turn 11"


def test_rotation_can_be_disabled(monkeypatch):
    monkeypatch.setenv("HARK_TRANSCRIPT_MAX_BYTES", "0")
    path = transcripts.transcript_log_path()

    for i in range(20):
        transcripts.record_turn({"ok": True, "text": f"turn {i}"}, kind="listen")

    assert len(_records()) == 20
    assert not path.with_name(path.name + ".1").exists()


def test_record_turn_never_raises_on_an_unusable_path(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.setenv("HARK_TRANSCRIPT_LOG", str(blocked))

    assert transcripts.record_turn({"ok": True, "text": "hi"}, kind="listen") is False


def test_a_pathological_transcript_is_capped_with_a_marker(monkeypatch):
    huge = "word " * 40_000

    assert transcripts.record_turn({"ok": True, "text": huge}, kind="listen") is True

    (rec,) = _records()
    assert rec["text_truncated"] is True
    assert len(rec["text"]) == transcripts.TEXT_MAX_CHARS
    assert rec["chars"] == len(huge)


def test_the_prompt_is_stored_as_a_preview(monkeypatch):
    long_prompt = "why " * 200

    transcripts.record_turn(
        {"ok": True, "text": "because"}, kind="ask", prompt=long_prompt
    )

    (rec,) = _records()
    assert len(rec["prompt"]) <= transcripts.PROMPT_PREVIEW_CHARS + 1
    assert rec["prompt"].endswith("…")


def test_tail_records_rejects_a_non_positive_count(monkeypatch):
    """`-n 0` must mean none, not "the whole file"."""
    transcripts.record_turn({"ok": True, "text": "only turn"}, kind="listen")

    assert transcripts.tail_records(0) == []
    assert transcripts.tail_records(-3) == []


def test_tail_records_returns_the_last_n_oldest_first(monkeypatch):
    for i in range(6):
        transcripts.record_turn({"ok": True, "text": f"turn {i}"}, kind="listen")

    rows = transcripts.tail_records(3)

    assert [r["text"] for r in rows] == ["turn 3", "turn 4", "turn 5"]


# --- hark transcripts ------------------------------------------------------


def _transcripts_args(**over) -> argparse.Namespace:
    base = dict(lines=5, json=False, path=False, last=False)
    base.update(over)
    return argparse.Namespace(**base)


def test_transcripts_last_prints_only_the_text(monkeypatch, capsys):
    _run_ask(monkeypatch, _ok_ask_result())
    _run_listen(
        monkeypatch,
        ListenResult(
            text="second turn", provider="mock", duration_ms=1, end_mode="silence"
        ),
    )
    capsys.readouterr()

    assert cli.cmd_transcripts(_transcripts_args(last=True)) == OK
    assert capsys.readouterr().out == "second turn\n"


def test_transcripts_path_prints_the_path(capsys):
    assert cli.cmd_transcripts(_transcripts_args(path=True)) == OK
    assert capsys.readouterr().out.strip() == str(transcripts.transcript_log_path())


def test_transcripts_json_is_one_record_per_line(monkeypatch, capsys):
    _run_ask(monkeypatch, _ok_ask_result())
    capsys.readouterr()

    assert cli.cmd_transcripts(_transcripts_args(json=True)) == OK
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == ANSWER


def test_transcripts_human_view_shows_the_whole_transcript(monkeypatch, capsys):
    _run_ask(monkeypatch, _ok_ask_result())
    capsys.readouterr()

    assert cli.cmd_transcripts(_transcripts_args()) == OK
    out = capsys.readouterr().out
    assert ANSWER in out
    assert "ask" in out


def test_transcripts_with_no_log_is_not_an_error(capsys):
    assert cli.cmd_transcripts(_transcripts_args()) == OK
    assert "transcripts.jsonl" in capsys.readouterr().err


def test_transcripts_is_wired_into_the_cli(monkeypatch, capsys):
    assert cli.main(["transcripts", "--path"]) == OK
    assert capsys.readouterr().out.strip().endswith("transcripts.jsonl")


@pytest.mark.parametrize("ok", [True, False])
def test_ask_logs_confirmation_fallback_separately_from_question(monkeypatch, capsys, ok):
    result = _ok_ask_result(
        ok=ok, exit=OK if ok else ABORT,
        confirm_tts={
            "ok": True, "provider": "xai",
            "fallback": {"requested": "google", "spoke": "xai", "steps": []},
        },
    )
    assert _run_ask(monkeypatch, result) == result["exit"]
    payload = json.loads(capsys.readouterr().out)
    assert payload["confirm_tts"] == result["confirm_tts"]
    (record,) = _records()
    assert record["tts_provider"] == "mock"
    assert record["confirm_tts_provider"] == "xai"
    assert record["confirm_tts_fallback_from"] == "google"


def test_transcript_without_confirmation_has_no_confirmation_provenance():
    record = transcripts.build_record(_ok_ask_result(), kind="ask")
    assert "confirm_tts_provider" not in record
    assert "confirm_tts_fallback_from" not in record


@pytest.mark.parametrize("text", [None, "", "   "])
def test_failed_nudge_is_durable_even_without_transcript(monkeypatch, text):
    nudges = [{"reason": "no_open", "profile": "bound_answer", "ok": False,
               "error": "recovery prompt failed", "exit": 4}]
    result = {"ok": False, "text": text, "exit": 6, "error": "no speech",
              "tts_nudges": nudges}
    assert _run_ask(monkeypatch, result) == 6
    (record,) = _records()
    assert record["tts_nudges"] == nudges
    assert record["diagnostic_only"] is True
    assert record["error"] == "no speech"
    assert not record["text"].strip()


def test_transcript_keeps_question_and_confirmation_nudges():
    answer = [{"reason": "no_open", "profile": "bound_answer", "ok": True}]
    confirm = [{"reason": "empty_stt", "profile": "confirm", "ok": False}]
    record = transcripts.build_record(
        _ok_ask_result(tts_nudges=answer, confirm_tts_nudges=confirm), kind="ask",
    )
    assert record["tts_nudges"] == answer
    assert record["confirm_tts_nudges"] == confirm
    assert "diagnostic_only" not in record


def test_nested_listen_nudge_outcomes_reach_transcript():
    nudges = [{"reason": "empty_stt", "ok": False, "error": "provider down"}]
    record = transcripts.build_record(
        {"ok": False, "text": "", "listen": {"tts_nudges": nudges}},
        kind="tts_listen",
    )
    assert record["tts_nudges"] == nudges
    assert record["diagnostic_only"] is True

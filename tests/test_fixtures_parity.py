"""Parity tests driven by fixtures/ — keep in sync with a future Rust client.

These load golden JSONL under fixtures/ and assert Python behavior. When porting
to Rust, re-run the same case files with identical expect_* rules.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hark.fingerprint import question_fingerprint
from hark.listen_end import evaluate_radio_transcript
from hark.partial import HOLD_INSTRUCTIONS, HOLD_WARNING
from hark.risk import classify_question
from hark.wake import match_activation

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _repo_rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# ---------------------------------------------------------------------------
# text goldens
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    _load_jsonl(FIX / "text" / "wake_match.jsonl"),
    ids=lambda c: c["id"],
)
def test_fixture_wake_match(case: dict) -> None:
    hit = match_activation(case["input"], anywhere=bool(case.get("anywhere")))
    if not case["expect_match"]:
        assert hit is None, f"{case['id']}: unexpected hit {hit}"
        return
    assert hit is not None, f"{case['id']}: expected match"
    if "expect_phrase" in case:
        assert hit.phrase == case["expect_phrase"]
    if "expect_phrase_contains" in case:
        assert case["expect_phrase_contains"] in hit.phrase
    if "expect_remainder" in case:
        assert hit.remainder == case["expect_remainder"]
    if "expect_remainder_contains" in case:
        assert case["expect_remainder_contains"] in hit.remainder


@pytest.mark.parametrize(
    "case",
    _load_jsonl(FIX / "text" / "radio_end.jsonl"),
    ids=lambda c: c["id"],
)
def test_fixture_radio_end(case: dict) -> None:
    hit = evaluate_radio_transcript(
        case["input"],
        soft_end_phrases_enabled=bool(case.get("soft_end_phrases_enabled", False)),
    )
    expect_kind = case.get("expect_kind")
    if expect_kind is None:
        assert hit is None, f"{case['id']}: unexpected {hit}"
        return
    assert hit is not None, f"{case['id']}: expected {expect_kind}"
    assert hit.kind == expect_kind
    if "expect_phrase" in case:
        assert hit.phrase == case["expect_phrase"]
    if "expect_body" in case:
        assert hit.body == case["expect_body"]
    if "expect_body_contains" in case:
        assert case["expect_body_contains"] in hit.body


@pytest.mark.parametrize(
    "case",
    _load_jsonl(FIX / "text" / "fingerprint.jsonl"),
    ids=lambda c: c["id"],
)
def test_fixture_fingerprint(case: dict) -> None:
    a = question_fingerprint(case["text_a"], case.get("choices_a"))
    b = question_fingerprint(case["text_b"], case.get("choices_b"))
    if case.get("expect_equal"):
        assert a == b, f"{case['id']}: {a} != {b}"
    else:
        assert a != b, f"{case['id']}: fingerprints collided"
    if "expect_prefix" in case:
        assert a.startswith(case["expect_prefix"])
        assert b.startswith(case["expect_prefix"])


@pytest.mark.parametrize(
    "case",
    _load_jsonl(FIX / "text" / "risk.jsonl"),
    ids=lambda c: c["id"],
)
def test_fixture_risk(case: dict) -> None:
    result = classify_question(case["text"])
    assert result.risk == case["expect_risk"], f"{case['id']}: got {result.risk}"


# ---------------------------------------------------------------------------
# live wake snips (text path — same as production after vosk)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    _load_jsonl(FIX / "voice" / "wake" / "cases.jsonl"),
    ids=lambda c: c["id"],
)
def test_fixture_voice_wake_cases(case: dict) -> None:
    # B071: cases may be audio (live/derived), text-only (no wav), or derived
    # without a sidecar meta. Text-path parity uses vosk_text / meta.text.
    tags = set(case.get("tags") or [])
    wav_rel = case.get("wav")
    meta_rel = case.get("meta")
    meta: dict = {}
    if meta_rel:
        meta_path = FIX / "voice" / "wake" / meta_rel
        assert meta_path.is_file(), f"missing meta {_repo_rel(meta_path)}"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if wav_rel:
        wav = FIX / "voice" / "wake" / wav_rel
        assert wav.is_file(), f"missing wav {_repo_rel(wav)}"
    elif "text-only" not in tags:
        # Non-text cases must point at audio.
        raise AssertionError(f"{case['id']}: missing wav (not tagged text-only)")
    text = case.get("vosk_text")
    if text is None:
        text = meta.get("text") or case.get("text") or ""
    hit = match_activation(text, anywhere=True)
    if not case["expect_match"]:
        assert hit is None, f"{case['id']}: unexpected hit for {text!r}"
        return
    assert hit is not None, f"{case['id']}: expected match for {text!r}"
    if "expect_phrase_contains" in case:
        assert case["expect_phrase_contains"] in hit.phrase


# ---------------------------------------------------------------------------
# HEP event shape / partial stream
# ---------------------------------------------------------------------------


def test_fixture_hep_watch_armed_shape() -> None:
    path = FIX / "events" / "hep" / "watch_armed.json"
    ev = json.loads(path.read_text(encoding="utf-8"))
    assert ev["schema"] == "hark.event.v1"
    assert ev["kind"] == "watch.armed"
    assert "event_id" in ev
    assert "observed_at" in ev


def test_fixture_hep_ambient_prompts_parse() -> None:
    path = FIX / "events" / "hep" / "ambient_prompt_samples.jsonl"
    rows = _load_jsonl(path)
    assert rows, "need at least one live ambient.prompt"
    for ev in rows:
        assert ev["schema"] == "hark.event.v1"
        assert ev["kind"] == "ambient.prompt"
        assert isinstance(ev.get("text"), str) and ev["text"]
        assert "event_id" in ev


def test_fixture_radio_partial_stream_hold_and_supersede() -> None:
    path = FIX / "events" / "hep" / "radio_partial_then_final.jsonl"
    rows = _load_jsonl(path)
    assert len(rows) >= 3
    stream_ids = {r["stream_id"] for r in rows}
    assert len(stream_ids) == 1
    partials = [r for r in rows if r.get("partial") is True]
    finals = [r for r in rows if r.get("final") is True]
    assert partials
    assert finals
    for p in partials:
        assert p["final"] is False
        assert "HOLD" in (p.get("warning") or "") or "PARTIAL" in (p.get("warning") or "")
        assert "HOLD" in (p.get("instructions") or "")
        # library strings stay aligned with fixtures
        assert HOLD_WARNING[:20] in (p.get("warning") or "")
        assert HOLD_INSTRUCTIONS[:10] in (p.get("instructions") or "")
    fin = finals[-1]
    assert fin["partial"] is False
    assert "FINAL" in (fin.get("instructions") or "")
    assert fin.get("seq") is None or fin.get("partials_emitted", 0) >= len(partials)


def test_fixture_syslog_wake_to_prompt_sequence() -> None:
    path = FIX / "events" / "syslog" / "wake_to_prompt_sequence.jsonl"
    rows = _load_jsonl(path)
    events = [r.get("event") for r in rows]
    assert "ambient.wake" in events
    assert "ambient.prompt" in events
    assert events.index("ambient.wake") < events.index("ambient.prompt")


def test_fixture_usage_sample() -> None:
    rows = _load_jsonl(FIX / "usage" / "sample.jsonl")
    assert rows
    kinds = {r.get("kind") for r in rows}
    assert kinds & {"tts", "stt"}


def test_fixture_manifest_lists_files() -> None:
    man_path = FIX / "MANIFEST.json"
    assert man_path.is_file(), "run scripts/export-fixtures.sh to generate MANIFEST"
    man = json.loads(man_path.read_text(encoding="utf-8"))
    assert man["schema"] == "hark.fixtures.manifest.v1"
    assert man["count"] >= 10
    paths = {f["path"] for f in man["files"]}
    assert "text/wake_match.jsonl" in paths
    assert "voice/wake/cases.jsonl" in paths

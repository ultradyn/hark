"""Wake eval harness (B071): hit/miss/FA summary for Vosk vs Sherpa KWS.

Default CI:
  - pure unit tests for outcome taxonomy + text_path scoring
  - summary table over cases.jsonl via text_path (no models)

Optional:
  - @pytest.mark.vosk — offline re-decode when model + package present
  - @pytest.mark.sherpa_kws — when B070 backend + model present (else skip)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hark.wake_eval import (
    OUTCOME_FA,
    OUTCOME_HIT,
    OUTCOME_MISS,
    OUTCOME_REJECT,
    classify_outcome,
    default_wake_fixtures_root,
    discover_backends,
    evaluate_cases,
    filter_cases,
    format_summary_table,
    load_cases,
    score_backend_case,
    score_text_path,
    sherpa_available,
    try_build_sherpa_backend,
    try_build_vosk_backend,
    vosk_available,
)

ROOT = Path(__file__).resolve().parents[1]
FIX_ROOT = default_wake_fixtures_root(ROOT)
CASES_PATH = FIX_ROOT / "cases.jsonl"


def _cases() -> list[dict]:
    rows = load_cases(CASES_PATH)
    assert rows, f"missing or empty {CASES_PATH}"
    return rows


# ---------------------------------------------------------------------------
# Pure unit tests (always on in CI)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expect,got,outcome",
    [
        (True, True, OUTCOME_HIT),
        (True, False, OUTCOME_MISS),
        (False, True, OUTCOME_FA),
        (False, False, OUTCOME_REJECT),
    ],
)
def test_classify_outcome(expect: bool, got: bool, outcome: str) -> None:
    assert classify_outcome(expect_match=expect, got_match=got) == outcome


def test_text_path_scores_live_and_synthetic_rows() -> None:
    cases = [c for c in _cases() if c.get("vosk_text") is not None or c.get("text") is not None]
    assert len(cases) >= 7
    summaries = evaluate_cases(
        cases, fixtures_root=FIX_ROOT, backends=[], include_text_path=True
    )
    assert len(summaries) == 1
    s = summaries[0]
    assert s.engine == "text_path"
    assert s.scored == len(cases)
    assert s.error == 0
    # At least the original 7 live ids should be perfect on text_path.
    live_ids = {
        "hey-harold-as-herald-hit",
        "hey-hook-as-hark-hit",
        "hey-hawk-as-hark-hit",
        "a-hawk-miss",
        "hey-hoc-miss",
        "hello-alone-miss",
        "hey-ho-miss",
    }
    by_id = {r.case_id: r for r in s.results}
    for lid in live_ids:
        assert lid in by_id, lid
        assert by_id[lid].outcome in (OUTCOME_HIT, OUTCOME_REJECT), (
            f"{lid}: {by_id[lid].outcome} text={by_id[lid].decoded_text!r}"
        )


def test_summary_table_format_includes_rates() -> None:
    cases = filter_cases(_cases(), tags_any=["live", "text-only"])
    summaries = evaluate_cases(
        cases, fixtures_root=FIX_ROOT, backends=[], include_text_path=True
    )
    table = format_summary_table(summaries)
    assert "hit_rate" in table
    assert "fa_rate" in table
    assert "text_path" in table
    # Print for pytest -s / human CI logs (B071 deliverable: summary table).
    print("\n" + table)


def test_cases_have_expected_schema_and_dimensions() -> None:
    cases = _cases()
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    # Expanded beyond original N=7.
    assert len(cases) >= 20, f"expected expanded eval set, got {len(cases)}"
    tags_seen: set[str] = set()
    n_audio = 0
    n_text = 0
    for c in cases:
        assert "expect_match" in c
        assert "id" in c
        tags_seen.update(c.get("tags") or [])
        if c.get("wav"):
            n_audio += 1
            wav = FIX_ROOT / c["wav"]
            assert wav.is_file(), f"missing {wav}"
        else:
            n_text += 1
            assert c.get("vosk_text") is not None or c.get("text") is not None
    assert "live" in tags_seen
    assert "derived" in tags_seen
    assert "text-only" in tags_seen
    assert n_audio >= 15  # 7 live + derived + silence/noise
    assert n_text >= 5
    # Dimension coverage for B071 goals.
    for needed in ("positive", "negative", "greeting", "bare", "noise-light", "custom-name"):
        assert needed in tags_seen, f"missing tag dimension {needed}"


def test_score_text_path_false_accept_and_hit() -> None:
    hit = score_text_path(
        {"id": "t-hit", "vosk_text": "hey hook", "expect_match": True, "expect_phrase_contains": "hark"}
    )
    assert hit.outcome == OUTCOME_HIT
    miss = score_text_path(
        {"id": "t-miss", "vosk_text": "hello", "expect_match": True}
    )
    assert miss.outcome == OUTCOME_MISS
    fa = score_text_path(
        {"id": "t-fa", "vosk_text": "hey hawk", "expect_match": False}
    )
    assert fa.outcome == OUTCOME_FA
    rej = score_text_path(
        {"id": "t-rej", "vosk_text": "a hawk", "expect_match": False}
    )
    assert rej.outcome == OUTCOME_REJECT


# ---------------------------------------------------------------------------
# Optional engine marks (skip when models / packages missing)
# ---------------------------------------------------------------------------

requires_vosk = pytest.mark.skipif(
    not vosk_available(),
    reason="vosk model/package missing (uv sync --extra wake + download model)",
)

requires_sherpa = pytest.mark.skipif(
    not sherpa_available(),
    reason=(
        "Sherpa KWS unavailable — needs B070 SherpaKwsWakeBackend, "
        "sherpa-onnx package, and GigaSpeech KWS model "
        "(optional; default CI must not require it)"
    ),
)


@pytest.mark.vosk
@requires_vosk
def test_vosk_eval_summary_on_audio_cases() -> None:
    backend = try_build_vosk_backend()
    assert backend is not None
    cases = filter_cases(_cases(), audio_only=True)
    summaries = evaluate_cases(
        cases,
        fixtures_root=FIX_ROOT,
        backends=[("vosk", backend)],
        include_text_path=False,
    )
    s = summaries[0]
    assert s.engine == "vosk"
    assert s.scored + s.skip + s.error == len(cases)
    table = format_summary_table(summaries)
    print("\nVosk wake eval\n" + table)
    # Live clean set should remain strong (allow some derived noise misses).
    live = [r for r in s.results if r.case_id in {
        "hey-harold-as-herald-hit",
        "hey-hook-as-hark-hit",
        "hey-hawk-as-hark-hit",
        "a-hawk-miss",
        "hey-hoc-miss",
        "hello-alone-miss",
        "hey-ho-miss",
    }]
    assert live, "expected live cases in audio set"
    live_fa = [r for r in live if r.outcome == OUTCOME_FA]
    assert not live_fa, f"unexpected FA on live set: {live_fa}"


@pytest.mark.sherpa_kws
@requires_sherpa
def test_sherpa_kws_eval_summary_on_audio_cases() -> None:
    backend = try_build_sherpa_backend()
    assert backend is not None
    cases = filter_cases(_cases(), audio_only=True)
    summaries = evaluate_cases(
        cases,
        fixtures_root=FIX_ROOT,
        backends=[("sherpa_kws", backend)],
        include_text_path=False,
    )
    s = summaries[0]
    assert s.scored + s.skip + s.error == len(cases)
    print("\nSherpa KWS wake eval\n" + format_summary_table(summaries))


@pytest.mark.vosk
@pytest.mark.sherpa_kws
def test_discover_backends_optional_skip_contract() -> None:
    """When engines missing, discover returns None slots — runner must not crash."""
    pairs = discover_backends(want_vosk=True, want_sherpa=True)
    names = [n for n, _ in pairs]
    assert "vosk" in names
    assert "sherpa_kws" in names
    # evaluate with whatever is present (None → all skip)
    cases = filter_cases(_cases(), audio_only=True)[:3]
    summaries = evaluate_cases(
        cases, fixtures_root=FIX_ROOT, backends=pairs, include_text_path=True
    )
    assert any(s.engine == "text_path" for s in summaries)
    table = format_summary_table(summaries)
    assert "vosk" in table and "sherpa_kws" in table
    print("\n" + table)


def test_backend_score_skips_text_only_rows() -> None:
    from hark.wake import TextProbeBackend

    backend = TextProbeBackend()
    case = {
        "id": "text-only-row",
        "vosk_text": "hey hark",
        "expect_match": True,
        "tags": ["text-only"],
    }
    r = score_backend_case(case, backend, fixtures_root=FIX_ROOT, engine="probe")
    assert r.outcome == "skip"

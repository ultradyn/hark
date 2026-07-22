#!/usr/bin/env python3
"""Offline wake fixture eval: Vosk vs Sherpa KWS hit/miss/FA table (B071).

Scores fixtures/voice/wake/cases.jsonl with:

  - text_path  — match_activation on vosk_text (always; no models)
  - vosk       — optional (vosk package + small en-us model)
  - sherpa_kws — optional (B070 backend + KWS model); skipped when absent

Vosk/Sherpa are skipped by default when unavailable; pass --require-vosk /
--require-sherpa to fail instead (exit 2).

Usage:
  uv run python scripts/eval-wake-fixtures.py
  uv run python scripts/eval-wake-fixtures.py --audio-only
  uv run python scripts/eval-wake-fixtures.py --json
  uv run python scripts/eval-wake-fixtures.py --no-sherpa --no-vosk
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hark.wake_eval import (  # noqa: E402
    default_wake_fixtures_root,
    discover_backends,
    evaluate_cases,
    filter_cases,
    format_summary_table,
    load_cases,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--fixtures",
        type=Path,
        default=None,
        help="wake fixtures root (default: fixtures/voice/wake)",
    )
    ap.add_argument(
        "--cases",
        type=Path,
        default=None,
        help="cases.jsonl path (default: <fixtures>/cases.jsonl)",
    )
    ap.add_argument("--audio-only", action="store_true", help="skip text-only rows")
    ap.add_argument("--tag", action="append", dest="tags", default=[], help="require tag (repeatable AND)")
    ap.add_argument("--tag-any", action="append", dest="tags_any", default=[], help="any of these tags")
    ap.add_argument("--no-text", action="store_true", help="skip text_path engine")
    ap.add_argument("--no-vosk", action="store_true")
    ap.add_argument("--no-sherpa", action="store_true")
    ap.add_argument(
        "--require-vosk",
        action="store_true",
        help="exit 2 if Vosk backend unavailable",
    )
    ap.add_argument(
        "--require-sherpa",
        action="store_true",
        help="exit 2 if Sherpa KWS unavailable (not for default CI)",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable summary")
    ap.add_argument("--verbose", "-v", action="store_true", help="per-case outcomes")
    args = ap.parse_args()

    fix_root = args.fixtures or default_wake_fixtures_root(ROOT)
    cases_path = args.cases or (fix_root / "cases.jsonl")
    cases = load_cases(cases_path)
    if not cases:
        print(f"no cases in {cases_path}", file=sys.stderr)
        return 1

    cases = filter_cases(
        cases,
        tags_all=args.tags or None,
        tags_any=args.tags_any or None,
        audio_only=args.audio_only,
    )
    if not cases:
        print("no cases after filters", file=sys.stderr)
        return 1

    backends = discover_backends(
        want_vosk=not args.no_vosk,
        want_sherpa=not args.no_sherpa,
    )
    if args.require_vosk and not any(b is not None for n, b in backends if n == "vosk"):
        print("Vosk required but unavailable", file=sys.stderr)
        return 2
    if args.require_sherpa and not any(
        b is not None for n, b in backends if n == "sherpa_kws"
    ):
        print(
            "Sherpa KWS required but unavailable "
            "(install sherpa-onnx + B070 backend + model; or omit --require-sherpa)",
            file=sys.stderr,
        )
        return 2

    summaries = evaluate_cases(
        cases,
        fixtures_root=fix_root,
        backends=backends,
        include_text_path=not args.no_text,
    )

    if args.json:
        if args.verbose:
            results = [r.to_dict() for s in summaries for r in s.results]
        else:
            results = [
                r.to_dict()
                for s in summaries
                for r in s.results
                if r.outcome in ("miss", "fa", "error")
            ]
        payload = {
            "cases": len(cases),
            "fixtures": str(fix_root),
            "engines": [s.row_dict() for s in summaries],
            "results": results,
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"Wake eval (B071) — {len(cases)} cases from {cases_path.relative_to(ROOT)}")
        for label, backend in backends:
            status = "ready" if backend is not None else "SKIP (not installed)"
            print(f"  backend {label}: {status}")
        print()
        print(format_summary_table(summaries))
        print()
        print("Legend: hit=TP  miss=FN  fa=false accept (FP)  reject=TN")
        if args.verbose:
            print()
            for s in summaries:
                print(f"## {s.engine}")
                for r in s.results:
                    extra = f" text={r.decoded_text!r}" if r.decoded_text else ""
                    if r.phrase:
                        extra += f" phrase={r.phrase!r}"
                    if r.error:
                        extra += f" err={r.error}"
                    print(f"  {r.outcome:7} {r.case_id}{extra}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

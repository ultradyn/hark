"""Offline wake fixture evaluation (B071).

Scores the same wake cases with available backends (Vosk, Sherpa KWS when
present) and aggregates hit / miss / false-accept (FA) counts.

Used by ``scripts/eval-wake-fixtures.py`` and ``tests/test_wake_eval_harness.py``.
Does not require network. Sherpa is optional — never a hard default-CI dep.
"""

from __future__ import annotations

import json
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from hark.wake import WakeBackend, match_activation

# Outcome labels for a single case × backend.
OUTCOME_HIT = "hit"  # true positive
OUTCOME_MISS = "miss"  # false negative
OUTCOME_FA = "fa"  # false accept (false positive)
OUTCOME_REJECT = "reject"  # true negative (correct reject)
OUTCOME_SKIP = "skip"  # no audio / backend unavailable / error
OUTCOME_ERROR = "error"

ALL_OUTCOMES = (
    OUTCOME_HIT,
    OUTCOME_MISS,
    OUTCOME_FA,
    OUTCOME_REJECT,
    OUTCOME_SKIP,
    OUTCOME_ERROR,
)


def default_wake_fixtures_root(repo_root: Path | None = None) -> Path:
    if repo_root is None:
        # src/hark/wake_eval.py → repo root
        repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "fixtures" / "voice" / "wake"


def load_cases(path: Path) -> list[dict[str, Any]]:
    """Load JSONL wake cases (skip blanks and # comments)."""
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def write_cases(path: Path, cases: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(c, ensure_ascii=False, separators=(",", ":")) for c in cases]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def read_pcm16_wav(path: Path) -> tuple[bytes, int]:
    """Read mono 16-bit PCM WAV → (pcm_bytes, sample_rate)."""
    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError(f"{path}: expected 16-bit PCM, got sampwidth={wf.getsampwidth()}")
        if wf.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono, got {wf.getnchannels()} ch")
        sample_rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    return pcm, sample_rate


def write_pcm16_wav(path: Path, pcm: bytes, sample_rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)


def classify_outcome(*, expect_match: bool, got_match: bool) -> str:
    """Map expect vs observed wake into hit/miss/fa/reject."""
    if expect_match and got_match:
        return OUTCOME_HIT
    if expect_match and not got_match:
        return OUTCOME_MISS
    if not expect_match and got_match:
        return OUTCOME_FA
    return OUTCOME_REJECT


@dataclass
class CaseResult:
    case_id: str
    engine: str
    outcome: str
    expect_match: bool
    got_match: bool
    decoded_text: str = ""
    phrase: str | None = None
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case_id,
            "engine": self.engine,
            "outcome": self.outcome,
            "expect_match": self.expect_match,
            "got_match": self.got_match,
            "decoded_text": self.decoded_text,
            "phrase": self.phrase,
            "notes": self.notes,
            "tags": list(self.tags),
            "error": self.error,
        }


@dataclass
class EngineSummary:
    engine: str
    hit: int = 0
    miss: int = 0
    fa: int = 0
    reject: int = 0
    skip: int = 0
    error: int = 0
    results: list[CaseResult] = field(default_factory=list)

    @property
    def scored(self) -> int:
        return self.hit + self.miss + self.fa + self.reject

    @property
    def positives(self) -> int:
        return self.hit + self.miss

    @property
    def negatives(self) -> int:
        return self.fa + self.reject

    @property
    def hit_rate(self) -> float | None:
        if self.positives == 0:
            return None
        return self.hit / self.positives

    @property
    def miss_rate(self) -> float | None:
        if self.positives == 0:
            return None
        return self.miss / self.positives

    @property
    def fa_rate(self) -> float | None:
        if self.negatives == 0:
            return None
        return self.fa / self.negatives

    @property
    def accuracy(self) -> float | None:
        if self.scored == 0:
            return None
        return (self.hit + self.reject) / self.scored

    def add(self, result: CaseResult) -> None:
        self.results.append(result)
        key = result.outcome
        if key == OUTCOME_HIT:
            self.hit += 1
        elif key == OUTCOME_MISS:
            self.miss += 1
        elif key == OUTCOME_FA:
            self.fa += 1
        elif key == OUTCOME_REJECT:
            self.reject += 1
        elif key == OUTCOME_SKIP:
            self.skip += 1
        elif key == OUTCOME_ERROR:
            self.error += 1

    def row_dict(self) -> dict[str, Any]:
        def pct(v: float | None) -> str:
            if v is None:
                return "—"
            return f"{100.0 * v:.1f}%"

        return {
            "engine": self.engine,
            "scored": self.scored,
            "hit": self.hit,
            "miss": self.miss,
            "fa": self.fa,
            "reject": self.reject,
            "skip": self.skip,
            "error": self.error,
            "hit_rate": pct(self.hit_rate),
            "miss_rate": pct(self.miss_rate),
            "fa_rate": pct(self.fa_rate),
            "accuracy": pct(self.accuracy),
        }


def format_summary_table(summaries: Sequence[EngineSummary]) -> str:
    """ASCII table: engine × hit/miss/FA/reject + rates."""
    headers = (
        "engine",
        "scored",
        "hit",
        "miss",
        "fa",
        "reject",
        "skip",
        "hit_rate",
        "miss_rate",
        "fa_rate",
        "accuracy",
    )
    rows: list[list[str]] = []
    for s in summaries:
        d = s.row_dict()
        rows.append([str(d[h]) for h in headers])
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: Sequence[str]) -> str:
        return " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells))

    sep = "-+-".join("-" * w for w in widths)
    lines = [fmt_row(headers), sep]
    for row in rows:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def score_text_path(
    case: dict[str, Any],
    *,
    engine: str = "text_path",
) -> CaseResult:
    """Score match_activation on case vosk_text / text (no audio)."""
    case_id = str(case.get("id") or "?")
    expect = bool(case.get("expect_match"))
    tags = list(case.get("tags") or [])
    text = str(case.get("vosk_text") if case.get("vosk_text") is not None else case.get("text") or "")
    try:
        hit = match_activation(text, anywhere=True)
        got = hit is not None
        if got and "expect_phrase_contains" in case:
            needle = str(case["expect_phrase_contains"])
            if needle not in (hit.phrase or ""):
                # Count as miss of the *intended* phrase class.
                return CaseResult(
                    case_id=case_id,
                    engine=engine,
                    outcome=OUTCOME_MISS if expect else OUTCOME_FA,
                    expect_match=expect,
                    got_match=True,
                    decoded_text=text,
                    phrase=hit.phrase,
                    notes=f"phrase {hit.phrase!r} missing {needle!r}",
                    tags=tags,
                )
        outcome = classify_outcome(expect_match=expect, got_match=got)
        return CaseResult(
            case_id=case_id,
            engine=engine,
            outcome=outcome,
            expect_match=expect,
            got_match=got,
            decoded_text=text,
            phrase=hit.phrase if hit else None,
            tags=tags,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return CaseResult(
            case_id=case_id,
            engine=engine,
            outcome=OUTCOME_ERROR,
            expect_match=expect,
            got_match=False,
            decoded_text=text,
            tags=tags,
            error=str(exc),
        )


def score_backend_case(
    case: dict[str, Any],
    backend: WakeBackend,
    *,
    fixtures_root: Path,
    engine: str | None = None,
) -> CaseResult:
    """Run one audio case through a WakeBackend.score_snippet + match path."""
    case_id = str(case.get("id") or "?")
    expect = bool(case.get("expect_match"))
    tags = list(case.get("tags") or [])
    eng = engine or getattr(backend, "name", "backend")
    rel = case.get("wav")
    if not rel:
        return CaseResult(
            case_id=case_id,
            engine=eng,
            outcome=OUTCOME_SKIP,
            expect_match=expect,
            got_match=False,
            notes="no wav field (text-only case)",
            tags=tags,
        )
    wav_path = fixtures_root / str(rel)
    if not wav_path.is_file():
        return CaseResult(
            case_id=case_id,
            engine=eng,
            outcome=OUTCOME_ERROR,
            expect_match=expect,
            got_match=False,
            notes=f"missing wav {wav_path}",
            tags=tags,
            error="missing_wav",
        )
    try:
        pcm, sample_rate = read_pcm16_wav(wav_path)
        hit = backend.score_snippet(pcm, sample_rate)
        # Prefer backend-reported text when present (Vosk / probes).
        text = str(getattr(backend, "last_text", "") or (hit.raw if hit else "") or "")
        got = hit is not None
        phrase = hit.phrase if hit else None
        if got and "expect_phrase_contains" in case:
            needle = str(case["expect_phrase_contains"])
            if needle not in (phrase or ""):
                return CaseResult(
                    case_id=case_id,
                    engine=eng,
                    outcome=OUTCOME_MISS if expect else OUTCOME_FA,
                    expect_match=expect,
                    got_match=True,
                    decoded_text=text,
                    phrase=phrase,
                    notes=f"phrase {phrase!r} missing {needle!r}",
                    tags=tags,
                )
        outcome = classify_outcome(expect_match=expect, got_match=got)
        return CaseResult(
            case_id=case_id,
            engine=eng,
            outcome=outcome,
            expect_match=expect,
            got_match=got,
            decoded_text=text,
            phrase=phrase,
            tags=tags,
        )
    except Exception as exc:
        return CaseResult(
            case_id=case_id,
            engine=eng,
            outcome=OUTCOME_ERROR,
            expect_match=expect,
            got_match=False,
            tags=tags,
            error=str(exc),
        )


def evaluate_cases(
    cases: Iterable[dict[str, Any]],
    *,
    fixtures_root: Path,
    backends: Sequence[tuple[str, WakeBackend | None]] | None = None,
    include_text_path: bool = True,
) -> list[EngineSummary]:
    """Score cases; backends is list of (label, backend|None). None → all skip."""
    summaries: list[EngineSummary] = []
    case_list = list(cases)

    if include_text_path:
        text_sum = EngineSummary(engine="text_path")
        for case in case_list:
            # Text path needs a text field; skip pure derived audio without text.
            if case.get("vosk_text") is None and case.get("text") is None:
                text_sum.add(
                    CaseResult(
                        case_id=str(case.get("id") or "?"),
                        engine="text_path",
                        outcome=OUTCOME_SKIP,
                        expect_match=bool(case.get("expect_match")),
                        got_match=False,
                        notes="no vosk_text/text",
                        tags=list(case.get("tags") or []),
                    )
                )
                continue
            text_sum.add(score_text_path(case))
        summaries.append(text_sum)

    for label, backend in backends or ():
        eng_sum = EngineSummary(engine=label)
        if backend is None:
            for case in case_list:
                eng_sum.add(
                    CaseResult(
                        case_id=str(case.get("id") or "?"),
                        engine=label,
                        outcome=OUTCOME_SKIP,
                        expect_match=bool(case.get("expect_match")),
                        got_match=False,
                        notes="backend unavailable",
                        tags=list(case.get("tags") or []),
                    )
                )
            summaries.append(eng_sum)
            continue
        for case in case_list:
            eng_sum.add(
                score_backend_case(
                    case, backend, fixtures_root=fixtures_root, engine=label
                )
            )
        summaries.append(eng_sum)

    return summaries


# ---------------------------------------------------------------------------
# Optional backend discovery (Vosk always when installed; Sherpa when B070+)
# ---------------------------------------------------------------------------


def vosk_available(model_path: Path | None = None) -> bool:
    from hark.config import default_vosk_model_path

    path = model_path or default_vosk_model_path()
    if not path.is_dir():
        return False
    try:
        import vosk  # noqa: F401
    except ImportError:
        return False
    return True


def try_build_vosk_backend(
    model_path: Path | None = None,
) -> WakeBackend | None:
    if not vosk_available(model_path):
        return None
    from hark.config import default_vosk_model_path
    from hark.wake import VoskWakeBackend

    path = model_path or default_vosk_model_path()
    return VoskWakeBackend(str(path))


def default_sherpa_model_path() -> Path:
    import os

    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    # B070 download script target (when present).
    return root / "hark" / "models" / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"


def sherpa_available(model_path: Path | None = None) -> bool:
    """True when B070 Sherpa backend + model can be used (optional for CI)."""
    path = model_path or default_sherpa_model_path()
    if not path.is_dir():
        # Allow env override for alternate install locations.
        import os

        env = os.environ.get("HARK_SHERPA_KWS_MODEL")
        if env and Path(env).is_dir():
            path = Path(env)
        else:
            return False
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False
    # Prefer dedicated class from B070 when landed.
    try:
        from hark.wake import SherpaKwsWakeBackend  # type: ignore  # noqa: F401
    except ImportError:
        # Package present but backend not shipped yet — still "not available"
        # for full product scoring; probe helpers may use raw sherpa later.
        return False
    return True


def try_build_sherpa_backend(
    model_path: Path | None = None,
) -> WakeBackend | None:
    """Build Sherpa KWS backend when B070 is present; else None (skip)."""
    import os

    path = model_path
    if path is None:
        env = os.environ.get("HARK_SHERPA_KWS_MODEL")
        path = Path(env) if env else default_sherpa_model_path()
    if not path.is_dir():
        return None
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return None
    try:
        from hark.wake import SherpaKwsWakeBackend  # type: ignore
    except ImportError:
        return None
    try:
        return SherpaKwsWakeBackend(str(path))  # type: ignore[call-arg, misc]
    except TypeError:
        # Signature may take keywords when B070 lands — try common forms.
        try:
            return SherpaKwsWakeBackend(model_path=str(path))  # type: ignore[call-arg, misc]
        except Exception:
            return None
    except Exception:
        return None


def discover_backends(
    *,
    vosk_model: Path | None = None,
    sherpa_model: Path | None = None,
    want_vosk: bool = True,
    want_sherpa: bool = True,
) -> list[tuple[str, WakeBackend | None]]:
    """Return (label, backend|None) pairs for the offline runner."""
    out: list[tuple[str, WakeBackend | None]] = []
    if want_vosk:
        out.append(("vosk", try_build_vosk_backend(vosk_model)))
    if want_sherpa:
        out.append(("sherpa_kws", try_build_sherpa_backend(sherpa_model)))
    return out


def filter_cases(
    cases: Sequence[dict[str, Any]],
    *,
    tags_any: Sequence[str] | None = None,
    tags_all: Sequence[str] | None = None,
    ids: Sequence[str] | None = None,
    audio_only: bool = False,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    id_set = set(ids) if ids else None
    any_set = set(tags_any) if tags_any else None
    all_list = list(tags_all) if tags_all else None
    for c in cases:
        if id_set is not None and c.get("id") not in id_set:
            continue
        if audio_only and not c.get("wav"):
            continue
        tags = set(c.get("tags") or [])
        if any_set is not None and tags.isdisjoint(any_set):
            continue
        if all_list is not None and not all(t in tags for t in all_list):
            continue
        selected.append(c)
    return selected

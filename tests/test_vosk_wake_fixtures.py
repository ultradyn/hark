"""Optional offline Vosk re-decode of live wake fixture WAVs (B008).

Skips when vosk is not installed or the small en-us model is missing.
Does not require network — uses the local model under XDG data home.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from hark.config import default_vosk_model_path
from hark.wake import VoskWakeBackend, match_activation

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"
CASES_PATH = FIX / "voice" / "wake" / "cases.jsonl"
VOSK_MODEL = default_vosk_model_path()


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _vosk_available() -> bool:
    if not VOSK_MODEL.is_dir():
        return False
    try:
        import vosk  # noqa: F401
    except ImportError:
        return False
    return True


def _read_pcm16_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise AssertionError(f"{path}: expected 16-bit PCM, got {wf.getsampwidth()}")
        if wf.getnchannels() != 1:
            raise AssertionError(f"{path}: expected mono, got {wf.getnchannels()} ch")
        sample_rate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
    return pcm, sample_rate


requires_vosk = pytest.mark.skipif(
    not _vosk_available(),
    reason=(
        f"vosk model missing or package not installed "
        f"(need {VOSK_MODEL} and uv sync --extra wake)"
    ),
)


@pytest.fixture(scope="module")
def vosk_backend() -> VoskWakeBackend:
    return VoskWakeBackend(str(VOSK_MODEL))


@pytest.mark.vosk
@requires_vosk
@pytest.mark.parametrize(
    "case",
    _load_jsonl(CASES_PATH),
    ids=lambda c: c["id"],
)
def test_offline_vosk_redecode_wake_fixture(
    case: dict, vosk_backend: VoskWakeBackend
) -> None:
    """Re-decode fixture WAV with Vosk; assert match_activation vs expect_*."""
    wav = FIX / "voice" / "wake" / case["wav"]
    assert wav.is_file(), f"missing wav {wav}"
    pcm, sample_rate = _read_pcm16_wav(wav)

    # Score once so last_text is populated (production wake path).
    vosk_backend.score_snippet(pcm, sample_rate)
    text = vosk_backend.last_text
    hit = match_activation(text, anywhere=True)

    if not case["expect_match"]:
        assert hit is None, (
            f"{case['id']}: unexpected hit {hit} for decoded {text!r} "
            f"(rms={vosk_backend.last_rms:.4f})"
        )
        return

    assert hit is not None, (
        f"{case['id']}: expected match for decoded {text!r} "
        f"(rms={vosk_backend.last_rms:.4f})"
    )
    if "expect_phrase_contains" in case:
        assert case["expect_phrase_contains"] in hit.phrase, (
            f"{case['id']}: phrase {hit.phrase!r} missing "
            f"{case['expect_phrase_contains']!r} (decoded {text!r})"
        )

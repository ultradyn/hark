"""Sherpa-ONNX KWS wake backend (B070).

Unit tests always run (keyword building, keyword_id mapping, build_wake_backend
routing). Offline fixture re-decode is optional via @pytest.mark.sherpa_kws
when the model + package are present.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from hark.config import default_sherpa_kws_model_path
from hark.wake import (
    WakePolicy,
    build_wake_backend,
    is_sherpa_kws_model_dir,
    keyword_id_to_phrase,
    kws_keyword_specs,
    encode_kws_keywords_file,
)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"
CASES_PATH = FIX / "voice" / "wake" / "cases.jsonl"
# Prefer XDG install; fall back to B069 probe path for local dogfood
_CANDIDATE_MODELS = [
    default_sherpa_kws_model_path(),
    Path("/tmp/hark-b069-probe/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"),
]


def _find_model() -> Path | None:
    for p in _CANDIDATE_MODELS:
        if is_sherpa_kws_model_dir(p):
            return p
    return None


SHERPA_MODEL = _find_model()


def _sherpa_available() -> bool:
    if SHERPA_MODEL is None:
        return False
    try:
        import sherpa_onnx  # noqa: F401
        import sentencepiece  # noqa: F401
    except ImportError:
        return False
    return True


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append(json.loads(line))
    return rows


def _read_pcm16_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise AssertionError(f"{path}: expected 16-bit PCM")
        if wf.getnchannels() != 1:
            raise AssertionError(f"{path}: expected mono")
        return wf.readframes(wf.getnframes()), wf.getframerate()


requires_sherpa = pytest.mark.skipif(
    not _sherpa_available(),
    reason=(
        "sherpa_kws model or package missing "
        "(download-sherpa-kws-model.sh + uv sync --extra wake-sherpa)"
    ),
)


def test_keyword_id_to_phrase():
    assert keyword_id_to_phrase("HEY_HARK") == "hey hark"
    assert keyword_id_to_phrase("@HEY_HERALD") == "hey herald"
    assert keyword_id_to_phrase("IRIS") == "iris"


def test_kws_keyword_specs_names_mode_includes_prefixes():
    pol = WakePolicy(mode="names", names=["iris", "hark"])
    specs = kws_keyword_specs(pol)
    spoken = {s for s, _k, _b, _t in specs}
    assert "HEY HARK" in spoken
    assert "HEY IRIS" in spoken
    assert "HELLO HARK" in spoken
    assert "HARK" in spoken
    assert "IRIS" in spoken
    # keyword ids unique
    ids = [k for _s, k, _b, _t in specs]
    assert len(ids) == len(set(ids))


def test_kws_keyword_specs_phrases_mode():
    pol = WakePolicy(mode="phrases", names=[], phrases=["start prompt"])
    specs = kws_keyword_specs(pol)
    spoken = {s for s, _k, _b, _t in specs}
    assert "START PROMPT" in spoken
    assert "HEY HARK" not in spoken


def test_kws_keyword_specs_includes_learned_name_alias():
    pol = WakePolicy(
        mode="names",
        names=["hark"],
        name_aliases={"hook": "hark"},
    )
    specs = kws_keyword_specs(pol)
    spoken = {s for s, _k, _b, _t in specs}
    assert "HEY HOOK" in spoken


def test_build_wake_backend_sherpa_requires_model():
    with pytest.raises(RuntimeError, match="model_path"):
        build_wake_backend("sherpa_kws", model_path=None)


def test_build_wake_backend_sherpa_rejects_bad_tree(tmp_path):
    bad = tmp_path / "not-a-model"
    bad.mkdir()
    with pytest.raises(RuntimeError, match="not a sherpa KWS"):
        build_wake_backend("sherpa_kws", model_path=str(bad))


def test_build_wake_backend_vosk_still_default_path():
    """Unknown engines still raise; vosk path unchanged."""
    with pytest.raises(ValueError, match="unknown ambient wake engine"):
        build_wake_backend("nope_engine")


@requires_sherpa
def test_encode_keywords_writes_bpe(tmp_path):
    assert SHERPA_MODEL is not None
    pol = WakePolicy(mode="names", names=["hark", "herald"])
    specs = kws_keyword_specs(pol)
    dest = tmp_path / "keywords.txt"
    encode_kws_keywords_file(
        specs,
        bpe_model=SHERPA_MODEL / "bpe.model",
        tokens_txt=SHERPA_MODEL / "tokens.txt",
        dest=dest,
    )
    text = dest.read_text(encoding="utf-8")
    assert "@HEY_HARK" in text
    assert "▁" in text  # BPE pieces


@pytest.fixture(scope="module")
def sherpa_backend():
    assert SHERPA_MODEL is not None
    from hark.wake import SherpaKwsWakeBackend

    return SherpaKwsWakeBackend(
        str(SHERPA_MODEL),
        policy=WakePolicy(mode="names", names=["hark", "herald", "iris", "mercury"]),
    )


@pytest.mark.sherpa_kws
@requires_sherpa
@pytest.mark.parametrize(
    "case",
    _load_jsonl(CASES_PATH),
    ids=lambda c: c["id"],
)
def test_offline_sherpa_kws_wake_fixture(case: dict, sherpa_backend) -> None:
    """Re-score live wake WAVs with Sherpa KWS (B069 measured clean hit/miss)."""
    wav = FIX / "voice" / "wake" / case["wav"]
    assert wav.is_file(), f"missing wav {wav}"
    pcm, sample_rate = _read_pcm16_wav(wav)
    hit = sherpa_backend.score_snippet(pcm, sample_rate)

    if not case["expect_match"]:
        assert hit is None, (
            f"{case['id']}: unexpected hit {hit} raw={sherpa_backend.last_text!r} "
            f"(rms={sherpa_backend.last_rms:.4f})"
        )
        return

    assert hit is not None, (
        f"{case['id']}: expected match, got none "
        f"raw={sherpa_backend.last_text!r} rms={sherpa_backend.last_rms:.4f}"
    )
    assert hit.backend == "sherpa_kws"
    if "expect_phrase_contains" in case:
        assert case["expect_phrase_contains"] in hit.phrase, (
            f"{case['id']}: phrase {hit.phrase!r} missing "
            f"{case['expect_phrase_contains']!r} (raw {sherpa_backend.last_text!r})"
        )


@pytest.mark.sherpa_kws
@requires_sherpa
def test_rebuild_keywords_on_policy_change(tmp_path, sherpa_backend):
    """Keyword set follows configured names without process restart."""
    sherpa_backend.keywords_path = tmp_path / "kw.txt"
    pol = WakePolicy(mode="names", names=["alice"])
    sherpa_backend.rebuild_keywords(pol)
    text = sherpa_backend.keywords_path.read_text(encoding="utf-8")
    assert "ALICE" in text or "@" in text
    # Quiet empty should not wake
    quiet = b"\x00\x00" * 1600
    assert sherpa_backend.score_snippet(quiet, 16000) is None

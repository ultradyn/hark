#!/usr/bin/env python3
"""Generate derived wake eval fixtures from live/ captures (B071).

Creates noise / gain / pad / silence variants under fixtures/voice/wake/derived/
and merges them into cases.jsonl. Safe to re-run (idempotent overwrite).

Does not require Vosk or Sherpa. Uses numpy (project dependency).

Usage:
  uv run python scripts/gen-wake-eval-fixtures.py
  uv run python scripts/gen-wake-eval-fixtures.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hark.wake_eval import (  # noqa: E402
    load_cases,
    read_pcm16_wav,
    write_cases,
    write_pcm16_wav,
)

WAKE = ROOT / "fixtures" / "voice" / "wake"
LIVE = WAKE / "live"
DERIVED = WAKE / "derived"
CASES = WAKE / "cases.jsonl"
SAMPLE_RATE = 16000
SNIP_SAMPLES = int(SAMPLE_RATE * 2.5)  # ambient default ~2.5 s


def _to_int16(x: np.ndarray) -> bytes:
    x = np.clip(x, -32768, 32767).astype(np.int16)
    return x.tobytes()


def _from_pcm(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float64)


def mix_noise(pcm: bytes, snr_db: float, *, seed: int) -> bytes:
    """Add white noise at approximate SNR (dB) relative to signal RMS."""
    sig = _from_pcm(pcm)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(sig.shape[0])
    sig_power = float(np.mean(sig**2)) + 1e-12
    noise_power = float(np.mean(noise**2)) + 1e-12
    # SNR = 10 log10(sig/noise_scaled)
    scale = np.sqrt(sig_power / (noise_power * (10.0 ** (snr_db / 10.0))))
    mixed = sig + noise * scale
    return _to_int16(mixed)


def scale_gain(pcm: bytes, gain: float) -> bytes:
    sig = _from_pcm(pcm) * gain
    return _to_int16(sig)


def pad_front(pcm: bytes, pad_ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Prepend silence and trim/pad to original length (timing shift)."""
    sig = _from_pcm(pcm)
    n_pad = int(sample_rate * pad_ms / 1000)
    if n_pad <= 0:
        return pcm
    out = np.concatenate([np.zeros(n_pad), sig])[: len(sig)]
    if len(out) < len(sig):
        out = np.concatenate([out, np.zeros(len(sig) - len(out))])
    return _to_int16(out)


def silence_pcm(n_samples: int = SNIP_SAMPLES) -> bytes:
    return _to_int16(np.zeros(n_samples))


def pure_noise_pcm(n_samples: int = SNIP_SAMPLES, *, seed: int = 42, amp: float = 800.0) -> bytes:
    rng = np.random.default_rng(seed)
    return _to_int16(rng.standard_normal(n_samples) * amp)


def live_base_cases() -> list[dict]:
    """Hand-curated live set (source of truth for expect labels)."""
    return [
        {
            "id": "hey-harold-as-herald-hit",
            "wav": "live/hey-harold-as-herald-hit.wav",
            "meta": "live/hey-harold-as-herald-hit.json",
            "vosk_text": "hey harold",
            "expect_match": True,
            "expect_phrase_contains": "herald",
            "tags": ["live", "positive", "herald", "greeting", "mishear", "speaker-op1"],
            "source": "live-capture-2026-07-13",
            "notes": "Live Wave capture; vosk said harold, fuzzy maps to herald",
        },
        {
            "id": "hey-hook-as-hark-hit",
            "wav": "live/hey-hook-as-hark-hit.wav",
            "meta": "live/hey-hook-as-hark-hit.json",
            "vosk_text": "hey hook",
            "expect_match": True,
            "expect_phrase_contains": "hark",
            "tags": ["live", "positive", "hark", "greeting", "mishear", "speaker-op1"],
            "source": "live-capture-2026-07-13",
            "notes": "Common vosk mishear of hark",
        },
        {
            "id": "hey-hawk-as-hark-hit",
            "wav": "live/hey-hawk-as-hark-hit.wav",
            "meta": "live/hey-hawk-as-hark-hit.json",
            "vosk_text": "hey hawk",
            "expect_match": True,
            "expect_phrase_contains": "hark",
            "tags": ["live", "positive", "hark", "greeting", "mishear", "speaker-op1"],
            "source": "live-capture-2026-07-13",
            "notes": "Common vosk mishear of hark",
        },
        {
            "id": "a-hawk-miss",
            "wav": "live/a-hawk-miss.wav",
            "meta": "live/a-hawk-miss.json",
            "vosk_text": "a hawk",
            "expect_match": False,
            "tags": ["live", "negative", "near-miss", "speaker-op1"],
            "source": "live-capture-2026-07-13",
            "notes": "No hey/ok prefix — must not wake",
        },
        {
            "id": "hey-hoc-miss",
            "wav": "live/hey-hoc-miss.wav",
            "meta": "live/hey-hoc-miss.json",
            "vosk_text": "hey hoc",
            "expect_match": False,
            "tags": ["live", "negative", "near-miss", "speaker-op1"],
            "source": "live-capture-2026-07-13",
            "notes": "Near-miss garbage; not in alias set",
        },
        {
            "id": "hello-alone-miss",
            "wav": "live/hello-alone-miss.wav",
            "meta": "live/hello-alone-miss.json",
            "vosk_text": "hello",
            "expect_match": False,
            "tags": ["live", "negative", "greeting-only", "speaker-op1"],
            "source": "live-capture-2026-07-13",
            "notes": "Prefix alone is not a wake",
        },
        {
            "id": "hey-ho-miss",
            "wav": "live/hey-ho-miss.wav",
            "meta": "live/hey-ho-miss.json",
            "vosk_text": "hey ho",
            "expect_match": False,
            "tags": ["live", "negative", "incomplete", "speaker-op1"],
            "source": "live-capture-2026-07-13",
            "notes": "Incomplete wake phrase",
        },
    ]


# Text-only eval rows: broader greating / bare / custom-name dimensions without
# new multi-speaker audio. Scored on text_path; audio backends skip (no wav).
TEXT_ONLY_CASES: list[dict] = [
    {
        "id": "text-bare-hark",
        "vosk_text": "hark",
        "expect_match": True,
        "expect_phrase_contains": "hark",
        "tags": ["text-only", "positive", "hark", "bare"],
        "source": "synthetic-text",
        "notes": "Bare name wake (names mode)",
    },
    {
        "id": "text-bare-herald",
        "vosk_text": "herald",
        "expect_match": True,
        "expect_phrase_contains": "herald",
        "tags": ["text-only", "positive", "herald", "bare"],
        "source": "synthetic-text",
        "notes": "Bare herald",
    },
    {
        "id": "text-bare-iris",
        "vosk_text": "iris",
        "expect_match": True,
        "expect_phrase_contains": "iris",
        "tags": ["text-only", "positive", "iris", "bare", "custom-name"],
        "source": "synthetic-text",
        "notes": "Default persona name bare",
    },
    {
        "id": "text-hey-iris",
        "vosk_text": "hey iris",
        "expect_match": True,
        "expect_phrase_contains": "iris",
        "tags": ["text-only", "positive", "iris", "greeting", "custom-name"],
        "source": "synthetic-text",
        "notes": "Greeting + Iris",
    },
    {
        "id": "text-hey-mercury",
        "vosk_text": "hey mercury",
        "expect_match": True,
        "expect_phrase_contains": "mercury",
        "tags": ["text-only", "positive", "mercury", "greeting", "custom-name"],
        "source": "synthetic-text",
        "notes": "Greeting + Mercury",
    },
    {
        "id": "text-hello-mercury",
        "vosk_text": "hello mercury status",
        "expect_match": True,
        "expect_phrase_contains": "mercury",
        "tags": ["text-only", "positive", "mercury", "greeting", "custom-name"],
        "source": "synthetic-text",
        "notes": "hello + mercury + remainder",
    },
    {
        "id": "text-ok-hark",
        "vosk_text": "ok hark list panes",
        "expect_match": True,
        "expect_phrase_contains": "hark",
        "tags": ["text-only", "positive", "hark", "greeting"],
        "source": "synthetic-text",
        "notes": "ok prefix",
    },
    {
        "id": "text-hark-back-false",
        "vosk_text": "please hark back to the design",
        "expect_match": False,
        "tags": ["text-only", "negative", "idiom"],
        "source": "synthetic-text",
        "notes": "Idiom must not wake",
    },
    {
        "id": "text-herald-of-false",
        "vosk_text": "the herald of spring arrived",
        "expect_match": False,
        "tags": ["text-only", "negative", "idiom"],
        "source": "synthetic-text",
        "notes": "herald-of construction",
    },
    {
        "id": "text-hard-drive-false",
        "vosk_text": "what about the hard drive",
        "expect_match": False,
        "tags": ["text-only", "negative", "near-miss"],
        "source": "synthetic-text",
        "notes": "hard alone is not a wake (needs greating for alias path)",
    },
]


def derive_variants(parent: dict, pcm: bytes, sample_rate: int) -> list[tuple[dict, bytes]]:
    """Build derived case dicts + PCM for one live parent."""
    pid = parent["id"]
    out: list[tuple[dict, bytes]] = []
    variants: list[tuple[str, str, bytes, str]] = [
        (
            f"{pid}-noise-light",
            "noise-light",
            mix_noise(pcm, snr_db=18.0, seed=hash(pid) & 0xFFFF),
            "Light white noise (~18 dB SNR) over live capture",
        ),
        (
            f"{pid}-noise-heavy",
            "noise-heavy",
            mix_noise(pcm, snr_db=8.0, seed=(hash(pid) >> 8) & 0xFFFF),
            "Heavy white noise (~8 dB SNR) over live capture",
        ),
        (
            f"{pid}-quiet",
            "quiet",
            scale_gain(pcm, 0.35),
            "Attenuated copy (gain 0.35) — soft close-talk / distance",
        ),
        (
            f"{pid}-pad150",
            "pad-front",
            pad_front(pcm, 150, sample_rate),
            "150 ms leading silence shift within same window",
        ),
    ]
    parent_tags = [t for t in (parent.get("tags") or []) if t != "live"]
    for vid, vtag, vpcm, notes in variants:
        rel = f"derived/{vid}.wav"
        case = {
            "id": vid,
            "wav": rel,
            "parent": pid,
            "vosk_text": parent.get("vosk_text", ""),
            "expect_match": parent["expect_match"],
            "tags": ["derived", vtag] + parent_tags,
            "source": "derived-from-live",
            "notes": notes,
        }
        if "expect_phrase_contains" in parent:
            case["expect_phrase_contains"] = parent["expect_phrase_contains"]
        if parent.get("meta"):
            # Sidecar optional for derived; parity tests tolerate missing meta.
            pass
        out.append((case, vpcm))
    return out


def build_all(dry_run: bool = False) -> list[dict]:
    DERIVED.mkdir(parents=True, exist_ok=True)
    cases: list[dict] = []
    # Live bases
    for base in live_base_cases():
        wav_path = WAKE / base["wav"]
        if not wav_path.is_file():
            print(f"warn: missing live wav {wav_path}", file=sys.stderr)
            continue
        cases.append(base)
        pcm, sr = read_pcm16_wav(wav_path)
        if sr != SAMPLE_RATE:
            print(f"warn: {wav_path} sample_rate={sr} (expected {SAMPLE_RATE})", file=sys.stderr)
        for case, vpcm in derive_variants(base, pcm, sr):
            out_path = WAKE / case["wav"]
            if not dry_run:
                write_pcm16_wav(out_path, vpcm, sr)
            cases.append(case)
            print(f"  {'would write' if dry_run else 'wrote'} {case['wav']}")

    # Synthetic ambient-window negatives (no speech / pure noise)
    synth_audio = [
        (
            {
                "id": "silence-2.5s-miss",
                "wav": "derived/silence-2.5s-miss.wav",
                "vosk_text": "",
                "expect_match": False,
                "tags": ["derived", "synthetic", "negative", "silence"],
                "source": "synthetic-pcm",
                "notes": "Full 2.5 s silence — energy floor should skip",
            },
            silence_pcm(),
        ),
        (
            {
                "id": "noise-only-miss",
                "wav": "derived/noise-only-miss.wav",
                "vosk_text": "",
                "expect_match": False,
                "tags": ["derived", "synthetic", "negative", "noise-only"],
                "source": "synthetic-pcm",
                "notes": "White noise only — must not false-accept",
            },
            pure_noise_pcm(seed=99, amp=1200.0),
        ),
    ]
    for case, pcm in synth_audio:
        if not dry_run:
            write_pcm16_wav(WAKE / case["wav"], pcm, SAMPLE_RATE)
        cases.append(case)
        print(f"  {'would write' if dry_run else 'wrote'} {case['wav']}")

    # Text-only dimension expansion
    cases.extend(TEXT_ONLY_CASES)

    if not dry_run:
        # Preserve any extra hand-added cases (unknown ids) from existing file.
        existing = {c["id"]: c for c in load_cases(CASES)}
        known = {c["id"] for c in cases}
        for eid, ec in existing.items():
            if eid not in known:
                # Drop stale auto-derived if parent missing; keep others.
                if "derived" in (ec.get("tags") or []) and ec.get("parent") not in {
                    c["id"] for c in live_base_cases()
                }:
                    continue
                cases.append(ec)
        # Stable order: live → derived (by parent) → synthetic → text
        def sort_key(c: dict) -> tuple:
            tags = c.get("tags") or []
            if "live" in tags:
                group = 0
            elif "derived" in tags and "synthetic" not in tags:
                group = 1
            elif "synthetic" in tags:
                group = 2
            elif "text-only" in tags:
                group = 3
            else:
                group = 4
            return (group, c.get("parent") or "", c["id"])

        cases.sort(key=sort_key)
        write_cases(CASES, cases)
        print(f"updated {CASES.relative_to(ROOT)} ({len(cases)} cases)")
    else:
        print(f"dry-run: would write {len(cases)} cases")
    return cases


def refresh_manifest() -> None:
    """Optional: recompute fixtures/MANIFEST.json hashes."""
    fix = ROOT / "fixtures"
    entries = []
    for path in sorted(fix.rglob("*")):
        if not path.is_file() or path.name == "MANIFEST.json":
            continue
        data = path.read_bytes()
        import hashlib
        import time

        entries.append(
            {
                "path": path.relative_to(fix).as_posix(),
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    import time

    manifest = {
        "schema": "hark.fixtures.manifest.v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(entries),
        "files": entries,
    }
    (fix / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"MANIFEST.json: {len(entries)} files")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-manifest", action="store_true", help="skip MANIFEST refresh")
    args = ap.parse_args()
    print("Generating wake eval fixtures (B071)…")
    build_all(dry_run=args.dry_run)
    if not args.dry_run and not args.no_manifest:
        refresh_manifest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

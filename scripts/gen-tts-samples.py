#!/usr/bin/env python3
"""Generate or re-index curated TTS samples under assets/tts/samples/.

Layout: samples/{provider}/{gender}/{voice_id}.mp3
Phrase template: samples/phrase.txt (must include {Voice} at least twice)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "assets" / "tts" / "samples"
PHRASE_FILE = SAMPLES / "phrase.txt"
MANIFEST = SAMPLES / "manifest.json"

# xAI catalog (hark providers voices) — keep in sync when catalog changes
XAI_VOICES: dict[str, str] = {
    "altair": "male",
    "ara": "female",
    "atlas": "male",
    "carina": "female",
    "castor": "male",
    "celeste": "female",
    "cosmo": "male",
    "eve": "female",
    "helios": "male",
    "helix": "male",
    "iris": "female",
    "kepler": "male",
    "leo": "male",
    "lumen": "male",
    "luna": "female",
    "lux": "male",
    "naksh": "male",
    "orion": "male",
    "perseus": "male",
    "rex": "male",
    "rigel": "male",
    "sal": "male",
    "sirius": "male",
    "ursa": "female",
    "zagan": "male",
    "zenith": "male",
}

# OpenAI TTS voices (gender not always official — use unknown unless known)
OPENAI_VOICES: dict[str, str] = {
    "alloy": "unknown",
    "ash": "unknown",
    "ballad": "unknown",
    "coral": "unknown",
    "echo": "unknown",
    "fable": "unknown",
    "onyx": "unknown",
    "nova": "unknown",
    "sage": "unknown",
    "shimmer": "unknown",
}

# MiniMax: extend when dogfooding; default id used by Hark if unset
MINIMAX_VOICES: dict[str, str] = {
    "English_expressive_narrator": "unknown",
    "English_Trustworthy_Man": "male",
    "English_Graceful_Lady": "female",
    "English_Aussie_Bloke": "male",
    "English_CalmWoman": "female",
    "English_ManWithDeepVoice": "male",
    "male-qn-qingse": "male",
    "female-shaonv": "female",
    "presenter_male": "male",
    "presenter_female": "female",
}


def phrase_template() -> str:
    if not PHRASE_FILE.is_file():
        raise SystemExit(f"missing {PHRASE_FILE}")
    return PHRASE_FILE.read_text(encoding="utf-8").strip()


def display_name(voice_id: str) -> str:
    """Human-spoken label for the voice (said at least twice in the sample)."""
    # Prefer readable words: English_expressive_narrator → "English expressive narrator"
    cleaned = voice_id.replace("_", " ").replace("-", " ").strip()
    # Title-case words; keep short ids like "eve" → "Eve"
    return " ".join(w[:1].upper() + w[1:] if w else w for w in cleaned.split())


def phrase_for_voice(voice_id: str, template: str | None = None) -> str:
    """Fill phrase.txt template. Placeholders: {Voice} (display), {voice} (raw id)."""
    tmpl = template if template is not None else phrase_template()
    name = display_name(voice_id)
    text = tmpl.replace("{Voice}", name).replace("{voice}", voice_id)
    # Require the spoken display name at least twice (case-insensitive whole-ish match)
    # Count non-overlapping occurrences of the display name.
    count = len(re.findall(re.escape(name), text, flags=re.IGNORECASE))
    if count < 2:
        raise SystemExit(
            f"phrase for {voice_id!r} must say the voice name ≥2 times "
            f"(found {count}). Use {{Voice}} twice in phrase.txt.\nGot: {text!r}"
        )
    return text


def out_path(provider: str, gender: str, voice_id: str) -> Path:
    g = gender if gender in ("male", "female", "unknown") else "unknown"
    return SAMPLES / provider / g / f"{voice_id}.mp3"


def synthesize(provider: str, voice_id: str, dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "hark",
        "tts",
        "--provider",
        provider,
        "--voice",
        voice_id,
        "--no-play",
        "--out",
        str(dest),
        text,
    ]
    print(f"+ [{voice_id}] {text}", flush=True)
    print("+", " ".join(cmd[:-1]), f'"{text[:40]}…"', flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def write_manifest() -> dict:
    tmpl = phrase_template()
    ph = hashlib.sha256(tmpl.encode()).hexdigest()[:16]
    samples: list[dict] = []
    for path in sorted(SAMPLES.rglob("*.mp3")):
        rel = path.relative_to(SAMPLES).as_posix()
        parts = rel.split("/")
        if len(parts) != 3:
            continue
        provider, gdir, fname = parts
        voice_id = Path(fname).stem
        samples.append(
            {
                "provider": provider,
                "voice_id": voice_id,
                "gender": gdir,
                "path": rel,
                "bytes": path.stat().st_size,
                "phrase": phrase_for_voice(voice_id, tmpl),
            }
        )
    manifest = {
        "schema": "hark.tts.samples.v1",
        "phrase_template": tmpl,
        "phrase_template_sha256_16": ph,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "layout": "samples/{provider}/{gender}/{voice_id}.mp3",
        "notes": [
            "Runtime TTS cache: assets/tts/{voice}/ (not samples).",
            "Each sample speaks the voice display name ≥2 times via {Voice} in phrase.txt.",
            "Wake name iris ≠ TTS voice_id iris; default pairing often Iris + eve.",
            "OpenAI/MiniMax filled after xAI dogfood feedback.",
        ],
        "samples": samples,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST} ({len(samples)} samples)", flush=True)
    return manifest


def catalog(provider: str) -> dict[str, str]:
    if provider == "xai":
        return XAI_VOICES
    if provider == "openai":
        return OPENAI_VOICES
    if provider == "minimax":
        return MINIMAX_VOICES
    raise SystemExit(f"unknown provider {provider!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--provider",
        choices=("xai", "openai", "minimax"),
        action="append",
        help="generate for this provider (repeatable); omit with --manifest-only",
    )
    ap.add_argument(
        "--voice",
        action="append",
        help="only these voice ids (default: full catalog for provider)",
    )
    ap.add_argument(
        "--manifest-only",
        action="store_true",
        help="only rebuild manifest.json from files on disk",
    )
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="do not re-download if mp3 already present",
    )
    args = ap.parse_args()

    if args.manifest_only:
        write_manifest()
        return 0

    if not args.provider:
        ap.error("pass --provider … or --manifest-only")

    tmpl = phrase_template()
    for provider in args.provider:
        cat = catalog(provider)
        voices = args.voice or list(cat.keys())
        for voice_id in voices:
            if voice_id not in cat:
                print(f"warn: {voice_id} not in {provider} catalog; using gender=unknown", file=sys.stderr)
                gender = "unknown"
            else:
                gender = cat[voice_id]
            dest = out_path(provider, gender, voice_id)
            if args.skip_existing and dest.is_file() and dest.stat().st_size > 0:
                print(f"skip existing {dest.relative_to(ROOT)}", flush=True)
                continue
            text = phrase_for_voice(voice_id, tmpl)
            try:
                synthesize(provider, voice_id, dest, text)
            except subprocess.CalledProcessError as e:
                print(f"FAILED {provider}/{voice_id}: {e}", file=sys.stderr)
                return e.returncode or 1

    write_manifest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

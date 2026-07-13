"""Wake enrollment — beep-paced activation sample gathering (I006).

CLI: ``hark wake-enroll``

Loop::

    [ready beep] → operator says phrase → [accept beep]
                 → … × N →
    [end beep]

Samples land under ``~/.local/state/hark/wake_enroll/<phrase-slug>/`` as
``01.wav`` … plus ``manifest.json``. Optional post-pass scores each WAV with
the active wake backend and seeds learned aliases (B077 denylist).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from hark.audio.capture import MicLease, capture_utterance, write_wav_bytes
from hark.audio.cues import (
    configure_cues_from_config,
    play_enroll_accept,
    play_enroll_end,
    play_enroll_ready,
    play_enroll_reject,
    phrase_slug,
)
from hark.config import HarkConfig, load_config
from hark.exitcodes import OK, USAGE
from hark.mic_coord import pause_ambient_for_mic
from hark.paths import state_dir
from hark.syslog import log as syslog
from hark.wake import (
    NearMiss,
    WakeHit,
    WakePolicy,
    build_wake_backend,
    default_wake_policy,
    match_activation,
    plausible_near_miss,
    suggest_learn_from_near_miss,
)
from hark.wake_learn import load_learned, learn_name_alias, learn_phrase_alias

DEFAULT_COUNT = 7
DEFAULT_MIN = 5
DEFAULT_MAX_UTTER_S = 3.0
DEFAULT_END_SILENCE_S = 0.55
DEFAULT_INITIAL_TIMEOUT_S = 8.0
DEFAULT_GAP_S = 0.45
# Quiet/empty rejection: peak RMS below this → reject
MIN_PEAK_RMS = 0.008


@dataclass
class EnrollSample:
    index: int
    path: str
    duration_ms: int
    peak_rms: float
    accepted: bool
    wake_hit: str | None = None
    wake_raw: str | None = None
    learned: list[str] = field(default_factory=list)


@dataclass
class EnrollResult:
    phrase: str
    out_dir: Path
    target: int
    accepted: int
    rejected: int
    samples: list[EnrollSample] = field(default_factory=list)
    manifest_path: Path | None = None
    ok: bool = False
    message: str = ""


def enroll_root() -> Path:
    return state_dir() / "wake_enroll"


def default_phrase(cfg: HarkConfig) -> str:
    amb = cfg.ambient
    phrases = list(amb.activation_phrases or [])
    if phrases:
        return str(phrases[0]).strip()
    names = list(amb.names or [])
    if names:
        return f"hey {names[0]}"
    return "hey iris"


def _pcm_peak_rms(pcm16: bytes) -> float:
    if not pcm16 or len(pcm16) < 2:
        return 0.0
    import array

    samples = array.array("h")
    samples.frombytes(pcm16[: len(pcm16) - (len(pcm16) % 2)])
    if not samples:
        return 0.0
    peak = max(abs(s) for s in samples)
    return peak / 32768.0


def _score_sample(
    pcm16: bytes,
    sample_rate: int,
    backend: Any,
    policy: WakePolicy,
    phrase: str,
) -> tuple[WakeHit | None, str]:
    """Score enrollment PCM with wake backend; return (hit, raw_text)."""
    raw = ""
    hit: WakeHit | None = None
    try:
        hit = backend.score_snippet(pcm16, sample_rate)
        raw = str(getattr(backend, "last_text", "") or "")
    except Exception as exc:
        syslog(
            "wake_enroll.score_error",
            component="wake_enroll",
            level="warn",
            error=str(exc)[:160],
        )
        return None, raw
    if hit is not None:
        return hit, raw or hit.raw or hit.phrase
    # Fall back to text path if backend exposed text
    if raw:
        m = match_activation(raw, policy.display_phrases(), policy=policy)
        if m is not None:
            return m, raw
    return None, raw


def _maybe_learn_from_score(
    raw: str,
    policy: WakePolicy,
    *,
    learn: bool,
) -> list[str]:
    """Seed wake_learned from enrollment transcript (respect B077)."""
    if not learn or not raw.strip() or not policy.learn:
        return []
    learned_notes: list[str] = []
    phrases = policy.display_phrases()
    miss = plausible_near_miss(raw, phrases, policy=policy)
    if miss is None:
        # Synthetic near-miss on free tokens so Vosk mangling can still learn
        tokens = re.findall(r"[a-z]+", raw.lower())
        names = policy.canonical_names()
        for tok in tokens:
            if len(tok) < 3 or tok in names:
                continue
            for name in names:
                fake = NearMiss(
                    text=tok,
                    best_phrase=f"hey {name}",
                    score=0.75,
                    reason="enroll_token",
                )
                sug = suggest_learn_from_near_miss(fake, policy)
                if sug is None:
                    continue
                kind, value, canonical = sug
                if kind == "name" and canonical:
                    _state, changed = learn_name_alias(value, canonical)
                    if changed:
                        learned_notes.append(f"{value}→{canonical}")
                break
        return learned_notes

    sug = suggest_learn_from_near_miss(miss, policy)
    if sug is None:
        return []
    kind, value, canonical = sug
    if kind == "name" and canonical:
        _state, changed = learn_name_alias(value, canonical)
        if changed:
            learned_notes.append(f"{value}→{canonical}")
    elif kind == "phrase":
        _state, changed = learn_phrase_alias(value)
        if changed:
            learned_notes.append(f"phrase:{value}")
    return learned_notes


def run_wake_enroll(
    cfg: HarkConfig | None = None,
    *,
    phrase: str | None = None,
    count: int = DEFAULT_COUNT,
    min_count: int = DEFAULT_MIN,
    max_utter_s: float = DEFAULT_MAX_UTTER_S,
    end_silence_s: float = DEFAULT_END_SILENCE_S,
    initial_timeout_s: float = DEFAULT_INITIAL_TIMEOUT_S,
    gap_s: float = DEFAULT_GAP_S,
    learn: bool = True,
    score: bool = True,
    dry_run: bool = False,
    beeps: bool = True,
    out: TextIO | None = None,
    capture_fn: Callable[..., Any] | None = None,
    play_ready: Callable[[], None] | None = None,
    play_accept: Callable[[], None] | None = None,
    play_reject: Callable[[], None] | None = None,
    play_end: Callable[[], None] | None = None,
) -> EnrollResult:
    """Run beep-paced enrollment; returns summary + paths.

    *capture_fn* / *play_** are injectable for unit tests (no hardware).
    """
    cfg = cfg or load_config()
    configure_cues_from_config(cfg)
    phrase = (phrase or default_phrase(cfg)).strip()
    count = max(1, min(20, int(count)))
    min_count = max(1, min(count, int(min_count)))

    log = out or __import__("sys").stdout
    slug = phrase_slug(phrase)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = enroll_root() / slug / stamp
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    _ready = play_ready or play_enroll_ready
    _accept = play_accept or play_enroll_accept
    _reject = play_reject or play_enroll_reject
    _end = play_end or play_enroll_end
    _capture = capture_fn or capture_utterance

    policy = cfg.ambient.wake_policy or default_wake_policy()
    if isinstance(policy, WakePolicy) and learn:
        learned0 = load_learned()
        policy = policy.merge_learned(
            name_aliases=learned0.name_aliases,
            phrase_aliases=learned0.phrase_aliases,
        )

    backend = None
    if score and not dry_run:
        try:
            amb = cfg.ambient
            backend = build_wake_backend(
                amb.engine,
                phrases=amb.activation_phrases,
                model_path=amb.model_path,
                policy=policy if isinstance(policy, WakePolicy) else None,
            )
        except Exception as exc:
            print(
                f"note: wake backend unavailable for scoring ({exc}); "
                "samples still saved",
                file=log,
            )
            backend = None

    print(
        f"Wake enrollment: say “{phrase}” after each ready beep "
        f"({count} samples, min {min_count}).\n"
        "Wait for the accept beep before the next try.\n"
        f"Output: {out_dir if not dry_run else '(dry-run — no files)'}",
        file=log,
    )

    samples: list[EnrollSample] = []
    accepted = 0
    rejected = 0
    slot = 0
    max_attempts = count * 4  # reject budget

    # Pause ambient so we own the mic for the whole session
    with pause_ambient_for_mic(reason="wake-enroll"):
        with MicLease("wake-enroll"):
            attempt = 0
            while accepted < count and attempt < max_attempts:
                attempt += 1
                slot = accepted + 1
                print(f"[{slot}/{count}] ready — speak now…", file=log, flush=True)
                if beeps and not dry_run:
                    _ready()
                elif dry_run and beeps:
                    _ready()

                if dry_run:
                    # Synthetic silence “sample” for beep dogfood only
                    if beeps:
                        time.sleep(0.15)
                        _accept()
                    samples.append(
                        EnrollSample(
                            index=slot,
                            path="",
                            duration_ms=0,
                            peak_rms=0.0,
                            accepted=True,
                        )
                    )
                    accepted += 1
                    if gap_s > 0 and accepted < count:
                        time.sleep(gap_s)
                    continue

                try:
                    cap = _capture(
                        sample_rate=16000,
                        max_s=max_utter_s,
                        end_silence_s=end_silence_s,
                        min_speech_s=0.2,
                        preroll_ms=300,
                        initial_timeout_s=initial_timeout_s,
                        post_tts_guard_s=0.0,
                    )
                except TimeoutError:
                    rejected += 1
                    print(f"  timed out waiting for speech — try again", file=log)
                    if beeps:
                        _reject()
                    time.sleep(gap_s)
                    continue
                except Exception as exc:
                    rejected += 1
                    print(f"  capture error: {exc}", file=log)
                    if beeps:
                        _reject()
                    time.sleep(gap_s)
                    continue

                pcm = getattr(cap, "pcm16", b"") or b""
                sr = int(getattr(cap, "sample_rate", 16000) or 16000)
                peak = _pcm_peak_rms(pcm)
                if len(pcm) < 16000 * 0.15 * 2 or peak < MIN_PEAK_RMS:
                    rejected += 1
                    print(
                        f"  rejected (too quiet peak_rms={peak:.4f}) — try again",
                        file=log,
                    )
                    if beeps:
                        _reject()
                    time.sleep(gap_s)
                    continue

                wav_name = f"{slot:02d}.wav"
                wav_path = out_dir / wav_name
                wav_path.write_bytes(write_wav_bytes(pcm, sr))
                dur_ms = int(1000 * len(pcm) / 2 / sr)

                wake_hit = None
                wake_raw = None
                learned_notes: list[str] = []
                if backend is not None:
                    hit, raw = _score_sample(pcm, sr, backend, policy, phrase)
                    wake_raw = raw or None
                    if hit is not None:
                        wake_hit = hit.phrase
                    if raw and isinstance(policy, WakePolicy):
                        learned_notes = _maybe_learn_from_score(
                            raw, policy, learn=learn
                        )
                        if learned_notes:
                            # refresh policy after learn
                            st = load_learned()
                            policy = policy.merge_learned(
                                name_aliases=st.name_aliases,
                                phrase_aliases=st.phrase_aliases,
                            )

                samples.append(
                    EnrollSample(
                        index=slot,
                        path=str(wav_path),
                        duration_ms=dur_ms,
                        peak_rms=peak,
                        accepted=True,
                        wake_hit=wake_hit,
                        wake_raw=wake_raw,
                        learned=learned_notes,
                    )
                )
                accepted += 1
                note = f"  saved {wav_name} ({dur_ms} ms, peak={peak:.3f})"
                if wake_hit:
                    note += f" wake={wake_hit!r}"
                elif wake_raw:
                    note += f" asr={wake_raw!r}"
                if learned_notes:
                    note += f" learned={learned_notes}"
                print(note, file=log)
                if beeps:
                    _accept()
                if gap_s > 0 and accepted < count:
                    time.sleep(gap_s)

    if beeps:
        _end()

    ok = accepted >= min_count
    manifest = {
        "version": 1,
        "phrase": phrase,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": count,
        "min_count": min_count,
        "accepted": accepted,
        "rejected": rejected,
        "dry_run": dry_run,
        "samples": [
            {
                "index": s.index,
                "path": s.path,
                "duration_ms": s.duration_ms,
                "peak_rms": s.peak_rms,
                "accepted": s.accepted,
                "wake_hit": s.wake_hit,
                "wake_raw": s.wake_raw,
                "learned": s.learned,
            }
            for s in samples
        ],
        "cloud_upload": False,
    }
    manifest_path = None
    if not dry_run:
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        # also write latest pointer
        latest = enroll_root() / slug / "latest"
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(out_dir.name)
        except OSError:
            (enroll_root() / slug / "latest.txt").write_text(
                out_dir.name + "\n", encoding="utf-8"
            )

    msg = (
        f"Enrollment complete: {accepted}/{count} accepted "
        f"({rejected} rejected). "
        + (f"manifest={manifest_path}" if manifest_path else "dry-run")
    )
    if not ok:
        msg += f" — below min {min_count}"
    print(msg, file=log)
    syslog(
        "wake_enroll.done",
        component="wake_enroll",
        phrase=phrase,
        accepted=accepted,
        rejected=rejected,
        ok=ok,
        out_dir=str(out_dir) if not dry_run else None,
    )
    return EnrollResult(
        phrase=phrase,
        out_dir=out_dir,
        target=count,
        accepted=accepted,
        rejected=rejected,
        samples=samples,
        manifest_path=manifest_path,
        ok=ok,
        message=msg,
    )


def cmd_wake_enroll(args: Any, cfg: HarkConfig) -> int:
    """CLI entry for ``hark wake-enroll``."""
    result = run_wake_enroll(
        cfg,
        phrase=getattr(args, "phrase", None),
        count=int(getattr(args, "count", DEFAULT_COUNT) or DEFAULT_COUNT),
        min_count=int(getattr(args, "min_count", DEFAULT_MIN) or DEFAULT_MIN),
        learn=not bool(getattr(args, "no_learn", False)),
        score=not bool(getattr(args, "no_score", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        beeps=not bool(getattr(args, "no_beep", False)),
    )
    return OK if result.ok else USAGE

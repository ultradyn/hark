"""Durable transcript log — recover an answer when stdout was lost (B186).

stdout is the primary channel for a captured transcript, but a caller that
truncates or mangles its own stdout used to have no fallback: ``usage.jsonl``
keeps chars/words counts for an STT turn and no body, and the listen state dir
keeps only ``*.cmd`` control files. A dropped answer therefore meant asking the
operator to repeat themselves. Every turn that produced a transcript is
appended here verbatim, so recovery is ``hark transcripts --last``.

Path: ``$XDG_STATE_HOME/hark/transcripts.jsonl`` (``HARK_TRANSCRIPT_LOG``
overrides). This file holds operator speech, so it is created ``0600``, its
records carry no credentials or TTS telemetry, and it is not one of the
``hark watch-logs`` sources. Turn it off with ``HARK_NO_TRANSCRIPT_LOG=1``.

Writes are best-effort and never raise: a logging fault must not break a
voice turn.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from hark.paths import state_dir

#: Defensive per-record cap. The transcript body is never *usefully* truncated
#: — losing it is the bug this module fixes — so this is far above any real
#: utterance (~100k chars is hours of speech) and marks itself when it bites.
TEXT_MAX_CHARS = 100_000

#: The prompt is context for identifying a record, not the payload to preserve.
PROMPT_PREVIEW_CHARS = 300

#: Rotation threshold; one previous generation is kept as ``…jsonl.1``.
DEFAULT_MAX_BYTES = 4 * 1024 * 1024

_TRUTHY = ("1", "true", "yes", "on")


def transcript_log_path() -> Path:
    override = os.environ.get("HARK_TRANSCRIPT_LOG")
    if override:
        return Path(override)
    return state_dir() / "transcripts.jsonl"


def logging_disabled() -> bool:
    return os.environ.get("HARK_NO_TRANSCRIPT_LOG", "").strip().lower() in _TRUTHY


def max_bytes() -> int:
    """Rotation threshold; ``HARK_TRANSCRIPT_MAX_BYTES=0`` disables rotation."""
    raw = os.environ.get("HARK_TRANSCRIPT_MAX_BYTES")
    if raw is None:
        return DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw.strip()))
    except ValueError:
        return DEFAULT_MAX_BYTES


def _pick(payload: dict[str, Any], key: str) -> Any:
    """Read *key* from the payload, or from its nested ``listen`` block.

    ``listen`` and ``ask`` carry capture fields at the top level;
    ``tts --listen`` nests them under ``listen`` beside the TTS result.
    """
    if payload.get(key) is not None:
        return payload[key]
    nested = payload.get("listen")
    if isinstance(nested, dict):
        return nested.get(key)
    return None


def _spoke(payload: dict[str, Any]) -> tuple[Any, Any]:
    """(provider that actually spoke, provider originally asked if it differs).

    B177: TTS can fall back mid-turn, so the requested provider is not
    necessarily the one the operator heard. A transcript that recorded the
    request would misreport the turn — and this log is a diagnostic people
    trust — so the spoken provider is recorded, plus the requested one whenever
    a fallback moved it.
    """
    tts = payload.get("tts")
    if not isinstance(tts, dict):
        return None, None
    spoke = tts.get("provider")
    fallback = tts.get("fallback")
    requested = fallback.get("requested") if isinstance(fallback, dict) else None
    if requested == spoke or spoke is None:
        # "fell back from X" with no "to" is an incoherent record for whoever
        # reads this log later, so a missing spoken provider drops the pair
        # rather than half of it. Only reachable on a malformed `tts` block.
        requested = None
    return spoke, requested


def _preview(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def build_record(
    payload: dict[str, Any],
    *,
    kind: str,
    prompt: str | None = None,
) -> dict[str, Any] | None:
    """One transcript record, or None when there is no transcript to keep."""
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    now = time.time()
    tts_provider, tts_fallback_from = _spoke(payload)
    rec: dict[str, Any] = {
        "ts": now,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "kind": kind,
        "ok": bool(payload.get("ok")),
        "text": text[:TEXT_MAX_CHARS],
        "chars": len(text),
        "words": len([w for w in text.split() if w]),
        "provider": _pick(payload, "provider"),
        # The heard leg is `provider`; these are the spoken one (B177).
        "tts_provider": tts_provider,
        "tts_fallback_from": tts_fallback_from,
        "duration_ms": _pick(payload, "duration_ms"),
        "end_mode": _pick(payload, "end_mode"),
        "end_phrase": _pick(payload, "end_phrase"),
        "stream_id": _pick(payload, "stream_id"),
        "meta_command": payload.get("meta_command"),
        "for_event": payload.get("for_event"),
        "pid": os.getpid(),
    }
    if payload.get("cancelled"):
        rec["cancelled"] = True
    if len(text) > TEXT_MAX_CHARS:
        rec["text_truncated"] = True
    if prompt and prompt.strip():
        rec["prompt"] = _preview(prompt, PROMPT_PREVIEW_CHARS)
    return {k: v for k, v in rec.items() if v is not None}


def record_turn(
    payload: dict[str, Any],
    *,
    kind: str,
    prompt: str | None = None,
    path: Path | None = None,
) -> bool:
    """Append one completed turn. Returns True when a record was written.

    Turns with no transcript body (a TTS or provider failure before capture)
    are skipped — there is nothing to recover. Never raises.
    """
    try:
        if logging_disabled():
            return False
        rec = build_record(payload, kind=kind, prompt=prompt)
        if rec is None:
            return False
        line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
        target = path or transcript_log_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        _rotate_if_full(target, len(line.encode("utf-8")))
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except Exception:
        # Logging must never break the product path.
        return False


def _rotate_if_full(path: Path, incoming_bytes: int) -> None:
    """Keep one previous generation; drop anything older.

    Concurrent hark processes can both decide to rotate, in which case one
    generation of history is lost. That is the right trade for a best-effort
    recovery log: the newest turn — the one a caller is about to need — is
    always in the live file.
    """
    limit = max_bytes()
    if limit <= 0:
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size + incoming_bytes <= limit:
        return
    path.replace(path.with_name(path.name + ".1"))


def tail_records(n: int = 5, path: Path | None = None) -> list[dict[str, Any]]:
    """The last *n* records, oldest first.

    Reads the live log only, so a just-rotated log can hold fewer than *n*.
    """
    if n <= 0:
        return []
    from hark.syslog import tail_lines

    return tail_lines(n, path or transcript_log_path())

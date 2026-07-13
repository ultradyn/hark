"""TTS/STT usage stats (local JSONL under state dir)."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hark.paths import state_dir


def _word_count(text: str) -> int:
    return len([w for w in (text or "").split() if w])


@dataclass
class UsageEvent:
    kind: str  # tts | stt
    ts: float = field(default_factory=time.time)
    provider: str | None = None
    voice: str | None = None
    ok: bool = True
    chars: int = 0
    words: int = 0
    audio_ms: int = 0  # synthesized or captured audio duration
    latency_ms: int = 0  # wall time for API call if known
    error: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class UsageStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (state_dir() / "usage.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: UsageEvent) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(event), separators=(",", ":")) + "\n")
        # Mirror into unified system timeline
        try:
            from hark.syslog import log

            log(
                f"{event.kind}.{'ok' if event.ok else 'error'}",
                component=event.kind,
                level="info" if event.ok else "error",
                message=event.error or event.kind,
                provider=event.provider,
                voice=event.voice,
                chars=event.chars,
                words=event.words,
                audio_ms=event.audio_ms,
                latency_ms=event.latency_ms,
                ok=event.ok,
                **(event.meta or {}),
            )
        except Exception:
            pass

    def record_tts(
        self,
        *,
        text: str,
        provider: str | None,
        voice: str | None,
        audio_ms: int = 0,
        latency_ms: int = 0,
        ok: bool = True,
        error: str | None = None,
        meta: dict | None = None,
    ) -> None:
        self.record(
            UsageEvent(
                kind="tts",
                provider=provider,
                voice=voice,
                ok=ok,
                chars=len(text or ""),
                words=_word_count(text or ""),
                audio_ms=audio_ms,
                latency_ms=latency_ms,
                error=error,
                meta=meta or {},
            )
        )

    def record_stt(
        self,
        *,
        text: str,
        provider: str | None,
        audio_ms: int = 0,
        latency_ms: int = 0,
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        self.record(
            UsageEvent(
                kind="stt",
                provider=provider,
                ok=ok,
                chars=len(text or ""),
                words=_word_count(text or ""),
                audio_ms=audio_ms,
                latency_ms=latency_ms,
                error=error,
            )
        )

    def iter_events(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def summary(self) -> dict[str, Any]:
        events = self.iter_events()
        return {
            "path": str(self.path),
            "tts": _agg([e for e in events if e.get("kind") == "tts"]),
            "stt": _agg([e for e in events if e.get("kind") == "stt"]),
            "total_events": len(events),
        }


def _agg(events: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(events)
    ok_n = sum(1 for e in events if e.get("ok", True))
    chars = sum(int(e.get("chars") or 0) for e in events)
    words = sum(int(e.get("words") or 0) for e in events)
    audio_ms = sum(int(e.get("audio_ms") or 0) for e in events)
    latency_ms = sum(int(e.get("latency_ms") or 0) for e in events)
    by_provider: dict[str, int] = {}
    empty_n = 0
    for e in events:
        p = str(e.get("provider") or "unknown")
        by_provider[p] = by_provider.get(p, 0) + 1
        err = (e.get("error") or "").lower()
        if "empty transcript" in err:
            empty_n += 1
    out = {
        "instances": n,
        "ok": ok_n,
        "errors": n - ok_n,
        "total_chars": chars,
        "total_words": words,
        "avg_chars": round(chars / n, 2) if n else 0,
        "avg_words": round(words / n, 2) if n else 0,
        "total_audio_ms": audio_ms,
        "total_audio_s": round(audio_ms / 1000.0, 3) if audio_ms else 0,
        "avg_audio_ms": round(audio_ms / n, 1) if n else 0,
        "total_latency_ms": latency_ms,
        "avg_latency_ms": round(latency_ms / n, 1) if n else 0,
        "by_provider": by_provider,
    }
    # Empty STT rate is meaningful for STT events only
    if any(e.get("kind") == "stt" for e in events) or empty_n:
        out["empty_transcript"] = empty_n
        out["empty_stt_rate"] = round(empty_n / n, 4) if n else 0.0
    return out

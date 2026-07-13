"""Playback helpers — WAV/PCM via sounddevice; MP3/other via ffplay/ffmpeg."""

from __future__ import annotations

import atexit
import io
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None  # type: ignore


@dataclass
class PlayResult:
    duration_ms: int
    format: str


# Cross-process FIFO speaker: synth may run in parallel; play is serial (B092).
# Ticket is claimed at *launch* (before synth) so N concurrent jobs keep order.
# B099: holders track pid+claim time so dead processes cannot stall the queue.
_play_tls = threading.local()
_play_lock_name = "tts_play.lock"
_play_queue_name = "tts_play_queue.json"

# After this many seconds waiting for a head with no holder (legacy/killed
# pre-B099), treat the head as abandoned. Live PIDs are never aged out here —
# long multi-chunk TTS can hold the speaker for minutes.
_MISSING_HOLDER_GRACE_S = 8.0
# Absolute safety: abandon a holder whose claim is older than this *and* whose
# PID is dead (redundant with pid check) — kept for documentation/tests.
_STALE_HOLDER_AGE_S = 600.0

_our_tickets: set[int] = set()
_our_tickets_lock = threading.Lock()
_cleanup_hooks_installed = False


def tts_play_lock_path() -> Path:
    from hark.paths import state_dir

    return state_dir() / _play_lock_name


def tts_play_queue_path() -> Path:
    from hark.paths import state_dir

    return state_dir() / _play_queue_name


def _pid_alive(pid: int) -> bool:
    """True if *pid* looks like a live (non-zombie) process."""
    if pid <= 0:
        return False
    try:
        from hark.daemon import pid_alive

        return bool(pid_alive(pid))
    except Exception:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True


def _syslog_tts_queue(event: str, **fields: Any) -> None:
    try:
        from hark.syslog import log as syslog

        syslog(event, component="tts", level=fields.pop("level", "warn"), **fields)
    except Exception:
        pass


def _empty_queue_state() -> dict[str, object]:
    return {"next": 0, "serving": 0, "cancelled": [], "holders": {}}


def _normalize_holders(raw: object) -> dict[str, dict[str, object]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, object]] = {}
    for k, v in raw.items():
        try:
            ticket_s = str(int(k))
        except (TypeError, ValueError):
            continue
        if not isinstance(v, dict):
            continue
        try:
            pid = int(v.get("pid", 0) or 0)
        except (TypeError, ValueError):
            pid = 0
        try:
            claimed_at = float(v.get("claimed_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            claimed_at = 0.0
        out[ticket_s] = {"pid": pid, "claimed_at": claimed_at}
    return out


def _queue_read(path: Path) -> dict[str, object]:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cancelled = data.get("cancelled") or []
        return {
            "next": int(data.get("next", 0)),
            "serving": int(data.get("serving", 0)),
            "cancelled": [int(x) for x in cancelled],
            "holders": _normalize_holders(data.get("holders")),
        }
    except Exception:
        return _empty_queue_state()


def _queue_write(path: Path, state: dict[str, object]) -> None:
    import json

    cancelled = sorted({int(x) for x in (state.get("cancelled") or [])})
    # Drop cancelled tickets already behind the head
    serving = int(state["serving"])
    cancelled = [c for c in cancelled if c >= serving]
    holders = _normalize_holders(state.get("holders"))
    # Drop holders already behind the head
    holders = {k: v for k, v in holders.items() if int(k) >= serving}
    path.write_text(
        json.dumps(
            {
                "next": int(state["next"]),
                "serving": serving,
                "cancelled": cancelled,
                "holders": holders,
            }
        ),
        encoding="utf-8",
    )


def _drop_holder(st: dict[str, object], ticket: int) -> None:
    holders = _normalize_holders(st.get("holders"))
    holders.pop(str(int(ticket)), None)
    st["holders"] = holders


def _set_holder(st: dict[str, object], ticket: int, *, pid: int, claimed_at: float) -> None:
    holders = _normalize_holders(st.get("holders"))
    holders[str(int(ticket))] = {"pid": int(pid), "claimed_at": float(claimed_at)}
    st["holders"] = holders


def _skip_cancelled_heads(st: dict[str, object]) -> dict[str, object]:
    """Advance serving past any tickets marked cancelled."""
    cancelled = set(int(x) for x in (st.get("cancelled") or []))
    serving = int(st["serving"])
    while serving in cancelled:
        cancelled.discard(serving)
        _drop_holder(st, serving)
        serving += 1
    st["serving"] = serving
    st["cancelled"] = sorted(cancelled)
    return st


def _advance_serving(st: dict[str, object]) -> dict[str, object]:
    """Move serving past the current head and any following cancelled tickets."""
    serving = int(st["serving"])
    _drop_holder(st, serving)
    st["serving"] = serving + 1
    return _skip_cancelled_heads(st)


def _holder_abandoned(
    holder: dict[str, object] | None,
    *,
    missing_as_abandoned: bool,
    now: float,
) -> str | None:
    """Return abandon reason or None if the holder still owns the ticket."""
    if holder is None:
        return "missing_holder" if missing_as_abandoned else None
    try:
        pid = int(holder.get("pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid > 0 and not _pid_alive(pid):
        # Prefer dead_pid; stale_age only when claim timestamp is ancient
        try:
            claimed_at = float(holder.get("claimed_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            claimed_at = 0.0
        if claimed_at > 0 and (now - claimed_at) > _STALE_HOLDER_AGE_S:
            return "stale_age"
        return "dead_pid"
    if pid <= 0:
        return "missing_holder" if missing_as_abandoned else None
    # Live PID: never age-out (long multi-chunk play is legitimate).
    return None


def _heal_abandoned_locked(
    st: dict[str, object],
    *,
    missing_as_abandoned: bool = False,
    now: float | None = None,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Advance serving past cancelled + abandoned heads. Caller holds flock."""
    now = time.time() if now is None else now
    st = _skip_cancelled_heads(st)
    healed: list[dict[str, object]] = []
    holders = _normalize_holders(st.get("holders"))
    st["holders"] = holders

    while int(st["serving"]) < int(st["next"]):
        serving = int(st["serving"])
        holder = holders.get(str(serving))
        reason = _holder_abandoned(
            holder, missing_as_abandoned=missing_as_abandoned, now=now
        )
        if reason is None:
            break
        entry: dict[str, object] = {
            "ticket": serving,
            "reason": reason,
            "pid": int((holder or {}).get("pid", 0) or 0) if holder else 0,
        }
        healed.append(entry)
        holders.pop(str(serving), None)
        st["holders"] = holders
        st["serving"] = serving + 1
        st = _skip_cancelled_heads(st)
        holders = _normalize_holders(st.get("holders"))
        st["holders"] = holders

    # Prune holders behind head
    serving = int(st["serving"])
    st["holders"] = {k: v for k, v in holders.items() if int(k) >= serving}
    return st, healed


def _track_our_ticket(ticket: int) -> None:
    with _our_tickets_lock:
        _our_tickets.add(int(ticket))
    _ensure_cleanup_hooks()


def _untrack_our_ticket(ticket: int) -> None:
    with _our_tickets_lock:
        _our_tickets.discard(int(ticket))


def _abandon_our_tickets() -> None:
    """Best-effort abandon of tickets still claimed by this process (B099)."""
    with _our_tickets_lock:
        tickets = list(_our_tickets)
    for ticket in tickets:
        try:
            abandon_tts_play_ticket(ticket)
        except Exception:
            pass


def _ensure_cleanup_hooks() -> None:
    """Install atexit + SIGTERM chain so claimed tickets are abandoned on exit."""
    global _cleanup_hooks_installed
    if _cleanup_hooks_installed:
        return
    _cleanup_hooks_installed = True
    atexit.register(_abandon_our_tickets)
    try:
        prev = signal.getsignal(signal.SIGTERM)

        def _on_sigterm(signum: int, frame: object) -> None:
            _abandon_our_tickets()
            if callable(prev) and prev not in (signal.SIG_DFL, signal.SIG_IGN):
                prev(signum, frame)  # type: ignore[operator]
            else:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                os.kill(os.getpid(), signal.SIGTERM)

        signal.signal(signal.SIGTERM, _on_sigterm)
    except Exception:
        # Restricted environments / non-main thread — atexit + PID heal remain.
        pass


def claim_tts_play_ticket() -> int:
    """Reserve FIFO place *before* synth so launch order is preserved (B092).

    Call once per outer utterance (not per multi-chunk). Then
    :func:`exclusive_playback` with that ticket. On failure before play, call
    :func:`abandon_tts_play_ticket` so the queue cannot stall.

    Records ``pid`` + ``claimed_at`` on the ticket (B099) so other waiters can
    advance past a dead holder.
    """
    import fcntl

    lock_path = tts_play_lock_path()
    queue_path = tts_play_queue_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = _queue_read(queue_path)
        # Opportunistic heal of clearly dead heads before taking a new ticket
        st, healed = _heal_abandoned_locked(st, missing_as_abandoned=False)
        if healed:
            for h in healed:
                _syslog_tts_queue(
                    "tts.play_queue_healed",
                    ticket=h.get("ticket"),
                    reason=h.get("reason"),
                    pid=h.get("pid"),
                    where="claim",
                )
        ticket = int(st["next"])
        st["next"] = ticket + 1
        if int(st["serving"]) > int(st["next"]):
            st["serving"] = ticket
        _set_holder(st, ticket, pid=os.getpid(), claimed_at=time.time())
        _queue_write(queue_path, st)
        _track_our_ticket(ticket)
        return ticket
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def abandon_tts_play_ticket(ticket: int) -> None:
    """Drop a claimed ticket without playing (synth error / early return / exit)."""
    import fcntl

    ticket = int(ticket)
    lock_path = tts_play_lock_path()
    queue_path = tts_play_queue_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = _queue_read(queue_path)
        _drop_holder(st, ticket)
        if int(st["serving"]) == ticket:
            _queue_write(queue_path, _advance_serving(st))
        else:
            cancelled = list(st.get("cancelled") or [])
            if ticket not in cancelled:
                cancelled.append(ticket)
            st["cancelled"] = cancelled
            # If cancelled ticket is somehow at head, skip it
            _queue_write(queue_path, _skip_cancelled_heads(st))
    finally:
        _untrack_our_ticket(ticket)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def heal_tts_play_queue(*, missing_as_abandoned: bool = True) -> dict[str, Any]:
    """Detect abandoned tickets and advance ``serving`` (B099).

    *missing_as_abandoned*: treat tickets with no holder record (legacy queue or
    crash before holder write) as abandoned. Safe for doctor/ambient startup;
    waiters use a grace period before enabling this.

    Returns a status dict suitable for doctor / syslog.
    """
    import fcntl

    lock_path = tts_play_lock_path()
    queue_path = tts_play_queue_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    before = _queue_read(queue_path) if queue_path.is_file() else _empty_queue_state()
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = _queue_read(queue_path)
        st, healed = _heal_abandoned_locked(
            st, missing_as_abandoned=missing_as_abandoned
        )
        if healed or int(st["serving"]) != int(before.get("serving", 0)):
            _queue_write(queue_path, st)
        for h in healed:
            _syslog_tts_queue(
                "tts.play_queue_healed",
                ticket=h.get("ticket"),
                reason=h.get("reason"),
                pid=h.get("pid"),
                where="heal",
                level="warn",
            )
        stuck = int(st["serving"]) < int(st["next"])
        return {
            "path": str(queue_path),
            "serving": int(st["serving"]),
            "next": int(st["next"]),
            "pending": max(0, int(st["next"]) - int(st["serving"])),
            "cancelled": list(st.get("cancelled") or []),
            "holders": _normalize_holders(st.get("holders")),
            "healed": healed,
            "healed_count": len(healed),
            "stuck": stuck and not healed,
            "ok": True,
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "path": str(queue_path),
            "ok": False,
            "error": str(exc)[:200],
            "healed": [],
            "healed_count": 0,
            "stuck": False,
        }
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(fd)


def inspect_tts_play_queue() -> dict[str, Any]:
    """Read queue status without healing (tests / diagnostics)."""
    queue_path = tts_play_queue_path()
    if not queue_path.is_file():
        st = _empty_queue_state()
    else:
        st = _queue_read(queue_path)
    pending = max(0, int(st["next"]) - int(st["serving"]))
    return {
        "path": str(queue_path),
        "serving": int(st["serving"]),
        "next": int(st["next"]),
        "pending": pending,
        "cancelled": list(st.get("cancelled") or []),
        "holders": _normalize_holders(st.get("holders")),
        "exists": queue_path.is_file(),
    }


@contextmanager
def exclusive_playback(
    ticket: int | None = None,
    *,
    wait_timeout_s: float | None = None,
) -> Iterator[None]:
    """Hold the global TTS speaker for *ticket* (FIFO). Re-entrant same thread.

    Prefer claiming with :func:`claim_tts_play_ticket` **before** synthesize so
    five concurrent ``hark tts`` keep launch order even if synth finishes out
    of order. If *ticket* is None, claim now (play-time claim).

    While waiting for our turn, dead-PID heads are auto-healed (B099). After
    :data:`_MISSING_HOLDER_GRACE_S`, heads with no holder record are also
    skipped (legacy abandoned tickets).

    *wait_timeout_s*: if set, raise ``TimeoutError`` after this many seconds
    waiting for the speaker (ticket is abandoned so the queue does not stall).
    Use a short timeout for ambient boot TTS so wake arming is never blocked.
    """
    depth = int(getattr(_play_tls, "depth", 0) or 0)
    if depth > 0:
        _play_tls.depth = depth + 1
        try:
            yield
        finally:
            _play_tls.depth = depth
        return

    import fcntl

    if ticket is None:
        ticket = claim_tts_play_ticket()

    lock_path = tts_play_lock_path()
    queue_path = tts_play_queue_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    advanced = False
    wait_start = time.monotonic()
    try:
        # Wait until we are head of line, then hold lock through playback
        while True:
            fcntl.flock(fd, fcntl.LOCK_EX)
            elapsed = time.monotonic() - wait_start
            missing = elapsed >= _MISSING_HOLDER_GRACE_S
            st = _queue_read(queue_path)
            st, healed = _heal_abandoned_locked(
                st, missing_as_abandoned=missing
            )
            st = _skip_cancelled_heads(st)
            if healed:
                for h in healed:
                    _syslog_tts_queue(
                        "tts.play_queue_healed",
                        ticket=h.get("ticket"),
                        reason=h.get("reason"),
                        pid=h.get("pid"),
                        where="wait",
                        waiter=ticket,
                    )
            _queue_write(queue_path, st)
            if int(st["serving"]) == ticket:
                break
            if wait_timeout_s is not None and elapsed >= wait_timeout_s:
                fcntl.flock(fd, fcntl.LOCK_UN)
                try:
                    abandon_tts_play_ticket(ticket)
                except Exception:
                    pass
                advanced = True  # abandon already advanced/cancelled
                _syslog_tts_queue(
                    "tts.play_queue_wait_timeout",
                    ticket=ticket,
                    wait_timeout_s=wait_timeout_s,
                    serving=int(st["serving"]),
                    next=int(st["next"]),
                    level="warn",
                )
                raise TimeoutError(
                    f"tts play queue wait exceeded {wait_timeout_s}s "
                    f"(ticket={ticket}, serving={st['serving']})"
                )
            fcntl.flock(fd, fcntl.LOCK_UN)
            time.sleep(0.03)

        _play_tls.depth = 1
        try:
            yield
        finally:
            _play_tls.depth = 0
            st = _queue_read(queue_path)
            if int(st["serving"]) == ticket:
                _queue_write(queue_path, _advance_serving(st))
                advanced = True
            _untrack_our_ticket(ticket)
            fcntl.flock(fd, fcntl.LOCK_UN)
    except BaseException:
        if not advanced:
            try:
                abandon_tts_play_ticket(ticket)
            except Exception:
                pass
        raise
    finally:
        os.close(fd)


def sniff_audio_format(data: bytes) -> str:
    """Return a short format tag: wav | mp3 | ogg | flac | pcm | unknown."""
    if len(data) < 4:
        return "unknown"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:3] == b"ID3":
        return "mp3"
    if data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:4] == b"OggS":
        return "ogg"
    return "pcm"


def estimate_duration_ms(data: bytes, sample_rate: int | None = None) -> int:
    """Best-effort duration from bytes (WAV exact; else ffprobe; else bitrate guess)."""
    fmt = sniff_audio_format(data)
    if fmt == "wav":
        try:
            with wave.open(io.BytesIO(data), "rb") as wf:
                return int(1000 * wf.getnframes() / max(1, wf.getframerate()))
        except Exception:
            pass
    if fmt == "pcm" and sample_rate:
        # 16-bit mono
        return int(1000 * (len(data) / 2) / sample_rate)
    # ffprobe
    if shutil.which("ffprobe"):
        with tempfile.NamedTemporaryFile(suffix=f".{fmt if fmt != 'unknown' else 'bin'}", delete=False) as tmp:
            p = Path(tmp.name)
        try:
            p.write_bytes(data)
            r = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(p),
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return max(0, int(float(r.stdout.strip()) * 1000))
        except Exception:
            pass
        finally:
            p.unlink(missing_ok=True)
    # MP3 crude: assume ~16 KB/s at 128kbps
    if fmt == "mp3" and len(data) > 0:
        return int(1000 * len(data) / 16000)
    return 0


def write_audio(path: Path | str, data: bytes, *, sample_rate: int = 16000) -> Path:
    path = Path(path)
    fmt = sniff_audio_format(data)
    suffix = path.suffix.lower()

    if fmt == "wav" or (fmt == "pcm" and suffix in (".wav", "")):
        if fmt == "wav":
            path.write_bytes(data)
        else:
            path.write_bytes(_pcm16_to_wav(data, sample_rate))
        return path

    if fmt == "mp3" and suffix in (".mp3", ".mpeg", ""):
        if not suffix:
            path = path.with_suffix(".mp3")
        path.write_bytes(data)
        return path

    if suffix == ".wav" or not suffix:
        if not suffix:
            path = path.with_suffix(".wav")
        return _ffmpeg_convert(data, path, fmt)

    path.write_bytes(data)
    return path


def write_wav(path: Path | str, pcm_or_wav: bytes, sample_rate: int = 16000) -> Path:
    return write_audio(path, pcm_or_wav, sample_rate=sample_rate)


def play_audio(
    data: bytes,
    *,
    sample_rate: int | None = None,
    on_near_end: Callable[[], None] | None = None,
    near_end_ms: int = 0,
    exclusive: bool = True,
) -> PlayResult:
    """Play audio. Optional on_near_end fires ~near_end_ms before playback ends.

    Used so listen can arm ~0.3s before TTS finishes.

    ``exclusive`` (default True): take the cross-process TTS play lock (B092) so
    concurrent speakers queue. Pass False only when the caller already holds
    :func:`exclusive_playback`.
    """
    if exclusive:
        with exclusive_playback():
            return _play_audio_unlocked(
                data,
                sample_rate=sample_rate,
                on_near_end=on_near_end,
                near_end_ms=near_end_ms,
            )
    return _play_audio_unlocked(
        data,
        sample_rate=sample_rate,
        on_near_end=on_near_end,
        near_end_ms=near_end_ms,
    )


def _play_audio_unlocked(
    data: bytes,
    *,
    sample_rate: int | None = None,
    on_near_end: Callable[[], None] | None = None,
    near_end_ms: int = 0,
) -> PlayResult:
    fmt = sniff_audio_format(data)
    duration_ms = estimate_duration_ms(data, sample_rate)

    def _maybe_schedule_near_end() -> threading.Timer | None:
        if not on_near_end or near_end_ms <= 0 or duration_ms <= 0:
            return None
        delay = max(0.0, (duration_ms - near_end_ms) / 1000.0)
        t = threading.Timer(delay, on_near_end)
        t.daemon = True
        t.start()
        return t

    timer = _maybe_schedule_near_end()
    t0 = time.monotonic()
    try:
        if fmt == "wav":
            pcm, sr = _wav_to_pcm16(data)
            _play_pcm16(pcm, sr)
        elif fmt == "pcm":
            _play_pcm16(data, sample_rate or 24000)
        else:
            ext = ".mp3" if fmt == "mp3" else f".{fmt}"
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                p = Path(tmp.name)
            try:
                p.write_bytes(data)
                _play_file(p)
            finally:
                p.unlink(missing_ok=True)
    finally:
        if timer is not None:
            timer.cancel()

    wall_ms = int(1000 * (time.monotonic() - t0))
    return PlayResult(duration_ms=duration_ms or wall_ms, format=fmt)


def play_wav_bytes(
    data: bytes,
    *,
    sample_rate: int | None = None,
    on_near_end: Callable[[], None] | None = None,
    near_end_ms: int = 0,
    exclusive: bool = True,
) -> PlayResult:
    return play_audio(
        data,
        sample_rate=sample_rate,
        on_near_end=on_near_end,
        near_end_ms=near_end_ms,
        exclusive=exclusive,
    )


def _play_pcm16(pcm: bytes, sample_rate: int) -> None:
    if sd is not None:
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if samples.size == 0:
            return
        sd.play(samples, sample_rate)
        sd.wait()
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        p = Path(tmp.name)
    try:
        p.write_bytes(_pcm16_to_wav(pcm, sample_rate))
        _play_file(p)
    finally:
        p.unlink(missing_ok=True)


def _play_file(path: Path) -> None:
    for cmd in (
        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error", str(path)],
        ["paplay", str(path)],
        ["aplay", str(path)],
    ):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    if shutil.which("ffmpeg") and sd is not None:
        wav_path = path.with_suffix(".decoded.wav")
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(path),
                    "-ac",
                    "1",
                    "-f",
                    "wav",
                    str(wav_path),
                ],
                check=True,
                capture_output=True,
            )
            pcm, sr = _wav_to_pcm16(wav_path.read_bytes())
            _play_pcm16(pcm, sr)
            return
        except (subprocess.CalledProcessError, OSError):
            pass
        finally:
            wav_path.unlink(missing_ok=True)
    raise RuntimeError(
        "could not play audio — need ffplay/ffmpeg for MP3 TTS, or WAV input"
    )


def _ffmpeg_convert(data: bytes, dest: Path, fmt: str) -> Path:
    if not shutil.which("ffmpeg"):
        alt = dest.with_suffix(".mp3" if fmt == "mp3" else dest.suffix or ".bin")
        alt.write_bytes(data)
        return alt
    with tempfile.NamedTemporaryFile(suffix=f".{fmt}", delete=False) as tmp:
        src = Path(tmp.name)
    try:
        src.write_bytes(data)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ac", "1", str(dest)],
            check=True,
            capture_output=True,
        )
        return dest
    finally:
        src.unlink(missing_ok=True)


def _pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _wav_to_pcm16(data: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(data), "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        width = wf.getsampwidth()
        ch = wf.getnchannels()
        if width != 2:
            raise RuntimeError(f"unsupported WAV sample width {width}")
        if ch > 1:
            mono = bytearray()
            step = width * ch
            for i in range(0, len(frames), step):
                mono.extend(frames[i : i + width])
            frames = bytes(mono)
        return frames, sr

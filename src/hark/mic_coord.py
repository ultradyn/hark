"""Coordinate mic ownership between ambient wake and bound listen/ask.

Ambient holds a continuous capture stream (MicLease + ring buffer) while
scanning for wake phrases. Handsfree answer flows (listen / ask / tts --listen)
need exclusive access. Cooperative protocol:

  1. Listener writes ``state/ambient.pause``
  2. Ambient closes the continuous stream (releases lease) when pause is set
  3. Listener acquires ``MicLease``, records, then clears the pause file

Max wait for ambient to yield is one hop (~0.5–1 s of 20 ms reads) plus poll
time — not a full snippet open/close cycle.

B097: TTS must not mute/play over an open listen/radio capture. Helpers below
detect active user capture and let ``run_tts`` defer playback until the stream
finalizes (or a max-wait cap elapses).

B105: when ``[ambient].streaming`` is on, live acks may play during a still-open
radio capture **only** after ~``streaming_ack_min_quiet_s`` (default 2s) of
operator quiet — continuous speech without that pause keeps TTS on HOLD until
a pause or the stream ends.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from hark.audio.capture import MicBusyError, MicLease
from hark.paths import state_dir
from hark.syslog import log as syslog

DEFAULT_YIELD_TIMEOUT_S = 15.0
_POLL_S = 0.05

# ambient.pause reasons that mean bound speech capture (not wake-enroll etc.)
_LISTEN_PAUSE_REASONS = frozenset({"listen", "ask", "tts-listen", "radio"})

DEFAULT_DEFER_MAX_WAIT_S = 45.0
DEFAULT_DEFER_POLL_MS = 100
DEFAULT_DEFER_QUIET_MS = 200


def pause_path() -> Path:
    return state_dir() / "ambient.pause"


def ambient_pause_requested() -> bool:
    return pause_path().is_file()


def read_ambient_pause() -> dict[str, Any] | None:
    """Parse ``state/ambient.pause`` if present."""
    path = pause_path()
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def request_ambient_pause(
    *,
    reason: str = "listen",
    pid: int | None = None,
) -> Path:
    path = pause_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "reason": reason,
        "pid": pid if pid is not None else os.getpid(),
        "requested_at": time.time(),
    }
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    syslog(
        "ambient.pause_request",
        component="mic",
        level="info",
        reason=reason,
        pid=payload["pid"],
    )
    return path


def clear_ambient_pause() -> None:
    path = pause_path()
    try:
        if path.is_file():
            path.unlink(missing_ok=True)
            syslog("ambient.pause_clear", component="mic", level="info")
    except OSError:
        pass


def wait_for_mic_free(*, timeout_s: float = DEFAULT_YIELD_TIMEOUT_S) -> None:
    """Block until no other process holds mic.lock (or timeout)."""
    deadline = time.monotonic() + max(0.1, timeout_s)
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            # Probe: acquire and immediately release
            with MicLease("mic-probe"):
                return
        except MicBusyError as exc:
            last_err = exc
            time.sleep(_POLL_S)
    raise MicBusyError(
        f"mic still busy after {timeout_s:.1f}s waiting for ambient to yield"
        + (f" ({last_err})" if last_err else "")
    )


@contextmanager
def pause_ambient_for_mic(
    *,
    reason: str = "listen",
    timeout_s: float = DEFAULT_YIELD_TIMEOUT_S,
) -> Iterator[None]:
    """Ask ambient to yield the mic, wait until free, clear pause on exit.

    Use around bound listen/ask so ambient wake scanning does not raise mic busy.
    """
    request_ambient_pause(reason=reason)
    try:
        wait_for_mic_free(timeout_s=timeout_s)
        yield
    finally:
        clear_ambient_pause()


def _pid_alive(pid: int | None) -> bool:
    """True if *pid* is missing (unknown) or still a live process."""
    if pid is None:
        return True
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return True
    if pid_i <= 0:
        return True
    try:
        os.kill(pid_i, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not signalable by us — treat as alive.
        return True
    except OSError:
        return False


def _is_own_pid(pid: Any, me: int | None) -> bool:
    if me is None or pid is None:
        return False
    try:
        return int(pid) == int(me)
    except (TypeError, ValueError):
        return False


def _listen_like_reason(reason: str | None) -> bool:
    r = (reason or "").strip().lower()
    if not r:
        return False
    if r in _LISTEN_PAUSE_REASONS:
        return True
    # e.g. "listen-radio", "listen_ask"
    return r.startswith("listen")


@dataclass(frozen=True)
class UserCaptureState:
    """Whether bound user speech capture is open (listen/radio)."""

    active: bool
    reason: str | None = None
    sources: tuple[str, ...] = ()
    stream_id: str | None = None
    mode: str | None = None
    pid: int | None = None

    def as_meta(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "reason": self.reason,
            "sources": list(self.sources),
            "stream_id": self.stream_id,
            "mode": self.mode,
            "pid": self.pid,
        }


def user_capture_active(*, ignore_own_pid: bool = True) -> UserCaptureState:
    """Detect open listen/radio capture that TTS must not interrupt (B097).

    Signals (any):
      - ``state/listen/active.json`` with a live (or unknown) owner PID
      - ``state/ambient.pause`` with a listen-like reason and live owner PID

    Same-process capture is ignored when ``ignore_own_pid`` is True so in-listen
    nudge TTS (``run_listen`` → ``run_tts``) does not wait on itself forever.
    Dead PIDs are fail-open (stale markers do not block speech).
    """
    me = os.getpid() if ignore_own_pid else None
    sources: list[str] = []
    reason: str | None = None
    stream_id: str | None = None
    mode: str | None = None
    owner_pid: int | None = None

    try:
        from hark.listen_control import read_active

        active = read_active()
    except Exception:
        active = None

    if active:
        raw_pid = active.get("pid")
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            pid = None
        if not _is_own_pid(pid, me) and _pid_alive(pid):
            sources.append("listen.active")
            stream_id = str(active.get("stream_id") or "") or None
            mode = str(active.get("mode") or "") or None
            owner_pid = pid
            reason = f"listen:{mode or 'unknown'}"
            if stream_id:
                reason = f"{reason}:{stream_id}"

    pause = read_ambient_pause()
    if pause and _listen_like_reason(str(pause.get("reason") or "")):
        raw_pid = pause.get("pid")
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            pid = None
        if not _is_own_pid(pid, me) and _pid_alive(pid):
            sources.append("ambient.pause")
            if owner_pid is None:
                owner_pid = pid
            pr = str(pause.get("reason") or "listen")
            if reason is None:
                reason = f"ambient.pause:{pr}"

    if not sources:
        return UserCaptureState(active=False)
    return UserCaptureState(
        active=True,
        reason=reason,
        sources=tuple(sources),
        stream_id=stream_id,
        mode=mode,
        pid=owner_pid,
    )


@dataclass
class DeferResult:
    """Outcome of waiting for user capture to end (or quiet gate) before TTS play."""

    deferred: bool = False
    wait_ms: int = 0
    timed_out: bool = False
    reason: str | None = None
    sources: list[str] = field(default_factory=list)
    # B105: "idle" = wait for capture end (B097); "quiet" = streaming min-quiet gate
    gate: str | None = None
    quiet_s: float | None = None
    min_quiet_s: float | None = None

    def as_meta(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "deferred": self.deferred,
            "wait_ms": self.wait_ms,
            "timed_out": self.timed_out,
            "reason": self.reason,
            "sources": list(self.sources),
        }
        if self.gate is not None:
            meta["gate"] = self.gate
        if self.quiet_s is not None:
            meta["quiet_s"] = self.quiet_s
        if self.min_quiet_s is not None:
            meta["min_quiet_s"] = self.min_quiet_s
        return meta


def wait_until_user_capture_idle(
    *,
    max_wait_s: float = DEFAULT_DEFER_MAX_WAIT_S,
    poll_ms: int = DEFAULT_DEFER_POLL_MS,
    quiet_ms: int = DEFAULT_DEFER_QUIET_MS,
    ignore_own_pid: bool = True,
    sleep_fn: Callable[[float], None] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
    probe_fn: Callable[[], UserCaptureState] | None = None,
) -> DeferResult:
    """Block until listen/radio capture ends (or *max_wait_s* elapses).

    When capture is active, TTS play and mic-mute should wait so half-duplex
    mute does not cut off the operator mid-utterance (B097). After the stream
    clears, optionally settle *quiet_ms* (trailing quiet / race pad) and
    re-check in case a new capture opened.

    ``max_wait_s <= 0`` means wait with no time cap (still poll; not recommended).
    On timeout, returns ``timed_out=True`` and the caller should speak anyway.
    """
    sleep = sleep_fn or time.sleep
    mono = monotonic_fn or time.monotonic
    probe = probe_fn or (
        lambda: user_capture_active(ignore_own_pid=ignore_own_pid)
    )
    poll_s = max(0.02, float(poll_ms) / 1000.0)
    quiet_s = max(0.0, float(quiet_ms) / 1000.0)
    cap = float(max_wait_s)
    t0 = mono()
    deadline = None if cap <= 0 else t0 + cap

    first = probe()
    if not first.active:
        return DeferResult(deferred=False, wait_ms=0)

    first_reason = first.reason
    first_sources = list(first.sources)
    syslog(
        "tts.defer_listen",
        component="tts",
        level="info",
        reason=first_reason,
        sources=first_sources,
        stream_id=first.stream_id,
        mode=first.mode,
        max_wait_s=cap if cap > 0 else None,
        message="deferring TTS play until user capture ends",
    )

    while True:
        state = probe()
        if not state.active:
            if quiet_s > 0:
                # Trailing quiet / stream-finalize pad; re-open aborts pad.
                quiet_deadline = mono() + quiet_s
                while mono() < quiet_deadline:
                    if deadline is not None and mono() >= deadline:
                        wait_ms = int(1000 * (mono() - t0))
                        syslog(
                            "tts.defer_listen_timeout",
                            component="tts",
                            level="warn",
                            reason=first_reason,
                            wait_ms=wait_ms,
                            message="max defer wait elapsed during quiet pad; speaking",
                        )
                        return DeferResult(
                            deferred=True,
                            wait_ms=wait_ms,
                            timed_out=True,
                            reason=first_reason,
                            sources=first_sources,
                        )
                    reopened = probe()
                    if reopened.active:
                        first_reason = reopened.reason or first_reason
                        first_sources = list(
                            dict.fromkeys(first_sources + list(reopened.sources))
                        )
                        break
                    sleep(min(poll_s, max(0.01, quiet_deadline - mono())))
                else:
                    # Quiet pad completed without re-open
                    wait_ms = int(1000 * (mono() - t0))
                    syslog(
                        "tts.defer_listen_done",
                        component="tts",
                        level="info",
                        reason=first_reason,
                        wait_ms=wait_ms,
                        sources=first_sources,
                    )
                    return DeferResult(
                        deferred=True,
                        wait_ms=wait_ms,
                        timed_out=False,
                        reason=first_reason,
                        sources=first_sources,
                    )
                continue

            wait_ms = int(1000 * (mono() - t0))
            syslog(
                "tts.defer_listen_done",
                component="tts",
                level="info",
                reason=first_reason,
                wait_ms=wait_ms,
                sources=first_sources,
            )
            return DeferResult(
                deferred=True,
                wait_ms=wait_ms,
                timed_out=False,
                reason=first_reason,
                sources=first_sources,
            )

        if deadline is not None and mono() >= deadline:
            wait_ms = int(1000 * (mono() - t0))
            syslog(
                "tts.defer_listen_timeout",
                component="tts",
                level="warn",
                reason=state.reason or first_reason,
                wait_ms=wait_ms,
                sources=list(state.sources) or first_sources,
                message="max defer wait elapsed; speaking despite open listen",
            )
            return DeferResult(
                deferred=True,
                wait_ms=wait_ms,
                timed_out=True,
                reason=state.reason or first_reason,
                sources=list(state.sources) or first_sources,
                gate="idle",
            )
        sleep(poll_s)


DEFAULT_STREAMING_ACK_MIN_QUIET_S = 2.0


def wait_until_tts_play_allowed(
    *,
    streaming: bool = False,
    min_quiet_s: float = DEFAULT_STREAMING_ACK_MIN_QUIET_S,
    max_wait_s: float = DEFAULT_DEFER_MAX_WAIT_S,
    poll_ms: int = DEFAULT_DEFER_POLL_MS,
    quiet_ms: int = DEFAULT_DEFER_QUIET_MS,
    ignore_own_pid: bool = True,
    sleep_fn: Callable[[float], None] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
    time_fn: Callable[[], float] | None = None,
    probe_fn: Callable[[], UserCaptureState] | None = None,
    quiet_fn: Callable[[], float | None] | None = None,
) -> DeferResult:
    """Block until TTS play is safe relative to open listen/radio capture.

    * **HOLD / non-streaming (B097):** wait until capture ends (then optional
      ``quiet_ms`` settle pad).
    * **Streaming (B105):** wait until operator has been quiet for
      ``min_quiet_s`` **or** the capture ends. Continuous speech without that
      pause keeps play deferred so short acks do not barge in / mute mid-thought.
      On max-wait timeout, speak anyway (same fail-open as B097).
    """
    if not streaming:
        result = wait_until_user_capture_idle(
            max_wait_s=max_wait_s,
            poll_ms=poll_ms,
            quiet_ms=quiet_ms,
            ignore_own_pid=ignore_own_pid,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            probe_fn=probe_fn,
        )
        if result.deferred and result.gate is None:
            result.gate = "idle"
        return result

    sleep = sleep_fn or time.sleep
    mono = monotonic_fn or time.monotonic
    wall = time_fn or time.time
    probe = probe_fn or (
        lambda: user_capture_active(ignore_own_pid=ignore_own_pid)
    )

    def _quiet() -> float | None:
        if quiet_fn is not None:
            return quiet_fn()
        from hark.listen_control import operator_quiet_s

        return operator_quiet_s(now=wall())

    poll_s = max(0.02, float(poll_ms) / 1000.0)
    need_quiet = max(0.0, float(min_quiet_s))
    cap = float(max_wait_s)
    t0 = mono()
    deadline = None if cap <= 0 else t0 + cap

    first = probe()
    if not first.active:
        return DeferResult(deferred=False, wait_ms=0, gate="quiet")

    first_reason = first.reason
    first_sources = list(first.sources)
    syslog(
        "tts.defer_streaming_quiet",
        component="tts",
        level="info",
        reason=first_reason,
        sources=first_sources,
        stream_id=first.stream_id,
        mode=first.mode,
        min_quiet_s=need_quiet,
        max_wait_s=cap if cap > 0 else None,
        message="deferring TTS play until operator quiet or listen ends",
    )

    while True:
        state = probe()
        if not state.active:
            # Stream ended — same settle pad as B097 idle path
            if quiet_ms > 0:
                quiet_pad_s = max(0.0, float(quiet_ms) / 1000.0)
                quiet_deadline = mono() + quiet_pad_s
                while mono() < quiet_deadline:
                    if deadline is not None and mono() >= deadline:
                        wait_ms = int(1000 * (mono() - t0))
                        syslog(
                            "tts.defer_streaming_timeout",
                            component="tts",
                            level="warn",
                            reason=first_reason,
                            wait_ms=wait_ms,
                            message="max defer wait during post-idle pad; speaking",
                        )
                        return DeferResult(
                            deferred=True,
                            wait_ms=wait_ms,
                            timed_out=True,
                            reason=first_reason,
                            sources=first_sources,
                            gate="quiet",
                            min_quiet_s=need_quiet,
                        )
                    reopened = probe()
                    if reopened.active:
                        first_reason = reopened.reason or first_reason
                        first_sources = list(
                            dict.fromkeys(first_sources + list(reopened.sources))
                        )
                        break
                    sleep(min(poll_s, max(0.01, quiet_deadline - mono())))
                else:
                    wait_ms = int(1000 * (mono() - t0))
                    syslog(
                        "tts.defer_streaming_idle",
                        component="tts",
                        level="info",
                        reason=first_reason,
                        wait_ms=wait_ms,
                        sources=first_sources,
                        message="listen ended; playing TTS",
                    )
                    return DeferResult(
                        deferred=True,
                        wait_ms=wait_ms,
                        timed_out=False,
                        reason=first_reason,
                        sources=first_sources,
                        gate="quiet",
                        min_quiet_s=need_quiet,
                    )
                continue

            wait_ms = int(1000 * (mono() - t0))
            return DeferResult(
                deferred=True,
                wait_ms=wait_ms,
                timed_out=False,
                reason=first_reason,
                sources=first_sources,
                gate="quiet",
                min_quiet_s=need_quiet,
            )

        quiet_now = _quiet()
        if quiet_now is not None and quiet_now >= need_quiet:
            wait_ms = int(1000 * (mono() - t0))
            syslog(
                "tts.defer_streaming_quiet_met",
                component="tts",
                level="info",
                reason=state.reason or first_reason,
                wait_ms=wait_ms,
                quiet_s=round(quiet_now, 3),
                min_quiet_s=need_quiet,
                sources=list(state.sources) or first_sources,
                message="operator quiet gate met; playing TTS while listen open",
            )
            return DeferResult(
                deferred=True,
                wait_ms=wait_ms,
                timed_out=False,
                reason=state.reason or first_reason,
                sources=list(state.sources) or first_sources,
                gate="quiet",
                quiet_s=float(quiet_now),
                min_quiet_s=need_quiet,
            )

        if deadline is not None and mono() >= deadline:
            wait_ms = int(1000 * (mono() - t0))
            syslog(
                "tts.defer_streaming_timeout",
                component="tts",
                level="warn",
                reason=state.reason or first_reason,
                wait_ms=wait_ms,
                quiet_s=None if quiet_now is None else round(float(quiet_now), 3),
                min_quiet_s=need_quiet,
                sources=list(state.sources) or first_sources,
                message="max defer wait elapsed; speaking despite open listen",
            )
            return DeferResult(
                deferred=True,
                wait_ms=wait_ms,
                timed_out=True,
                reason=state.reason or first_reason,
                sources=list(state.sources) or first_sources,
                gate="quiet",
                quiet_s=None if quiet_now is None else float(quiet_now),
                min_quiet_s=need_quiet,
            )
        sleep(poll_s)

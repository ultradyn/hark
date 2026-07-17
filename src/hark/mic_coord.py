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

B108: silence ``end_mode`` always uses HOLD defer (wait for capture end), even
when streaming is on. Mid-capture TTS mute freezes silence clocks (B084) and
races ``end_silence_s``; silence-mode streams must auto-finalize on pause.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Iterator

from hark.audio.capture import (
    MicBusyError,
    MicLease,
    cancellation_cleanup,
    capture_attempt,
    raise_if_capture_cancelled,
)
from hark.paths import state_dir
from hark.syslog import log as syslog

DEFAULT_YIELD_TIMEOUT_S = 15.0
_POLL_S = 0.05
AMBIENT_PAUSE_VERSION = 2
_LEGACY_AMBIENT_PAUSE_VERSIONS = frozenset({1})
_LEGACY_SINGLETON_FIELDS = frozenset({"reason", "pid", "requested_at"})
_REGISTRY_ONLY_PAUSE_FIELDS = frozenset(
    {"owners", "token", "process_start", "boot_id", "legacy"}
)
DEFAULT_PAUSE_OWNER_MAX_AGE_S = 4 * 60 * 60
_FUTURE_SKEW_S = 5.0

# ambient.pause reasons that mean bound speech capture (not wake-enroll etc.)
_LISTEN_PAUSE_REASONS = frozenset({"listen", "ask", "tts-listen", "radio"})

DEFAULT_DEFER_MAX_WAIT_S = 45.0
DEFAULT_DEFER_POLL_MS = 100
DEFAULT_DEFER_QUIET_MS = 200


def pause_path() -> Path:
    return state_dir() / "ambient.pause"


def _pause_lock_path() -> Path:
    return state_dir() / "ambient.pause.lock"


@contextmanager
def _pause_lock() -> Iterator[None]:
    path = _pause_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _boot_id() -> str:
    value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8")
    boot_id = value.strip()
    if not boot_id:
        raise ValueError("empty Linux boot id")
    return boot_id


@dataclass(frozen=True)
class _ProcessStat:
    state: str
    start_time: str


def _process_stat(pid: int) -> _ProcessStat:
    """Return proc state/start ticks while preserving read failure semantics."""
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    # comm is parenthesized and may itself contain spaces or parentheses.
    after_comm = raw[raw.rfind(")") + 2 :].split()
    try:
        return _ProcessStat(state=after_comm[0], start_time=after_comm[19])
    except IndexError as exc:
        raise ValueError(f"malformed /proc/{pid}/stat") from exc


def _process_start_time(pid: int) -> str:
    """Return Linux procfs start ticks for *pid* (stable across PID reuse)."""
    return _process_stat(pid).start_time


class _OwnerState(Enum):
    LIVE = auto()
    STALE = auto()
    UNVERIFIABLE = auto()


def _identity_state(owner: dict[str, Any]) -> _OwnerState:
    """Compare stored process identity without confusing absence with I/O failure."""
    try:
        boot_id = _boot_id()
    except (OSError, UnicodeError, ValueError):
        return _OwnerState.UNVERIFIABLE
    if boot_id != owner["boot_id"]:
        return _OwnerState.STALE
    try:
        process = _process_stat(owner["pid"])
    except (FileNotFoundError, ProcessLookupError):
        return _OwnerState.STALE
    except (OSError, UnicodeError, ValueError):
        return _OwnerState.UNVERIFIABLE
    if process.state in {"Z", "X", "x"}:
        return _OwnerState.STALE
    if process.start_time != owner["process_start"]:
        return _OwnerState.STALE
    return _OwnerState.LIVE


def _common_owner_state(owner: dict[str, Any], *, now: float) -> _OwnerState:
    pid = owner.get("pid")
    requested_at = owner.get("requested_at")
    token = owner.get("token")
    reason = owner.get("reason")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or not isinstance(requested_at, (int, float))
        or isinstance(requested_at, bool)
        or not isinstance(token, str)
        or not isinstance(reason, str)
    ):
        return _OwnerState.STALE
    if (
        pid <= 0
        or not token
        or not reason
        or (isinstance(requested_at, float) and not math.isfinite(requested_at))
        # Compare against precomputed bounds rather than subtracting the input:
        # JSON integers are unbounded and float conversion/subtraction can overflow.
        or requested_at > now + _FUTURE_SKEW_S
        or requested_at < now - DEFAULT_PAUSE_OWNER_MAX_AGE_S
    ):
        return _OwnerState.STALE
    return _OwnerState.LIVE


def _normalize_owner(
    owner: dict[str, Any], *, now: float
) -> tuple[dict[str, Any], _OwnerState, bool]:
    if _common_owner_state(owner, now=now) is _OwnerState.STALE:
        return owner, _OwnerState.STALE, False
    if owner.get("legacy") is True:
        try:
            boot_id = _boot_id()
            process = _process_stat(owner["pid"])
        except (FileNotFoundError, ProcessLookupError):
            return owner, _OwnerState.STALE, False
        except (OSError, UnicodeError, ValueError):
            return owner, _OwnerState.UNVERIFIABLE, False
        if process.state in {"Z", "X", "x"}:
            return owner, _OwnerState.STALE, False
        migrated = dict(owner)
        migrated.pop("legacy", None)
        migrated["boot_id"] = boot_id
        migrated["process_start"] = process.start_time
        return migrated, _OwnerState.LIVE, True

    if (
        not isinstance(owner.get("process_start"), str)
        or not owner["process_start"]
        or not isinstance(owner.get("boot_id"), str)
        or not owner["boot_id"]
    ):
        return owner, _OwnerState.STALE, False
    return owner, _identity_state(owner), False


def _legacy_owner(payload: dict[str, Any]) -> dict[str, Any]:
    reason = payload.get("reason")
    pid = payload.get("pid")
    requested_at = payload.get("requested_at")
    token = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"hark:ambient.pause:{pid!r}:{requested_at!r}:{reason!r}",
    ).hex
    return {
        "legacy": True,
        "token": token,
        "reason": reason,
        "pid": pid,
        "requested_at": requested_at,
    }


class _UnsupportedPauseSchema(RuntimeError):
    """A future registry version that this process must not rewrite."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(
            f"unsupported ambient pause version: {payload.get('version')!r}"
        )
        self.payload = payload


def _read_live_owners_unlocked(*, now: float) -> tuple[list[dict[str, Any]], bool]:
    path = pause_path()
    if not path.is_file():
        return [], False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return [], True
    if not isinstance(payload, dict):
        return [], True
    version = payload.get("version")
    has_version = "version" in payload
    known_legacy_version = (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version in _LEGACY_AMBIENT_PAUSE_VERSIONS
    )
    current_version = (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version == AMBIENT_PAUSE_VERSION
    )
    expected_legacy_fields = _LEGACY_SINGLETON_FIELDS | (
        {"version"} if has_version else set()
    )
    genuine_legacy_singleton = (
        not has_version or known_legacy_version
    ) and payload.keys() == expected_legacy_fields
    if current_version:
        raw_owners = payload.get("owners")
        if not isinstance(raw_owners, list):
            return [], True
        # An empty v2 registry represents no pause and must not leave a marker
        # that disagrees with read_ambient_pause()/ambient_pause_requested().
        dirty = not raw_owners
    elif genuine_legacy_singleton:
        # Rolling upgrade from the singleton format. Until procfs and boot id
        # can be read, the explicit legacy marker remains a fail-closed owner.
        raw_owners = [_legacy_owner(payload)]
        dirty = True
    elif (
        has_version
        or _REGISTRY_ONLY_PAUSE_FIELDS.intersection(payload)
        or _LEGACY_SINGLETON_FIELDS <= payload.keys()
    ):
        # A future writer may retain the v2 top-level compatibility fields.
        # Likewise, a stripped-version registry or a singleton-shaped payload
        # with extra registry fields is not genuine legacy state. Never
        # collapse owner/token data that we do not know how to interpret.
        raise _UnsupportedPauseSchema(payload)
    else:
        return [], True
    owners = []
    for owner in raw_owners:
        if not isinstance(owner, dict):
            dirty = True
            continue
        normalized, state, normalized_dirty = _normalize_owner(owner, now=now)
        dirty = dirty or normalized_dirty
        # Transient identity I/O failures fail closed: retain the pause until a
        # later read can prove that its owner is stale.
        if state is not _OwnerState.STALE:
            owners.append(normalized)
        else:
            dirty = True
    return owners, dirty


def _payload_for_owners(owners: list[dict[str, Any]]) -> dict[str, Any]:
    latest = owners[-1]
    return {
        "version": AMBIENT_PAUSE_VERSION,
        # Keep the original top-level shape for existing pause readers.
        "reason": latest["reason"],
        "pid": latest["pid"],
        "requested_at": latest["requested_at"],
        "token": latest["token"],
        "owners": owners,
    }


def _fsync_parent(path: Path) -> None:
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_owners_unlocked(owners: list[dict[str, Any]]) -> None:
    path = pause_path()
    if not owners:
        existed = path.exists()
        path.unlink(missing_ok=True)
        if existed:
            _fsync_parent(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(_payload_for_owners(owners), handle, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        temporary.unlink(missing_ok=True)


def ambient_pause_requested() -> bool:
    return read_ambient_pause() is not None


def read_ambient_pause() -> dict[str, Any] | None:
    """Read active pause owners, pruning dead, reused, expired, or bad state."""
    try:
        with _pause_lock():
            owners, dirty = _read_live_owners_unlocked(now=time.time())
            if dirty:
                _write_owners_unlocked(owners)
            return _payload_for_owners(owners) if owners else None
    except _UnsupportedPauseSchema as exc:
        # The marker itself is the fail-closed signal. Return it unchanged and
        # leave the on-disk future schema for a compatible process to manage.
        return exc.payload
    except OSError:
        # An unreadable marker is safer treated as a pause than ignored.
        if pause_path().is_file():
            return {"reason": "unknown", "pid": None, "owners": []}
        return None


def request_ambient_pause(
    *,
    reason: str = "listen",
    pid: int | None = None,
) -> str:
    """Acquire a pause owner and return the token required to release it."""
    owner_pid = pid if pid is not None else os.getpid()
    if not isinstance(owner_pid, int) or isinstance(owner_pid, bool) or owner_pid <= 0:
        raise ValueError("ambient pause owner pid must be a positive integer")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("ambient pause reason must be a non-empty string")
    try:
        process = _process_stat(owner_pid)
        boot_id = _boot_id()
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(
            f"cannot verify ambient pause owner identity for pid {owner_pid}"
        ) from exc
    if process.state in {"Z", "X", "x"} or not process.start_time or not boot_id:
        raise RuntimeError(
            f"cannot verify ambient pause owner identity for pid {owner_pid}"
        )
    token = uuid.uuid4().hex
    owner = {
        "token": token,
        "reason": reason,
        "pid": owner_pid,
        "requested_at": time.time(),
        "process_start": process.start_time,
        "boot_id": boot_id,
    }
    published = False
    try:
        with _pause_lock():
            try:
                owners, _dirty = _read_live_owners_unlocked(now=time.time())
            except _UnsupportedPauseSchema as exc:
                raise RuntimeError(str(exc)) from exc
            attempted = [*owners, owner]
            try:
                _write_owners_unlocked(attempted)
                published = True
            except BaseException:  # rollback includes cancellation/interrupts
                # os.replace may have succeeded before directory fsync failed.
                try:
                    _write_owners_unlocked(owners)
                except BaseException:
                    pass
                raise
        syslog(
            "ambient.pause_request",
            component="mic",
            level="info",
            reason=reason,
            pid=owner_pid,
            token=token,
        )
        return token
    except BaseException as exc:
        if published:
            # Publication succeeded but the caller never received ownership.
            # Remove only our exact token, preserving concurrent owners.
            with cancellation_cleanup(primary=exc):
                try:
                    with _pause_lock():
                        current, _dirty = _read_live_owners_unlocked(now=time.time())
                        retained = [
                            item for item in current if item.get("token") != token
                        ]
                        if len(retained) != len(current):
                            _write_owners_unlocked(retained)
                except BaseException:
                    pass
        raise


def clear_ambient_pause(token: str | None = None) -> None:
    """Release exactly *token*, or a sole pause owner held by this process.

    The tokenless form retains compatibility with existing cleanup callers but
    refuses ambiguous overlapping state and never removes a newer live request.
    """
    try:
        with _pause_lock():
            owners, dirty = _read_live_owners_unlocked(now=time.time())
            if token is None:
                pid = os.getpid()
                process_start = _process_start_time(pid)
                boot_id = _boot_id()
                sole_owner_is_ours = len(owners) == 1 and (
                    owners[0].get("pid") == pid
                    and owners[0].get("process_start") == process_start
                    and owners[0].get("boot_id") == boot_id
                )
                retained = [] if sole_owner_is_ours else owners
            else:
                retained = [owner for owner in owners if owner.get("token") != token]
            if dirty or len(retained) != len(owners):
                _write_owners_unlocked(retained)
                syslog(
                    "ambient.pause_clear",
                    component="mic",
                    level="info",
                    token=token,
                    remaining=len(retained),
                )
    except (_UnsupportedPauseSchema, OSError):
        pass


def wait_for_mic_free(*, timeout_s: float = DEFAULT_YIELD_TIMEOUT_S) -> None:
    """Block until no other process holds mic.lock (or timeout)."""
    deadline = time.monotonic() + max(0.1, timeout_s)
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        raise_if_capture_cancelled()
        try:
            # Probe: acquire and immediately release
            with MicLease("mic-probe"):
                raise_if_capture_cancelled()
                return
        except MicBusyError as exc:
            last_err = exc
            time.sleep(_POLL_S)
    raise_if_capture_cancelled()
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
    with capture_attempt():
        token = request_ambient_pause(reason=reason)
        primary: BaseException | None = None
        try:
            wait_for_mic_free(timeout_s=timeout_s)
            yield
        except BaseException as exc:
            primary = exc
            raise
        finally:
            with cancellation_cleanup(primary=primary):
                try:
                    clear_ambient_pause(token)
                except BaseException:
                    if primary is None:
                        raise


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
    pause_owners = pause.get("owners", []) if pause else []
    if pause and not pause_owners:
        # Fail-closed compatibility for an unreadable marker.
        pause_owners = [pause]
    for pause_owner in reversed(pause_owners):
        if not _listen_like_reason(str(pause_owner.get("reason") or "")):
            continue
        raw_pid = pause_owner.get("pid")
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (TypeError, ValueError):
            pid = None
        if not _is_own_pid(pid, me) and _pid_alive(pid):
            sources.append("ambient.pause")
            if owner_pid is None:
                owner_pid = pid
            pr = str(pause_owner.get("reason") or "listen")
            if reason is None:
                reason = f"ambient.pause:{pr}"
            break

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
    probe = probe_fn or (lambda: user_capture_active(ignore_own_pid=ignore_own_pid))
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
    * **Silence end_mode (B108):** always HOLD even when ``streaming=True``.
      Quiet-gate mid-capture TTS races silence endpointing (mute freezes silence
      clocks). Live streaming acks apply to **radio** captures only.
    """
    probe = probe_fn or (lambda: user_capture_active(ignore_own_pid=ignore_own_pid))

    # B108: silence-mode streams must finalize on end_silence_s. Streaming quiet
    # gate is radio-only; force HOLD when the active capture is silence mode.
    use_streaming = bool(streaming)
    if use_streaming:
        first_probe = probe()
        mode = (first_probe.mode or "").strip().lower() if first_probe.active else ""
        if mode == "silence":
            use_streaming = False
            syslog(
                "tts.defer_silence_hold",
                component="tts",
                level="debug",
                reason=first_probe.reason,
                stream_id=first_probe.stream_id,
                mode=first_probe.mode,
                message=(
                    "silence end_mode: HOLD TTS until capture ends "
                    "(streaming quiet-gate is radio-only; B108)"
                ),
            )

    if not use_streaming:
        result = wait_until_user_capture_idle(
            max_wait_s=max_wait_s,
            poll_ms=poll_ms,
            quiet_ms=quiet_ms,
            ignore_own_pid=ignore_own_pid,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            probe_fn=probe,
        )
        if result.deferred and result.gate is None:
            result.gate = "idle"
        return result

    sleep = sleep_fn or time.sleep
    mono = monotonic_fn or time.monotonic
    wall = time_fn or time.time

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

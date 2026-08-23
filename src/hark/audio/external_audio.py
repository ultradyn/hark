"""One gate for commands that change audio state *outside* Hark (B188).

Ducking a sink-input volume, pausing an MPRIS player, and muting the capture
source all reach into the operator's desktop. Under test that is not a harmless
stubbed side effect — it is real interference: a suite that spawns a real
``hark ask`` child will pause the operator's music and drop their volume for
real, on their actual machine, while they are using it.

Every mutating call asks :func:`media_control_blocked` first. Read-only probes
(``pactl list sink-inputs``, ``playerctl status``, ``arecord -l``) are *not*
gated — they tell Hark what is playing without touching it.

Two layers, because they cover different failure modes:

* ``HARK_MEDIA_CONTROL=0`` blocks. Set in ``os.environ`` (see
  ``tests/conftest.py``), so a **spawned child process inherits it**. This is
  the load-bearing layer: a child ``hark`` has no idea it was started by a test
  and no monkeypatch can reach it.
* Otherwise, an in-process pytest is detected and blocks by default. This
  catches a direct call from a test that predates the fixture, or a new test
  file that never loads the root ``conftest.py``.

``HARK_MEDIA_CONTROL=1`` is an explicit opt-in that wins over both, including
under pytest. A test that sets it takes responsibility for stubbing
``subprocess.run`` itself — otherwise it will actuate the operator's desktop.

Unset in production means allowed: the default behaviour of a real Hark run is
unchanged by this module.

Follows the ``HARK_UPDATE_CHECK`` precedent from B088 — default off under test,
opt in per test.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

ENV_FLAG = "HARK_MEDIA_CONTROL"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class BlockedCall:
    """A mutating argv that the gate refused, kept for assertions/telemetry."""

    argv: tuple[str, ...]
    reason: str

    def as_meta(self) -> dict[str, object]:
        return {"argv": list(self.argv), "reason": self.reason}


@dataclass
class _Log:
    calls: list[BlockedCall] = field(default_factory=list)


_log = _Log()

# Bounded so a long-running daemon with a stuck config cannot grow this without
# limit; the tail is what a caller ever wants.
_MAX_LOGGED = 64


def _under_pytest() -> bool:
    """True when this interpreter is running a test.

    ``PYTEST_CURRENT_TEST`` is set by pytest per test and is inherited by child
    processes; ``sys.modules`` catches collection/module scope, where that env
    var is not yet set.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return "pytest" in sys.modules


def media_control_blocked() -> str | None:
    """Return why external audio control is blocked, or ``None`` when allowed.

    The string is for logs and test assertions; callers should treat any
    non-``None`` value as "do not run this command".
    """
    raw = os.environ.get(ENV_FLAG)
    if raw is not None:
        val = raw.strip().lower()
        if val in _TRUTHY:
            return None
        if val in _FALSY:
            return f"{ENV_FLAG}={raw}"
        # An unparseable value is a misconfiguration. Refuse rather than guess:
        # the cost of a wrong "allow" is the operator's audio, the cost of a
        # wrong "block" is a missing duck.
        return f"{ENV_FLAG}={raw!r} (unrecognised; treated as off)"
    if _under_pytest():
        return "pytest detected"
    return None


def note_blocked(argv: list[str] | tuple[str, ...], reason: str) -> None:
    """Record a refused argv. Never raises."""
    try:
        _log.calls.append(BlockedCall(tuple(str(a) for a in argv), reason))
        if len(_log.calls) > _MAX_LOGGED:
            del _log.calls[:-_MAX_LOGGED]
    except Exception:
        pass


def blocked_calls() -> tuple[BlockedCall, ...]:
    """Mutating argvs refused so far, oldest first (bounded to the last 64)."""
    return tuple(_log.calls)


def reset_blocked_calls() -> None:
    _log.calls.clear()


# ---------------------------------------------------------------------------
# argv classification — shared by the gate's own tests and the conftest tripwire
# ---------------------------------------------------------------------------

# Subcommands that change state. Anything not listed is treated as read-only,
# so adding a new mutating command to Hark means adding it here too — the
# tripwire in tests/conftest.py is what makes that omission visible.
_MUTATING: dict[str, tuple[str, ...]] = {
    "pactl": ("set-",),
    "wpctl": ("set-",),
    "playerctl": ("play", "pause", "play-pause", "stop", "next", "previous"),
    "amixer": ("sset", "set"),
}


#: Binaries that make audible sound. ``ffmpeg`` is deliberately absent: Hark
#: uses it to *convert* audio (playback.py:1300, :1395, :1430), which is silent,
#: and blanket-blocking it would break MP3 handling. Only the players belong
#: here.
_PLAYERS = frozenset({"ffplay", "paplay", "aplay", "pw-play", "mpv"})


def is_audio_playback_argv(argv: list[str] | tuple[str, ...]) -> bool:
    """True when ``argv`` would make audible sound on the operator's machine.

    Separate from :func:`is_mutating_argv` because the two failures differ: a
    mutating command changes state the operator has to notice and undo, while a
    playback command just makes noise at them. Both are unacceptable from a
    test; only the first is something a production caller might want gated.
    """
    if not argv:
        return False
    return os.path.basename(str(argv[0])) in _PLAYERS


def is_mutating_argv(argv: list[str] | tuple[str, ...]) -> bool:
    """True when ``argv`` would change audio state outside Hark.

    Conservative by construction: an unrecognised tool is not mutating, but any
    recognised tool with a state-changing verb anywhere in its arguments is.
    ``playerctl -p spotify pause`` and ``pactl set-sink-input-volume 3 40%``
    both match; ``playerctl status`` and ``pactl list sink-inputs`` do not.
    """
    if not argv:
        return False
    tool = os.path.basename(str(argv[0]))
    verbs = _MUTATING.get(tool)
    if not verbs:
        return False
    for token in (str(a) for a in argv[1:]):
        for verb in verbs:
            if verb.endswith("-"):
                if token.startswith(verb):
                    return True
            elif token == verb:
                return True
    return False

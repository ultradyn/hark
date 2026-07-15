"""Stdout/stderr buffering policy for piped (non-TTY) consumers.

When stdout is a pipe or file, CPython fully buffers it. Progressive NDJSON
(monitor, partials) and status lines then appear only after the buffer fills
(~8 KiB) or the process exits — so ``hark … | grep`` / harness Monitors look
hung. Interactive pipes to ``tail`` without ``-f`` are worse: even with
line-buffering, ``tail -N`` waits for EOF before printing.

B109: reconfigure stdout (and stderr) to line-buffered early in CLI entry,
and warn once when interactive long-running commands have a non-TTY stdout.
"""

from __future__ import annotations

import os
import sys
from typing import Any, TextIO

# Subcommands / flags that stream for a long time or run interactively.
# Used for the one-shot non-TTY warning (not for buffering — that is always on).
_INTERACTIVE_CMDS = frozenset(
    {
        "monitor",
        "watch",
        "ambient",
        "listen",
        "ask",
        "watch-logs",
        "tts",  # interactive when --listen / --listen-for-user-response
        "daemon",  # harkd-style long run via `hark daemon start`
        "agentapi",  # deliver --follow-monitor
    }
)

_CONFIGURED = False
_WARNED = False


def is_tty(stream: TextIO | Any | None = None) -> bool:
    """True if *stream* is attached to a terminal."""
    s = stream if stream is not None else sys.stdout
    try:
        return bool(s.isatty())
    except Exception:
        return False


def configure_stdio(
    *,
    stdout: TextIO | Any | None = None,
    stderr: TextIO | Any | None = None,
    force: bool = False,
) -> dict[str, bool]:
    """Line-buffer stdout/stderr so piped line consumers see progress.

    Safe to call multiple times; no-ops when streams lack ``reconfigure``
    (e.g. ``StringIO`` in tests) or when already configured unless *force*.

    Returns a small status dict for tests / diagnostics::

        {"stdout": True/False, "stderr": True/False}
    """
    global _CONFIGURED
    out_ok = False
    err_ok = False
    if _CONFIGURED and not force:
        return {"stdout": False, "stderr": False}

    # PYTHONUNBUFFERED already forces unbuffered binary wrappers in CPython;
    # still reconfigure for line_buffering so TextIO newlines flush promptly.
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr

    out_ok = _reconfigure_line_buffered(out)
    err_ok = _reconfigure_line_buffered(err)
    _CONFIGURED = True
    return {"stdout": out_ok, "stderr": err_ok}


def _reconfigure_line_buffered(stream: Any) -> bool:
    reconf = getattr(stream, "reconfigure", None)
    if not callable(reconf):
        return False
    try:
        # line_buffering: flush on newline (HEP NDJSON, print()).
        # write_through: also flush after every write (covers write() without \n).
        reconf(line_buffering=True, write_through=True)
        return True
    except (OSError, ValueError, AttributeError, TypeError):
        try:
            reconf(line_buffering=True)
            return True
        except (OSError, ValueError, AttributeError, TypeError):
            return False


def maybe_warn_non_tty_stdout(
    argv: list[str] | None = None,
    *,
    stream: TextIO | Any | None = None,
    err: TextIO | Any | None = None,
    force: bool = False,
) -> bool:
    """Warn once when an interactive command has non-TTY stdout.

    Agents often append ``| tail`` which waits for EOF and hides live state.
    Returns True if a warning was written.
    """
    global _WARNED
    if _WARNED and not force:
        return False
    if os.environ.get("HARK_NO_PIPE_WARN", "").strip() in ("1", "true", "yes"):
        return False

    out = stream if stream is not None else sys.stdout
    if is_tty(out):
        return False

    args = list(sys.argv[1:] if argv is None else argv)
    if not _looks_interactive(args):
        return False

    msg = (
        "hark: warning: stdout is not a TTY (piped/redirected). "
        "Output is line-buffered so progressive lines can stream, but "
        "`| tail` (without -f) still waits for the process to exit before "
        "printing. Do not pipe interactive commands "
        "(tts --listen, listen, ask, monitor, watch, ambient) to tail — "
        "use the harness Monitor tool, `hark monitor --for-monitor`, or "
        "`tail -f` if you must. Set HARK_NO_PIPE_WARN=1 to silence."
    )
    dest = err if err is not None else sys.stderr
    try:
        print(msg, file=dest, flush=True)
    except Exception:
        return False
    _WARNED = True
    return True


def _looks_interactive(argv: list[str]) -> bool:
    """Heuristic: long-running / listen-oriented invocations."""
    if not argv:
        return False
    # Skip global flags to find subcommand
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        # --config PATH consumes next token
        if argv[i] in ("--config",) and i + 1 < len(argv):
            i += 2
            continue
        if argv[i].startswith("--config="):
            i += 1
            continue
        i += 1
    if i >= len(argv):
        return False
    cmd = argv[i]
    if cmd not in _INTERACTIVE_CMDS:
        return False
    if cmd == "tts":
        # Only flag args, not free text that happens to contain the substring.
        return "--listen" in argv[i + 1 :] or "--listen-for-user-response" in argv[
            i + 1 :
        ]
    if cmd == "agentapi":
        return "--follow-monitor" in argv[i + 1 :] or "deliver" in argv[i + 1 :]
    if cmd == "watch-logs":
        return "--follow" in argv[i + 1 :] or "-f" in argv[i + 1 :]
    if cmd == "daemon":
        return "start" in argv[i + 1 :]
    return True


def _reset_for_tests() -> None:
    """Test helper: clear module latches."""
    global _CONFIGURED, _WARNED
    _CONFIGURED = False
    _WARNED = False

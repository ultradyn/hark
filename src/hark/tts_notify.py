"""Desktop notification while TTS plays, with a Skip action (B161).

Linux libnotify: ``notify-send --print-id --action=skip=Skip`` prints the new
notification id, then blocks until the notification is closed or an action is
invoked, printing the invoked action token on stdout. A daemon reader thread
watches for the skip token and calls the playback-skip hook; when playback
ends naturally we close the notification (``gdbus … CloseNotification``, with
a 1ms-expire replace as fallback) and reap the helper process.

Everything here is best-effort: a headless host, missing ``notify-send``, or
a dead session bus silently disables the notification — playback never fails
because a notification could not be shown or closed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

_TITLE = "hark is speaking"
_SKIP_ACTION = "skip=Skip"
_SKIP_TOKEN = "skip"
_ID_READY_TIMEOUT_S = 1.0
_PROC_REAP_TIMEOUT_S = 1.0
_DISMISS_TIMEOUT_S = 2.0

# Test seams (monkeypatch like the rest of the codebase does).
_which = shutil.which
_popen = subprocess.Popen
_run = subprocess.run


def _escape_markup(text: str) -> str:
    """Escape Pango markup — notification daemons parse the body as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TtsSkipNotification:
    """One live ``notify-send`` notification tied to a playback span."""

    def __init__(
        self,
        text: str,
        *,
        on_skip: Callable[[], None] | None = None,
    ) -> None:
        self._text = text
        self._on_skip = on_skip
        self._proc: Any = None
        self._reader: threading.Thread | None = None
        self._notification_id: str | None = None
        self._id_ready = threading.Event()
        self._closed = False
        self._close_lock = threading.Lock()

    def start(self) -> bool:
        """Launch the notification; False when unavailable (best-effort)."""
        exe = _which("notify-send")
        if not exe or not self._text.strip():
            return False
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            # Headless host: notify-send would only fail against a missing bus.
            return False
        try:
            self._proc = _popen(
                [
                    exe,
                    "--print-id",
                    f"--action={_SKIP_ACTION}",
                    "--urgency=normal",
                    "--icon=audio-speakers",
                    "--app-name=hark",
                    _TITLE,
                    _escape_markup(self._text),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except Exception:
            self._proc = None
            return False
        reader = threading.Thread(
            target=self._read_loop,
            name="hark-tts-notify",
            daemon=True,
        )
        self._reader = reader
        try:
            reader.start()
        except Exception:
            self._reader = None
            self._terminate_proc()
            self._proc = None
            return False
        return True

    def _read_loop(self) -> None:
        proc = self._proc
        stdout = getattr(proc, "stdout", None)
        if proc is None or stdout is None:
            self._id_ready.set()
            return
        try:
            for i, line in enumerate(stdout):
                token = line.strip()
                if i == 0:
                    # --print-id: first line is the notification id.
                    self._notification_id = token or None
                    self._id_ready.set()
                    continue
                if token == _SKIP_TOKEN:
                    callback = self._on_skip
                    if callback is not None:
                        try:
                            callback()
                        except Exception:
                            pass
                    break
        except Exception:
            pass
        finally:
            # EOF without any line still releases a waiting close().
            self._id_ready.set()

    def close(self) -> None:
        """Dismiss the notification and reap the helper; safe to call twice."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        if self._proc is None:
            return
        # The reader publishes the id asynchronously; wait briefly so a
        # short playback still dismisses its notification instead of
        # leaving it on screen until the server's own timeout.
        self._id_ready.wait(_ID_READY_TIMEOUT_S)
        self._dismiss()
        self._terminate_proc()
        reader = self._reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=_PROC_REAP_TIMEOUT_S)

    def _terminate_proc(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=_PROC_REAP_TIMEOUT_S)
            except Exception:
                try:
                    proc.kill()
                    # Reap the SIGKILLed child so it cannot linger as a zombie.
                    proc.wait(timeout=_PROC_REAP_TIMEOUT_S)
                except Exception:
                    pass
        except Exception:
            pass

    def _dismiss(self) -> None:
        nid = self._notification_id
        if not nid or not nid.isdigit():
            return
        gdbus = _which("gdbus")
        if gdbus:
            try:
                _run(
                    [
                        gdbus,
                        "call",
                        "--session",
                        "--dest",
                        "org.freedesktop.Notifications",
                        "--object-path",
                        "/org/freedesktop/Notifications",
                        "--method",
                        "org.freedesktop.Notifications.CloseNotification",
                        nid,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_DISMISS_TIMEOUT_S,
                )
                return
            except Exception:
                pass
        exe = _which("notify-send")
        if exe:
            try:
                _run(
                    [
                        exe,
                        f"--replace-id={nid}",
                        "--expire-time=1",
                        _TITLE,
                        "",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_DISMISS_TIMEOUT_S,
                )
            except Exception:
                pass


@contextmanager
def tts_skip_notification(cfg: Any, text: str) -> Iterator[None]:
    """Show the skip notification for one TTS playback span (B161).

    Enabled by ``tts.notify_skip`` (default on). Clicking Skip stops the
    in-process playback via :func:`hark.audio.playback.request_playback_skip`;
    the notification is closed when the span ends for any reason.
    """
    if not bool(getattr(cfg.tts, "notify_skip", True)):
        yield
        return

    def _skip() -> None:
        from hark.audio.playback import request_playback_skip

        request_playback_skip()

    note = TtsSkipNotification(text, on_skip=_skip)
    if not note.start():
        yield
        return
    try:
        yield
    finally:
        note.close()

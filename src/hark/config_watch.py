"""Watch config.toml for changes and request ambient config reload.

Mode A ambient already reloads on **SIGHUP** via ``lifecycle.request_reload``.
This module adds a **file mtime poll** (with debounce) so editing
``HARK_CONFIG`` / ``~/.config/hark/config.toml`` applies the same path without
remembering ``kill -HUP``.

No third-party deps: pure ``stat`` polling. Optional Linux inotify can be
layered later; mtime is enough for operator-scale edits.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Callable

from hark.lifecycle import request_reload
from hark.paths import default_config_path
from hark.syslog import log as syslog

# Defaults (also exposed as AmbientConfig / DEFAULT_CONFIG_TOML)
DEFAULT_CONFIG_WATCH = True
DEFAULT_CONFIG_WATCH_POLL_MS = 1000
DEFAULT_CONFIG_WATCH_DEBOUNCE_MS = 400


def config_watch_enabled_from_env(default: bool = DEFAULT_CONFIG_WATCH) -> bool:
    """``HARK_CONFIG_WATCH=0|false|off`` disables; ``1|true|on`` forces on."""
    raw = os.environ.get("HARK_CONFIG_WATCH")
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _mtime_ns(path: Path) -> int | None:
    try:
        if not path.is_file():
            return None
        st = path.stat()
        return int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
    except OSError:
        return None


class ConfigFileWatcher:
    """Background mtime poller that calls ``request_reload`` after debounce.

    Parameters
    ----------
    path:
        Config file to watch (typically ``cfg.path`` or ``default_config_path()``).
    poll_s:
        How often to stat the file.
    debounce_s:
        Require mtime to stay stable this long before firing (absorbs atomic
        write races and editors that touch the file multiple times).
    on_change:
        Optional callback; default is ``request_reload(source="config_watch")``.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        poll_s: float = DEFAULT_CONFIG_WATCH_POLL_MS / 1000.0,
        debounce_s: float = DEFAULT_CONFIG_WATCH_DEBOUNCE_MS / 1000.0,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_config_path()
        self.poll_s = max(0.05, float(poll_s))
        self.debounce_s = max(0.0, float(debounce_s))
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_mtime: int | None = _mtime_ns(self.path)
        self._pending_mtime: int | None = None
        self._pending_since: float | None = None
        self._fire_count = 0

    @property
    def fire_count(self) -> int:
        return self._fire_count

    @property
    def last_mtime(self) -> int | None:
        return self._last_mtime

    def start(self) -> ConfigFileWatcher:
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        # Re-baseline so start never fires for the current file state
        self._last_mtime = _mtime_ns(self.path)
        self._pending_mtime = None
        self._pending_since = None
        self._thread = threading.Thread(
            target=self._run,
            name="hark-config-watch",
            daemon=True,
        )
        self._thread.start()
        syslog(
            "config_watch.started",
            component="config_watch",
            path=str(self.path),
            poll_s=self.poll_s,
            debounce_s=self.debounce_s,
            mtime_ns=self._last_mtime,
        )
        return self

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None
        syslog(
            "config_watch.stopped",
            component="config_watch",
            path=str(self.path),
            fire_count=self._fire_count,
        )

    def poll_once(self) -> bool:
        """One poll step. Returns True if a reload was requested this step.

        Public for tests; the background thread calls this on each tick.
        """
        now = time.monotonic()
        mtime = _mtime_ns(self.path)

        # File missing: remember absence; creation is a change
        if mtime is None:
            if self._last_mtime is not None:
                # Deleted — wait for recreate; clear baseline
                self._last_mtime = None
                self._pending_mtime = None
                self._pending_since = None
            return False

        if self._last_mtime is None:
            # File appeared after missing (or never baselined): debounce then fire
            self._arm_pending(mtime, now)
        elif mtime != self._last_mtime:
            self._arm_pending(mtime, now)
        else:
            # Unchanged relative to last applied mtime
            if (
                self._pending_mtime is not None
                and self._pending_mtime == self._last_mtime
            ):
                self._pending_mtime = None
                self._pending_since = None
            return False

        if self._pending_mtime is None or self._pending_since is None:
            return False

        # Still changing? Re-arm debounce window on a different mtime
        if mtime != self._pending_mtime:
            self._arm_pending(mtime, now)
            return False

        if (now - self._pending_since) < self.debounce_s:
            return False

        # Stable long enough
        self._last_mtime = self._pending_mtime
        self._pending_mtime = None
        self._pending_since = None
        self._fire()
        return True

    def _arm_pending(self, mtime: int, now: float) -> None:
        if self._pending_mtime == mtime and self._pending_since is not None:
            return  # same pending target; keep original since for debounce
        self._pending_mtime = mtime
        self._pending_since = now

    def _fire(self) -> None:
        self._fire_count += 1
        syslog(
            "config_watch.changed",
            component="config_watch",
            path=str(self.path),
            mtime_ns=self._last_mtime,
            fire_count=self._fire_count,
        )
        if self._on_change is not None:
            self._on_change()
        else:
            request_reload(source="config_watch")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # pragma: no cover — defensive
                syslog(
                    "config_watch.error",
                    component="config_watch",
                    level="warn",
                    error=str(exc)[:200],
                    path=str(self.path),
                )
            self._stop.wait(self.poll_s)


def start_config_watcher(
    path: Path | str | None,
    *,
    enabled: bool = True,
    poll_ms: int = DEFAULT_CONFIG_WATCH_POLL_MS,
    debounce_ms: int = DEFAULT_CONFIG_WATCH_DEBOUNCE_MS,
) -> ConfigFileWatcher | None:
    """Start a watcher if enabled (config + ``HARK_CONFIG_WATCH`` env).

    Returns ``None`` when disabled. Env override wins: ``0``/``false``/``off``
    disables even when config says true; ``1``/``true``/``on`` enables even
    when config says false.
    """
    if not config_watch_enabled_from_env(default=enabled):
        return None

    watch_path = Path(path) if path is not None else default_config_path()
    return ConfigFileWatcher(
        watch_path,
        poll_s=max(50, int(poll_ms)) / 1000.0,
        debounce_s=max(0, int(debounce_ms)) / 1000.0,
    ).start()

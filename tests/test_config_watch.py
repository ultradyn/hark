"""B036: config.toml mtime file-watch → same reload path as SIGHUP."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path

import hark.ambient as ambient
import hark.lifecycle as lc
from hark.config import load_config
from hark.config_watch import (
    DEFAULT_CONFIG_WATCH,
    DEFAULT_CONFIG_WATCH_DEBOUNCE_MS,
    DEFAULT_CONFIG_WATCH_POLL_MS,
    ConfigFileWatcher,
    config_watch_enabled_from_env,
    start_config_watcher,
)
from hark.lifecycle import (
    clear_reload_request,
    reload_requested,
    reload_source,
    request_reload,
    request_shutdown,
)
from hark.speech import ListenResult


def _write_ambient_config(
    path: Path,
    *,
    extra: list[str] | None = None,
    end_mode: str | None = None,
    config_watch: bool | None = None,
    poll_ms: int | None = None,
    debounce_ms: int | None = None,
) -> None:
    lines = [
        "[ambient]",
        "enabled = true",
        'engine = "text_probe"',
        "snippet_s = 1.0",
        "timeout_s = 5",
        "debug = false",
    ]
    if extra is not None:
        joined = ", ".join(f'"{p}"' for p in extra)
        lines.append(f"extra_trigger_phrases = [{joined}]")
    if config_watch is not None:
        lines.append(f"config_watch = {'true' if config_watch else 'false'}")
    if poll_ms is not None:
        lines.append(f"config_watch_poll_ms = {poll_ms}")
    if debounce_ms is not None:
        lines.append(f"config_watch_debounce_ms = {debounce_ms}")
    if end_mode is not None:
        lines.extend(["", "[listen]", f'end_mode = "{end_mode}"'])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_config_defaults():
    assert DEFAULT_CONFIG_WATCH is True
    assert DEFAULT_CONFIG_WATCH_POLL_MS == 1000
    assert DEFAULT_CONFIG_WATCH_DEBOUNCE_MS == 400


def test_ambient_config_watch_defaults(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(cfg_path, extra=[])
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    monkeypatch.delenv("HARK_CONFIG_WATCH", raising=False)
    cfg = load_config(cfg_path)
    assert cfg.ambient.config_watch is True
    assert cfg.ambient.config_watch_poll_ms == 1000
    assert cfg.ambient.config_watch_debounce_ms == 400


def test_ambient_config_watch_from_toml(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(
        cfg_path, extra=[], config_watch=False, poll_ms=250, debounce_ms=100
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_path)
    assert cfg.ambient.config_watch is False
    assert cfg.ambient.config_watch_poll_ms == 250
    assert cfg.ambient.config_watch_debounce_ms == 100


def test_env_config_watch_override(monkeypatch):
    monkeypatch.delenv("HARK_CONFIG_WATCH", raising=False)
    assert config_watch_enabled_from_env(default=True) is True
    assert config_watch_enabled_from_env(default=False) is False
    monkeypatch.setenv("HARK_CONFIG_WATCH", "0")
    assert config_watch_enabled_from_env(default=True) is False
    monkeypatch.setenv("HARK_CONFIG_WATCH", "true")
    assert config_watch_enabled_from_env(default=False) is True


def test_poll_once_mtime_triggers_reload(tmp_path):
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(cfg_path, extra=[])
    clear_reload_request()
    fired: list[str] = []

    w = ConfigFileWatcher(
        cfg_path,
        poll_s=0.05,
        debounce_s=0.0,
        on_change=lambda: (
            fired.append("yes"),
            request_reload(source="config_watch"),
        ),
    )
    # Baseline current mtime without starting the thread
    w._last_mtime = cfg_path.stat().st_mtime_ns
    assert w.poll_once() is False
    assert fired == []
    assert reload_requested() is False

    time.sleep(0.01)
    _write_ambient_config(cfg_path, extra=["start prompt"])
    # Ensure mtime advances (some FS have 1s resolution; bump if needed)
    if cfg_path.stat().st_mtime_ns == w._last_mtime:
        st = cfg_path.stat()
        # rewrite again after tiny delay
        time.sleep(0.02)
        _write_ambient_config(cfg_path, extra=["start prompt"])
        if cfg_path.stat().st_mtime_ns == w._last_mtime:
            # force touch via utime
            import os

            now = time.time() + 1
            os.utime(cfg_path, (now, now))

    assert w.poll_once() is True
    assert fired == ["yes"]
    assert reload_requested() is True
    assert reload_source() == "config_watch"
    clear_reload_request()


def test_debounce_waits_for_stable_mtime(tmp_path):
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(cfg_path, extra=[])
    clear_reload_request()
    count = {"n": 0}

    w = ConfigFileWatcher(
        cfg_path,
        poll_s=0.01,
        debounce_s=0.15,
        on_change=lambda: count.__setitem__("n", count["n"] + 1)
        or request_reload(source="config_watch"),
    )
    w._last_mtime = cfg_path.stat().st_mtime_ns

    _write_ambient_config(cfg_path, extra=["a"])
    import os

    os.utime(cfg_path, None)
    # First poll arms pending, should not fire yet (debounce)
    assert w.poll_once() is False
    assert count["n"] == 0

    # Rapid second write while pending — re-arms, still no fire
    time.sleep(0.02)
    _write_ambient_config(cfg_path, extra=["a", "b"])
    os.utime(cfg_path, (time.time() + 2, time.time() + 2))
    assert w.poll_once() is False
    assert count["n"] == 0

    # Wait out debounce with stable mtime
    time.sleep(0.16)
    assert w.poll_once() is True
    assert count["n"] == 1
    # Stable again — no second fire
    assert w.poll_once() is False
    assert count["n"] == 1
    clear_reload_request()


def test_start_config_watcher_respects_disable(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(cfg_path)
    monkeypatch.setenv("HARK_CONFIG_WATCH", "0")
    assert start_config_watcher(cfg_path, enabled=True) is None
    monkeypatch.delenv("HARK_CONFIG_WATCH", raising=False)
    assert start_config_watcher(cfg_path, enabled=False) is None
    w = start_config_watcher(cfg_path, enabled=True, poll_ms=200, debounce_ms=50)
    assert w is not None
    try:
        assert w.path == cfg_path
    finally:
        w.stop()


def test_ambient_loop_file_watch_reload(tmp_path, monkeypatch):
    """Edit config on disk → watcher → ambient.reloaded (no SIGHUP)."""
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(
        cfg_path,
        extra=[],
        config_watch=True,
        poll_ms=50,
        debounce_ms=30,
        end_mode="silence",
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    monkeypatch.delenv("HARK_CONFIG_WATCH", raising=False)
    cfg = load_config(cfg_path)
    assert cfg.path == cfg_path

    monkeypatch.setattr("hark.audio.capture.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(ambient, "ambient_pause_requested", lambda: False)
    monkeypatch.setattr(lc, "state_dir", lambda: tmp_path / "state")
    lc._shutdown = False
    clear_reload_request()

    phase = {"n": 0}

    def next_pcm() -> bytes:
        phase["n"] += 1
        if phase["n"] == 1:
            # Operator edits config mid-wait; watcher will request_reload
            time.sleep(0.05)
            _write_ambient_config(
                cfg_path,
                extra=["start prompt"],
                config_watch=True,
                poll_ms=50,
                debounce_ms=30,
                end_mode="radio",
            )
            import os

            os.utime(cfg_path, (time.time() + 5, time.time() + 5))
            # Wait for watcher poll + debounce
            deadline = time.time() + 2.0
            while time.time() < deadline and not reload_requested():
                time.sleep(0.03)
            return b"TXT:unrelated speech"
        return b"TXT:start prompt deploy now"

    listened = ListenResult(
        text="deploy now",
        provider="mock-stt",
        duration_ms=10,
        end_mode="radio",
    )

    def fake_listen(cfg, **kwargs):
        request_shutdown(reason="stop")
        return listened

    # Minimal ContinuousMicStream stand-in (same pattern as test_custom_wake_e2e)
    class FakeStream:
        def __init__(self, *a, **k):
            self._last = b"TXT:silence"

        def open(self):
            return self

        def close(self):
            pass

        def __enter__(self):
            return self.open()

        def __exit__(self, *a):
            pass

        @property
        def available_s(self):
            return 5.0

        def read_for(self, duration_s, *, should_stop=None):
            if should_stop is not None and should_stop():
                return False
            self._last = next_pcm()
            return True

        def window_pcm16(self, duration_s, *, end_offset_s=0.0):
            return self._last

    monkeypatch.setattr(ambient, "ContinuousMicStream", FakeStream)
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(ambient, "run_tts", lambda *a, **k: None)

    out = io.StringIO()
    cfg.ambient.timeout_s = 3.0
    cfg.ambient.config_watch_poll_ms = 50
    cfg.ambient.config_watch_debounce_ms = 30
    rc = ambient.run_ambient_loop(cfg, out=out, announce=False, idle_log_s=999)
    assert rc == 0

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    kinds = [e.get("kind") for e in events]
    assert "ambient.armed" in kinds
    assert "ambient.reloaded" in kinds, kinds
    assert "ambient.prompt" in kinds

    reloaded = next(e for e in events if e["kind"] == "ambient.reloaded")
    assert reloaded.get("source") == "config_watch"
    assert "start prompt" in (reloaded.get("phrases") or [])
    assert reloaded.get("end_mode") == "radio"
    assert reloaded.get("path") == str(cfg_path)

    prompt = next(e for e in events if e["kind"] == "ambient.prompt")
    assert prompt["phrase"] == "start prompt"
    assert prompt["text"] == "deploy now"

    lc._shutdown = False
    clear_reload_request()


def test_reload_source_sighup_label():
    clear_reload_request()
    request_reload(signum=getattr(__import__("signal"), "SIGHUP", 1), source="sighup")
    assert reload_source() == "sighup"
    clear_reload_request()
    assert reload_source() is None

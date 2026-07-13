"""B020: custom activation phrase end-to-end (no live mic / cloud STT)."""

from __future__ import annotations

import io
import json
import signal
from collections.abc import Callable
from pathlib import Path

import hark.ambient as ambient
import hark.lifecycle as lc
from hark.config import load_config
from hark.lifecycle import (
    clear_reload_request,
    install_signal_handlers,
    reload_requested,
    request_reload,
    request_shutdown,
)
from hark.speech import ListenResult
from hark.wake import TextProbeBackend, build_wake_backend


def _fake_continuous_mic(next_pcm: Callable[[], bytes]):
    """Stand-in for ContinuousMicStream that yields synthetic score windows."""

    class FakeStream:
        def __init__(self, *args, **kwargs) -> None:
            self._last = b"TXT:silence"
            self._open = False

        def open(self):
            self._open = True
            return self

        def close(self) -> None:
            self._open = False

        def __enter__(self):
            return self.open()

        def __exit__(self, *a) -> None:
            self.close()

        @property
        def is_open(self) -> bool:
            return self._open

        @property
        def available_s(self) -> float:
            return 5.0

        def read_for(self, duration_s: float, *, should_stop=None) -> bool:
            if should_stop is not None and should_stop():
                return False
            self._last = next_pcm()
            return True

        def window_pcm16(self, duration_s: float, *, end_offset_s: float = 0.0) -> bytes:
            return self._last

        def tail_ms(self, ms: int) -> bytes:
            return self._last

    return FakeStream


def _write_ambient_config(
    path: Path,
    *,
    extra: list[str] | None = None,
    trigger: list[str] | None = None,
) -> None:
    lines = [
        "[ambient]",
        "enabled = true",
        'engine = "text_probe"',
        "snippet_s = 1.0",
        "timeout_s = 5",
        "debug = true",
    ]
    if trigger is not None:
        joined = ", ".join(f'"{p}"' for p in trigger)
        lines.append(f"trigger_phrases = [{joined}]")
    if extra is not None:
        joined = ", ".join(f'"{p}"' for p in extra)
        lines.append(f"extra_trigger_phrases = [{joined}]")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_config_extra_trigger_builds_text_probe_hit(tmp_path, monkeypatch):
    """Load TOML with extra_trigger_phrases → backend scores custom wake."""
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(cfg_path, extra=["start prompt"])
    monkeypatch.delenv("HARK_AMBIENT", raising=False)

    cfg = load_config(cfg_path)
    assert "start prompt" in cfg.ambient.activation_phrases
    assert "hey hark" in cfg.ambient.activation_phrases

    backend = build_wake_backend(
        cfg.ambient.engine,
        phrases=cfg.ambient.activation_phrases,
        model_path=cfg.ambient.model_path,
    )
    assert isinstance(backend, TextProbeBackend)

    miss = backend.score_snippet(b"TXT:please open the PR")
    assert miss is None

    hit = backend.score_snippet(b"TXT:start prompt do the thing")
    assert hit is not None
    assert hit.phrase == "start prompt"
    assert "do the thing" in hit.remainder
    assert hit.backend == "text_probe"


def test_trigger_phrases_replace_only_custom(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(cfg_path, trigger=["start prompt"])
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_path)
    backend = build_wake_backend(
        "text_probe", phrases=cfg.ambient.activation_phrases
    )
    assert backend.score_snippet(b"TXT:hey hark ship it") is None
    hit = backend.score_snippet(b"TXT:start prompt ship it")
    assert hit is not None
    assert hit.phrase == "start prompt"


def test_ambient_cycle_custom_phrase_to_prompt(tmp_path, monkeypatch):
    """One ambient cycle: custom wake (mock mic) → cloud STT path → ambient.prompt."""
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(cfg_path, extra=["start prompt"])
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_path)
    cfg.ambient.debug = True

    # Isolate mic lease + debug snips
    monkeypatch.setattr("hark.audio.capture.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr("hark.debug_snips.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr("hark.debug_snips.debug_wake_dir", lambda: tmp_path / "state" / "debug" / "wake")
    monkeypatch.setattr(ambient, "ambient_pause_requested", lambda: False)
    monkeypatch.setattr(lc, "state_dir", lambda: tmp_path / "state")
    lc._shutdown = False
    clear_reload_request()

    snippets = [b"TXT:noise only", b"TXT:start prompt please open the PR"]

    def next_pcm() -> bytes:
        if snippets:
            return snippets.pop(0)
        return b"TXT:silence"

    listened = ListenResult(
        text="please open the PR",
        provider="mock-stt",
        duration_ms=50,
        end_mode="silence",
        stream_id="stream-custom",
        partials_emitted=0,
    )
    monkeypatch.setattr(ambient, "ContinuousMicStream", _fake_continuous_mic(next_pcm))
    monkeypatch.setattr(
        ambient,
        "run_listen",
        lambda cfg, **kwargs: listened,
    )

    backend = build_wake_backend(
        "text_probe", phrases=cfg.ambient.activation_phrases
    )
    result = ambient.run_ambient(
        cfg,
        once=True,
        timeout_s=3.0,
        announce=False,
        backend=backend,
    )

    assert result.activated is True
    assert result.phrase == "start prompt"
    assert result.text == "please open the PR"
    assert result.listen is not None
    assert result.listen["provider"] == "mock-stt"
    assert result.wake_backend == "text_probe"

    line = ambient.ambient_event_line(result)
    assert line["kind"] == "ambient.prompt"
    assert line["phrase"] == "start prompt"
    assert line["text"] == "please open the PR"

    # debug snip for the hit should exist when ambient.debug is on
    wake_dir = tmp_path / "state" / "debug" / "wake"
    hits = list(wake_dir.rglob("*-hit.json")) if wake_dir.is_dir() else []
    assert hits, "expected debug wake hit sidecar when ambient.debug=true"
    meta = json.loads(hits[0].read_text(encoding="utf-8"))
    assert meta["matched"] is True
    assert meta["phrase"] == "start prompt"


def test_apply_config_reload_hot_updates_phrases(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(cfg_path, extra=["start prompt"])
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_path)
    backend = build_wake_backend(
        "text_probe", phrases=cfg.ambient.activation_phrases
    )
    assert backend.score_snippet(b"TXT:begin dictation now") is None

    # Operator adds another custom phrase on disk
    _write_ambient_config(
        cfg_path, extra=["start prompt", "begin dictation"]
    )
    new_cfg, new_backend, info = ambient.apply_config_reload(cfg, backend)
    assert new_backend is backend  # in-place phrase update
    assert info["rebuilt_backend"] is False
    assert "begin dictation" in info["phrases"]
    assert "begin dictation" in backend.phrases
    hit = backend.score_snippet(b"TXT:begin dictation ship it")
    assert hit is not None
    assert hit.phrase == "begin dictation"
    assert new_cfg.ambient.enabled is True


def test_apply_config_reload_rebuilds_on_engine_change(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.toml"
    _write_ambient_config(cfg_path, extra=["start prompt"])
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_path)
    backend = build_wake_backend(
        "text_probe", phrases=cfg.ambient.activation_phrases
    )

    cfg_path.write_text(
        """
[ambient]
enabled = true
engine = "mock"
trigger_phrases = ["only this"]
""",
        encoding="utf-8",
    )
    new_cfg, new_backend, info = ambient.apply_config_reload(cfg, backend)
    assert info["rebuilt_backend"] is True
    assert new_backend is not backend
    assert new_cfg.ambient.activation_phrases == ["only this"]
    assert new_backend.score_snippet(b"TXT:only this go") is not None
    assert new_backend.score_snippet(b"TXT:start prompt go") is None


def test_apply_config_reload_detects_wake_label_name_change(tmp_path, monkeypatch):
    """Live-reload of primary name flags wake_label_changed for TTS announce."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        """
[ambient]
enabled = true
engine = "text_probe"
wake_mode = "names"
names = ["hark", "herald"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_path)
    backend = build_wake_backend(
        "text_probe", phrases=cfg.ambient.activation_phrases
    )
    assert ambient.primary_wake_label(cfg) == "hey hark"

    cfg_path.write_text(
        """
[ambient]
enabled = true
engine = "text_probe"
wake_mode = "names"
names = ["clanker", "hark", "herald"]
""",
        encoding="utf-8",
    )
    new_cfg, _new_backend, info = ambient.apply_config_reload(cfg, backend)
    assert info["wake_label_changed"] is True
    assert info["wake_label_prev"] == "hey hark"
    assert info["wake_label"] == "hey clanker"
    assert ambient.primary_wake_label(new_cfg) == "hey clanker"

    # Second reload with same names → no change
    _, _, info2 = ambient.apply_config_reload(new_cfg, backend)
    assert info2["wake_label_changed"] is False


def test_request_reload_and_clear():
    clear_reload_request()
    assert reload_requested() is False
    request_reload(signum=1)
    assert reload_requested() is True
    clear_reload_request()
    assert reload_requested() is False


def test_sighup_sets_reload_flag(monkeypatch):
    if not hasattr(signal, "SIGHUP"):
        return
    clear_reload_request()
    lc._handlers_installed = False
    # Avoid clobbering pytest's handlers permanently more than needed
    install_signal_handlers()
    assert reload_requested() is False
    request_reload()  # direct path (signal delivery is OS-dependent in CI)
    assert reload_requested() is True
    # Also exercise the real SIGHUP path when safe
    clear_reload_request()
    signal.raise_signal(signal.SIGHUP)
    assert reload_requested() is True
    clear_reload_request()


def test_ambient_loop_reloads_then_wakes(tmp_path, monkeypatch):
    """Loop: start with defaults → SIGHUP reload custom phrase → wake → stop."""
    cfg_path = tmp_path / "config.toml"
    # Initially only defaults (no custom)
    _write_ambient_config(cfg_path, extra=[])
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_path)

    monkeypatch.setattr("hark.audio.capture.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(ambient, "ambient_pause_requested", lambda: False)
    monkeypatch.setattr(lc, "state_dir", lambda: tmp_path / "state")
    lc._shutdown = False
    clear_reload_request()

    # After reload, config gains custom phrase. Snippets: first miss default,
    # then after reload hit custom. We schedule reload before second score.
    phase = {"n": 0, "reloaded_written": False}

    def next_pcm() -> bytes:
        phase["n"] += 1
        if phase["n"] == 1:
            # First wait: no hit; request reload mid-wait
            request_reload()
            return b"TXT:unrelated speech"
        # After loop reloads, custom phrase is armed
        return b"TXT:start prompt deploy now"

    listened = ListenResult(
        text="deploy now",
        provider="mock-stt",
        duration_ms=10,
        end_mode="silence",
    )

    def fake_listen(cfg, **kwargs):
        # Stop loop after successful activation
        request_shutdown(reason="stop")
        return listened

    monkeypatch.setattr(ambient, "ContinuousMicStream", _fake_continuous_mic(next_pcm))
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(ambient, "run_tts", lambda *a, **k: None)

    # Simulate operator editing config before/at HUP (apply reads disk)
    real_apply = ambient.apply_config_reload

    def apply_with_disk_edit(cfg, backend):
        _write_ambient_config(cfg_path, extra=["start prompt"])
        phase["reloaded_written"] = True
        return real_apply(cfg, backend)

    monkeypatch.setattr(ambient, "apply_config_reload", apply_with_disk_edit)

    out = io.StringIO()
    # Short timeouts so a missed wake does not hang
    cfg.ambient.timeout_s = 2.0
    rc = ambient.run_ambient_loop(cfg, out=out, announce=False, idle_log_s=999)
    assert rc == 0

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    kinds = [e.get("kind") for e in events]
    assert "ambient.armed" in kinds
    assert "ambient.reloaded" in kinds
    assert "ambient.prompt" in kinds
    assert phase["reloaded_written"] is True

    reloaded = next(e for e in events if e["kind"] == "ambient.reloaded")
    assert "start prompt" in (reloaded.get("phrases") or [])

    prompt = next(e for e in events if e["kind"] == "ambient.prompt")
    assert prompt["phrase"] == "start prompt"
    assert prompt["text"] == "deploy now"
    assert prompt.get("listen", {}).get("provider") == "mock-stt"

    # cleanup lifecycle
    lc._shutdown = False
    clear_reload_request()

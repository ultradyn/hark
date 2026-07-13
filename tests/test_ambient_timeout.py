"""B033: gate ambient.timeout emission (surface_timeouts) + timeout_s=0."""

from __future__ import annotations

import io
import json
import math

import hark.ambient as ambient
import hark.lifecycle as lc
from hark.ambient import AmbientResult, _wake_deadline, ambient_event_line
from hark.config import HarkConfig, config_to_dict, load_config
from hark.lifecycle import clear_reload_request, request_shutdown


def test_surface_timeouts_default_on():
    cfg = HarkConfig()
    assert cfg.ambient.surface_timeouts is True


def test_surface_timeouts_from_toml_default(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text("[ambient]\nenabled = true\n", encoding="utf-8")
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(path)
    assert cfg.ambient.surface_timeouts is True
    assert config_to_dict(cfg)["ambient"]["surface_timeouts"] is True
    # known key — no unknown warning
    assert not any("surface_timeouts" in w for w in cfg.warnings)


def test_surface_timeouts_off_from_toml(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        "[ambient]\nsurface_timeouts = false\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(path)
    assert cfg.ambient.surface_timeouts is False


def test_emit_timeout_events_alias(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        "[ambient]\nemit_timeout_events = false\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(path)
    assert cfg.ambient.surface_timeouts is False
    assert not any("emit_timeout_events" in w for w in cfg.warnings)


def test_surface_timeouts_wins_over_alias(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        "[ambient]\nsurface_timeouts = true\nemit_timeout_events = false\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(path)
    assert cfg.ambient.surface_timeouts is True


def test_wake_deadline_default_and_zero():
    d = _wake_deadline(None, 300.0)
    assert math.isfinite(d)
    d0 = _wake_deadline(0, 300.0)
    assert d0 == float("inf")
    d_none_cfg = _wake_deadline(None, 0.0)
    assert d_none_cfg == float("inf")
    d_neg = _wake_deadline(-1.0, None)
    assert d_neg == float("inf")
    # call arg overrides config
    d_arg = _wake_deadline(10.0, 0.0)
    assert math.isfinite(d_arg)


def test_ambient_event_line_timeout_kind():
    r = AmbientResult(activated=False, phrase=None, text=None)
    line = ambient_event_line(r)
    assert line["kind"] == "ambient.timeout"


def _loop_with_timeout_then_stop(
    monkeypatch,
    tmp_path,
    *,
    surface_timeouts: bool,
    timeout_s: float = 0.05,
):
    """Drive run_ambient_loop: one idle timeout, then shutdown."""
    cfg = HarkConfig()
    cfg.ambient.enabled = True
    cfg.ambient.engine = "text_probe"
    cfg.ambient.timeout_s = timeout_s
    cfg.ambient.surface_timeouts = surface_timeouts
    cfg.ambient.model_path = None

    lc._shutdown = False
    clear_reload_request()

    calls = {"n": 0}
    syslog_kinds: list[str] = []

    def fake_run_ambient(cfg, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            request_shutdown(reason="stop")
        return AmbientResult(
            activated=False,
            phrase=None,
            text=None,
            wake_backend="text_probe",
        )

    def fake_syslog(kind, **kwargs):
        syslog_kinds.append(str(kind))

    class Backend:
        def score_snippet(self, *a, **k):
            return None

    monkeypatch.setattr(ambient, "run_ambient", fake_run_ambient)
    monkeypatch.setattr(ambient, "syslog", fake_syslog)
    monkeypatch.setattr(ambient, "run_tts", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "build_wake_backend", lambda *a, **k: Backend())
    monkeypatch.setattr(ambient, "install_signal_handlers", lambda: None)
    # B104 dual-write: isolate ambient.jsonl under tmp_path
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)

    out = io.StringIO()
    rc = ambient.run_ambient_loop(cfg, out=out, announce=False, idle_log_s=999)
    assert rc == 0
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    kinds = [e.get("kind") for e in events]
    # cleanup
    lc._shutdown = False
    clear_reload_request()
    return kinds, syslog_kinds, events


def test_loop_default_surfaces_timeout(monkeypatch, tmp_path):
    kinds, syslog_kinds, _ = _loop_with_timeout_then_stop(
        monkeypatch, tmp_path, surface_timeouts=True
    )
    assert "ambient.armed" in kinds
    assert "ambient.timeout" in kinds
    assert "ambient.timeout" in syslog_kinds


def test_loop_surface_timeouts_off_suppresses(monkeypatch, tmp_path):
    kinds, syslog_kinds, _ = _loop_with_timeout_then_stop(
        monkeypatch, tmp_path, surface_timeouts=False
    )
    assert "ambient.armed" in kinds
    assert "ambient.timeout" not in kinds
    assert "ambient.timeout" not in syslog_kinds
    # still ran more than one cycle (timeout still ticks the loop)
    # armed only once; no timeout lines


def test_timeout_s_zero_no_deadline_in_run_ambient(monkeypatch):
    """timeout_s=0 → wait until shutdown/reload, not ambient.timeout after 300s."""
    cfg = HarkConfig()
    cfg.ambient.enabled = True
    cfg.ambient.engine = "text_probe"
    cfg.ambient.timeout_s = 0.0

    seen: dict = {}

    def fake_wait(backend, *, deadline, **kwargs):
        seen["deadline"] = deadline
        # pretend shutdown so we exit cleanly
        return None

    monkeypatch.setattr(ambient, "_wait_for_wake", fake_wait)
    monkeypatch.setattr(ambient, "build_wake_backend", lambda *a, **k: object())
    monkeypatch.setattr(lc, "shutdown_requested", lambda: False)

    result = ambient.run_ambient(cfg, once=True, timeout_s=0, announce=False)
    assert result.activated is False
    assert seen["deadline"] == float("inf")


def test_loop_dual_writes_prompt_when_stdout_is_restart_log(monkeypatch, tmp_path):
    """B104: ambient.prompt lands in ambient.jsonl even if out → restart log."""
    restart = tmp_path / "ambient-restart.log"
    feed = tmp_path / "ambient.jsonl"
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)

    cfg = HarkConfig()
    cfg.ambient.enabled = True
    cfg.ambient.engine = "text_probe"
    cfg.ambient.timeout_s = 0.05
    cfg.ambient.surface_timeouts = False
    cfg.ambient.model_path = None

    lc._shutdown = False
    clear_reload_request()
    calls = {"n": 0}

    def fake_run_ambient(cfg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return AmbientResult(
                activated=True,
                phrase="hey hark",
                text="ship the dual-write fix over",
                wake_backend="text_probe",
                event_id="b104-loop-prompt",
                stream_id="s-b104",
                final=True,
                partial=False,
                listen={"provider": "mock", "duration_ms": 10},
            )
        request_shutdown(reason="stop")
        return AmbientResult(activated=False, phrase=None, text=None)

    class Backend:
        def score_snippet(self, *a, **k):
            return None

    monkeypatch.setattr(ambient, "run_ambient", fake_run_ambient)
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "run_tts", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "build_wake_backend", lambda *a, **k: Backend())
    monkeypatch.setattr(ambient, "install_signal_handlers", lambda: None)

    with restart.open("a", encoding="utf-8") as out:
        rc = ambient.run_ambient_loop(cfg, out=out, announce=False, idle_log_s=999)
    assert rc == 0

    restart_text = restart.read_text(encoding="utf-8")
    feed_text = feed.read_text(encoding="utf-8")
    assert "b104-loop-prompt" in restart_text
    assert "b104-loop-prompt" in feed_text
    assert '"kind":"ambient.prompt"' in feed_text or '"kind": "ambient.prompt"' in feed_text

    feed_events = [
        json.loads(line) for line in feed_text.splitlines() if line.strip()
    ]
    prompts = [e for e in feed_events if e.get("kind") == "ambient.prompt"]
    assert len(prompts) == 1
    assert prompts[0]["text"] == "ship the dual-write fix over"
    assert prompts[0]["event_id"] == "b104-loop-prompt"

    # monitor compact path still sees it
    from hark.monitor_feed import MODE_A_WAKE_KINDS, replay_matching

    out_mon = io.StringIO()
    n = replay_matching(
        [feed], kinds=MODE_A_WAKE_KINDS, limit=20, for_monitor=True, out=out_mon
    )
    assert n >= 1
    mon_kinds = [
        json.loads(l).get("kind") for l in out_mon.getvalue().splitlines() if l.strip()
    ]
    assert "ambient.prompt" in mon_kinds

    lc._shutdown = False
    clear_reload_request()

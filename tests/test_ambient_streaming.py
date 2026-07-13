"""B098: ambient.streaming config + partial instruction policy."""

from __future__ import annotations

from hark.config import AmbientConfig, HarkConfig, config_to_dict, load_config
from hark.monitor_feed import compact_mode_a_event
from hark.partial import (
    HOLD_COMPACT_INSTRUCTIONS,
    HOLD_INSTRUCTIONS,
    STREAMING_COMPACT_INSTRUCTIONS,
    STREAMING_INSTRUCTIONS,
    make_partial_event,
)


def test_ambient_streaming_default_false(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("version = 1\n[ambient]\nenabled = true\n", encoding="utf-8")
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.ambient.streaming is False
    d = config_to_dict(cfg)
    assert d["ambient"]["streaming"] is False


def test_ambient_streaming_loads_true(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
version = 1
[ambient]
enabled = true
streaming = true
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.ambient.streaming is True
    d = config_to_dict(cfg)
    assert d["ambient"]["streaming"] is True


def test_ambient_streaming_string_bool(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[ambient]\nstreaming = "yes"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.ambient.streaming is True


def test_ambient_config_dataclass_default():
    assert AmbientConfig().streaming is False
    assert HarkConfig().ambient.streaming is False


def test_partial_instructions_differ_hold_vs_streaming():
    hold = make_partial_event(stream_id="s1", seq=1, text="hey", streaming=False)
    live = make_partial_event(stream_id="s1", seq=2, text="hey", streaming=True)
    assert hold["instructions"] == HOLD_INSTRUCTIONS
    assert live["instructions"] == STREAMING_INSTRUCTIONS
    assert hold["instructions"] != live["instructions"]
    assert "HOLD" in hold["instructions"]
    assert "STREAMING" in live["instructions"]
    assert "short live" in live["instructions"].lower() or "brief" in live[
        "instructions"
    ].lower()
    # Both still require listen-end backup
    for ev in (hold, live):
        assert "MUST" in ev["instructions"]
        assert "end_recording" in ev["instructions"]
        assert ev["partial"] is True
        assert ev["final"] is False


def test_monitor_compact_partial_hold_vs_streaming():
    base = {
        "kind": "ambient.partial",
        "event_id": "e1",
        "stream_id": "s1",
        "seq": 1,
        "text": "please ship it over",
        "fragment": "over",
    }
    hold = compact_mode_a_event({**base, "streaming": False})
    live = compact_mode_a_event({**base, "streaming": True})
    # default missing streaming → HOLD
    default = compact_mode_a_event(base)

    assert hold["streaming"] is False
    assert live["streaming"] is True
    assert default["streaming"] is False
    assert hold["instructions"] == HOLD_COMPACT_INSTRUCTIONS
    assert live["instructions"] == STREAMING_COMPACT_INSTRUCTIONS
    assert hold["instructions"] != live["instructions"]
    assert "HOLD" in hold["instructions"]
    assert "STREAMING" in live["instructions"]
    assert "short live TTS" in live["instructions"] or "brief" in live["instructions"].lower()
    assert "listen-end" in hold["instructions"]
    assert "listen-end" in live["instructions"]
    assert "MUST" in hold["instructions"]
    assert "MUST" in live["instructions"]

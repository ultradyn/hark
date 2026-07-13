"""B007: endpoint config plumbing (TOML + env + defaults + round-trip)."""

from __future__ import annotations

from hark.config import config_to_dict, load_config


def test_endpoint_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.listen.endpoint_strategy == "energy"
    assert cfg.listen.endpoint_probe_silence_s == 0.0
    assert cfg.listen.endpoint_max_silence_s == 0.0
    assert cfg.listen.smart_turn_model_path is None
    assert cfg.listen.smart_turn_threshold == 0.5
    # No spurious warnings for the default (energy) config
    assert not [w for w in cfg.warnings if "endpoint" in w or "smart_turn" in w]


def test_endpoint_toml_loads(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[listen]
endpoint_strategy = "smart_turn"
endpoint_probe_silence_s = 0.4
endpoint_max_silence_s = 3.0
smart_turn_model_path = "/models/st.onnx"
smart_turn_threshold = 0.7
""",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.listen.endpoint_strategy == "smart_turn"
    assert cfg.listen.endpoint_probe_silence_s == 0.4
    assert cfg.listen.endpoint_max_silence_s == 3.0
    assert cfg.listen.smart_turn_model_path == "/models/st.onnx"
    assert cfg.listen.smart_turn_threshold == 0.7
    # No unknown-key warnings for the new keys
    assert not [w for w in cfg.warnings if "listen." in w]


def test_endpoint_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HARK_LISTEN_ENDPOINT_STRATEGY", "smart_turn")
    monkeypatch.setenv("HARK_SMART_TURN_MODEL", "/env/model.onnx")
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.listen.endpoint_strategy == "smart_turn"
    assert cfg.listen.smart_turn_model_path == "/env/model.onnx"


def test_endpoint_config_to_dict_roundtrip(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    d = config_to_dict(cfg)
    assert d["listen"]["endpoint_strategy"] == "energy"
    assert "smart_turn_threshold" in d["listen"]


def test_unknown_endpoint_key_warns(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[listen]\nendpoint_strat = 'smart_turn'\n", encoding="utf-8")
    cfg = load_config(path)
    assert "unknown config key: listen.endpoint_strat" in cfg.warnings

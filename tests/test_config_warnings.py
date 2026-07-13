import hark.cli as cli
from hark.config import load_config


def test_load_config_warns_for_unknown_nested_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
[herdr]
unexpected = true
[[herdr.sessions]]
id = "local"
socket_typo = "/tmp/herdr.sock"
[listen]
end_mod = "radio"
[ambient]
enabled = false
snippit_s = 3
""",
        encoding="utf-8",
    )

    cfg = load_config(path)

    assert "unknown config key: herdr.unexpected" in cfg.warnings
    assert "unknown config key: herdr.sessions[0].socket_typo" in cfg.warnings
    assert "unknown config key: listen.end_mod" in cfg.warnings
    assert "unknown config key: ambient.snippit_s" in cfg.warnings


def test_cli_emits_config_warnings(tmp_path, capsys):
    path = tmp_path / "config.toml"
    path.write_text("[listen]\nend_mod = 'radio'\n", encoding="utf-8")

    assert cli.main(["--config", str(path), "config", "show"]) == 0

    assert "hark config warning: unknown config key: listen.end_mod" in capsys.readouterr().err

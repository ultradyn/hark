from hark.config import load_config
from hark.listen_end import (
    DEFAULT_CANCEL_PHRASES,
    DEFAULT_END_PHRASES,
    EndMode,
    evaluate_radio_transcript,
    find_terminal_phrase,
    parse_end_mode,
    should_keep_listening,
)


def test_defaults_are_product_scoped():
    for p in DEFAULT_CANCEL_PHRASES:
        assert "hark" in p.lower()
    assert "cancel that" not in DEFAULT_CANCEL_PHRASES
    assert "send it" not in DEFAULT_END_PHRASES
    assert "over" not in DEFAULT_END_PHRASES


def test_parse_end_mode():
    assert parse_end_mode("radio") is EndMode.RADIO
    assert parse_end_mode("silence") is EndMode.SILENCE


def test_end_phrase_hark_send():
    hit = evaluate_radio_transcript("Please use black formatting okay hark send")
    assert hit is not None
    assert hit.kind == "end"
    assert hit.phrase == "okay hark send"
    assert hit.body == "please use black formatting"


def test_end_prompt():
    hit = evaluate_radio_transcript("refactor the auth module. End prompt!")
    assert hit is not None
    assert hit.phrase == "end prompt"
    assert "refactor" in hit.body


def test_casual_cancel_does_not_fire():
    assert evaluate_radio_transcript("actually wait cancel that") is None
    assert evaluate_radio_transcript("never mind the old approach") is None


def test_hark_cancel():
    hit = evaluate_radio_transcript("scratch this whole idea hark cancel")
    assert hit is not None
    assert hit.kind == "cancel"


def test_no_match_keeps_listening():
    assert evaluate_radio_transcript("I am still thinking about the design") is None
    keep, hit = should_keep_listening(
        EndMode.RADIO,
        "I am still thinking about the design",
        silence_would_end=True,
    )
    assert keep is True
    assert hit is None


def test_silence_mode_respects_silence_flag():
    keep, _ = should_keep_listening(
        EndMode.SILENCE, "hello world", silence_would_end=True
    )
    assert keep is False


def test_radio_ignores_silence_until_phrase():
    keep, hit = should_keep_listening(
        "radio",
        "long thoughtful answer without terminator",
        silence_would_end=True,
    )
    assert keep is True
    keep2, hit2 = should_keep_listening(
        "radio",
        "long thoughtful answer hark send",
        silence_would_end=False,
    )
    assert keep2 is False
    assert hit2 is not None and hit2.kind == "end"


def test_word_boundary():
    assert find_terminal_phrase("handover", ["over"], kind="end") is None


def test_config_loads_listen(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
version = 1
[listen]
end_mode = "radio"
end_phrases = ["end prompt", "hark send"]
max_listen_s = 120
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.listen.end_mode == "radio"
    assert cfg.listen.end_phrases == ["end prompt", "hark send"]


def test_env_overrides_end_mode(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[listen]\nend_mode = "silence"\n', encoding="utf-8")
    monkeypatch.setenv("HARK_LISTEN_END_MODE", "radio")
    cfg = load_config(cfg_file)
    assert cfg.listen.end_mode == "radio"


def test_config_warns_for_unknown_nested_keys_in_all_sections(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[herdr]
unknown_herdr = true
[[herdr.sessions]]
id = "local"
unknown_session = true
[watch]
unknown_watch = true
[audio]
unknown_audio = true
[listen]
unknown_listen = true
[ambient]
unknown_ambient = true
[stt]
unknown_stt = true
[tts]
unknown_tts = true
[confirm]
unknown_confirm = true
[safety]
unknown_safety = true
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)

    cfg = load_config(cfg_file)

    for name in (
        "herdr.unknown_herdr",
        "herdr.sessions[0].unknown_session",
        "watch.unknown_watch",
        "audio.unknown_audio",
        "listen.unknown_listen",
        "ambient.unknown_ambient",
        "stt.unknown_stt",
        "tts.unknown_tts",
        "confirm.unknown_confirm",
        "safety.unknown_safety",
    ):
        assert any(name in warning for warning in cfg.warnings)


def test_cli_prints_config_warnings_to_stderr_on_normal_startup(tmp_path, capsys):
    from hark.cli import main

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[watch]\nunknown_watch = true\n", encoding="utf-8")

    assert main(["--config", str(cfg_file), "config", "show"]) == 0
    assert "config warning" in capsys.readouterr().err.lower()

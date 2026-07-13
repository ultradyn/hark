from hark.config import load_config
from hark.listen_end import (
    EndMode,
    evaluate_radio_transcript,
    find_terminal_phrase,
    parse_end_mode,
    should_keep_listening,
)


def test_parse_end_mode():
    assert parse_end_mode("radio") is EndMode.RADIO
    assert parse_end_mode("silence") is EndMode.SILENCE
    assert parse_end_mode(None) is EndMode.SILENCE


def test_end_phrase_longest_wins():
    hit = evaluate_radio_transcript("Please use black formatting okay send it")
    assert hit is not None
    assert hit.kind == "end"
    assert hit.phrase == "okay send it"
    assert hit.body == "please use black formatting"


def test_end_prompt():
    hit = evaluate_radio_transcript("refactor the auth module. End prompt!")
    assert hit is not None
    assert hit.phrase == "end prompt"
    assert "end prompt" not in hit.body
    assert "refactor" in hit.body


def test_over_radio():
    hit = evaluate_radio_transcript("ship it over")
    assert hit is not None
    assert hit.phrase == "over"
    assert hit.body == "ship it"


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
    keep2, _ = should_keep_listening(
        EndMode.SILENCE, "hello world", silence_would_end=False
    )
    assert keep2 is True


def test_radio_ignores_silence_until_phrase():
    keep, hit = should_keep_listening(
        "radio",
        "long thoughtful answer without terminator",
        silence_would_end=True,
    )
    assert keep is True
    assert hit is None
    keep2, hit2 = should_keep_listening(
        "radio",
        "long thoughtful answer send it",
        silence_would_end=False,
    )
    assert keep2 is False
    assert hit2 is not None and hit2.kind == "end"


def test_cancel_phrase():
    hit = evaluate_radio_transcript("actually wait cancel that")
    assert hit is not None
    assert hit.kind == "cancel"


def test_mid_sentence_not_end():
    # "send it" only counts at the end — still true if those words are last
    # but "send it to prod tomorrow" should NOT match (does not end with phrase alone as boundary... it ends with tomorrow)
    assert evaluate_radio_transcript("please send it to prod tomorrow") is None


def test_word_boundary():
    # should not match phrase that is only a suffix of a word
    assert find_terminal_phrase("handover", ["over"], kind="end") is None


def test_config_loads_listen(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
version = 1
[listen]
end_mode = "radio"
end_phrases = ["end prompt", "send it"]
max_listen_s = 120
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.listen.end_mode == "radio"
    assert cfg.listen.end_phrases == ["end prompt", "send it"]
    assert cfg.listen.max_listen_s == 120.0


def test_env_overrides_end_mode(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[listen]\nend_mode = "silence"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HARK_LISTEN_END_MODE", "radio")
    cfg = load_config(cfg_file)
    assert cfg.listen.end_mode == "radio"

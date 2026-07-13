from hark.config import load_config
from hark.listen_end import (
    DEFAULT_CANCEL_PHRASES,
    DEFAULT_END_PHRASES,
    DEFAULT_SOFT_END_PHRASES,
    DEFAULT_SOFT_END_PHRASES_ENABLED,
    SENTENCE_FINAL_SOFT_PHRASES,
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
    hit = evaluate_radio_transcript(
        "Please use black formatting okay hark send",
        soft_end_phrases_enabled=False,
    )
    assert hit is not None
    assert hit.kind == "end"
    assert hit.phrase == "okay hark send"
    assert hit.body == "please use black formatting"


def test_end_prompt():
    hit = evaluate_radio_transcript(
        "refactor the auth module. End prompt!",
        soft_end_phrases_enabled=False,
    )
    assert hit is not None
    assert hit.phrase == "end prompt"
    assert "refactor" in hit.body


def test_casual_cancel_does_not_fire():
    assert evaluate_radio_transcript(
        "actually wait cancel that", soft_end_phrases_enabled=False
    ) is None
    assert evaluate_radio_transcript(
        "never mind the old approach", soft_end_phrases_enabled=False
    ) is None


def test_hark_cancel():
    hit = evaluate_radio_transcript("scratch this whole idea hark cancel")
    assert hit is not None
    assert hit.kind == "cancel"


def test_no_match_keeps_listening():
    assert evaluate_radio_transcript(
        "I am still thinking about the design", soft_end_phrases_enabled=False
    ) is None
    keep, hit = should_keep_listening(
        EndMode.RADIO,
        "I am still thinking about the design",
        soft_end_phrases_enabled=False,
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
        soft_end_phrases_enabled=False,
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


# ---------------------------------------------------------------------------
# Soft end phrases (default ON for radio dogfood — B039)
# ---------------------------------------------------------------------------


def test_soft_end_default_enabled_constant():
    assert DEFAULT_SOFT_END_PHRASES_ENABLED is True
    # Default evaluate path should accept bare "send it"
    hit = evaluate_radio_transcript("long prompt about the branch. Send it.")
    assert hit is not None
    assert hit.kind == "end"
    assert hit.phrase == "send it"


def test_soft_end_disabled_blocks_informal():
    # With soft end off, informal closers must not finish.
    assert (
        evaluate_radio_transcript(
            "refactor the module that's all", soft_end_phrases_enabled=False
        )
        is None
    )
    assert (
        evaluate_radio_transcript(
            "ship it okay send it", soft_end_phrases_enabled=False
        )
        is None
    )
    assert (
        evaluate_radio_transcript("end of message", soft_end_phrases_enabled=False)
        is None
    )
    assert (
        evaluate_radio_transcript("Send it.", soft_end_phrases_enabled=False) is None
    )


def test_soft_end_matches_when_enabled_terminal():
    hit = evaluate_radio_transcript(
        "refactor the auth module that's all",
        soft_end_phrases_enabled=True,
    )
    assert hit is not None
    assert hit.kind == "end"
    assert hit.phrase == "that's all"
    assert "refactor" in hit.body


def test_soft_end_whole_utterance():
    hit = evaluate_radio_transcript(
        "okay send it",
        soft_end_phrases_enabled=True,
    )
    assert hit is not None
    assert hit.phrase in ("okay send it", "ok send it", "send it")
    # Prefer longest match when multi-word
    assert hit.phrase == "okay send it"
    assert hit.body == ""


def test_soft_end_bare_send_it():
    """B039: bare 'send it' finalizes when soft end is on (utterance-final)."""
    for text in (
        "Send it.",
        "Send it",
        "please review the plan send it",
        "this is over. Send it. Send it.",
    ):
        hit = evaluate_radio_transcript(text, soft_end_phrases_enabled=True)
        assert hit is not None, f"expected end for {text!r}"
        assert hit.kind == "end"
        assert hit.phrase == "send it"


def test_soft_end_bare_send_that():
    hit = evaluate_radio_transcript(
        "ship the feature send that",
        soft_end_phrases_enabled=True,
    )
    assert hit is not None
    assert hit.phrase == "send that"
    assert "ship" in hit.body


def test_soft_end_sentence_final_over():
    """B039 addendum: utterance-final 'over' after sentence end."""
    positives = [
        "please implement. over.",
        "and that is what you should please implement. over.",
        "ready to ship! over",
        "Over.",  # sole utterance
        "over",
        "done with the plan? over",
    ]
    for text in positives:
        hit = evaluate_radio_transcript(text, soft_end_phrases_enabled=True)
        assert hit is not None, f"expected end for {text!r}"
        assert hit.kind == "end"
        assert hit.phrase == "over"


def test_soft_end_over_and_out_still_works():
    hit = evaluate_radio_transcript(
        "that covers the plan over and out",
        soft_end_phrases_enabled=True,
    )
    assert hit is not None
    assert hit.phrase == "over and out"


def test_soft_end_no_mid_clause_over():
    """Bare 'over' must not fire mid-clause or without sentence boundary."""
    negatives = [
        "think it over and continue",
        "over the weekend we ship",
        "turn it over carefully",
        "turn it over",  # word-final but not sentence-final
        "hand it over",
        "look over the diff",
        "please go over the checklist again",
        "this is over",  # no sentence punct before terminal over
    ]
    for text in negatives:
        assert (
            evaluate_radio_transcript(text, soft_end_phrases_enabled=True) is None
        ), f"false finish on: {text!r}"


def test_soft_end_punct_trail():
    hit = evaluate_radio_transcript(
        "done for now. End of message!",
        soft_end_phrases_enabled=True,
    )
    assert hit is not None
    assert hit.phrase == "end of message"


def test_soft_end_no_mid_clause():
    """Mid-thought speech must not false-finish on soft phrases."""
    mid = [
        "that's all I know about the auth bug",
        "that's all I wanted to cover in this pass",
        "please just send it to production",  # "send it" not terminal
        "I think that's all for the first part but wait",
        "turn it over carefully",
        "over and out of memory errors",  # soft multi-word not terminal
        "send that to staging first please",
    ]
    for text in mid:
        assert (
            evaluate_radio_transcript(text, soft_end_phrases_enabled=True) is None
        ), f"false finish on: {text!r}"


def test_soft_end_product_phrases_still_win():
    # Product cancel/end take priority over soft
    hit = evaluate_radio_transcript(
        "scratch this hark cancel",
        soft_end_phrases_enabled=True,
    )
    assert hit is not None and hit.kind == "cancel"
    hit2 = evaluate_radio_transcript(
        "please use black formatting okay hark send",
        soft_end_phrases_enabled=True,
    )
    assert hit2 is not None and hit2.phrase == "okay hark send"
    # hark over is product-scoped; wins as product end (not bare soft over)
    hit3 = evaluate_radio_transcript(
        "ship the branch hark over",
        soft_end_phrases_enabled=True,
    )
    assert hit3 is not None and hit3.phrase == "hark over"


def test_soft_end_safe_list_documented():
    soft_norm = {p.lower() for p in DEFAULT_SOFT_END_PHRASES}
    # B039: bare send it / send that / over are in the soft list
    for required in (
        "that's all",
        "end of message",
        "okay send it",
        "send it",
        "send that",
        "over and out",
        "over",
    ):
        assert required in soft_norm
    # Bare over is sentence-final only
    assert "over" in SENTENCE_FINAL_SOFT_PHRASES
    # Still exclude high-risk singles
    unsafe = {
        "done",
        "i'm done",
        "that's it",
        "finished",
        "go",
        "go ahead",
        "cancel that",
    }
    assert soft_norm.isdisjoint(unsafe)


def test_soft_end_should_keep_listening():
    keep, hit = should_keep_listening(
        EndMode.RADIO,
        "long answer that's all",
        soft_end_phrases_enabled=False,
        silence_would_end=True,
    )
    assert keep is True and hit is None
    keep2, hit2 = should_keep_listening(
        EndMode.RADIO,
        "long answer that's all",
        soft_end_phrases_enabled=True,
        silence_would_end=True,
    )
    assert keep2 is False
    assert hit2 is not None and hit2.kind == "end"
    keep3, hit3 = should_keep_listening(
        EndMode.RADIO,
        "please implement. over.",
        soft_end_phrases_enabled=True,
        silence_would_end=True,
    )
    assert keep3 is False
    assert hit3 is not None and hit3.phrase == "over"


def test_config_soft_end_default_on(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[listen]\nend_mode = "radio"\n', encoding="utf-8")
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    monkeypatch.delenv("HARK_SOFT_END_PHRASES_ENABLED", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.listen.soft_end_phrases_enabled is True
    assert "send it" in cfg.listen.soft_end_phrases
    assert "over" in cfg.listen.soft_end_phrases
    assert "that's all" in cfg.listen.soft_end_phrases


def test_config_soft_end_can_disable(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[listen]
end_mode = "radio"
soft_end_phrases_enabled = false
soft_end_phrases = ["that's all", "end of message"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    monkeypatch.delenv("HARK_SOFT_END_PHRASES_ENABLED", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.listen.soft_end_phrases_enabled is False
    assert cfg.listen.soft_end_phrases == ["that's all", "end of message"]


def test_config_soft_end_enabled_explicit(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[listen]
end_mode = "radio"
soft_end_phrases_enabled = true
soft_end_phrases = ["that's all", "end of message"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_LISTEN_END_MODE", raising=False)
    monkeypatch.delenv("HARK_SOFT_END_PHRASES_ENABLED", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.listen.soft_end_phrases_enabled is True
    assert cfg.listen.soft_end_phrases == ["that's all", "end of message"]


def test_env_overrides_soft_end(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[listen]\nsoft_end_phrases_enabled = false\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HARK_SOFT_END_PHRASES_ENABLED", "true")
    cfg = load_config(cfg_file)
    assert cfg.listen.soft_end_phrases_enabled is True


def test_env_disables_soft_end(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[listen]\nsoft_end_phrases_enabled = true\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HARK_SOFT_END_PHRASES_ENABLED", "false")
    cfg = load_config(cfg_file)
    assert cfg.listen.soft_end_phrases_enabled is False


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

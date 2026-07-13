"""B095: print TTS question text to terminal for ask / tts --listen."""

from __future__ import annotations

import argparse
import io
from types import SimpleNamespace

import hark.cli as cli
from hark.config import HarkConfig, config_to_dict, load_config
from hark.exitcodes import OK
from hark.speech import (
    ListenResult,
    extract_question_items,
    format_tts_question_text,
    maybe_print_tts_question,
    print_tts_question_text,
    speak_and_listen,
)


def test_format_single_question_unchanged():
    text = "Which color do you prefer?"
    assert format_tts_question_text(text) == text
    assert extract_question_items(text) == [text]


def test_format_already_numbered_lines():
    text = "1. Local only\n2. Remote SSH\n3. Both"
    out = format_tts_question_text(text)
    assert out == "1. Local only\n2. Remote SSH\n3. Both"


def test_format_bullet_lines_renumbered():
    text = "- apple\n- banana\n* cherry"
    out = format_tts_question_text(text)
    assert out == "1. apple\n2. banana\n3. cherry"


def test_format_word_ordinals():
    text = (
        "One: health check. Two: sessions — local or remote. "
        "Three: persona — Iris or Mercury."
    )
    items = extract_question_items(text)
    assert len(items) == 3
    assert "health" in items[0].lower()
    assert "session" in items[1].lower()
    assert "persona" in items[2].lower()
    out = format_tts_question_text(text)
    assert out.startswith("1. ")
    assert "\n2. " in out
    assert "\n3. " in out


def test_word_ordinal_ignores_mid_clause_lowercase():
    text = "The second: attempt failed after the first: retry."
    assert extract_question_items(text) == [text]


def test_format_q_labels():
    text = "Q1: Which host? Q2: Which agent?"
    out = format_tts_question_text(text)
    assert "1. Which host?" in out
    assert "2. Which agent?" in out


def test_format_multiple_question_sentences():
    text = "Which sessions should I watch? Local only or remote as well?"
    out = format_tts_question_text(text)
    assert out.startswith("1. ")
    assert "\n2. " in out
    assert "sessions" in out
    assert "remote" in out.lower()


def test_format_plain_multiline_paragraphs():
    text = "First topic goes here.\nSecond topic goes here."
    out = format_tts_question_text(text)
    assert out == "1. First topic goes here.\n2. Second topic goes here."


def test_print_tts_question_text_banner_to_stderr():
    buf = io.StringIO()
    print_tts_question_text("1. Alpha\n2. Beta", stream=buf)
    s = buf.getvalue()
    assert "hark question" in s
    assert "1. Alpha" in s
    assert "2. Beta" in s
    assert s.count("=") >= 10


def test_print_empty_is_noop():
    buf = io.StringIO()
    print_tts_question_text("   ", stream=buf)
    assert buf.getvalue() == ""


def test_maybe_print_respects_config_flag():
    cfg = HarkConfig()
    cfg.tts.print_prompt = False
    printed: list[str] = []

    def fake_print(text, *, stream=None):
        printed.append(text)

    import hark.speech as speech

    orig = speech.print_tts_question_text
    speech.print_tts_question_text = fake_print  # type: ignore[assignment]
    try:
        maybe_print_tts_question(cfg, "Should not print")
        assert printed == []
        cfg.tts.print_prompt = True
        maybe_print_tts_question(cfg, "Should print")
        assert printed == ["Should print"]
    finally:
        speech.print_tts_question_text = orig  # type: ignore[assignment]


def test_print_prompt_config_default_and_toml(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.tts.print_prompt is True
    d = config_to_dict(cfg)
    assert d["tts"]["print_prompt"] is True

    path = tmp_path / "config.toml"
    path.write_text("[tts]\nprint_prompt = false\n", encoding="utf-8")
    loaded = load_config(path)
    assert loaded.tts.print_prompt is False
    assert not [w for w in loaded.warnings if "print_prompt" in w]


def test_speak_and_listen_prints_question(monkeypatch, capsys):
    cfg = HarkConfig()
    cfg.tts.print_prompt = True
    cfg.audio.listen_pre_arm_ms = 0
    cfg.audio.overlap_prearm = False

    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *a, **k: {"ok": True, "provider": "xai", "voice": "eve"},
    )
    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda *a, **k: ListenResult(
            text="blue",
            provider="xai",
            duration_ms=100,
            end_mode="silence",
        ),
    )
    monkeypatch.setattr("hark.speech._tag_meta_command", lambda r: None)

    prompt = "1. Color?\n2. Size?"
    tts_info, listened = speak_and_listen(cfg, prompt)
    assert tts_info["ok"] is True
    assert listened.text == "blue"
    captured = capsys.readouterr()
    assert "hark question" in captured.err
    assert "Color" in captured.err
    assert "Size" in captured.err
    # Must not go to stdout (JSON/partial channel)
    assert "hark question" not in captured.out


def test_speak_and_listen_skips_print_when_disabled(monkeypatch, capsys):
    cfg = HarkConfig()
    cfg.tts.print_prompt = False
    cfg.audio.listen_pre_arm_ms = 0

    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *a, **k: {"ok": True, "provider": "xai"},
    )
    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda *a, **k: ListenResult(
            text="ok",
            provider="xai",
            duration_ms=50,
            end_mode="silence",
        ),
    )
    monkeypatch.setattr("hark.speech._tag_meta_command", lambda r: None)

    speak_and_listen(cfg, "Should stay silent on terminal")
    assert "hark question" not in capsys.readouterr().err


def test_run_ask_prints_via_speak_and_listen(monkeypatch, capsys):
    from hark.speech import run_ask

    cfg = HarkConfig()
    cfg.tts.print_prompt = True
    cfg.confirm.mode = "never"

    def fake_sal(cfg, text, **kwargs):
        maybe_print_tts_question(cfg, text)
        return (
            {"ok": True, "provider": "xai"},
            ListenResult(
                text="local only",
                provider="xai",
                duration_ms=200,
                end_mode="silence",
            ),
        )

    monkeypatch.setattr("hark.speech.speak_and_listen", fake_sal)
    result = run_ask(cfg, "Which session? Local or remote?")
    assert result["ok"] is True
    err = capsys.readouterr().err
    assert "hark question" in err
    assert "session" in err.lower() or "Local" in err or "remote" in err.lower()


def test_cmd_ask_uses_run_ask_print_path(monkeypatch, capsys):
    """CLI ask surfaces question on stderr before JSON on stdout."""
    printed: list[str] = []

    def fake_run_ask(cfg, prompt, **kwargs):
        maybe_print_tts_question(cfg, prompt)
        printed.append(prompt)
        return {
            "ok": True,
            "text": "yes",
            "provider": "xai",
            "duration_ms": 10,
            "end_mode": "silence",
            "exit": OK,
        }

    monkeypatch.setattr("hark.speech.run_ask", fake_run_ask)
    args = argparse.Namespace(
        text=["One:", "health.", "Two:", "sessions?"],
        confirm=None,
        end_mode=None,
        provider=None,
        json=True,
        event_id=None,
    )
    code = cli.cmd_ask(args, HarkConfig())
    assert code == OK
    assert printed
    captured = capsys.readouterr()
    assert "hark question" in captured.err
    assert '"ok": true' in captured.out.lower() or '"ok": true' in captured.out
    # Question banner must not pollute JSON stdout
    assert "hark question" not in captured.out


def test_run_tts_alone_does_not_print_question(monkeypatch, capsys):
    """Plain run_tts (acks / ambient) must not spam the question banner."""
    from hark.speech import run_tts

    print_calls: list[str] = []

    class FakeMute:
        def __enter__(self):
            return SimpleNamespace(applied=False)

        def __exit__(self, *a):
            return False

    class FakeDuck:
        def __enter__(self):
            return SimpleNamespace(as_meta=lambda: {"media_ducked": False})

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "hark.speech.maybe_print_tts_question",
        lambda *a, **k: print_calls.append("printed"),
    )
    monkeypatch.setattr("hark.speech.lookup_cached_tts", lambda *a, **k: b"fake")
    monkeypatch.setattr(
        "hark.speech.play_wav_bytes",
        lambda *a, **k: SimpleNamespace(duration_ms=10),
    )
    monkeypatch.setattr("hark.speech.duck_media", lambda *a, **k: FakeDuck())
    monkeypatch.setattr("hark.speech.mic_muted_during_tts", lambda **k: FakeMute())
    monkeypatch.setattr(
        "hark.speech.repair_tts_mute_after_play",
        lambda **k: {"repaired": False, "reasons": []},
    )
    monkeypatch.setattr(
        "hark.conference.apply_conference_hold",
        lambda *a, **k: SimpleNamespace(
            skipped=False, as_meta=lambda: {"held": False}
        ),
    )

    cfg = HarkConfig()
    cfg.tts.print_prompt = True
    cfg.audio.hold_during_conference = False
    out = run_tts(
        cfg, "All good, thanks.", play=True, conference_policy="force", use_cache=True
    )
    assert out["ok"] is True
    assert print_calls == []
    assert "hark question" not in capsys.readouterr().err

"""Ambient startup TTS text + cache keying by primary name / custom phrase (B034)."""

from __future__ import annotations

from hark.ambient import (
    ambient_boot_tts_text,
    primary_wake_label,
    wake_label_change_tts_text,
)
from hark.audio.cues import ambient_boot_line, tts_boot_cache_path, tts_cache_path
from hark.config import AmbientConfig, HarkConfig


def _cfg(*, phrases=None, names=None, wake_mode=None) -> HarkConfig:
    amb = AmbientConfig(
        enabled=True,
        activation_phrases=list(phrases or ["hey hark", "hey herald"]),
    )
    if names is not None:
        amb.names = list(names)  # type: ignore[attr-defined]
    if wake_mode is not None:
        amb.wake_mode = wake_mode  # type: ignore[attr-defined]
    return HarkConfig(ambient=amb)


def test_default_boot_uses_first_activation_phrase():
    # Default (names) wake mode: boot label tracks the first configured name,
    # i.e. "hey <names[0]>", which also equals the first default activation
    # phrase. Derive the expectation from the defaults so this stays correct if
    # the default persona names change again (see B080 / B074/B076).
    cfg = HarkConfig(ambient=AmbientConfig(enabled=True))
    expected = f"hey {AmbientConfig().names[0]}"
    # The first default activation phrase is the "hey <name>" form of the first
    # name, so the boot label is equally "the first activation phrase".
    assert AmbientConfig().activation_phrases[0] == expected
    assert primary_wake_label(cfg) == expected
    assert ambient_boot_tts_text(cfg) == ambient_boot_line(expected)
    assert expected in ambient_boot_tts_text(cfg)


def test_custom_phrase_boot_label():
    cfg = _cfg(phrases=["start prompt", "begin dictation"])
    assert primary_wake_label(cfg) == "start prompt"
    text = ambient_boot_tts_text(cfg)
    assert "start prompt" in text
    assert "hey hark" not in text


def test_phrases_mode_uses_first_trigger():
    cfg = _cfg(phrases=["begin dictation"], wake_mode="phrases")
    assert primary_wake_label(cfg) == "begin dictation"


def test_names_mode_uses_first_name():
    cfg = _cfg(phrases=["hey hark"], names=["alice", "bob"], wake_mode="names")
    assert primary_wake_label(cfg) == "hey alice"
    assert "alice" in ambient_boot_tts_text(cfg)


def test_boot_cache_path_keyed_on_label():
    p_hark = tts_boot_cache_path("eve", "hey hark")
    p_alice = tts_boot_cache_path("eve", "hey alice")
    p_custom = tts_boot_cache_path("eve", "start prompt")
    assert p_hark != p_alice
    assert p_hark != p_custom
    assert "eve" in str(p_hark)
    # Same as generic full-text cache path so synthesize + lookup share one file
    full = ambient_boot_line("hey hark")
    assert p_hark == tts_cache_path("eve", full)
    # Slug reflects primary label
    assert "hey-hark" in p_hark.name or "hark" in p_hark.name
    assert "alice" in p_alice.name or "hey-alice" in p_alice.name
    assert "start-prompt" in p_custom.name or "start" in p_custom.name


def test_different_labels_different_cache_paths():
    paths = {
        tts_boot_cache_path("eve", label)
        for label in ("hey hark", "hey herald", "start prompt", "hey alice")
    }
    assert len(paths) == 4


def test_wake_label_change_tts_text():
    text = wake_label_change_tts_text("hey hark", "hey clanker")
    assert "hey hark" in text
    assert "hey clanker" in text
    assert "updated" in text.lower()
    # Same label → nothing to speak
    assert wake_label_change_tts_text("hey hark", "hey hark") == ""
    assert wake_label_change_tts_text("Hey Hark", "hey hark") == ""

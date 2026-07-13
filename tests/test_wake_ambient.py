from hark.config import load_config
from hark.wake import (
    DEFAULT_ACTIVATION_PHRASES,
    TextProbeBackend,
    match_activation,
)


def test_activation_hey_hark():
    hit = match_activation("Hey Hark, open the PR checklist")
    assert hit is not None
    assert hit.phrase == "hey hark"
    assert "open the pr" in hit.remainder


def test_activation_hey_herald():
    hit = match_activation("hey herald")
    assert hit is not None
    assert hit.remainder == ""


def test_activation_anywhere_in_snippet():
    hit = match_activation("um yes hey hark ship it", anywhere=True)
    assert hit is not None
    assert hit.phrase == "hey hark"
    assert "ship" in hit.remainder


def test_no_false_wake_on_normal_speech():
    assert match_activation("please hark back to the earlier design") is None
    assert match_activation("the herald of spring arrived") is None
    # without anywhere, mid-phrase start-only fails exact match
    assert match_activation("noise hey hark") is None


def test_fuzzy_hey_hook_is_hark():
    # vosk often hears "hark" as "hook"
    hit = match_activation("hey hook", anywhere=True)
    assert hit is not None
    assert "hark" in hit.phrase


def test_fuzzy_hey_harold_is_herald():
    hit = match_activation("hey harold please", anywhere=True)
    assert hit is not None
    assert "herald" in hit.phrase


def test_text_probe_backend():
    be = TextProbeBackend()
    assert be.score_snippet(b"\x00\x01\x02\x03") is None
    hit = be.score_snippet(b"TXT:hey hark ship the feature")
    assert hit is not None
    assert hit.phrase == "hey hark"


def test_ambient_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[ambient]
enabled = true
engine = "text_probe"
activation_phrases = ["hey hark", "hey herald"]
snippet_s = 2.0
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_AMBIENT", raising=False)
    cfg = load_config(cfg_file)
    assert cfg.ambient.enabled is True
    assert cfg.ambient.engine == "text_probe"
    assert "hey herald" in cfg.ambient.activation_phrases


def test_default_activation_includes_herald():
    assert "hey hark" in DEFAULT_ACTIVATION_PHRASES
    assert "hey herald" in DEFAULT_ACTIVATION_PHRASES

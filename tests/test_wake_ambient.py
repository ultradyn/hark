from hark.config import load_config
from hark.ambient import complete_after_wake
from hark.config import HarkConfig
from hark.speech import ListenResult
from hark.wake import (
    DEFAULT_ACTIVATION_PHRASES,
    TextProbeBackend,
    WakeHit,
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
    # bare "hark" without hey/ok prefix should not fire
    assert match_activation("hark back to the design") is None


def test_fuzzy_hey_hook_is_hark():
    # vosk often hears "hark" as "hook"
    hit = match_activation("hey hook", anywhere=True)
    assert hit is not None
    assert "hark" in hit.phrase


def test_fuzzy_hey_harold_is_herald():
    hit = match_activation("hey harold please", anywhere=True)
    assert hit is not None
    assert "herald" in hit.phrase


def test_hello_herald_wake():
    hit = match_activation("hello herald", anywhere=True)
    assert hit is not None
    assert "herald" in hit.phrase


def test_hello_hark_fuzzy():
    hit = match_activation("hello hook", anywhere=True)
    assert hit is not None
    assert "hark" in hit.phrase


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


def test_wake_remainder_is_discarded_and_cloud_listen_captures_prompt(monkeypatch):
    listened = ListenResult(
        text="cloud captured prompt",
        provider="xai",
        duration_ms=123,
        end_mode="radio",
    )
    calls = []

    def fake_listen(cfg, *, end_mode, **kwargs):
        calls.append((cfg, end_mode, kwargs))
        return listened

    monkeypatch.setattr("hark.ambient.run_listen", fake_listen)
    result = complete_after_wake(
        HarkConfig(),
        WakeHit(
            phrase="hey hark",
            remainder="locally heard but untrusted prompt",
            raw="hey hark locally heard but untrusted prompt",
            backend="vosk",
        ),
        announce=False,
    )

    assert calls and calls[0][1] == "silence"
    assert result.text == "cloud captured prompt"
    assert result.listen["provider"] == "xai"

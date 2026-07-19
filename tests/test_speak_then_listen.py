"""P1.M4 SpeakThenListen module: re-exports, handoff order, confirm profile."""

from __future__ import annotations

import time
import unicodedata
from contextlib import contextmanager

import pytest

import hark.confirm_lexicon as confirm_lexicon
from confirm_unicode_cases import (
    ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES,
    ALPHABETIC_TRIPLE_COMPATIBILITY_REPRODUCTIONS,
    APOSTROPHE_VARIANTS,
    BENIGN_PROSE_AFFIRMATIONS,
    COMPATIBILITY_PROSE_AFFIRMATIONS,
    COMPATIBILITY_EXPANSION_REPRODUCTIONS,
    COMPATIBILITY_WHITESPACE_ALPHA_REPRODUCTIONS,
    COMPOSITE_CONTRACTION_SEPARATORS,
    CONTRACTION_PARTS,
    EDGE_MATERIAL_REPRODUCTIONS,
    FULLWIDTH_CONTRACTION_CASES,
    FULLWIDTH_WORD_BASE_BOUNDARY_CONTROLS,
    MULTISOURCE_COMPATIBILITY_REPRODUCTIONS,
    NORMALIZATION_FORMS,
    ORDINARY_UNICODE_AFFIRMATIONS,
    ORDINARY_NONASCII_WORD_BASES,
    ORDINARY_PROSE_BRIDGE_EVIDENCE,
    PROVENANCE_PRESERVING_NORMALIZATION_FORMS,
    PROSE_TRANSPARENT_SUFFIXES,
    RAW_WORD_BASE_COMPATIBILITY_PREFIXES,
    SHADOWED_NORMALIZED_BRIDGE_EVIDENCE,
    SUPPORTED_FULLWIDTH_CONTRACTION_CASES,
    UNSUPPORTED_IN_WORD_FRAGMENTS,
    WORD_BASE_BOUNDARY_CONTROLS,
    compatibility_whitespace_sources,
    is_fully_collapsed_alphabetic_material,
    right_fragment_suffix_compatibility_sources,
)
from hark.config import HarkConfig
from hark.speech import ListenResult, run_ask, speak_and_listen
from hark.speak_then_listen import HandoffState, attach_tts_info
from hark.speak_then_listen import run_ask as stl_run_ask
from hark.speak_then_listen import speak_and_listen as stl_speak


def test_speech_reexports_same_callables():
    assert speak_and_listen is stl_speak
    assert run_ask is stl_run_ask
    assert HandoffState.SPEAKING.value == "speaking"
    assert set(HandoffState) == {
        HandoffState.SPEAKING,
        HandoffState.ARMED,
        HandoffState.LISTENING,
        HandoffState.CONFIRMING,
    }


def test_attach_tts_info_on_exception():
    exc = TimeoutError("no speech")
    tts = {"ok": True, "provider": "mock"}
    out = attach_tts_info(exc, tts)
    assert out is exc
    assert getattr(exc, "tts_info") == tts


def test_run_tts_play_stack_order_conference_mute_duck(monkeypatch):
    """Internalized order: conference hold → mic mute → media duck (adapters stay)."""
    import hark.speech as speech_mod

    order: list[str] = []
    cfg = HarkConfig()
    cfg.audio.mute_mic_during_tts = True
    cfg.audio.duck_media_during_tts = True
    cfg.audio.hold_during_conference = True

    class Hold:
        skipped = False

        def as_meta(self):
            return {"held": True}

    def fake_hold(cfg, text, *, policy=None):
        order.append(f"conference:{policy}")
        return Hold()

    @contextmanager
    def fake_mute(*, enabled=True):
        order.append(f"mute_enter:{enabled}")
        yield type("S", (), {"applied": bool(enabled)})()
        order.append("mute_exit")

    @contextmanager
    def fake_duck(cfg, *, enabled=True, exclude_conference=False):
        order.append(f"duck_enter:{enabled}:excl={exclude_conference}")
        yield type(
            "D",
            (),
            {
                "as_meta": lambda self: {
                    "media_ducked": True,
                    "duck_count": 1,
                }
            },
        )()
        order.append("duck_exit")

    monkeypatch.setattr("hark.conference.apply_conference_hold", fake_hold)
    monkeypatch.setattr("hark.speech.mic_muted_during_tts", fake_mute)
    monkeypatch.setattr("hark.speech.duck_media", fake_duck)
    monkeypatch.setattr("hark.speech.claim_tts_play_ticket", lambda: object())
    monkeypatch.setattr("hark.speech.abandon_tts_play_ticket", lambda *a, **k: None)

    @contextmanager
    def fake_exclusive(*, ticket=None, wait_timeout_s=None):
        order.append("exclusive")
        yield

    monkeypatch.setattr("hark.speech.exclusive_playback", fake_exclusive)
    monkeypatch.setattr("hark.speech.lookup_cached_tts", lambda *a, **k: b"\x00\x01")
    monkeypatch.setattr(
        "hark.speech.play_wav_bytes",
        lambda *a, **k: type("PR", (), {"duration_ms": 10})(),
    )
    monkeypatch.setattr(
        "hark.speech.repair_tts_mute_after_play",
        lambda **k: {"repaired": False},
    )
    monkeypatch.setattr(
        "hark.speech.wait_until_tts_play_allowed",
        lambda **k: type(
            "D",
            (),
            {
                "deferred": False,
                "gate": None,
                "as_meta": lambda self: {},
            },
        )(),
    )

    # UsageStore no-op
    class Store:
        def record_tts(self, **kw):
            return None

    monkeypatch.setattr("hark.speech.UsageStore", Store)

    result = speech_mod.run_tts(cfg, "hello stack", play=True, use_cache=True)
    assert result["ok"]
    # conference before mute/duck; mute wraps duck
    assert order.index("conference:hold") < order.index("mute_enter:True")
    assert order.index("mute_enter:True") < order.index("duck_enter:True:excl=True")
    assert order.index("duck_enter:True:excl=True") < order.index("duck_exit")
    assert order.index("duck_exit") < order.index("mute_exit")


def test_run_ask_confirm_profile_readback_silence_lexicon(monkeypatch):
    """Confirm path: readback TTS + profile=confirm silence listen + lexicon."""
    cfg = HarkConfig()
    cfg.confirm.mode = "always"
    calls: list[tuple[str, dict]] = []

    def fake_speak(cfg, prompt, **kwargs):
        calls.append(("speak", {"prompt": prompt}))
        return (
            {"ok": True, "provider": "mock"},
            ListenResult(
                text="delete the database",
                provider="mock",
                duration_ms=100,
                end_mode="silence",
                stream_id="s1",
            ),
        )

    def fake_tts(cfg, text, **kwargs):
        calls.append(("tts", {"text": text}))
        return {"ok": True, "provider": "mock"}

    def fake_listen(cfg, **kwargs):
        calls.append(("listen", dict(kwargs)))
        assert kwargs.get("profile") == "confirm"
        assert kwargs.get("end_mode") == "silence"
        assert "I heard:" in (kwargs.get("last_tts") or "")
        return ListenResult(
            text="yes",
            provider="mock",
            duration_ms=50,
            end_mode="silence",
            stream_id="s2",
        )

    monkeypatch.setattr("hark.speech.speak_and_listen", fake_speak)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    out = run_ask(cfg, "Should I wipe production?", risk_hint="R2")
    assert out["ok"] is True
    assert out["text"] == "delete the database"
    assert out["tts"]["ok"] is True
    assert any(c[0] == "tts" and "I heard:" in c[1]["text"] for c in calls)
    assert any(c[0] == "listen" and c[1].get("profile") == "confirm" for c in calls)


@pytest.mark.parametrize(
    "confirm_reply",
    [
        "cancel",
        "yes I cant approve this",
        "yes I wont approve this",
        "yes I dont approve this",
        # B148: deferred / conditional / hedged confirm replies cancel.
        "yes but wait",
        "yes hold on",
        "okay wait a second",
        "sure after I review it",
        "yes if the tests pass",
        "yes unless the tests fail",
        "yes maybe",
        # B159: Format/Mark inside multi-letter NEGATE must cancel R2.
        "yes I ca\u200bn’t approve this",
        "yes c\u200bancel",
        "yes n\u200bot",
        "yes a\u200bbort",
        "yes re\u200bject",
        "yes c\u0301ancel",
    ],
)
def test_run_ask_confirm_cancel_on_no(monkeypatch, confirm_reply):
    cfg = HarkConfig()
    cfg.confirm.mode = "always"

    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: (
            {"ok": True},
            ListenResult(
                text="ship it",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="x",
            ),
        ),
    )
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda *a, **k: ListenResult(
            text=confirm_reply,
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            stream_id="y",
        ),
    )

    out = run_ask(cfg, "Deploy now?", risk_hint="R2")
    assert out["ok"] is False
    assert out.get("cancelled") is True
    assert out.get("confirm_reply") == confirm_reply


def _run_ask_with_confirmation(monkeypatch, confirm_reply, *, configured_mode="always"):
    cfg = HarkConfig()
    cfg.confirm.mode = configured_mode

    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: (
            {"ok": True},
            ListenResult(
                text="ship it",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="answer",
            ),
        ),
    )
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda *a, **k: ListenResult(
            text=confirm_reply,
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            stream_id="confirm",
        ),
    )

    return run_ask(cfg, "Deploy now?", risk_hint="R2")


@pytest.mark.parametrize(
    "raw_confirm_reply",
    (
        "yes I can\u1e9bt approve this",
        "yes I can\u1d43\u1d47\u02b0t approve this",
    ),
)
def test_run_ask_passes_raw_confirm_transcript_to_classifier(
    monkeypatch, raw_confirm_reply
):
    classified = []

    def capture_raw_transcript(text):
        classified.append(text)
        return "yes"

    monkeypatch.setattr(
        "hark.speak_then_listen.ask.classify_confirm_reply",
        capture_raw_transcript,
    )

    out = _run_ask_with_confirmation(monkeypatch, raw_confirm_reply)

    assert out["ok"] is True
    assert classified == [raw_confirm_reply]


def test_run_ask_rejects_long_combining_segment_before_whole_normalization(
    monkeypatch,
):
    real_normalize = confirm_lexicon.unicodedata.normalize
    normalized_lengths = []

    def counted_normalize(form, text):
        normalized_lengths.append(len(text))
        return real_normalize(form, text)

    monkeypatch.setattr(confirm_lexicon.unicodedata, "normalize", counted_normalize)
    confirm_reply = ("\u0315\u0300" * 4000)[:8000]

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply
    assert len(normalized_lengths) == (
        2 * (confirm_lexicon._MAX_NORMALIZATION_SEGMENT_CHARS + 1)
    )
    assert max(normalized_lengths) == 1


@pytest.mark.parametrize("character", COMPATIBILITY_EXPANSION_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
def test_run_ask_applies_observable_alphanumeric_compatibility_policy(
    monkeypatch, character, normalization
):
    material = (
        character
        if normalization is None
        else unicodedata.normalize(normalization, character)
    )
    confirm_reply = f"yes I can{material}t approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    expected_ok = (
        normalization not in PROVENANCE_PRESERVING_NORMALIZATION_FORMS
        and is_fully_collapsed_alphabetic_material(material)
    )
    assert out["ok"] is expected_ok
    if expected_ok:
        assert out.get("cancelled") is not True
        assert "confirm_reply" not in out
    else:
        assert out["cancelled"] is True
        assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("bridge", MULTISOURCE_COMPATIBILITY_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_run_ask_applies_observable_multisource_compatibility_policy(
    monkeypatch, bridge, normalization, parts
):
    left, right = parts
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )
    confirm_reply = f"yes I {left}{material}{right} approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    expected_ok = normalization not in PROVENANCE_PRESERVING_NORMALIZATION_FORMS and (
        bridge == "\u00a8\u00a8" or is_fully_collapsed_alphabetic_material(material)
    )
    assert out["ok"] is expected_ok
    if expected_ok:
        assert out.get("cancelled") is not True
        assert "confirm_reply" not in out
    else:
        assert out["cancelled"] is True
        assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("bridge", ALPHABETIC_TRIPLE_COMPATIBILITY_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_run_ask_applies_observable_alphabetic_triple_policy(
    monkeypatch, bridge, normalization, parts
):
    left, right = parts
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )
    confirm_reply = f"yes I {left}{material}{right} approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    expected_ok = normalization not in PROVENANCE_PRESERVING_NORMALIZATION_FORMS
    if expected_ok:
        assert is_fully_collapsed_alphabetic_material(material)
    assert out["ok"] is expected_ok
    if expected_ok:
        assert out.get("cancelled") is not True
        assert "confirm_reply" not in out
    else:
        assert out["cancelled"] is True
        assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("bridge", COMPATIBILITY_WHITESPACE_ALPHA_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", PROVENANCE_PRESERVING_NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_run_ask_applies_compatibility_whitespace_raw_boundary_policy(
    monkeypatch, bridge, normalization, parts
):
    left, right = parts
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )
    confirm_reply = f"yes I {left}{material}{right} approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    expected_ok = bridge == "a\u2002"
    assert out["ok"] is expected_ok
    if expected_ok:
        assert out.get("cancelled") is not True
    else:
        assert out["cancelled"] is True
        assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("normalization", ("NFKC", "NFKD"))
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_run_ask_rejects_normalized_whitespace_with_surviving_format_evidence(
    monkeypatch, normalization, parts
):
    left, right = parts
    source = compatibility_whitespace_sources()[0]
    material = unicodedata.normalize(normalization, source + "\u200b\u1d43")
    confirm_reply = f"yes I {left}{material}{right} approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
@pytest.mark.parametrize(("category", "bridge"), SHADOWED_NORMALIZED_BRIDGE_EVIDENCE)
def test_run_ask_applies_collapsed_shadowed_bridge_boundary_policy(
    monkeypatch, category, bridge, normalization, parts
):
    left, right = parts
    assert unicodedata.category(bridge[1]) == category
    material = (
        bridge
        if normalization is None
        else unicodedata.normalize(normalization, bridge)
    )
    confirm_reply = f"yes I {left}{material}{right} approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    expected_ok = True
    assert out["ok"] is expected_ok
    if expected_ok:
        assert out.get("cancelled") is not True
    else:
        assert out["cancelled"] is True
        assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("normalization", ("NFKC", "NFKD"))
@pytest.mark.parametrize(
    "bridge",
    (
        "\u00a0\u200b\u1d43",
        "\u00a0\u1d43\u200b",
        "\u00a0\u200b\u1d43\u200b",
    ),
    ids=("evidence-before-alpha", "evidence-after-alpha", "evidence-both-sides"),
)
def test_configured_r2_rejects_order_independent_observable_bridge_evidence(
    monkeypatch, bridge, normalization
):
    material = unicodedata.normalize(normalization, bridge)
    confirm_reply = f"yes I can{material}t approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply, configured_mode="auto")

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("evidence", ("\u200b", "\ufe0f", "\u0301", "/", "©"))
def test_configured_r2_accepts_after_later_literal_prose_boundary(
    monkeypatch, evidence
):
    confirm_reply = f"yes I can {evidence}do it"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply, configured_mode="auto")

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("initial_boundary", (" ", "\u00a0"))
@pytest.mark.parametrize("later_boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize(
    ("category", "evidence"),
    (("Cf", "\u200b"), ("Mn", "\u0301"), ("Po", "/"), ("So", "©")),
)
def test_configured_r2_direct_separator_respects_later_raw_whitespace_expiry(
    monkeypatch,
    category,
    evidence,
    later_boundary,
    initial_boundary,
    normalization,
):
    assert unicodedata.category(evidence) == category
    raw_reply = f"yes I can{initial_boundary}{evidence}{later_boundary}t approve this"
    confirm_reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize(
    ("category", "evidence"),
    (("Cf", "\u200b"), ("Mn", "\u0301"), ("Po", "/"), ("So", "©")),
)
def test_configured_r2_direct_separator_cannot_claim_first_later_whitespace(
    monkeypatch,
    category,
    evidence,
    boundary,
    normalization,
):
    assert unicodedata.category(evidence) == category
    raw_reply = f"yes I can{evidence}{boundary}t approve this"
    confirm_reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize(
    ("category", "evidence"),
    (("Cf", "\u200b"), ("Mn", "\u0301"), ("Po", "/"), ("So", "©")),
)
def test_configured_r2_preserves_candidate_owned_initial_boundary(
    monkeypatch,
    category,
    evidence,
    boundary,
    normalization,
):
    assert unicodedata.category(evidence) == category
    raw_reply = f"yes I can{boundary}{evidence}t approve this"
    confirm_reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize(
    "confirm_reply",
    (
        "yes 'can't'",
        "yes 'can't' approve this",
        "yes ‘can’t’",
        "yes ‘can’t’ approve this",
        "yes 'won't' approve this",
        "yes 'don't' do it",
    ),
)
def test_configured_r2_rejects_quoted_negative_contractions(
    monkeypatch, confirm_reply
):
    """B157: R2 run_ask must cancel on quoted complete negative contractions."""
    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize(
    "confirm_reply",
    (
        "yes can't approve this",
        "yes can’t approve this",
    ),
)
def test_configured_r2_still_rejects_unquoted_supported_contractions(
    monkeypatch, confirm_reply
):
    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", ("\u00a0", "\u2002", "\u202f", "\u3000"))
def test_configured_r2_accepts_after_independent_compatibility_space_boundary(
    monkeypatch, boundary, normalization
):
    raw_reply = f"yes I can \u200bdo{boundary}it"
    confirm_reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("source", ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES)
@pytest.mark.parametrize("boundary", ("\u00a0", "\u2002", "\u202f", "\u3000"))
def test_configured_r2_compatibility_space_expires_declared_source_span(
    monkeypatch, boundary, source, normalization
):
    raw_reply = f"yes I can{source}\u200bᵃ{boundary}t approve this"
    confirm_reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize(
    "word_base",
    ("\u03bb", "\u0661", "\u203f"),
    ids=("unicode-letter", "unicode-number", "connector-punctuation"),
)
@pytest.mark.parametrize("initial_boundary", ("", " "))
def test_configured_r2_unicode_word_base_ends_candidate_before_later_space(
    monkeypatch, initial_boundary, word_base, boundary, normalization
):
    raw_reply = f"yes I can{initial_boundary}{word_base}{boundary}t approve this"
    confirm_reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\u00a0", "\u2002"))
@pytest.mark.parametrize("source", ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES)
def test_configured_r2_declared_source_expires_before_unrelated_right_fragment(
    monkeypatch, source, boundary, normalization
):
    raw_reply = f"yes I can{source}{boundary}t approve this"
    confirm_reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("boundary", (" ", "\t", "\n", "\u2028"))
@pytest.mark.parametrize(
    "evidence_and_alpha",
    ("\u200b\u1d43", "\u1d43\u200b", "\u200b\u1d43\u200b"),
    ids=("evidence-before-alpha", "evidence-after-alpha", "evidence-both-sides"),
)
@pytest.mark.parametrize("source", ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES)
def test_configured_r2_later_boundary_expires_single_source_exception(
    monkeypatch, source, evidence_and_alpha, boundary, normalization
):
    raw_reply = f"yes I can{source}{evidence_and_alpha}{boundary}do it"
    confirm_reply = (
        raw_reply
        if normalization is None
        else unicodedata.normalize(normalization, raw_reply)
    )

    if normalization in ("NFKC", "NFKD"):
        literal_reply = (
            "yes I can"
            + unicodedata.normalize(normalization, source + evidence_and_alpha)
            + boundary
            + "do it"
        )
        assert confirm_reply == literal_reply
    out = _run_ask_with_confirmation(
        monkeypatch,
        confirm_reply,
        configured_mode="auto",
    )

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", PROVENANCE_PRESERVING_NORMALIZATION_FORMS)
@pytest.mark.parametrize("prefix", RAW_WORD_BASE_COMPATIBILITY_PREFIXES)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_run_ask_preserves_raw_word_base_compatibility_boundary(
    monkeypatch, parts, prefix, normalization
):
    left, right = parts
    assert unicodedata.category(prefix)[0] in {"L", "N"}
    confirm_reply = f"yes {prefix}{left}__{right}"
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("transparent", PROSE_TRANSPARENT_SUFFIXES)
@pytest.mark.parametrize(
    "template",
    ("yes I can do i{}t", "yes I can do{} it"),
)
def test_run_ask_resets_bridge_at_literal_prose_whitespace(
    monkeypatch, template, transparent, normalization
):
    confirm_reply = template.format(transparent)
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize(("category", "evidence"), ORDINARY_PROSE_BRIDGE_EVIDENCE)
@pytest.mark.parametrize("word_base", ORDINARY_NONASCII_WORD_BASES)
def test_run_ask_resets_ordinary_nonascii_word_candidate_at_prose_whitespace(
    monkeypatch, word_base, category, evidence, normalization
):
    assert unicodedata.category(word_base)[0] == "L"
    assert unicodedata.category(evidence) == category
    confirm_reply = f"yes I can{word_base} do {evidence}at"
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("word_base", ORDINARY_NONASCII_WORD_BASES)
def test_run_ask_nonascii_prose_reset_keeps_later_standalone_candidate(
    monkeypatch, word_base
):
    confirm_reply = f"yes I can{word_base} do can__t"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("normalization", ("NFKC", "NFKD"))
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
@pytest.mark.parametrize("source", ALPHABETIC_COMPATIBILITY_WHITESPACE_SOURCES)
def test_run_ask_accepts_collapsed_alphabetic_compatibility_whitespace_prose(
    monkeypatch, source, parts, normalization
):
    left, right = parts
    material = unicodedata.normalize(normalization, source + "\u200b\u1d43")
    confirm_reply = f"yes I {left}{material}{right} approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_run_ask_rejects_recursive_compatibility_source(
    monkeypatch, normalization, parts
):
    left, right = parts
    confirm_reply = f"yes I {left}\u1e9b{right} approve this"
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("parts", CONTRACTION_PARTS)
def test_run_ask_rejects_shadowed_same_left_compatibility_source(
    monkeypatch, normalization, parts
):
    left, right = parts
    confirm_reply = f"yes I {left}\u2474{left}x{right} approve this"
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("suffix", ["t", "\u1d40", "\uff34", "\U0001d413"])
def test_run_ask_accepts_boundary_invalid_first_right_fragment_suffix(
    monkeypatch, normalization, suffix
):
    confirm_reply = f"yes can__t{suffix}"
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("suffix", ["1", "é", "λ"])
def test_run_ask_preserves_word_base_suffix_controls(
    monkeypatch, normalization, suffix
):
    confirm_reply = f"yes can__t{suffix}"
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is True
    assert out.get("cancelled") is not True


def test_run_ask_accepts_all_compatibility_word_base_right_suffixes(
    monkeypatch,
):
    failures = []
    sources = right_fragment_suffix_compatibility_sources()
    audited = 0

    for source in sources:
        for normalization in NORMALIZATION_FORMS:
            material = (
                source
                if normalization is None
                else unicodedata.normalize(normalization, source)
            )
            for left, right in CONTRACTION_PARTS:
                audited += 1
                confirm_reply = f"yes {left}__{right}{material}"
                out = _run_ask_with_confirmation(monkeypatch, confirm_reply)
                if out["ok"] is not True or out.get("cancelled") is True:
                    failures.append(
                        (f"U+{ord(source):04X}", normalization, left, right)
                    )

    assert len(sources) == 33
    assert audited == 495
    assert failures == []


@pytest.mark.parametrize("confirm_reply", EDGE_MATERIAL_REPRODUCTIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
def test_run_ask_rejects_transparent_edge_material(
    monkeypatch, confirm_reply, normalization
):
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)
    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("apostrophe", APOSTROPHE_VARIANTS)
def test_run_ask_confirm_cancels_normalization_closed_apostrophe_negation(
    monkeypatch, apostrophe, normalization
):
    confirm_reply = f"yes I can{apostrophe}t approve this"
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
@pytest.mark.parametrize("fragment", UNSUPPORTED_IN_WORD_FRAGMENTS)
def test_run_ask_confirm_cancels_lossy_or_repeated_in_word_material(
    monkeypatch, fragment, normalization
):
    confirm_reply = f"yes I can{fragment}t approve this"
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("separator", COMPOSITE_CONTRACTION_SEPARATORS)
def test_run_ask_confirm_cancels_composite_contraction_separator(
    monkeypatch, separator
):
    confirm_reply = f"yes I can{separator}t approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("confirm_reply", ORDINARY_UNICODE_AFFIRMATIONS)
def test_run_ask_confirm_accepts_ordinary_unicode_words(monkeypatch, confirm_reply):
    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("confirm_reply", BENIGN_PROSE_AFFIRMATIONS)
def test_run_ask_accepts_contraction_prefixes_in_benign_prose(
    monkeypatch, confirm_reply
):
    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("confirm_reply", COMPATIBILITY_PROSE_AFFIRMATIONS)
@pytest.mark.parametrize("normalization", NORMALIZATION_FORMS)
def test_run_ask_accepts_compatibility_prefix_before_raw_whitespace_prose(
    monkeypatch, confirm_reply, normalization
):
    if normalization is not None:
        confirm_reply = unicodedata.normalize(normalization, confirm_reply)

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is True
    assert out.get("cancelled") is not True


@pytest.mark.parametrize("token", WORD_BASE_BOUNDARY_CONTROLS)
def test_run_ask_confirm_accepts_malformed_contraction_inside_unicode_token(
    monkeypatch, token
):
    out = _run_ask_with_confirmation(monkeypatch, f"yes {token}")

    assert out["ok"] is True
    assert out.get("cancelled") is not True


def test_run_ask_confirm_cancels_standalone_malformed_contraction(monkeypatch):
    confirm_reply = "yes can__t"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("token", FULLWIDTH_CONTRACTION_CASES)
def test_run_ask_confirm_cancels_fullwidth_or_mixed_contraction_skeleton(
    monkeypatch, token
):
    confirm_reply = f"yes {token}"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("token", SUPPORTED_FULLWIDTH_CONTRACTION_CASES)
def test_run_ask_confirm_cancels_supported_fullwidth_or_mixed_contraction(
    monkeypatch, token
):
    confirm_reply = f"yes {token}"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


@pytest.mark.parametrize("token", FULLWIDTH_WORD_BASE_BOUNDARY_CONTROLS)
def test_run_ask_confirm_accepts_fullwidth_malformed_inside_unicode_token(
    monkeypatch, token
):
    out = _run_ask_with_confirmation(monkeypatch, f"yes {token}")

    assert out["ok"] is True
    assert out.get("cancelled") is not True


def test_run_ask_confirm_cancels_unknown_in_word_punctuation(monkeypatch):
    confirm_reply = "yes I can\u055at approve this"

    out = _run_ask_with_confirmation(monkeypatch, confirm_reply)

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == confirm_reply


def test_run_ask_confirmation_cancel_precedes_affirmative_text(monkeypatch):
    """Answer Window cancellation is authoritative even if STT text says yes."""
    cfg = HarkConfig()
    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: (
            {"ok": True},
            ListenResult(
                text="ship it",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="answer",
            ),
        ),
    )
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda *a, **k: ListenResult(
            text="yes",
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            end_phrase="agent:cancel",
            cancelled=True,
            stream_id="confirm",
        ),
    )

    out = run_ask(cfg, "Publish this package?", risk_hint="R3")

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == "yes"
    assert out["end_phrase"] == "agent:cancel"
    assert out["exit"] != 0


@pytest.mark.parametrize(
    ("confirm", "configured_mode", "risk", "expect_confirm"),
    [
        ("never", "always", "R0", False),
        ("never", "always", "R1", False),
        ("never", "always", "R2", False),
        ("never", "always", "R3", False),
        ("auto", "always", "R0", False),
        ("auto", "always", "R1", False),
        ("auto", "always", "R2", True),
        ("auto", "always", "R3", True),
        ("always", "never", "R0", True),
        ("always", "never", "R1", True),
        ("always", "never", "R2", True),
        ("always", "never", "R3", True),
        (None, "never", "R0", False),
        (None, "never", "R1", False),
        (None, "never", "R2", True),
        (None, "never", "R3", True),
    ],
)
def test_run_ask_confirmation_policy_by_risk(
    monkeypatch, confirm, configured_mode, risk, expect_confirm
):
    cfg = HarkConfig()
    cfg.confirm.mode = configured_mode
    calls: list[str] = []

    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: (
            {"ok": True},
            ListenResult(
                text="three slices is fine",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="answer",
            ),
        ),
    )
    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *a, **k: calls.append("tts") or {"ok": True},
    )
    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda *a, **k: (
            calls.append("listen")
            or ListenResult(
                text="  Yes. ",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="confirm",
            )
        ),
    )

    out = run_ask(
        cfg,
        "Should I publish this package?",
        confirm=confirm,
        risk_hint=risk,
    )

    assert out["ok"] is True
    assert out.get("cancelled") is not True
    assert ("listen" in calls) is expect_confirm


def test_run_ask_timeout_preserves_tts_info(monkeypatch):
    """Listen TimeoutError after TTS maps to run_ask JSON with tts attached."""
    cfg = HarkConfig()

    def boom(*a, **k):
        exc = TimeoutError("no speech detected")
        attach_tts_info(exc, {"ok": True, "provider": "mock", "voice": "eve"})
        raise exc

    monkeypatch.setattr("hark.speech.speak_and_listen", boom)
    out = run_ask(cfg, "hello?")
    assert out["ok"] is False
    assert out["exit"] != 0
    assert out["tts"]["provider"] == "mock"
    assert "no speech" in out["error"]


def test_half_duplex_still_via_speech_import(monkeypatch):
    """Facade path: half-duplex listen after TTS (regression for re-export)."""
    order: list[str] = []
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = False
    cfg.audio.listen_pre_arm_ms = 50

    def fake_tts(cfg, text, **kwargs):
        order.append("tts")
        on_near = kwargs.get("on_near_end")
        if on_near:
            on_near()
            order.append("near")
        time.sleep(0.01)
        order.append("tts_done")
        return {"ok": True, "provider": "mock", "voice": "eve", "mic_muted": True}

    def fake_listen(cfg, **kwargs):
        order.append("listen")
        assert kwargs.get("already_armed") is True
        return ListenResult(
            text="hi",
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            stream_id="z",
        )

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    tts_info, listened = speak_and_listen(cfg, "prompt?")
    assert tts_info["ok"]
    assert listened.text == "hi"
    assert order == ["tts", "near", "tts_done", "listen"]

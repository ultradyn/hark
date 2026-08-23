"""B177 / gh-9: Gemini TTS 400s on question-shaped text; ask must survive it.

The upstream 400 is faked at the provider seam — no live API call. The real
failure body from the filing session is used verbatim as the fixture.
"""

from __future__ import annotations

import pytest
from hark.providers.base import ProviderError, ProviderUnsupported
from hark.providers.tts_fallback import (
    TEXT_INSTEAD_OF_AUDIO,
    TTS_FALLBACK_ORDER,
    classify_tts_error,
    harden_tts_text,
    is_fallback_eligible,
    plan_synth_attempts,
    synthesize_with_fallback,
)

# Verbatim shape of what Gemini returns for question-shaped input (gh-9).
GEMINI_400 = (
    'Gemini TTS HTTP 400: {"error":{"code":400,"message":"Model tried to '
    'generate text, but it should only be used for TTS.","status":'
    '"INVALID_ARGUMENT"}} (pin a TTS-capable model or use xai/openai/minimax)'
)


# --- classification: eligible class only ------------------------------------


def test_the_reported_gemini_400_is_eligible() -> None:
    assert classify_tts_error(ProviderError(GEMINI_400)) == TEXT_INSTEAD_OF_AUDIO
    assert is_fallback_eligible(ProviderError(GEMINI_400))


def test_a_200_with_no_audio_part_is_the_same_class() -> None:
    # Same symptom, no 400: the model answered instead of speaking.
    assert (
        classify_tts_error(ProviderError("Gemini TTS missing audio data"))
        == TEXT_INSTEAD_OF_AUDIO
    )


@pytest.mark.parametrize(
    "message",
    [
        "Gemini TTS HTTP 401: invalid API key",
        "Gemini TTS HTTP 403: permission denied on model",
        "Gemini TTS HTTP 429: quota exceeded for this project",
        "xAI TTS HTTP 400: rate limit reached",
        "Gemini TTS HTTP 500: internal",
        "no TTS provider available - grok login / XAI_API_KEY",
        "connection refused",
    ],
)
def test_auth_quota_and_everything_else_still_fails_fast(message: str) -> None:
    # B175 lesson: never let a fallback mask a credential problem.
    assert classify_tts_error(ProviderError(message)) is None
    assert not is_fallback_eligible(ProviderError(message))


def test_a_fail_fast_marker_beats_a_content_marker() -> None:
    mixed = "Gemini TTS HTTP 401: tried to generate text; check your api key"
    assert classify_tts_error(ProviderError(mixed)) is None


def test_unsupported_provider_is_never_retried() -> None:
    exc = ProviderUnsupported("anthropic: no public TTS API for hark")
    assert classify_tts_error(exc) is None


def test_interrupt_is_not_a_provider_error_class() -> None:
    assert classify_tts_error(KeyboardInterrupt()) is None


# --- request hardening (best-effort half) -----------------------------------


def test_hardening_targets_gemini_only() -> None:
    hardened = harden_tts_text("google", "Should we ship it?")
    assert hardened is not None
    assert hardened.endswith("Should we ship it?")
    assert harden_tts_text("gemini", "x") is not None
    assert harden_tts_text("xai", "Should we ship it?") is None
    assert harden_tts_text("minimax", "Should we ship it?") is None


def test_hardening_is_idempotent() -> None:
    once = harden_tts_text("google", "Ship it?")
    assert once is not None
    assert harden_tts_text("google", once) is None


# --- ladder shape -----------------------------------------------------------


def test_ladder_retries_the_chosen_provider_before_leaving_it() -> None:
    plan = plan_synth_attempts("google", "Ship it?", chain=["xai", "openai"])
    assert [(a.provider, a.step) for a in plan] == [
        ("google", "primary"),
        ("google", "retry"),
        ("xai", "fallback"),
        ("openai", "fallback"),
    ]
    assert plan[0].hardened is False
    assert plan[1].hardened is True  # google has a shaping lever
    assert plan[1].text != plan[0].text


def test_ladder_retry_is_identical_when_no_hardening_exists() -> None:
    plan = plan_synth_attempts("xai", "Ship it?", chain=[])
    assert [(a.provider, a.step) for a in plan] == [
        ("xai", "primary"),
        ("xai", "retry"),
    ]
    assert plan[1].hardened is False
    assert plan[1].text == "Ship it?"


def test_ladder_never_revisits_the_starting_provider() -> None:
    plan = plan_synth_attempts("google", "hi", chain=["xai", "google", "openai"])
    assert [a.provider for a in plan] == ["google", "google", "xai", "openai"]


def test_fallback_order_mirrors_the_documented_auto_order() -> None:
    assert TTS_FALLBACK_ORDER == ("xai", "openai", "minimax", "google")


# --- the walk ---------------------------------------------------------------


def test_happy_path_never_resolves_a_chain() -> None:
    chain_calls = {"n": 0}

    def chain():
        chain_calls["n"] += 1
        return ["xai"]

    result, steps = synthesize_with_fallback(
        provider="google",
        text="Testing one two three.",
        call=lambda p, t: f"audio:{p}",
        chain=chain,
    )
    assert result == "audio:google"
    assert steps == []
    assert chain_calls["n"] == 0


def test_second_attempt_on_the_same_provider_can_succeed() -> None:
    calls: list[tuple[str, str]] = []

    def call(provider: str, text: str) -> str:
        calls.append((provider, text))
        if len(calls) == 1:
            raise ProviderError(GEMINI_400)
        return f"audio:{provider}"

    result, steps = synthesize_with_fallback(
        provider="google",
        text="Ship it?",
        call=call,
        chain=lambda: ["xai"],
    )
    assert result == "audio:google"
    assert [s.step for s in steps] == ["retry"]
    assert steps[0].hardened is True
    # The retry used the hardened request, not the original text.
    assert calls[1][1] != calls[0][1]


def test_falls_back_to_the_next_provider_and_reports_it() -> None:
    def call(provider: str, text: str) -> str:
        if provider == "google":
            raise ProviderError(GEMINI_400)
        return f"audio:{provider}"

    result, steps = synthesize_with_fallback(
        provider="google",
        text="Should the main message be that every chain is protected?",
        call=call,
        chain=lambda: ["xai", "openai"],
    )
    assert result == "audio:xai"
    assert [(s.from_provider, s.to_provider, s.step) for s in steps] == [
        ("google", "google", "retry"),
        ("google", "xai", "fallback"),
    ]
    assert all(s.error_class == TEXT_INSTEAD_OF_AUDIO for s in steps)
    assert "should only be used for TTS" in steps[0].error


def test_on_step_fires_for_every_hop() -> None:
    seen: list[str] = []

    def call(provider: str, text: str) -> str:
        if provider in ("google", "xai"):
            raise ProviderError(GEMINI_400)
        return f"audio:{provider}"

    result, steps = synthesize_with_fallback(
        provider="google",
        text="q?",
        call=call,
        chain=lambda: ["xai", "openai"],
        on_step=lambda s: seen.append(f"{s.from_provider}->{s.to_provider}"),
    )
    assert result == "audio:openai"
    assert seen == ["google->google", "google->xai", "xai->openai"]
    assert len(steps) == 3


def test_a_fail_fast_error_stops_the_ladder_immediately() -> None:
    calls: list[str] = []

    def call(provider: str, text: str) -> str:
        calls.append(provider)
        raise ProviderError("Gemini TTS HTTP 403: permission denied")

    with pytest.raises(ProviderError, match="403"):
        synthesize_with_fallback(
            provider="google",
            text="q?",
            call=call,
            chain=lambda: ["xai", "openai"],
        )
    assert calls == ["google"]  # no retry, no fallback


def test_exhausted_ladder_reraises_the_first_error_not_the_last() -> None:
    def call(provider: str, text: str) -> str:
        if provider == "google":
            raise ProviderError(GEMINI_400)
        raise ProviderError("xAI TTS missing audio data")

    with pytest.raises(ProviderError) as excinfo:
        synthesize_with_fallback(
            provider="google",
            text="q?",
            call=call,
            chain=lambda: ["xai"],
        )
    # The operator needs the real trigger, not the last provider's complaint.
    assert "should only be used for TTS" in str(excinfo.value)
    steps = getattr(excinfo.value, "tts_fallback_steps", None)
    assert steps is not None
    assert [s["to_provider"] for s in steps] == ["google", "xai"]


def test_disabled_fallback_makes_one_attempt_only() -> None:
    calls: list[str] = []

    def call(provider: str, text: str) -> str:
        calls.append(provider)
        raise ProviderError(GEMINI_400)

    with pytest.raises(ProviderError):
        synthesize_with_fallback(
            provider="google",
            text="q?",
            call=call,
            chain=lambda: ["xai"],
            enabled=False,
        )
    assert calls == ["google"]


def test_no_chain_available_still_gets_the_one_retry() -> None:
    calls: list[str] = []

    def call(provider: str, text: str) -> str:
        calls.append(provider)
        if len(calls) == 1:
            raise ProviderError(GEMINI_400)
        return "audio:google"

    result, steps = synthesize_with_fallback(
        provider="google",
        text="q?",
        call=call,
        chain=lambda: [],
    )
    assert result == "audio:google"
    assert [s.step for s in steps] == ["retry"]


# --- chain construction reuses the real `auto` walk -------------------------


def test_chain_only_offers_providers_auto_would_actually_pick(monkeypatch) -> None:
    """An explicit pin skips the availability check; the chain must not.

    Without this, a google 400 would fall back to xai, xai would raise a
    credential error, and the operator would be shown an auth failure instead
    of the real trigger.
    """
    from hark.config import TtsConfig
    from hark.providers import resolve as resolve_mod
    from hark.providers.tts_fallback import fallback_provider_chain

    class _Avail:
        def __init__(self, available: bool) -> None:
            self.available = available

    # Only OpenAI has credentials. xAI and MiniMax do not.
    monkeypatch.setattr(resolve_mod, "xai_auth", lambda: _Avail(False))
    monkeypatch.setattr(resolve_mod, "openai_auth", lambda: _Avail(True))
    monkeypatch.setattr(resolve_mod, "minimax_auth", lambda: _Avail(False))
    monkeypatch.setattr(resolve_mod, "google_auth", lambda: _Avail(True))
    monkeypatch.delenv("HARK_TTS_DISABLED", raising=False)
    monkeypatch.delenv("HARK_TTS_MINIMAX_OK", raising=False)

    chain = fallback_provider_chain("google", tts_cfg=TtsConfig())
    assert chain == ["openai"]


def test_chain_honours_the_operator_disabled_list(monkeypatch) -> None:
    from hark.config import TtsConfig
    from hark.providers import resolve as resolve_mod
    from hark.providers.tts_fallback import fallback_provider_chain

    class _Avail:
        def __init__(self, available: bool) -> None:
            self.available = available

    monkeypatch.setattr(resolve_mod, "xai_auth", lambda: _Avail(True))
    monkeypatch.setattr(resolve_mod, "openai_auth", lambda: _Avail(True))
    monkeypatch.setattr(resolve_mod, "minimax_auth", lambda: _Avail(True))
    monkeypatch.setattr(resolve_mod, "google_auth", lambda: _Avail(True))
    monkeypatch.delenv("HARK_TTS_DISABLED", raising=False)
    monkeypatch.delenv("HARK_TTS_MINIMAX_OK", raising=False)

    # The gh-9 configuration: xai + openai disabled, so auto landed on google.
    cfg = TtsConfig(disabled=["xai", "openai"], minimax_ok=True)
    assert fallback_provider_chain("google", tts_cfg=cfg) == ["minimax"]
    # And the operator's disabled list is not mutated by building the chain.
    assert cfg.disabled == ["xai", "openai"]


def test_chain_skips_minimax_without_consent(monkeypatch) -> None:
    from hark.config import TtsConfig
    from hark.providers import resolve as resolve_mod
    from hark.providers.tts_fallback import fallback_provider_chain

    class _Avail:
        def __init__(self, available: bool) -> None:
            self.available = available

    monkeypatch.setattr(resolve_mod, "xai_auth", lambda: _Avail(False))
    monkeypatch.setattr(resolve_mod, "openai_auth", lambda: _Avail(False))
    monkeypatch.setattr(resolve_mod, "minimax_auth", lambda: _Avail(True))
    monkeypatch.setattr(resolve_mod, "google_auth", lambda: _Avail(False))
    monkeypatch.delenv("HARK_TTS_DISABLED", raising=False)
    monkeypatch.delenv("HARK_TTS_MINIMAX_OK", raising=False)
    # No TTY consent prompt may run mid-turn, so MiniMax is simply skipped.
    monkeypatch.setattr(resolve_mod, "_prompt_minimax_ok", lambda: True)

    assert fallback_provider_chain("google", tts_cfg=TtsConfig(minimax_ok=False)) == []


def test_chain_excludes_the_current_provider_by_alias(monkeypatch) -> None:
    from hark.config import TtsConfig
    from hark.providers import resolve as resolve_mod
    from hark.providers.tts_fallback import fallback_provider_chain

    class _Avail:
        def __init__(self, available: bool) -> None:
            self.available = available

    monkeypatch.setattr(resolve_mod, "xai_auth", lambda: _Avail(False))
    monkeypatch.setattr(resolve_mod, "openai_auth", lambda: _Avail(False))
    monkeypatch.setattr(resolve_mod, "minimax_auth", lambda: _Avail(False))
    monkeypatch.setattr(resolve_mod, "google_auth", lambda: _Avail(True))
    monkeypatch.delenv("HARK_TTS_DISABLED", raising=False)
    monkeypatch.delenv("HARK_TTS_MINIMAX_OK", raising=False)

    # "gemini" is the same provider as "google" — do not offer it back.
    assert fallback_provider_chain("gemini", tts_cfg=TtsConfig()) == []


# --- Gemini model pin (best-effort hardening half) --------------------------


def test_gemini_tts_model_is_tts_only_and_overridable(monkeypatch) -> None:
    from hark.providers.google_p import gemini_tts_model

    monkeypatch.delenv("GEMINI_TTS_MODEL", raising=False)
    default = gemini_tts_model()
    # The shipped pin is already a TTS-only model — which is why model pinning
    # cannot be the fix for gh-9 and the fallback is load-bearing.
    assert default.endswith("-tts")

    monkeypatch.setenv("GEMINI_TTS_MODEL", "gemini-2.5-pro-preview-tts")
    assert gemini_tts_model() == "gemini-2.5-pro-preview-tts"
    monkeypatch.setenv("GEMINI_TTS_MODEL", "   ")
    assert gemini_tts_model() == default


# --- config surface ---------------------------------------------------------


def test_fallback_on_error_config_default_and_toml(tmp_path) -> None:
    from hark.config import config_to_dict, load_config

    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.tts.fallback_on_error is True
    assert config_to_dict(cfg)["tts"]["fallback_on_error"] is True

    path = tmp_path / "config.toml"
    path.write_text("[tts]\nfallback_on_error = false\n", encoding="utf-8")
    loaded = load_config(path)
    assert loaded.tts.fallback_on_error is False
    # Registered as a known key, so it must not raise an unknown-key warning.
    assert not [w for w in loaded.warnings if "fallback_on_error" in w]


def test_default_config_template_ships_the_key(tmp_path) -> None:
    from hark.config import load_config, write_default_config

    path = tmp_path / "config.toml"
    write_default_config(path=path, force=True)
    assert "fallback_on_error" in path.read_text(encoding="utf-8")
    loaded = load_config(path)
    assert loaded.tts.fallback_on_error is True
    assert not [w for w in loaded.warnings if "fallback_on_error" in w]


# --- review findings: the ladder must not be truncated or misattributed -----


def test_a_stale_key_on_a_later_rung_does_not_truncate_the_chain() -> None:
    """`auto` picks on credential presence, not viability (docs/PROVIDERS.md).

    A present-but-revoked key therefore lands in the chain. If its 401 aborted
    the walk, one stale credential would kill the very fallback B177 exists to
    provide — and the operator would be shown an auth error for a content bug.
    """
    calls: list[str] = []

    def call(provider: str, text: str) -> str:
        calls.append(provider)
        if provider == "google":
            raise ProviderError(GEMINI_400)
        if provider == "openai":
            raise ProviderError("OpenAI TTS HTTP 401: invalid_api_key")
        return f"audio:{provider}"

    result, steps = synthesize_with_fallback(
        provider="google",
        text="q?",
        call=call,
        chain=lambda: ["openai", "minimax"],
    )
    assert result == "audio:minimax"
    assert calls == ["google", "google", "openai", "minimax"]
    # The skipped rung is recorded, with its own class — not silently dropped.
    assert [s.error_class for s in steps] == [
        TEXT_INSTEAD_OF_AUDIO,
        TEXT_INSTEAD_OF_AUDIO,
        "not_retryable",
    ]
    assert "401" in steps[2].error


def test_an_ineligible_error_on_the_chosen_provider_still_fails_fast() -> None:
    """Only LATER rungs may be walked past — the primary decides fail-fast."""
    calls: list[str] = []

    def call(provider: str, text: str) -> str:
        calls.append(provider)
        raise ProviderError("Gemini TTS HTTP 401: API key not valid")

    with pytest.raises(ProviderError, match="401"):
        synthesize_with_fallback(
            provider="google",
            text="q?",
            call=call,
            chain=lambda: ["openai"],
        )
    assert calls == ["google"]


def test_exhaustion_after_a_stale_rung_still_reports_the_real_trigger() -> None:
    def call(provider: str, text: str) -> str:
        if provider == "google":
            raise ProviderError(GEMINI_400)
        raise ProviderError("OpenAI TTS HTTP 403: permission denied")

    with pytest.raises(ProviderError) as excinfo:
        synthesize_with_fallback(
            provider="google",
            text="q?",
            call=call,
            chain=lambda: ["openai"],
        )
    assert "should only be used for TTS" in str(excinfo.value)
    assert getattr(excinfo.value, "tts_last_provider") == "openai"


def test_a_keyboard_interrupt_on_a_later_rung_is_never_walked_past() -> None:
    calls: list[str] = []

    def call(provider: str, text: str) -> str:
        calls.append(provider)
        if provider == "google":
            raise ProviderError(GEMINI_400)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        synthesize_with_fallback(
            provider="google",
            text="q?",
            call=call,
            chain=lambda: ["openai", "minimax"],
        )
    assert calls == ["google", "google", "openai"]  # minimax never asked


def test_the_chain_is_not_probed_until_the_retry_is_spent() -> None:
    """Credential probes cost latency while the operator waits mid-turn."""
    probes = {"n": 0}
    calls: list[str] = []

    def chain():
        probes["n"] += 1
        return ["xai"]

    def call(provider: str, text: str) -> str:
        calls.append(provider)
        if len(calls) == 1:
            raise ProviderError(GEMINI_400)
        return "audio"

    synthesize_with_fallback(
        provider="google", text="q?", call=call, chain=chain
    )
    assert calls == ["google", "google"]
    assert probes["n"] == 0


def test_hardening_is_skipped_for_non_english_output() -> None:
    # The directive is English; speaking it would be worse than not shaping.
    assert harden_tts_text("google", "¿Lo enviamos?", language="es") is None
    assert harden_tts_text("google", "Ship it?", language="en-AU") is not None
    assert harden_tts_text("google", "Ship it?", language=None) is not None
    plan = plan_synth_attempts("google", "¿Lo enviamos?", language="es")
    assert plan[1].hardened is False
    assert plan[1].text == "¿Lo enviamos?"

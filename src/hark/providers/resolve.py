"""Provider auto-resolution."""

from __future__ import annotations

from hark.providers.auth import google_auth, minimax_auth, openai_auth, xai_auth
from hark.providers.base import ProviderError, ProviderUnsupported, SttProvider, TtsProvider
from hark.providers.google_p import GoogleStt, GoogleTts
from hark.providers.minimax import MinimaxStt, MinimaxTts
from hark.providers.openai_p import OpenAIStt, OpenAITts
from hark.providers.xai import XaiStt, XaiTts


def resolve_stt(name: str = "auto") -> SttProvider:
    name = (name or "auto").lower()
    if name == "anthropic":
        raise ProviderUnsupported(
            "anthropic: no public STT API; use xai|openai|google"
        )
    if name == "minimax":
        return MinimaxStt()
    if name == "xai":
        return XaiStt()
    if name == "openai":
        return OpenAIStt()
    if name in ("google", "gemini"):
        return GoogleStt()
    if name != "auto":
        raise ProviderError(f"unknown STT provider: {name}")
    if xai_auth().available:
        return XaiStt()
    if openai_auth().available:
        return OpenAIStt()
    if google_auth().available:
        return GoogleStt()
    raise ProviderError(
        "no STT provider available — grok login / XAI_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY"
    )


def resolve_tts(
    name: str = "auto",
    *,
    voice: str | None = None,
    language: str | None = None,
) -> TtsProvider:
    name = (name or "auto").lower()
    if name == "anthropic":
        raise ProviderUnsupported(
            "anthropic: no public TTS API for hark; use xai|openai|minimax|google"
        )

    def _xai() -> XaiTts:
        return XaiTts(
            voice=voice or "eve",
            language=language or "en",
        )

    if name == "xai":
        return _xai()
    if name == "openai":
        return OpenAITts()
    if name == "minimax":
        return MinimaxTts()
    if name in ("google", "gemini"):
        return GoogleTts()
    if name != "auto":
        raise ProviderError(f"unknown TTS provider: {name}")
    if xai_auth().available:
        return _xai()
    if openai_auth().available:
        return OpenAITts()
    if minimax_auth().available:
        return MinimaxTts()
    if google_auth().available:
        return GoogleTts()
    raise ProviderError(
        "no TTS provider available — grok login / XAI_API_KEY / OPENAI_API_KEY / MINIMAX_API_KEY"
    )

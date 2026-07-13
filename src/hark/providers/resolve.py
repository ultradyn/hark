"""Provider auto-resolution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hark.providers.auth import google_auth, minimax_auth, openai_auth, xai_auth
from hark.providers.base import ProviderError, ProviderUnsupported, SttProvider, TtsProvider
from hark.providers.google_p import GoogleStt, GoogleTts
from hark.providers.minimax import MinimaxStt, MinimaxTts
from hark.providers.openai_p import OpenAIStt, OpenAITts
from hark.providers.xai import XaiStt, XaiTts

if TYPE_CHECKING:
    from hark.config import SttConfig

log = logging.getLogger("hark.providers")

# Local full-STT names (B072). Cloud remains default under "auto".
_LOCAL_STT_NAMES = frozenset(
    {
        "faster_whisper",
        "faster-whisper",
        "whisper",  # alias → faster-whisper
        "local",  # alias → faster-whisper
        "moonshine",
    }
)


def _normalize_stt_name(name: str) -> str:
    n = (name or "auto").lower().strip()
    if n in ("faster-whisper", "whisper", "local"):
        return "faster_whisper"
    return n


def _local_opts(stt_cfg: SttConfig | None) -> dict:
    """Defaults when no SttConfig is passed (tests / direct resolve)."""
    if stt_cfg is None:
        return {
            "model": "tiny.en",
            "device": "cpu",
            "compute_type": "int8",
            "model_path": None,
            "fail_open": True,
            "download": True,
        }
    return {
        "model": getattr(stt_cfg, "local_model", None) or "tiny.en",
        "device": getattr(stt_cfg, "local_device", None) or "cpu",
        "compute_type": getattr(stt_cfg, "local_compute_type", None) or "int8",
        "model_path": getattr(stt_cfg, "local_model_path", None),
        "fail_open": bool(getattr(stt_cfg, "local_fail_open", True)),
        "download": bool(getattr(stt_cfg, "local_download", True)),
    }


def _resolve_cloud_stt(name: str = "auto") -> SttProvider:
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


def _try_local_stt(name: str, opts: dict) -> SttProvider:
    """Build a local STT provider; raises ProviderError on missing deps/models."""
    from hark.providers.local_stt import FasterWhisperStt, MoonshineStt

    if name == "moonshine":
        m = opts["model"] or "moonshine/tiny"
        # SttConfig defaults to tiny.en (faster-whisper); map to Moonshine tiny.
        if m.endswith(".en") or m in ("tiny", "base", "small"):
            m = "moonshine/tiny" if "base" not in m and "small" not in m else "moonshine/base"
        return MoonshineStt(model=m)

    # Probe load so fail-open can kick in before first listen.
    stt = FasterWhisperStt(
        model=opts["model"],
        device=opts["device"],
        compute_type=opts["compute_type"],
        model_path=opts["model_path"],
        download=opts["download"],
    )
    stt._ensure_model()
    return stt


def resolve_stt(
    name: str = "auto",
    *,
    stt_cfg: SttConfig | None = None,
) -> SttProvider:
    """Resolve STT provider.

    Cloud providers are the default (``auto`` / xai / openai / google).
    Optional local engines (``faster_whisper``, ``moonshine``) require the
    ``local-stt`` extra (or Moonshine stretch install). When local is selected
    and unavailable, ``stt.local_fail_open`` (default True) falls back to cloud
    auto-resolution.
    """
    raw = (name or "auto").lower().strip()
    if raw in _LOCAL_STT_NAMES or _normalize_stt_name(raw) in (
        "faster_whisper",
        "moonshine",
    ):
        local_name = _normalize_stt_name(raw)
        opts = _local_opts(stt_cfg)
        try:
            return _try_local_stt(local_name, opts)
        except ProviderError as exc:
            if opts["fail_open"]:
                log.warning(
                    "local STT %s unavailable (%s); fail-open to cloud",
                    local_name,
                    exc,
                )
                return _resolve_cloud_stt("auto")
            raise

    return _resolve_cloud_stt(raw)


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

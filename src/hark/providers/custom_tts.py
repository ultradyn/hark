"""Custom TTS — OpenAI-compatible speech synthesis against a configurable base URL.

Product name: **Custom TTS**. Talks to any HTTP gateway that implements the
OpenAI audio speech surface (and optionally a native ``/tts`` alias accepting
the same JSON body).

See ``docs/PROVIDERS.md`` § Custom TTS for the normative client contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from hark.providers.base import ProviderError, SynthResult, provider_operation
from hark.providers.custom_stt import (
    join_custom_url,
    normalize_custom_base_url,
    normalize_custom_path,
    resolve_custom_api_key,
)

DEFAULT_CUSTOM_TTS_PATH = "/audio/speech"
DEFAULT_NATIVE_TTS_PATH = "/tts"
DEFAULT_CUSTOM_VOICE = "alloy"  # OpenAI parity


@dataclass(frozen=True)
class CustomTtsStatus:
    name: str
    available: bool
    detail: str
    base_url: str | None = None
    path: str | None = None
    model: str | None = None
    voice: str | None = None


def custom_tts_status(
    *,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    voice: str | None = None,
    path: str | None = None,
    api_key_file: str | None = None,
    api_key_command: str | None = None,
) -> CustomTtsStatus:
    """Soft readiness: base URL + API key (+ model when /audio/speech) present."""
    bu = (base_url or "").strip()
    md = (model or "").strip() or None
    vc = (voice or "").strip() or None
    try:
        key = resolve_custom_api_key(
            api_key, api_key_file, api_key_command, label="custom TTS"
        ) or ""
    except ProviderError as exc:
        return CustomTtsStatus(
            name="custom",
            available=False,
            detail=str(exc),
            base_url=bu.rstrip("/") or None,
            path=path,
            model=md,
            voice=vc,
        )
    try:
        p = normalize_custom_path(path or DEFAULT_CUSTOM_TTS_PATH)
    except ProviderError as exc:
        return CustomTtsStatus(
            name="custom",
            available=False,
            detail=str(exc),
            base_url=bu or None,
            path=path,
            model=md,
            voice=vc,
        )
    if not bu:
        return CustomTtsStatus(
            name="custom",
            available=False,
            detail=(
                "not configured — set [tts].custom_base_url + key via "
                "HARK_TTS_CUSTOM_API_KEY / custom_api_key_file / "
                "custom_api_key_command (provider=custom is explicit-only)"
            ),
            base_url=None,
            path=p,
            model=md,
            voice=vc,
        )
    if not key:
        return CustomTtsStatus(
            name="custom",
            available=False,
            detail=(
                f"base_url set ({bu.rstrip('/')}) but API key missing — "
                "HARK_TTS_CUSTOM_API_KEY, custom_api_key_file, "
                "or custom_api_key_command"
            ),
            base_url=bu.rstrip("/"),
            path=p,
            model=md,
            voice=vc,
        )
    # OpenAI surface requires model; native /tts may inject a default.
    if _path_requires_model(p) and not md:
        return CustomTtsStatus(
            name="custom",
            available=False,
            detail=(
                f"base_url+key ok but custom_model required for path {p} "
                "(OpenAI-compatible audio/speech)"
            ),
            base_url=bu.rstrip("/"),
            path=p,
            model=None,
            voice=vc,
        )
    host = bu.rstrip("/")
    model_bit = f", model={md}" if md else ", model=(server default)"
    voice_bit = f", voice={vc or DEFAULT_CUSTOM_VOICE}" if vc else ""
    return CustomTtsStatus(
        name="custom",
        available=True,
        detail=f"configured → POST {host}{p}{model_bit}{voice_bit}",
        base_url=host,
        path=p,
        model=md,
        voice=vc,
    )


def _path_requires_model(path: str) -> bool:
    p = path.rstrip("/")
    return p.endswith("/audio/speech")


class CustomTts:
    """Batch TTS client for OpenAI-compatible (or native /tts dual-mount) gateways."""

    name = "custom"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str | None = None,
        voice: str | None = None,
        path: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = normalize_custom_base_url(base_url)
        key = (api_key or "").strip()
        if not key:
            raise ProviderError(
                "custom TTS API key missing — set HARK_TTS_CUSTOM_API_KEY, "
                "[tts].custom_api_key_file, or [tts].custom_api_key_command"
            )
        self.api_key = key
        self.path = normalize_custom_path(path or DEFAULT_CUSTOM_TTS_PATH)
        self.model = (model or "").strip() or None
        self.voice = (voice or "").strip() or None
        self.timeout = timeout
        # OpenAI speech requires model; native dual-mount may omit it.
        if _path_requires_model(self.path) and not self.model:
            raise ProviderError(
                "custom TTS requires custom_model for OpenAI-compatible path "
                f"{self.path} (config [tts].custom_model or HARK_TTS_CUSTOM_MODEL)"
            )
        self.url = join_custom_url(self.base_url, self.path)

    @provider_operation("Custom TTS")
    def synthesize(self, text: str, *, voice: str | None = None) -> SynthResult:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        body: dict[str, Any] = {
            "input": text,
            "voice": voice or self.voice or DEFAULT_CUSTOM_VOICE,
        }
        if self.model:
            body["model"] = self.model
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(self.url, headers=headers, json=body)
            if r.status_code == 401:
                raise ProviderError(
                    "Custom TTS 401 — check HARK_TTS_CUSTOM_API_KEY / "
                    "custom_api_key_file / custom_api_key_command"
                )
            if r.status_code >= 400:
                raise ProviderError(f"Custom TTS HTTP {r.status_code}: {r.text[:300]}")
            return SynthResult(
                audio=r.content,
                provider=self.name,
                content_type=r.headers.get("content-type", "audio/mpeg"),
                voice=body["voice"],
            )

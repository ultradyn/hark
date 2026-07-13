"""xAI STT/TTS via Grok OAuth or XAI_API_KEY."""

from __future__ import annotations

import json
from typing import Any

import httpx

from hark.providers.auth import resolve_xai_token
from hark.providers.base import ProviderError, SynthResult, Transcript, provider_operation

STT_URL = "https://api.x.ai/v1/stt"
TTS_URL = "https://api.x.ai/v1/tts"
VOICES_URL = "https://api.x.ai/v1/tts/voices"
DEFAULT_VOICE = "eve"
DEFAULT_LANGUAGE = "en"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token() -> str:
    tok = resolve_xai_token()
    if not tok:
        raise ProviderError(
            "xAI auth missing — run `grok login` or set XAI_API_KEY"
        )
    return tok


class XaiStt:
    name = "xai"

    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout

    @provider_operation("xAI STT")
    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        token = _token()
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data: dict[str, Any] = {}
        if language:
            data["language"] = language
        with httpx.Client(timeout=self.timeout) as client:
            # Try multipart file first; fall back to JSON body shapes if needed
            r = client.post(
                STT_URL,
                headers=_headers(token),
                files=files,
                data=data or None,
            )
            if r.status_code == 404:
                # alternate path seen in some docs
                r = client.post(
                    "https://api.x.ai/v1/audio/transcriptions",
                    headers=_headers(token),
                    files=files,
                    data=data or None,
                )
            if r.status_code == 401:
                raise ProviderError(
                    "xAI STT 401 — run `grok login` or set XAI_API_KEY"
                )
            if r.status_code >= 400:
                raise ProviderError(f"xAI STT HTTP {r.status_code}: {r.text[:300]}")
            payload = r.json()
        text = (
            payload.get("text")
            or payload.get("transcript")
            or payload.get("result", {}).get("text")
            or ""
        )
        if isinstance(payload.get("words"), list) and not text:
            text = " ".join(
                str(w.get("word") or w.get("text") or "") for w in payload["words"]
            )
        return Transcript(text=str(text).strip(), provider=self.name)


@provider_operation("xAI voices")
def list_xai_voices() -> list[dict[str, Any]]:
    """GET /v1/tts/voices — built-in voice catalog."""
    token = _token()
    with httpx.Client(timeout=30.0) as client:
        r = client.get(VOICES_URL, headers=_headers(token))
        if r.status_code == 401:
            raise ProviderError(
                "xAI voices 401 — run `grok login` or set XAI_API_KEY"
            )
        if r.status_code >= 400:
            raise ProviderError(f"xAI voices HTTP {r.status_code}: {r.text[:300]}")
        payload = r.json()
    voices = payload.get("voices") if isinstance(payload, dict) else None
    if not isinstance(voices, list):
        raise ProviderError("xAI voices response missing voices[]")
    return [v for v in voices if isinstance(v, dict)]


class XaiTts:
    name = "xai"

    def __init__(
        self,
        timeout: float = 120.0,
        voice: str = DEFAULT_VOICE,
        language: str = DEFAULT_LANGUAGE,
    ) -> None:
        self.timeout = timeout
        self.voice = voice
        self.language = language

    @provider_operation("xAI TTS")
    def synthesize(self, text: str, *, voice: str | None = None) -> SynthResult:
        token = _token()
        voice_id = voice or self.voice or DEFAULT_VOICE
        # Official REST: text, voice_id, language (required)
        body = {
            "text": text,
            "voice_id": voice_id,
            "language": self.language or DEFAULT_LANGUAGE,
        }
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                TTS_URL,
                headers={**_headers(token), "Content-Type": "application/json"},
                json=body,
            )
            if r.status_code == 401:
                raise ProviderError(
                    "xAI TTS 401 — run `grok login` or set XAI_API_KEY"
                )
            if r.status_code == 404:
                raise ProviderError(
                    f"xAI TTS unknown voice_id={voice_id!r} — "
                    "run: hark providers voices"
                )
            if r.status_code >= 400:
                raise ProviderError(f"xAI TTS HTTP {r.status_code}: {r.text[:300]}")
            ctype = r.headers.get("content-type", "audio/mpeg")
            if "json" in ctype:
                payload = r.json()
                import base64

                b64 = payload.get("audio") or payload.get("data")
                if not b64:
                    raise ProviderError(
                        f"xAI TTS JSON missing audio: {list(payload)[:8]}"
                    )
                audio = base64.b64decode(b64)
                return SynthResult(
                    audio=audio,
                    provider=self.name,
                    content_type=str(payload.get("content_type") or "audio/mpeg"),
                    voice=voice_id,
                )
            return SynthResult(
                audio=r.content,
                provider=self.name,
                content_type=ctype,
                voice=voice_id,
            )

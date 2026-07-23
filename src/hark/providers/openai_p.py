"""OpenAI STT/TTS."""

from __future__ import annotations

import os

import httpx

from hark.providers.auth import resolve_openai_api_key
from hark.providers.base import ProviderError, SynthResult, Transcript, provider_operation

API = "https://api.openai.com/v1"

# Codex ChatGPT OAuth lacks whisper-1 / TTS scopes; gpt-4o-*-transcribe works.
_DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"
_FALLBACK_STT_MODEL = "whisper-1"


def _key() -> str:
    k = resolve_openai_api_key()
    if not k:
        raise ProviderError(
            "OpenAI auth missing — set OPENAI_API_KEY or use codex/opencode/pi login"
        )
    return k


def _stt_model() -> str:
    return (os.environ.get("OPENAI_STT_MODEL") or _DEFAULT_STT_MODEL).strip() or _DEFAULT_STT_MODEL


class OpenAIStt:
    name = "openai"

    @provider_operation("OpenAI STT")
    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        headers = {"Authorization": f"Bearer {_key()}"}
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        model = _stt_model()
        data = {"model": model}
        if language:
            data["language"] = language
        with httpx.Client(timeout=120.0) as client:
            r = client.post(
                f"{API}/audio/transcriptions",
                headers=headers,
                files=files,
                data=data,
            )
            # API keys often allow whisper-1; Codex OAuth often only allows gpt-4o-*.
            if (
                r.status_code in (400, 401, 403, 404)
                and model != _FALLBACK_STT_MODEL
                and not os.environ.get("OPENAI_STT_MODEL")
            ):
                data = dict(data)
                data["model"] = _FALLBACK_STT_MODEL
                r = client.post(
                    f"{API}/audio/transcriptions",
                    headers=headers,
                    files=files,
                    data=data,
                )
            if r.status_code >= 400:
                raise ProviderError(f"OpenAI STT HTTP {r.status_code}: {r.text[:300]}")
            payload = r.json()
        return Transcript(text=str(payload.get("text") or "").strip(), provider=self.name)


class OpenAITts:
    name = "openai"

    @provider_operation("OpenAI TTS")
    def synthesize(self, text: str, *, voice: str | None = None) -> SynthResult:
        headers = {
            "Authorization": f"Bearer {_key()}",
            "Content-Type": "application/json",
        }
        body = {
            "model": "gpt-4o-mini-tts",
            "input": text,
            "voice": voice or "alloy",
        }
        with httpx.Client(timeout=120.0) as client:
            r = client.post(f"{API}/audio/speech", headers=headers, json=body)
            if r.status_code == 400:
                # fallback older tts-1
                body["model"] = "tts-1"
                r = client.post(f"{API}/audio/speech", headers=headers, json=body)
            if r.status_code >= 400:
                raise ProviderError(f"OpenAI TTS HTTP {r.status_code}: {r.text[:300]}")
            return SynthResult(
                audio=r.content,
                provider=self.name,
                content_type=r.headers.get("content-type", "audio/mpeg"),
                voice=voice or "alloy",
            )

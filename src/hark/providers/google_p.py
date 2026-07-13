"""Google/Gemini batch STT and TTS (best-effort)."""

from __future__ import annotations

import base64
import os

import httpx

from hark.providers.base import ProviderError, SynthResult, Transcript, provider_operation

GEMINI = "https://generativelanguage.googleapis.com/v1beta"


def _key() -> str:
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not k:
        raise ProviderError("set GEMINI_API_KEY or GOOGLE_API_KEY")
    return k


class GoogleStt:
    name = "google"

    @provider_operation("Gemini STT")
    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        key = _key()
        b64 = base64.b64encode(wav_bytes).decode("ascii")
        model = "gemini-2.0-flash"
        url = f"{GEMINI}/models/{model}:generateContent?key={key}"
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": b64,
                            }
                        },
                        {
                            "text": (
                                "Transcribe this audio verbatim. "
                                "Output only the transcript text, nothing else."
                            )
                        },
                    ]
                }
            ]
        }
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, json=body)
            if r.status_code >= 400:
                raise ProviderError(f"Gemini STT HTTP {r.status_code}: {r.text[:300]}")
            payload = r.json()
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Gemini STT unexpected response: {payload!r}"[:400]) from exc
        return Transcript(text=str(text).strip(), provider=self.name)


class GoogleTts:
    name = "google"

    @provider_operation("Gemini TTS")
    def synthesize(self, text: str, *, voice: str | None = None) -> SynthResult:
        # Gemini TTS is model/version sensitive; try audio generation if available
        key = _key()
        model = "gemini-2.5-flash-preview-tts"
        url = f"{GEMINI}/models/{model}:generateContent?key={key}"
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
            },
        }
        with httpx.Client(timeout=120.0) as client:
            r = client.post(url, json=body)
            if r.status_code >= 400:
                raise ProviderError(
                    f"Gemini TTS HTTP {r.status_code}: {r.text[:300]} "
                    "(pin a TTS-capable model or use xai/openai/minimax)"
                )
            payload = r.json()
        try:
            part = payload["candidates"][0]["content"]["parts"][0]
            inline = part.get("inlineData") or part.get("inline_data") or {}
            b64 = inline.get("data")
            mime = inline.get("mimeType") or inline.get("mime_type") or "audio/wav"
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Gemini TTS unexpected: {str(payload)[:300]}") from exc
        if not b64:
            raise ProviderError("Gemini TTS missing audio data")
        return SynthResult(
            audio=base64.b64decode(b64),
            provider=self.name,
            content_type=mime,
            voice=voice,
        )

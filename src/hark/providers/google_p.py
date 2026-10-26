"""Google/Gemini batch STT and TTS (best-effort)."""

from __future__ import annotations

import base64
import os

import httpx

from hark.providers import auth
from hark.providers.base import ProviderError, SynthResult, Transcript, provider_operation

GEMINI = "https://generativelanguage.googleapis.com/v1beta"

# B177 / gh-9: this model is **already TTS-only** — the 400 "Model tried to
# generate text, but it should only be used for TTS" comes from the model
# choosing to answer question-shaped input rather than speak it, not from a
# general-purpose model being used by mistake. So model pinning is not the fix
# (see hark.providers.tts_fallback); the pin is named here only so it cannot be
# casually swapped for a non-TTS model, and is overridable for an operator who
# has a TTS model that behaves better on their account.
_DEFAULT_TTS_MODEL = "gemini-2.5-flash-preview-tts"


def gemini_tts_model() -> str:
    """TTS-only Gemini model; ``GEMINI_TTS_MODEL`` pins a different one."""
    return (os.environ.get("GEMINI_TTS_MODEL") or "").strip() or _DEFAULT_TTS_MODEL


def _key_and_headers() -> tuple[str, dict[str, str]]:
    token, detail = auth.resolve_google_token()
    if not token:
        raise ProviderError(f"Google STT/TTS: {detail}")
    headers: dict[str, str] = {}
    if token.startswith("ya29.") or token.startswith("eyJ") or "oauth" in detail.lower():
        headers["Authorization"] = f"Bearer {token}"
        key_param = ""
    else:
        key_param = f"?key={token}"
    return key_param, headers


class GoogleStt:
    name = "google"

    @provider_operation("Gemini STT")
    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        key_param, headers = _key_and_headers()
        b64 = base64.b64encode(wav_bytes).decode("ascii")
        model = "gemini-2.0-flash"
        url = f"{GEMINI}/models/{model}:generateContent{key_param}"
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
            r = client.post(url, headers=headers, json=body)
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
        key_param, headers = _key_and_headers()
        model = gemini_tts_model()
        url = f"{GEMINI}/models/{model}:generateContent{key_param}"
        body = {
            "contents": [{"parts": [{"text": text}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
            },
        }
        # The parent also enforces a 30s wall deadline. Fail a silent network
        # stall sooner and leave time for isolated-worker cleanup.
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0)) as client:
            r = client.post(url, headers=headers, json=body)

            if r.status_code >= 400:
                raise ProviderError(
                    f"Gemini TTS HTTP {r.status_code}: {r.text[:300]} "
                    f"(model={model}; pin another TTS model with GEMINI_TTS_MODEL, "
                    "or use xai/openai/minimax)"
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

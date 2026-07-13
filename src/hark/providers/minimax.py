"""MiniMax TTS (T2A). STT unsupported."""

from __future__ import annotations

import os

import httpx

from hark.providers.auth import resolve_minimax_api_key
from hark.providers.base import (
    ProviderError,
    ProviderUnsupported,
    SynthResult,
    Transcript,
    provider_operation,
)

T2A_URL = "https://api.minimax.io/v1/t2a_v2"


def _key() -> str:
    k = resolve_minimax_api_key()
    if not k:
        raise ProviderError(
            "MiniMax auth missing — set MINIMAX_API_KEY or run: mmx auth login "
            "(also checks Pi/OpenCode minimax keys and ~/.minimax)"
        )
    return k


class MinimaxStt:
    name = "minimax"

    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        raise ProviderUnsupported(
            "minimax STT unsupported (no stable public ASR in v1); use xai|openai|google"
        )


class MinimaxTts:
    name = "minimax"

    @provider_operation("MiniMax TTS")
    def synthesize(self, text: str, *, voice: str | None = None) -> SynthResult:
        key = _key()
        group = os.environ.get("MINIMAX_GROUP_ID", "")
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if group:
            headers["GroupId"] = group
        body = {
            "model": os.environ.get("MINIMAX_TTS_MODEL", "speech-2.6-hd"),
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice or os.environ.get("MINIMAX_VOICE", "English_expressive_narrator"),
                "speed": 1.0,
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }
        with httpx.Client(timeout=120.0) as client:
            r = client.post(T2A_URL, headers=headers, json=body)
            if r.status_code >= 400:
                # try alternate host
                r = client.post(
                    "https://api-uw.minimax.io/v1/t2a_v2",
                    headers=headers,
                    json=body,
                )
            if r.status_code >= 400:
                raise ProviderError(f"MiniMax TTS HTTP {r.status_code}: {r.text[:300]}")
            payload = r.json()
        # Response shapes vary: data.audio base64 hex, etc.
        data = payload.get("data") or payload
        audio_hex = None
        if isinstance(data, dict):
            audio_hex = data.get("audio") or data.get("audio_hex")
        if not audio_hex and isinstance(payload.get("audio"), str):
            audio_hex = payload["audio"]
        if not audio_hex:
            raise ProviderError(f"MiniMax TTS missing audio field: {list(payload)[:12]}")
        try:
            audio = bytes.fromhex(audio_hex)
        except ValueError:
            import base64

            audio = base64.b64decode(audio_hex)
        return SynthResult(
            audio=audio,
            provider=self.name,
            content_type="audio/mpeg",
            voice=voice,
        )

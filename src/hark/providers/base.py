"""Provider interfaces."""

from __future__ import annotations

import binascii
import json
from dataclasses import dataclass
from functools import wraps
from typing import Protocol

import httpx


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, code: int = 4) -> None:
        super().__init__(message)
        self.code = code


class ProviderUnsupported(ProviderError):
    pass


def provider_operation(label: str):
    """Convert provider transport and response decoding faults to ProviderError."""

    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ProviderError:
                raise
            except (
                httpx.HTTPError,
                json.JSONDecodeError,
                binascii.Error,
                UnicodeDecodeError,
                KeyError,
                IndexError,
                TypeError,
                ValueError,
            ) as exc:
                raise ProviderError(f"{label} failed: {exc}") from exc

        return wrapped

    return decorate


@dataclass
class Transcript:
    text: str
    provider: str
    duration_ms: int = 0
    confidence: float | None = None


@dataclass
class SynthResult:
    audio: bytes  # wav or provider format
    provider: str
    content_type: str = "audio/wav"
    voice: str | None = None


class SttProvider(Protocol):
    name: str

    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        ...


class TtsProvider(Protocol):
    name: str

    def synthesize(self, text: str, *, voice: str | None = None) -> SynthResult:
        ...

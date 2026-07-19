"""Provider interfaces."""

from __future__ import annotations

import binascii
import json
from dataclasses import dataclass
from functools import wraps
from typing import Any, Protocol

import httpx

from hark.exitcodes import PROVIDER, normalize_failure_exit

_FAILURE_DETAIL_FALLBACK = "provider failure"
_FAILURE_DETAIL_LIMIT = 400


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, code: int = 4) -> None:
        super().__init__(message)
        self.code = code


class ProviderUnsupported(ProviderError):
    pass


@dataclass(frozen=True)
class ProviderFailureInfo:
    """Safe, bounded values extracted once from an untrusted provider error."""

    detail: str
    code: int
    tts_info: Any


def safe_exception_detail(
    exc: BaseException,
    *,
    fallback: str = _FAILURE_DETAIL_FALLBACK,
    limit: int = _FAILURE_DETAIL_LIMIT,
) -> str:
    """Render an exception once, containing hostile coercion and controls."""
    try:
        rendered = str(exc)
        if not isinstance(rendered, str):
            return fallback
    except BaseException:
        return fallback
    cleaned = "".join(
        character if character.isprintable() else " " for character in rendered
    ).strip()
    if not cleaned:
        return fallback
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 3].rstrip() + "..."
    return cleaned


def safe_provider_failure(exc: BaseException) -> ProviderFailureInfo:
    """Extract failure fields without trusting exception coercion/descriptors.

    Each hostile surface is evaluated at most once and independently. Even
    ``BaseException`` subclasses raised by ``__str__`` or a descriptor are
    contained so failure reporting cannot replace the primary provider error.
    """
    detail = safe_exception_detail(exc)
    try:
        raw_code = exc.code  # type: ignore[attr-defined]
    except BaseException:
        raw_code = None
    code = normalize_failure_exit(raw_code, fallback=PROVIDER)
    try:
        tts_info = exc.tts_info  # type: ignore[attr-defined]
    except BaseException:
        tts_info = None
    return ProviderFailureInfo(detail=detail, code=code, tts_info=tts_info)


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

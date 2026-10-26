"""Auto-only auth handover; no API calls or audio playback."""

import sys
from types import SimpleNamespace

import httpx
import pytest

from hark.config import SttConfig
from hark.providers import resolve
from hark.providers.auth_fallback import is_auth_failure
from hark.providers.base import ProviderError, ProviderUnsupported, Transcript
from hark.providers.tts_fallback import synthesize_with_fallback


@pytest.mark.parametrize(
    "message",
    [
        "xAI TTS 401 — run grok login",
        "Gemini STT HTTP 403: denied",
        "OpenAI TTS HTTP 401: scope missing",
        "xAI auth missing — run grok login",
    ],
)
def test_auth_rejection_is_recognized(message):
    assert is_auth_failure(ProviderError(message))


@pytest.mark.parametrize(
    "exc",
    [
        ProviderError("xAI TTS HTTP 429: quota exhausted; prior 401"),
        ProviderError("Gemini TTS HTTP 400: request text mentions 403"),
        RuntimeError("xAI TTS HTTP 401: denied"),
        ProviderUnsupported("xAI STT HTTP 403: unavailable"),
        KeyboardInterrupt(),
    ],
)
def test_other_failures_are_not_auth_rejections(exc):
    assert not is_auth_failure(exc)


def test_tts_auto_auth_skips_retry_and_reports_handover():
    calls = []

    def call(provider, text):
        calls.append((provider, text))
        if provider == "xai":
            raise ProviderError("xAI TTS HTTP 403: revoked")
        return "spoken"

    result, steps = synthesize_with_fallback(
        provider="xai",
        text="hello",
        call=call,
        chain=lambda: ["openai"],
        auto_auth=True,
    )
    assert result == "spoken"
    assert calls == [("xai", "hello"), ("openai", "hello")]
    assert [(s.from_provider, s.to_provider, s.error_class, s.step) for s in steps] == [
        ("xai", "openai", "authentication", "fallback"),
    ]


@pytest.mark.parametrize(
    "auto_auth,enabled,status",
    [
        (False, True, 403),
        (True, False, 403),
        (True, True, 429),
    ],
)
def test_tts_explicit_pin_disabled_fallback_and_quota_fail_fast(
    auto_auth, enabled, status
):
    calls = []
    error = ProviderError(f"xAI TTS HTTP {status}: rejected")

    def call(provider, text):
        calls.append(provider)
        raise error

    with pytest.raises(ProviderError) as caught:
        synthesize_with_fallback(
            provider="xai",
            text="hello",
            call=call,
            chain=lambda: pytest.fail("must not probe chain"),
            auto_auth=auto_auth,
            enabled=enabled,
        )
    assert caught.value is error
    assert calls == ["xai"]


def test_tts_exhausted_auto_auth_reports_first_error():
    errors = {
        p: ProviderError(f"{p} TTS HTTP 401: rejected") for p in ("xai", "openai")
    }
    calls = []

    def call(provider, text):
        calls.append(provider)
        raise errors[provider]

    with pytest.raises(ProviderError) as caught:
        synthesize_with_fallback(
            provider="xai",
            text="hello",
            call=call,
            chain=lambda: ["openai"],
            auto_auth=True,
        )
    assert caught.value is errors["xai"]
    assert calls == ["xai", "openai"]


def _stt_providers(monkeypatch, *, first_status=403):
    calls = []

    class First:
        name = "xai"

        def transcribe(self, wav_bytes, *, language=None):
            calls.append((self.name, wav_bytes, language))
            raise ProviderError(f"xAI STT HTTP {first_status}: denied")

    class Second:
        name = "openai"

        def transcribe(self, wav_bytes, *, language=None):
            calls.append((self.name, wav_bytes, language))
            return Transcript(text="hello", provider=self.name)

    monkeypatch.setattr(resolve, "XaiStt", First)
    monkeypatch.setattr(resolve, "OpenAIStt", Second)
    monkeypatch.setattr(resolve, "xai_auth", lambda: SimpleNamespace(available=True))
    monkeypatch.setattr(resolve, "openai_auth", lambda: SimpleNamespace(available=True))
    monkeypatch.setattr(
        resolve, "google_auth", lambda: SimpleNamespace(available=False)
    )
    return calls


def test_stt_auto_reuses_audio_on_next_provider_and_keeps_healthy_provider(monkeypatch):
    calls = _stt_providers(monkeypatch)
    events = []
    monkeypatch.setattr("hark.syslog.log", lambda *a, **kw: events.append((a, kw)))
    provider = resolve.resolve_stt("auto", stt_cfg=SttConfig())
    assert provider.transcribe(b"wav", language="fr").provider == "openai"
    assert provider.transcribe(b"next", language="en").text == "hello"
    assert calls == [
        ("xai", b"wav", "fr"),
        ("openai", b"wav", "fr"),
        ("openai", b"next", "en"),
    ]
    assert provider.name == "openai"
    assert events[0][0] == ("stt.provider_fallback",)
    assert events[0][1]["error_class"] == "authentication"


def test_stt_handover_logging_cannot_replace_auth_failure_with_hostile_render(
    monkeypatch,
):
    _stt_providers(monkeypatch)
    events = []

    class Hostile(ProviderError):
        renders = 0

        def __str__(self):
            self.renders += 1
            if self.renders > 1:
                raise RuntimeError("render failed after classification")
            return "xAI STT HTTP 403: denied"

    error = Hostile("denied")

    def reject(*a, **kw):
        raise error

    monkeypatch.setattr(
        resolve, "XaiStt", lambda: SimpleNamespace(name="xai", transcribe=reject)
    )
    monkeypatch.setattr("hark.syslog.log", lambda *a, **kw: events.append(kw))
    provider = resolve.resolve_stt("auto", stt_cfg=SttConfig())
    assert provider.transcribe(b"wav").provider == "openai"
    assert events[0]["error"] == "provider failure"


@pytest.mark.parametrize("pin,status", [("xai", 403), ("auto", 429)])
def test_stt_explicit_and_quota_do_not_switch(monkeypatch, pin, status):
    calls = _stt_providers(monkeypatch, first_status=status)
    provider = resolve.resolve_stt(pin, stt_cfg=SttConfig())
    with pytest.raises(ProviderError):
        provider.transcribe(b"wav")
    assert [c[0] for c in calls] == ["xai"]


def test_stt_auto_honours_disabled_and_exhaustion(monkeypatch):
    calls = _stt_providers(monkeypatch)
    provider = resolve.resolve_stt("auto", stt_cfg=SttConfig(disabled=["openai"]))
    with pytest.raises(ProviderError, match="xAI STT HTTP 403"):
        provider.transcribe(b"wav")
    assert [c[0] for c in calls] == ["xai"]


def test_real_openai_auto_auth_does_not_retry_model_with_rejected_key(monkeypatch):
    from hark.providers import openai_p

    calls = []
    real_client = httpx.Client

    def respond(request):
        calls.append(request)
        return httpx.Response(401, json={"error": "missing_scope"})

    monkeypatch.setattr(openai_p, "resolve_openai_api_key", lambda: "test-key")
    monkeypatch.setattr(
        openai_p.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw),
    )
    monkeypatch.setattr(resolve, "xai_auth", lambda: SimpleNamespace(available=False))
    monkeypatch.setattr(resolve, "openai_auth", lambda: SimpleNamespace(available=True))
    monkeypatch.setattr(
        resolve, "google_auth", lambda: SimpleNamespace(available=False)
    )
    monkeypatch.delenv("OPENAI_STT_MODEL", raising=False)
    provider = resolve.resolve_stt("auto", stt_cfg=SttConfig())
    with pytest.raises(ProviderError, match="HTTP 401"):
        provider.transcribe(b"wav")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "message,auto_auth",
    [
        ("xAI TTS HTTP 403: revoked", True),
        ("Gemini TTS missing audio data", False),
    ],
)
def test_subprocess_transport_fallback_can_reuse_pool_authority(message, auto_auth):
    from hark.speech import _InterruptibleSynthPool
    from hark.tts_isolation import SubprocessSynthTransport, SynthRequest

    owner = _InterruptibleSynthPool()
    calls = []

    def call(provider, text):
        calls.append(provider)
        if provider == "xai":
            result = {
                "status": "error",
                "kind": "provider",
                "message": message,
                "code": 4,
            }
        else:
            result = {
                "status": "ok",
                "audio_size": 0,
                "provider": provider,
                "content_type": "audio/wav",
                "voice": "v",
            }
        source = (
            "import os,json,struct,sys; "
            "n=struct.unpack('!I',sys.stdin.buffer.read(4))[0]; sys.stdin.buffer.read(n); "
            f"m=json.dumps({result!r}).encode(); "
            "fd=int(os.environ['HARK_TTS_RESULT_FD']); "
            "os.write(fd,struct.pack('!I',len(m))+m); os.close(fd)"
        )
        return SubprocessSynthTransport(
            owner,
            command_factory=lambda: [sys.executable, "-c", source],
            timeout_s=10,
        ).synthesize(SynthRequest(provider, "v", "en", text))

    try:
        result, steps = synthesize_with_fallback(
            provider="xai",
            text="hello",
            call=call,
            chain=lambda: ["openai"],
            auto_auth=auto_auth,
        )
        assert result.provider == "openai"
        assert calls == (["xai", "openai"] if auto_auth else ["xai", "xai", "openai"])
        assert steps[-1].step == "fallback"
        assert not owner._process_lifecycle.active
    finally:
        owner._process_lifecycle.cancel()
        owner._pool.shutdown(wait=False, cancel_futures=True)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux cleanup supervisor")
def test_actual_cleanup_supervisor_provider_error_leaves_owner_reusable():
    from hark.speech import _InterruptibleSynthPool
    from hark.tts_isolation import SubprocessSynthTransport, SynthRequest

    owner = _InterruptibleSynthPool()
    modes = iter(["--test-provider-error", "--test-success"])
    transport = SubprocessSynthTransport(
        owner,
        command_factory=lambda: [sys.executable, "-m", "hark.tts_worker", next(modes)],
    )
    try:
        with pytest.raises(ProviderError, match="worker provider failed"):
            transport.synthesize(SynthRequest("xai", "v", "en", "hello"))
        assert not owner._process_lifecycle.active
        result = transport.synthesize(SynthRequest("openai", "v", "en", "hello"))
        assert result.provider == "test-worker"
    finally:
        owner._process_lifecycle.cancel()
        owner._pool.shutdown(wait=False, cancel_futures=True)

"""B121/B122: streaming ambient is conversation mode (no re-wake between turns)."""

from __future__ import annotations

import io
import json

import pytest

import hark.ambient as ambient
from hark.ambient import AmbientResult, complete_after_wake
from hark.answer_window.result import ListenResult
from hark.config import AmbientConfig, HarkConfig, ListenConfig
from hark.providers.base import ProviderError
from hark.wake import WakeHit


class _RaisingStrRuntimeError(RuntimeError):
    def __str__(self) -> str:
        raise RuntimeError("exception rendering failed")


class _NonStringStrRuntimeError(RuntimeError):
    def __str__(self):
        return 123


class _SurrogateNameMeta(type):
    def __getattribute__(cls, name):
        if name == "__name__":
            return "Surrogate\ud800RuntimeError"
        return super().__getattribute__(name)


class _SurrogateNamedRuntimeError(RuntimeError, metaclass=_SurrogateNameMeta):
    pass


class _NoSpeechNameMeta(type):
    def __getattribute__(cls, name):
        if name == "__name__":
            return "no speech detected"
        return super().__getattribute__(name)


class _HostileNoSpeechNamedTimeoutError(TimeoutError, metaclass=_NoSpeechNameMeta):
    def __str__(self) -> str:
        raise RuntimeError("exception rendering failed")


def _hit() -> WakeHit:
    return WakeHit(
        phrase="hey iris",
        remainder="",
        raw="hey iris",
        backend="text_probe",
    )


def test_streaming_conversation_idle_config_default():
    assert AmbientConfig().streaming_conversation_idle_s == 45.0
    assert HarkConfig().ambient.streaming_conversation_idle_s == 45.0


def test_streaming_conversation_no_rewake_between_turns(monkeypatch):
    """B122: one wake → multiple silence listens without re-arming wake."""
    cfg = HarkConfig(
        ambient=AmbientConfig(
            streaming=True,
            streaming_ack_min_quiet_s=2.0,
            streaming_conversation_idle_s=30.0,
            post_wake_arm_cue=True,
        ),
        listen=ListenConfig(end_mode="radio"),  # conversation forces silence turns
    )
    texts = ["status of deploy", "and the logs", ""]
    listen_calls: list[dict] = []
    idx = {"n": 0}

    def fake_listen(cfg_arg, **kwargs):
        pol = kwargs.get("policy")
        listen_calls.append(
            {
                "policy": pol,
                "end_mode": getattr(pol, "end_mode", None)
                if pol
                else kwargs.get("end_mode"),
                "streaming": getattr(pol, "streaming", None)
                if pol
                else kwargs.get("streaming"),
                "profile": getattr(pol, "profile", None)
                if pol
                else kwargs.get("profile"),
                "initial_timeout_s": (
                    getattr(pol, "initial_timeout_s", None) if pol else None
                ),
            }
        )
        n = idx["n"]
        idx["n"] += 1
        if n < 2:
            return ListenResult(
                text=texts[n],
                provider="fake",
                duration_ms=50 + n,
                end_mode="silence",
                end_phrase="turn_quiet",
                stream_id=f"sturn{n}",
                partials_emitted=0,
            )
        # Third open: no speech → conversation idle end
        raise TimeoutError("no speech detected within timeout")

    out = io.StringIO()
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)

    assert len(listen_calls) == 3
    for c in listen_calls:
        assert c["policy"] is not None
        assert (
            str(
                c["end_mode"].value
                if hasattr(c["end_mode"], "value")
                else c["end_mode"]
            )
            == "silence"
        )
        assert c["streaming"] is True
        assert c["profile"] == "post_wake"
    # Later turns use conversation idle as gate timeout
    assert listen_calls[1]["initial_timeout_s"] == 30.0
    assert listen_calls[2]["initial_timeout_s"] == 30.0

    assert result.skip_emit is True
    assert result.kind == "ambient.conversation_end"
    assert result.conversation_id
    assert result.turn == 2
    assert result.listen and result.listen.get("reason") == "conversation_idle"

    # Parse dual-written HEP lines from out
    import json

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    kinds = [e["kind"] for e in events]
    assert kinds.count("ambient.turn") == 2
    assert "ambient.conversation_end" in kinds
    assert "ambient.prompt" not in kinds  # idle end does not re-emit as prompt
    turns = [e for e in events if e["kind"] == "ambient.turn"]
    assert turns[0]["text"] == "status of deploy"
    assert turns[1]["text"] == "and the logs"
    assert turns[0]["turn"] == 1
    assert turns[1]["turn"] == 2
    assert turns[0]["conversation_id"] == turns[1]["conversation_id"]
    assert turns[0]["streaming"] is True
    assert turns[0]["final"] is False
    assert "full" in (turns[0].get("instructions") or "").lower() or "TTS" in (
        turns[0].get("instructions") or ""
    )


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (ProviderError("provider unavailable"), "listen_provider_error"),
        (ambient.MicBusyError("mic busy (another listener)"), "listen_mic_busy"),
        (RuntimeError("internal listener exploded: " + "x" * 400), "listen_failed"),
        (RuntimeError(), "listen_failed"),
    ],
)
def test_streaming_later_listen_failure_is_not_idle(monkeypatch, failure, reason):
    """B132: non-timeout failures remain failures after a successful turn."""
    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True, streaming_conversation_idle_s=30.0),
        listen=ListenConfig(end_mode="silence"),
    )
    calls = {"n": 0}
    logs: list[tuple[str, dict]] = []

    def fake_listen(cfg_arg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ListenResult(
                text="successful turn",
                provider="fake-provider",
                duration_ms=73,
                end_mode="silence",
                end_phrase="turn_quiet",
                stream_id="last-success-stream",
                partials_emitted=2,
            )
        raise failure

    out = io.StringIO()
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(
        ambient, "syslog", lambda kind, **fields: logs.append((kind, fields))
    )
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)
    detail = (str(failure).strip() or type(failure).__name__)[:240]

    assert result.kind == "ambient.conversation_end"
    assert result.text == "successful turn"
    assert result.stream_id == "last-success-stream"
    assert result.turn == 1
    assert result.partials_emitted == 2
    assert result.listen
    assert result.listen["provider"] == "fake-provider"
    assert result.listen["duration_ms"] == 73
    assert result.listen["reason"] == reason
    assert result.listen["error"] == detail
    assert len(result.listen["error"]) <= 240

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    end = next(event for event in events if event["kind"] == "ambient.conversation_end")
    assert end["reason"] == reason
    assert end["reason"] != "conversation_idle"
    assert end["listen_error"] == detail
    assert len(end["listen_error"]) <= 240
    assert end["last_stream_id"] == "last-success-stream"
    assert end["last_turn"] == 1
    assert end["last_event_id"] == result.event_id
    assert end["last_text"] == "successful turn"
    assert end["last_provider"] == "fake-provider"
    assert "failure" in end["instructions"]

    error_logs = [fields for kind, fields in logs if kind == "ambient.error"]
    assert len(error_logs) == 1
    assert error_logs[0]["reason"] == reason
    assert error_logs[0]["listen_error"] == detail
    assert error_logs[0]["error_type"] == type(failure).__name__
    assert error_logs[0]["last_event_id"] == result.event_id
    assert error_logs[0]["last_stream_id"] == "last-success-stream"
    assert error_logs[0]["last_turn"] == 1
    assert error_logs[0]["last_text"] == "successful turn"
    assert error_logs[0]["last_provider"] == "fake-provider"


@pytest.mark.parametrize(
    "failure",
    [_RaisingStrRuntimeError(), _NonStringStrRuntimeError()],
)
def test_streaming_later_hostile_exception_rendering_is_contained(monkeypatch, failure):
    """Failure reporting cannot itself escape or duplicate an unbounded transcript."""
    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True, streaming_conversation_idle_s=30.0),
        listen=ListenConfig(end_mode="silence"),
    )
    calls = {"n": 0}
    logs: list[tuple[str, dict]] = []
    long_text = "last successful transcript " + "x" * 400
    long_provider = "provider-" + "y" * 200

    def fake_listen(cfg_arg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ListenResult(
                text=long_text,
                provider=long_provider,
                duration_ms=73,
                end_mode="silence",
                stream_id="last-success-stream",
            )
        raise failure

    out = io.StringIO()
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(
        ambient, "syslog", lambda kind, **fields: logs.append((kind, fields))
    )
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)
    error_type = type(failure).__name__

    assert result.kind == "ambient.conversation_end"
    assert result.text == long_text
    assert result.listen
    assert result.listen["reason"] == "listen_failed"
    assert result.listen["error"] == error_type
    assert result.listen["error_type"] == error_type

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    end = next(event for event in events if event["kind"] == "ambient.conversation_end")
    assert end["listen_error"] == error_type
    assert end["error_type"] == error_type
    assert end["last_event_id"] == result.event_id
    assert end["last_stream_id"] == "last-success-stream"
    assert end["last_turn"] == 1
    assert end["last_text"] == long_text[:240]
    assert end["last_provider"] == long_provider[:120]

    error_log = next(fields for kind, fields in logs if kind == "ambient.error")
    assert error_log["listen_error"] == error_type
    assert error_log["last_event_id"] == result.event_id
    assert error_log["last_stream_id"] == "last-success-stream"
    assert error_log["last_turn"] == 1
    assert error_log["last_text"] == long_text[:240]
    assert error_log["last_provider"] == long_provider[:120]


def test_streaming_failure_bounds_exact_10k_stream_id_but_preserves_result(
    monkeypatch,
):
    """Diagnostic HEP stays below 10 KiB without truncating the in-memory result."""
    cfg = HarkConfig(ambient=AmbientConfig(streaming=True))
    calls = {"n": 0}
    logs: list[tuple[str, dict]] = []
    long_stream_id = "s" * 10_000
    assert len(long_stream_id) == 10_000

    def fake_listen(cfg_arg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ListenResult(
                text="successful turn",
                provider="fake-provider",
                duration_ms=5,
                end_mode="silence",
                stream_id=long_stream_id,
            )
        raise RuntimeError("later listen failed")

    out = io.StringIO()
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(
        ambient, "syslog", lambda kind, **fields: logs.append((kind, fields))
    )
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)

    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    assert lines
    assert all(len(line.encode("utf-8")) < 10 * 1024 for line in lines)
    events = [json.loads(line) for line in lines]
    turn = next(event for event in events if event["kind"] == "ambient.turn")
    end = next(event for event in events if event["kind"] == "ambient.conversation_end")
    assert turn["stream_id"] == long_stream_id[:160]
    assert end["last_stream_id"] == long_stream_id[:160]
    error_log = next(fields for kind, fields in logs if kind == "ambient.error")
    assert error_log["last_stream_id"] == long_stream_id[:160]

    assert result.stream_id == long_stream_id
    assert result.text == "successful turn"
    assert result.listen and result.listen["provider"] == "fake-provider"


def test_streaming_failure_sanitizes_lone_surrogates_for_strict_utf8(monkeypatch):
    """Exception and last-turn diagnostics always encode as strict UTF-8."""
    cfg = HarkConfig(ambient=AmbientConfig(streaming=True))
    calls = {"n": 0}
    logs: list[tuple[str, dict]] = []
    last_text = "successful\ud800turn"
    last_provider = "provider\udfffname"
    last_stream_id = "stream\ud800id"

    def fake_listen(cfg_arg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ListenResult(
                text=last_text,
                provider=last_provider,
                duration_ms=5,
                end_mode="silence",
                stream_id=last_stream_id,
            )
        raise _SurrogateNamedRuntimeError("detail\udfffbroken")

    raw = io.BytesIO()
    out = io.TextIOWrapper(
        raw,
        encoding="utf-8",
        errors="strict",
        write_through=True,
    )
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(
        ambient, "syslog", lambda kind, **fields: logs.append((kind, fields))
    )
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)
    payload = raw.getvalue().decode("utf-8", errors="strict")
    events = [json.loads(line) for line in payload.splitlines() if line.strip()]
    turn = next(event for event in events if event["kind"] == "ambient.turn")
    end = next(event for event in events if event["kind"] == "ambient.conversation_end")

    replacement = "\N{REPLACEMENT CHARACTER}"
    assert turn["stream_id"] == f"stream{replacement}id"
    assert turn["text"] == f"successful{replacement}turn"
    assert turn["provider"] == f"provider{replacement}name"
    assert end["listen_error"] == f"detail{replacement}broken"
    assert end["error_type"] == f"Surrogate{replacement}RuntimeError"
    assert end["last_stream_id"] == f"stream{replacement}id"
    assert end["last_text"] == f"successful{replacement}turn"
    assert end["last_provider"] == f"provider{replacement}name"
    error_log = next(fields for kind, fields in logs if kind == "ambient.error")
    assert error_log["error_type"] == f"Surrogate{replacement}RuntimeError"

    def assert_strict_utf8(value):
        if isinstance(value, str):
            value.encode("utf-8", errors="strict")
        elif isinstance(value, dict):
            for key, item in value.items():
                assert_strict_utf8(key)
                assert_strict_utf8(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                assert_strict_utf8(item)

    assert_strict_utf8(events)
    assert_strict_utf8(logs)
    assert result.text == last_text
    assert result.stream_id == last_stream_id
    assert result.listen and result.listen["provider"] == last_provider


def test_streaming_later_runtime_with_no_speech_text_is_not_idle(monkeypatch):
    """Idle classification requires TimeoutError, not a matching message alone."""
    cfg = HarkConfig(ambient=AmbientConfig(streaming=True))
    calls = {"n": 0}

    def fake_listen(cfg_arg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ListenResult(
                text="first turn",
                provider="fake",
                duration_ms=5,
                end_mode="silence",
                stream_id="turn-1",
            )
        raise RuntimeError("no speech detected because decoder crashed")

    out = io.StringIO()
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)

    assert result.listen and result.listen["reason"] == "listen_failed"
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    end = next(event for event in events if event["kind"] == "ambient.conversation_end")
    assert end["reason"] == "listen_failed"


def test_streaming_hostile_type_fallback_is_not_idle_evidence(monkeypatch):
    """A hostile type-name fallback cannot impersonate a rendered timeout."""
    cfg = HarkConfig(ambient=AmbientConfig(streaming=True))
    calls = {"n": 0}

    def fake_listen(cfg_arg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ListenResult(
                text="first turn",
                provider="fake",
                duration_ms=5,
                end_mode="silence",
                stream_id="turn-1",
            )
        raise _HostileNoSpeechNamedTimeoutError()

    out = io.StringIO()
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)

    assert result.listen and result.listen["reason"] == "listen_failed"
    assert result.listen["error"] == "no speech detected"
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    end = next(event for event in events if event["kind"] == "ambient.conversation_end")
    assert end["reason"] == "listen_failed"
    assert end["listen_error"] == "no speech detected"
    assert end["error_type"] == "no speech detected"


def test_streaming_later_timeout_classifies_marker_after_bounded_detail(monkeypatch):
    """Idle classification uses the full safe error text, not its event excerpt."""
    cfg = HarkConfig(ambient=AmbientConfig(streaming=True))
    calls = {"n": 0}
    failure = TimeoutError("x" * 300 + " no speech detected")

    def fake_listen(cfg_arg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return ListenResult(
                text="first turn",
                provider="fake",
                duration_ms=5,
                end_mode="silence",
                stream_id="turn-1",
            )
        raise failure

    out = io.StringIO()
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)

    assert result.listen and result.listen["reason"] == "conversation_idle"
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    end = next(event for event in events if event["kind"] == "ambient.conversation_end")
    assert end["reason"] == "conversation_idle"


def test_streaming_first_timeout_classifies_marker_after_bounded_detail(monkeypatch):
    """First-turn no_open compatibility also uses the full safe error text."""
    cfg = HarkConfig(ambient=AmbientConfig(streaming=True))
    failure = TimeoutError("x" * 300 + " no speech captured")
    monkeypatch.setattr(
        ambient,
        "run_listen",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False)

    assert result.listen and result.listen["reason"] == "no_open"
    assert result.listen["error"] == "x" * 240


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (TimeoutError("no speech detected within timeout"), "no_open"),
        (ProviderError("provider unavailable"), "listen_failed"),
    ],
)
def test_streaming_first_turn_failure_reason_compatibility(
    monkeypatch, failure, reason
):
    """B132 does not change the established first-turn failure contract."""
    cfg = HarkConfig(ambient=AmbientConfig(streaming=True))
    logs: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ambient,
        "run_listen",
        lambda *args, **kwargs: (_ for _ in ()).throw(failure),
    )
    monkeypatch.setattr(
        ambient, "syslog", lambda kind, **fields: logs.append((kind, fields))
    )
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False)

    assert result.kind is None
    assert result.skip_emit is False
    assert result.turn is None
    assert result.listen and result.listen["reason"] == reason
    assert result.listen["error"] == str(failure)
    error = next(fields for kind, fields in logs if kind == "ambient.error")
    assert error["reason"] == reason


def test_streaming_false_single_listen_uses_config_end_mode(monkeypatch):
    """Classic path: one listen then return (outer loop re-arms wake)."""
    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=False),
        listen=ListenConfig(end_mode="silence"),
    )
    calls: list[dict] = []

    def fake_listen(cfg_arg, **kwargs):
        calls.append(kwargs)
        return ListenResult(
            text="one shot",
            provider="fake",
            duration_ms=10,
            end_mode="silence",
            stream_id="s1",
        )

    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)

    result = complete_after_wake(cfg, _hit(), announce=False)
    assert len(calls) == 1
    assert calls[0].get("profile") == "post_wake"
    assert calls[0].get("end_mode") == "silence"
    assert result.text == "one shot"
    assert result.skip_emit is False
    assert result.kind is None


def test_streaming_end_phrase_finalizes_conversation(monkeypatch):
    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True, streaming_conversation_idle_s=40.0),
        listen=ListenConfig(end_mode="silence"),
    )
    calls = {"n": 0}

    def fake_listen(cfg_arg, **kwargs):
        calls["n"] += 1
        return ListenResult(
            text="ship it okay hark send",
            provider="fake",
            duration_ms=20,
            end_mode="silence",
            stream_id="sfin",
        )

    out = io.StringIO()
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)
    assert calls["n"] == 1  # session ends after product end phrase
    assert result.kind == "ambient.prompt"
    assert result.skip_emit is True
    assert "ship it" in (result.text or "")
    import json

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert any(e["kind"] == "ambient.prompt" and e.get("final") is True for e in events)
    assert not any(e["kind"] == "ambient.turn" for e in events)


def test_streaming_cancel_phrase_ends_conversation(monkeypatch):
    cfg = HarkConfig(ambient=AmbientConfig(streaming=True))
    monkeypatch.setattr(
        ambient,
        "run_listen",
        lambda *a, **k: ListenResult(
            text="never mind hark cancel",
            provider="fake",
            duration_ms=5,
            end_mode="silence",
            stream_id="sc",
        ),
    )
    out = io.StringIO()
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)

    result = complete_after_wake(cfg, _hit(), announce=False, out=out)
    assert result.kind == "ambient.cancelled"
    assert result.skip_emit is True
    import json

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert any(e["kind"] == "ambient.cancelled" for e in events)


def test_bound_answer_still_ignores_ambient_streaming():
    """P1.M6 invariant: bound windows must not inherit conversation streaming."""
    from hark.answer_window import ListenSessionPolicy

    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True, streaming_conversation_idle_s=12.0),
        listen=ListenConfig(end_mode="radio"),
    )
    bound = ListenSessionPolicy.from_config(cfg, "bound_answer")
    assert bound.streaming is False
    post = ListenSessionPolicy.from_config(cfg, "post_wake")
    assert post.streaming is True


def test_run_ambient_streaming_does_not_call_wake_between_turns(monkeypatch):
    """run_ambient: one wake wait, multi-turn conversation, then return (loop re-arms)."""
    cfg = HarkConfig(
        ambient=AmbientConfig(streaming=True, enabled=True, engine="text_probe")
    )
    wake_calls = {"n": 0}
    listen_n = {"n": 0}

    def fake_wait(*a, **k):
        wake_calls["n"] += 1
        return _hit()

    def fake_listen(cfg_arg, **kwargs):
        listen_n["n"] += 1
        if listen_n["n"] == 1:
            return ListenResult(
                text="first turn",
                provider="fake",
                duration_ms=10,
                end_mode="silence",
                stream_id="a1",
            )
        raise TimeoutError("no speech detected within timeout")

    class Backend:
        name = "text_probe"

        def score_snippet(self, *a, **k):
            return None

    out = io.StringIO()
    monkeypatch.setattr(ambient, "_wait_for_wake", fake_wait)
    monkeypatch.setattr(ambient, "run_listen", fake_listen)
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "shutdown_requested", lambda: False)
    monkeypatch.setattr(ambient, "reload_requested", lambda: False)
    monkeypatch.setattr(ambient, "build_wake_backend", lambda *a, **k: Backend())

    result = ambient.run_ambient(cfg, once=True, timeout_s=5, announce=False, out=out)
    assert wake_calls["n"] == 1
    assert listen_n["n"] == 2
    assert result.activated is True
    assert result.skip_emit is True
    import json

    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert any(e["kind"] == "ambient.turn" for e in events)


def test_loop_skips_emit_when_conversation_already_wrote(monkeypatch):
    """Outer ambient loop must not re-emit ambient.prompt after conversation path."""
    import json

    import hark.lifecycle as lc
    from hark.lifecycle import clear_reload_request, request_shutdown

    cfg = HarkConfig()
    cfg.ambient.enabled = True
    cfg.ambient.engine = "text_probe"
    cfg.ambient.timeout_s = 0.05
    cfg.ambient.surface_timeouts = False
    cfg.ambient.streaming = True

    lc._shutdown = False
    clear_reload_request()
    calls = {"n": 0}

    def fake_run_ambient(cfg, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Simulate conversation path that dual-wrote turns already
            out = kwargs.get("out")
            if out is not None:
                ambient.emit_hep(
                    {
                        "schema": "hark.event.v1",
                        "kind": "ambient.turn",
                        "event_id": "t1",
                        "text": "hello",
                        "conversation_id": "c1",
                        "turn": 1,
                        "streaming": True,
                        "final": False,
                        "partial": False,
                    },
                    out,
                )
            return AmbientResult(
                activated=True,
                phrase="hey iris",
                text="hello",
                skip_emit=True,
                kind="ambient.conversation_end",
                conversation_id="c1",
                turn=1,
                event_id="e1",
                stream_id="s1",
            )
        request_shutdown(reason="stop")
        return AmbientResult(activated=False, phrase=None, text=None)

    class Backend:
        def score_snippet(self, *a, **k):
            return None

    monkeypatch.setattr(ambient, "run_ambient", fake_run_ambient)
    monkeypatch.setattr(ambient, "syslog", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "run_tts", lambda *a, **k: None)
    monkeypatch.setattr(ambient, "build_wake_backend", lambda *a, **k: Backend())
    monkeypatch.setattr(ambient, "install_signal_handlers", lambda: None)

    out = io.StringIO()
    rc = ambient.run_ambient_loop(cfg, out=out, announce=False, idle_log_s=999)
    assert rc == 0
    kinds = [
        json.loads(line).get("kind")
        for line in out.getvalue().splitlines()
        if line.strip()
    ]
    # conversation dual-wrote turn; loop skip_emit must not add ambient.prompt
    assert "ambient.turn" in kinds
    assert "ambient.prompt" not in kinds

    lc._shutdown = False
    clear_reload_request()


def test_present_ambient_turn_compact():
    from hark.state_feed.present import present_for_monitor

    compact = present_for_monitor(
        {
            "schema": "hark.event.v1",
            "kind": "ambient.turn",
            "event_id": "e1",
            "observed_at": "2026-01-01T00:00:00Z",
            "text": "hello there",
            "stream_id": "s1",
            "conversation_id": "c1",
            "turn": 2,
            "streaming": True,
            "final": False,
            "partial": False,
            "ack_min_quiet_s": 2.0,
        }
    )
    assert compact["kind"] == "ambient.turn"
    assert compact["turn"] == 2
    assert compact["conversation_id"] == "c1"
    assert compact["streaming"] is True
    assert compact["final"] is False
    assert (
        "TTS" in compact["instructions"] or "reply" in compact["instructions"].lower()
    )


def test_present_conversation_end_preserves_bounded_failure_diagnostics():
    from hark.state_feed.present import present_for_monitor

    event = {
        "schema": "hark.event.v1",
        "kind": "ambient.conversation_end",
        "event_id": "end-1",
        "observed_at": "2026-01-01T00:00:00Z",
        "conversation_id": "conversation-1",
        "turns": 8,
        "reason": "listen_failed",
        "listen_error": "e" * 400,
        "error_type": "T" * 120,
        "failure_stream_id": "f" * 220,
        "last_event_id": "v" * 220,
        "last_stream_id": "s" * 220,
        "last_turn": 7,
        "last_text": "x" * 400,
        "last_provider": "p" * 200,
    }

    compact = present_for_monitor(event)

    assert compact["listen_error"] == "e" * 240
    assert compact["error_type"] == "T" * 80
    assert compact["failure_stream_id"] == "f" * 160
    assert compact["last_event_id"] == "v" * 160
    assert compact["last_stream_id"] == "s" * 160
    assert compact["last_turn"] == 7
    assert compact["last_text"] == "x" * 240
    assert compact["last_provider"] == "p" * 120


def test_mode_a_wake_kinds_include_turn():
    from hark.monitor_feed import MODE_A_WAKE_KINDS

    assert "ambient.turn" in MODE_A_WAKE_KINDS
    assert "ambient.conversation_end" in MODE_A_WAKE_KINDS

"""B177 / gh-9 end-to-end: run_tts / run_ask survive the Gemini TTS 400.

The 400 is injected at the provider seam (`hark.speech.resolve_tts`) — no live
API call is made by any test in this file.
"""

from __future__ import annotations

from types import SimpleNamespace

import hark.speech as speech_mod
import pytest
from hark.config import HarkConfig
from hark.providers.base import ProviderError
from hark.speech import run_tts

GEMINI_400 = (
    'Gemini TTS HTTP 400: {"error":{"code":400,"message":"Model tried to '
    'generate text, but it should only be used for TTS.","status":'
    '"INVALID_ARGUMENT"}} (pin a TTS-capable model or use xai/openai/minimax)'
)

QUESTION = (
    "Quick question about the diagram. Should the main message be that every "
    "chain is protected by the whole network?"
)


class _FakeProvider:
    """One provider whose per-call outcome comes from the shared script."""

    def __init__(self, name: str, state: SimpleNamespace) -> None:
        self.name = name
        self._state = state

    def _outcome(self) -> str | None:
        """Next scripted error for this provider (last entry repeats)."""
        script = self._state.script.get(self.name)
        if script is None:
            return self._state.errors.get(self.name)
        seen = self._state.seen.get(self.name, 0)
        self._state.seen[self.name] = seen + 1
        return script[min(seen, len(script) - 1)]

    def synthesize(self, text: str, voice: str | None = None):
        error = self._outcome()
        self._state.calls.append((self.name, text))
        if error is not None:
            raise ProviderError(error)
        return SimpleNamespace(
            audio=b"AUDIO-" + self.name.encode(),
            provider=self.name,
            content_type="audio/mpeg",
            voice=voice or "eve",
        )


@pytest.fixture
def synth(monkeypatch):
    """Wire a controllable provider table + captured events into run_tts."""
    state = SimpleNamespace(
        calls=[],          # [(provider, text_sent)]
        errors={},         # provider -> error message for every call
        script={},         # provider -> [error|None, ...] consumed in order
        seen={},           # provider -> calls consumed from its script
        chain=[],          # what fallback_provider_chain returns
        chain_calls=0,
        events=[],         # surfaced HEP events
        cached=[],         # store_cached_tts(voice, text, audio)
    )

    def fake_resolve(name, **kwargs):
        return _FakeProvider(name, state)

    def fake_chain(current, **kwargs):
        state.chain_calls += 1
        return [p for p in state.chain if p != current]

    monkeypatch.setattr("hark.speech.resolve_tts", fake_resolve)
    monkeypatch.setattr("hark.speech.fallback_provider_chain", fake_chain)
    monkeypatch.setattr(
        "hark.speech._synth_transport_factory",
        speech_mod._in_process_synth_transport_factory,
    )
    monkeypatch.setattr("hark.speech.lookup_cached_tts", lambda *a, **k: None)
    monkeypatch.setattr(
        "hark.speech.store_cached_tts",
        lambda voice, text, audio: state.cached.append((voice, text, audio)),
    )
    monkeypatch.setattr(
        "hark.speech.surface_tts_event",
        lambda kind, **fields: state.events.append({"kind": kind, **fields}),
    )
    return state


def _cfg() -> HarkConfig:
    cfg = HarkConfig()
    cfg.tts.provider = "google"
    cfg.tts.max_chars = 0
    return cfg


def test_gemini_400_falls_back_and_the_run_reports_who_spoke(synth) -> None:
    synth.errors = {"google": GEMINI_400}
    synth.chain = ["xai"]

    result = run_tts(_cfg(), QUESTION, play=False, use_cache=False)

    # 1. The ask survives.
    assert result["ok"] is True
    # 2. The provider that actually produced audio is what is reported.
    assert result["provider"] == "xai"
    # 3. The downgrade is explicit — not a plain success.
    assert result["fallback"]["requested"] == "google"
    assert result["fallback"]["spoke"] == "xai"
    steps = result["fallback"]["steps"]
    assert [(s["from_provider"], s["to_provider"], s["step"]) for s in steps] == [
        ("google", "google", "retry"),
        ("google", "xai", "fallback"),
    ]
    assert all(s["error_class"] == "text_instead_of_audio" for s in steps)
    assert "should only be used for TTS" in steps[0]["error"]
    # 4. Google was asked exactly twice (primary + one retry), then dropped.
    assert [p for p, _ in synth.calls] == ["google", "google", "xai"]


def test_the_fallback_is_visible_on_the_monitor_feed(synth) -> None:
    synth.errors = {"google": GEMINI_400}
    synth.chain = ["xai"]

    run_tts(_cfg(), QUESTION, play=False, use_cache=False)

    hops = [e for e in synth.events if e["kind"] == "tts.provider_fallback"]
    assert [(e["from_provider"], e["to_provider"]) for e in hops] == [
        ("google", "google"),
        ("google", "xai"),
    ]
    assert all("B177" in e["instructions"] for e in hops)


def test_the_fallback_is_visible_on_stderr(synth, capsys) -> None:
    synth.errors = {"google": GEMINI_400}
    synth.chain = ["xai"]

    run_tts(_cfg(), QUESTION, play=False, use_cache=False)

    err = capsys.readouterr().err
    # Not prefixed "hark tts:" — the same line fires under ask and ambient.
    assert "hark: TTS google failed (text_instead_of_audio); fallback with xai" in err


def test_hep_kind_is_registered_for_hark_monitor() -> None:
    from hark.monitor_feed import MODE_A_WAKE_KINDS
    from hark.state_feed.present import present_for_monitor

    assert "tts.provider_fallback" in MODE_A_WAKE_KINDS
    compact = present_for_monitor(
        {
            "kind": "tts.provider_fallback",
            "event_id": "e1",
            "observed_at": "2026-08-23T00:00:00Z",
            "from_provider": "google",
            "to_provider": "xai",
            "step": "fallback",
            "error_class": "text_instead_of_audio",
            "error": GEMINI_400,
        }
    )
    assert compact["from_provider"] == "google"
    assert compact["to_provider"] == "xai"
    assert "B177" in compact["instructions"]


def test_the_retry_alone_can_rescue_the_run(synth) -> None:
    """First attempt 400s, the hardened retry on the same provider succeeds."""
    synth.script = {"google": [GEMINI_400, None]}
    synth.chain = ["xai"]

    result = run_tts(_cfg(), QUESTION, play=False, use_cache=False)

    assert result["ok"] is True
    assert result["provider"] == "google"
    assert [s["step"] for s in result["fallback"]["steps"]] == ["retry"]
    sent = [text for provider, text in synth.calls if provider == "google"]
    # The retry shaped the request; the first attempt did not.
    assert sent[0] == QUESTION
    assert sent[1] != QUESTION
    assert sent[1].endswith(QUESTION)
    # A run the retry rescues must not pay for credential probes: the operator
    # is already waiting mid-turn.
    assert synth.chain_calls == 0


def test_a_hardened_retry_caches_under_the_text_the_operator_asked_for(
    synth,
) -> None:
    """The cache must key on the spoken text, never on the shaped request."""
    short = "Ship it?"
    synth.script = {"google": [GEMINI_400, None]}
    synth.chain = []

    run_tts(_cfg(), short, play=False, use_cache=True)

    assert [text for _voice, text, _audio in synth.cached] == [short]


@pytest.mark.parametrize(
    "message",
    [
        "Gemini TTS HTTP 403: permission denied on model",
        "Gemini TTS HTTP 401: API key not valid",
        "Gemini TTS HTTP 429: quota exceeded",
    ],
)
def test_auth_and_quota_failures_still_fail_fast(synth, message: str) -> None:
    """B175 lesson: a real credential problem must not be papered over."""
    synth.errors = {"google": message}
    synth.chain = ["xai"]

    with pytest.raises(ProviderError):
        run_tts(_cfg(), QUESTION, play=False, use_cache=False)

    assert [p for p, _ in synth.calls] == ["google"]  # no retry, no fallback
    assert synth.chain_calls == 0
    assert not [e for e in synth.events if e["kind"] == "tts.provider_fallback"]


def test_fallback_can_be_turned_off_in_config(synth) -> None:
    synth.errors = {"google": GEMINI_400}
    synth.chain = ["xai"]
    cfg = _cfg()
    cfg.tts.fallback_on_error = False

    with pytest.raises(ProviderError):
        run_tts(cfg, QUESTION, play=False, use_cache=False)

    assert [p for p, _ in synth.calls] == ["google"]


def test_no_working_fallback_raises_the_real_trigger(synth) -> None:
    synth.errors = {"google": GEMINI_400, "xai": "xAI TTS missing audio data"}
    synth.chain = ["xai"]

    with pytest.raises(ProviderError) as excinfo:
        run_tts(_cfg(), QUESTION, play=False, use_cache=False)

    # Not xAI's complaint — the operator needs the trigger they hit.
    assert "should only be used for TTS" in str(excinfo.value)
    assert getattr(excinfo.value, "tts_fallback_steps", None)


def test_later_chunks_start_from_the_provider_that_worked(synth) -> None:
    """A long reply must not re-fail on the dead provider for every chunk."""
    synth.errors = {"google": GEMINI_400}
    synth.chain = ["xai"]
    cfg = _cfg()
    cfg.tts.chunk_chars = 200
    long = "Is this the plan we want to ship to the operator this week? " * 12

    result = run_tts(cfg, long, play=False, use_cache=False)

    assert result["ok"] is True
    assert result["chunks"] >= 3
    asked = [p for p, _ in synth.calls]
    # Exactly one primary + one retry on google, then everything on xai.
    assert asked.count("google") == 2
    assert asked.count("xai") == result["chunks"]


def test_a_clean_run_never_probes_for_a_fallback(synth) -> None:
    synth.errors = {}
    synth.chain = ["xai"]

    result = run_tts(_cfg(), QUESTION, play=False, use_cache=False)

    assert result["ok"] is True
    assert "fallback" not in result
    assert synth.chain_calls == 0
    assert [p for p, _ in synth.calls] == ["google"]


def test_ask_json_carries_the_fallback_so_it_cannot_read_as_clean(monkeypatch) -> None:
    """`hark ask --json` must show the downgrade, not just ok: true."""
    from hark.answer_window.result import ListenResult
    from hark.exitcodes import OK
    from hark.speech import run_ask

    tts_info = {
        "ok": True,
        "provider": "xai",
        "voice": "eve",
        "fallback": {
            "requested": "google",
            "spoke": "xai",
            "steps": [
                {
                    "from_provider": "google",
                    "to_provider": "xai",
                    "step": "fallback",
                    "hardened": False,
                    "error_class": "text_instead_of_audio",
                    "error": GEMINI_400,
                }
            ],
        },
    }

    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda cfg, text, **kwargs: (
            tts_info,
            ListenResult(
                text="yes",
                provider="custom",
                duration_ms=100,
                end_mode="silence",
                stream_id="s1",
            ),
        ),
    )

    result = run_ask(HarkConfig(), QUESTION, confirm="never")
    assert result["exit"] == OK
    assert result["tts"]["provider"] == "xai"
    assert result["tts"]["fallback"]["requested"] == "google"
    assert result["tts"]["fallback"]["spoke"] == "xai"


def test_the_failed_attempt_is_recorded_in_the_usage_trail(synth, monkeypatch) -> None:
    """A 400 costs a request; the audit trail must show it, not just the win."""
    recorded: list[dict] = []

    class _Store:
        def record_tts(self, **kwargs):
            recorded.append(kwargs)

    monkeypatch.setattr("hark.speech.UsageStore", lambda *a, **k: _Store())
    synth.errors = {"google": GEMINI_400}
    synth.chain = ["xai"]

    run_tts(_cfg(), QUESTION, play=False, use_cache=False)

    failures = [r for r in recorded if r["ok"] is False]
    assert [r["provider"] for r in failures] == ["google", "google"]
    assert all("should only be used for TTS" in r["error"] for r in failures)
    successes = [r for r in recorded if r["ok"] is True]
    assert [r["provider"] for r in successes] == ["xai"]


def test_a_fallback_rung_does_not_carry_the_previous_providers_voice(synth) -> None:
    """Voice ids are per-provider namespaces, not a shared vocabulary.

    `eve` is xAI's; OpenAI 400s on it (`voice or "alloy"`) and MiniMax rejects
    it as a voice_id. Carrying it onto a fallback rung would turn the content
    400 into a voice-not-found 400 — and in the gh-9 configuration (xai and
    openai disabled, so the chain is just minimax) that was the only rung.
    """
    seen: list[tuple[str, str]] = []

    class _Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        def synthesize(self, text: str, voice: str | None = None):
            seen.append((self.name, voice or ""))
            if self.name == "google":
                raise ProviderError(GEMINI_400)
            return SimpleNamespace(
                audio=b"A",
                provider=self.name,
                content_type="audio/mpeg",
                voice=voice or "alloy",
            )

    import hark.speech as sp

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sp, "resolve_tts", lambda name, **kw: _Recorder(name))
    synth.chain = ["minimax"]
    try:
        result = run_tts(_cfg(), QUESTION, play=False, use_cache=False, voice="eve")
    finally:
        monkey.undo()

    assert result["ok"] is True
    assert dict(seen)["google"] == "eve"      # the chosen provider keeps it
    assert dict(seen)["minimax"] == ""        # the fallback takes its own default


def test_a_fallback_providers_audio_is_not_written_to_the_phrase_cache(
    synth,
) -> None:
    """The cache keys on (voice, text) with no provider.

    Storing the fallback's audio there would make every later run replay it as
    provider="cache" with no fallback record — a silent downgrade that reads as
    a clean success.
    """
    synth.errors = {"google": GEMINI_400}
    synth.chain = ["xai"]

    result = run_tts(_cfg(), "Ship it?", play=False, use_cache=True)

    assert result["provider"] == "xai"
    assert synth.cached == []


def test_the_window_nudge_speaks_on_the_tts_leg_not_the_stt_pin() -> None:
    """B187: an STT-only pin must never drive a synth call.

    `--stt-provider custom` would have routed the spoken nudge into the Custom
    *TTS* gateway, and `faster_whisper` would have raised "unknown TTS
    provider" into a swallowing except, dropping the nudge silently.
    """
    from hark.answer_window.policy import policy_from_config

    cfg = HarkConfig()
    cfg.tts.provider = "minimax"
    cfg.stt.provider = "custom"

    policy = policy_from_config(cfg, "bound_answer", stt_provider="faster_whisper")
    assert policy.stt_provider == "faster_whisper"
    assert policy.tts_provider == "minimax"

    # An explicit --tts-provider wins for the nudge too.
    policy = policy_from_config(
        cfg,
        "bound_answer",
        stt_provider="faster_whisper",
        tts_provider="xai",
    )
    assert policy.tts_provider == "xai"


def test_run_listen_threads_the_tts_leg_into_the_policy(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_open(policy, deps):
        seen["stt"] = policy.stt_provider
        seen["tts"] = policy.tts_provider
        from hark.answer_window.result import ListenResult

        return ListenResult(
            text="ok", provider="custom", duration_ms=1, end_mode="silence"
        )

    monkeypatch.setattr("hark.answer_window.open_window.open_answer_window", fake_open)
    from hark.speech import run_listen

    cfg = HarkConfig()
    cfg.tts.provider = "google"
    run_listen(cfg, profile="bound_answer", provider="custom", tts_provider="minimax")
    assert seen == {"stt": "custom", "tts": "minimax"}


def test_plain_hark_tts_still_shows_the_downgrade(monkeypatch, capsys) -> None:
    """The default (non --json) line is what an orchestrator shelling out gets.

    That branch prints a REDUCED dict, so it has to opt the fallback in
    explicitly or the downgrade is invisible exactly where it is least likely
    to be noticed.
    """
    import argparse
    import json

    import hark.cli as cli

    fallback = {
        "requested": "google",
        "spoke": "xai",
        "steps": [
            {
                "from_provider": "google",
                "to_provider": "xai",
                "step": "fallback",
                "hardened": False,
                "error_class": "text_instead_of_audio",
                "error": GEMINI_400,
            }
        ],
    }
    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda cfg, text, **kw: {
            "ok": True,
            "provider": "xai",
            "voice": "eve",
            "fallback": fallback,
        },
    )
    args = argparse.Namespace(
        text=["Ship", "it?"],
        provider=None,
        tts_provider=None,
        stt_provider=None,
        voice=None,
        no_play=False,
        out=None,
        json=False,
        listen=False,
        end_mode=None,
    )
    assert cli.cmd_tts(args, _cfg()) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["provider"] == "xai"
    assert payload["fallback"]["requested"] == "google"
    assert payload["fallback"]["spoke"] == "xai"


def test_plain_hark_tts_stays_terse_on_a_clean_run(monkeypatch, capsys) -> None:
    import argparse
    import json

    import hark.cli as cli

    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda cfg, text, **kw: {"ok": True, "provider": "xai", "voice": "eve"},
    )
    args = argparse.Namespace(
        text=["hello"],
        provider=None,
        tts_provider=None,
        stt_provider=None,
        voice=None,
        no_play=False,
        out=None,
        json=False,
        listen=False,
        end_mode=None,
    )
    assert cli.cmd_tts(args, _cfg()) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"ok": True, "provider": "xai"}


def test_the_cache_stays_closed_for_every_chunk_after_a_hop(synth) -> None:
    """Defect found in review: the handover chunk was not the only risk.

    Chunk 2 starts straight on the handed-over provider, takes no hop of its
    own, and would otherwise be cached under the REQUESTED voice + text — the
    exact key a later run on the chosen provider looks up.
    """
    synth.errors = {"google": GEMINI_400}
    synth.chain = ["xai"]
    cfg = _cfg()
    cfg.tts.chunk_chars = 120
    text = "Is this the plan? " * 8

    result = run_tts(cfg, text, play=False, use_cache=True)

    assert result["chunks"] >= 2
    assert result["provider"] == "xai"
    assert synth.cached == []


def test_the_window_nudge_synthesizes_on_the_tts_leg(monkeypatch, tmp_path) -> None:
    """Drive the real empty-STT nudge and check which provider it speaks with.

    Before B187 this synthesized through `policy.stt_provider`, so a listen
    pinned to an STT-only provider either routed the spoken nudge into a
    different service or raised "unknown TTS provider" into a swallowing
    except and dropped it.
    """
    from hark.audio.capture import CaptureResult
    from hark.config import HarkConfig as Cfg
    from hark.config import ListenConfig
    from hark.providers.base import Transcript
    from hark.speech import EMPTY_STT_NUDGE_TEXT, run_listen
    from hark.usage import UsageStore

    class _Stt:
        name = "custom"

        def __init__(self) -> None:
            self.texts = ["", "yes please"]

        def transcribe(self, wav_bytes, *, language=None):
            return Transcript(text=self.texts.pop(0), provider=self.name)

    def _cap():
        return CaptureResult(
            pcm16=b"\x00\x00" * 1600,
            sample_rate=16000,
            duration_ms=2540,
            speech_ms=2540,
            wait_speech_ms=80,
            peak_rms=0.02,
            peak_db=-34.0,
        )

    class _NullCtx:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    caps = iter([_cap(), _cap()])
    monkeypatch.setattr("hark.speech.resolve_stt", lambda *a, **k: _Stt())
    monkeypatch.setattr("hark.speech.capture_utterance", lambda **k: next(caps))
    # Stub the mic/lease/cue stack exactly as tests/test_empty_stt.py does, so
    # no real audio device or desktop media control is touched (B188/B190).
    monkeypatch.setattr("hark.speech.pause_ambient_for_mic", lambda **k: _NullCtx())
    monkeypatch.setattr("hark.speech.MicLease", lambda *a, **k: _NullCtx())
    monkeypatch.setattr("hark.speech.BusySection", lambda *a, **k: _NullCtx())
    monkeypatch.setattr("hark.speech.duck_media", lambda *a, **k: _NullCtx())
    monkeypatch.setattr("hark.speech.register_active_listen", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.clear_active_listen", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.consume_listen_action", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.poll_listen_action", lambda *a, **k: None)
    monkeypatch.setattr("hark.speech.play_record_start", lambda: None)
    monkeypatch.setattr("hark.speech.play_record_stop", lambda: None)
    monkeypatch.setattr("hark.speech.configure_cues_from_config", lambda cfg: None)
    monkeypatch.setattr(
        "hark.speech.UsageStore", lambda *a, **k: UsageStore(tmp_path / "u.jsonl")
    )
    spoke: list[str | None] = []
    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda cfg, text, **kw: spoke.append(kw.get("provider")) or {"ok": True},
    )

    cfg = Cfg(listen=ListenConfig(empty_stt_retry=False, empty_stt_nudge=True))
    cfg.tts.provider = "minimax"
    cfg.stt.provider = "custom"

    result = run_listen(
        cfg,
        last_tts="menu text here",
        post_tts_guard_s=0,
        provider="custom",
        tts_provider="minimax",
    )

    assert result.text == "yes please"
    # The nudge was spoken, and on the TTS leg — never on "custom" (STT).
    assert spoke == ["minimax"]
    assert EMPTY_STT_NUDGE_TEXT


def test_exhaustion_records_the_last_provider_asked(synth, monkeypatch) -> None:
    """The tts_last_provider attribution branch, at run level."""
    recorded: list[dict] = []

    class _Store:
        def record_tts(self, **kwargs):
            recorded.append(kwargs)

    monkeypatch.setattr("hark.speech.UsageStore", lambda *a, **k: _Store())
    synth.errors = {"google": GEMINI_400, "xai": "xAI TTS missing audio data"}
    synth.chain = ["xai"]

    with pytest.raises(ProviderError):
        run_tts(_cfg(), QUESTION, play=False, use_cache=False)

    # google's two attempts came from on-step records; the terminal record is
    # attributed to xai (the last provider asked), not to the requested one.
    assert [r["provider"] for r in recorded] == ["google", "google", "xai"]
    assert all(r["ok"] is False for r in recorded)


def test_a_minimax_shaped_fallback_reports_its_own_voice(synth) -> None:
    """MiniMax echoes the request back, so a blank must not become 'eve'.

    This is the rung that exists in the gh-9 configuration (xai + openai
    disabled), so it is the one whose reporting has to be honest.
    """

    class _Minimax:
        name = "minimax"

        def synthesize(self, text: str, voice: str | None = None):
            # Mirrors providers/minimax.py: report the voice actually used.
            used = voice or "English_expressive_narrator"
            return SimpleNamespace(
                audio=b"A", provider="minimax", content_type="audio/mpeg", voice=used
            )

    class _Google:
        name = "google"

        def synthesize(self, text: str, voice: str | None = None):
            raise ProviderError(GEMINI_400)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(
        "hark.speech.resolve_tts",
        lambda name, **kw: _Google() if name == "google" else _Minimax(),
    )
    synth.chain = ["minimax"]
    try:
        result = run_tts(_cfg(), QUESTION, play=False, use_cache=False, voice="eve")
    finally:
        monkey.undo()

    assert result["provider"] == "minimax"
    assert result["voice"] == "English_expressive_narrator"

"""P1.M4 SpeakThenListen module: re-exports, handoff order, confirm profile."""

from __future__ import annotations

import time
from contextlib import contextmanager

import pytest

from hark.config import HarkConfig
from hark.speech import ListenResult, run_ask, speak_and_listen
from hark.speak_then_listen import HandoffState, attach_tts_info
from hark.speak_then_listen import run_ask as stl_run_ask
from hark.speak_then_listen import speak_and_listen as stl_speak


def test_speech_reexports_same_callables():
    assert speak_and_listen is stl_speak
    assert run_ask is stl_run_ask
    assert HandoffState.SPEAKING.value == "speaking"
    assert set(HandoffState) == {
        HandoffState.SPEAKING,
        HandoffState.ARMED,
        HandoffState.LISTENING,
        HandoffState.CONFIRMING,
    }


def test_attach_tts_info_on_exception():
    exc = TimeoutError("no speech")
    tts = {"ok": True, "provider": "mock"}
    out = attach_tts_info(exc, tts)
    assert out is exc
    assert getattr(exc, "tts_info") == tts


def test_run_tts_play_stack_order_conference_mute_duck(monkeypatch):
    """Internalized order: conference hold → mic mute → media duck (adapters stay)."""
    import hark.speech as speech_mod

    order: list[str] = []
    cfg = HarkConfig()
    cfg.audio.mute_mic_during_tts = True
    cfg.audio.duck_media_during_tts = True
    cfg.audio.hold_during_conference = True

    class Hold:
        skipped = False

        def as_meta(self):
            return {"held": True}

    def fake_hold(cfg, text, *, policy=None):
        order.append(f"conference:{policy}")
        return Hold()

    @contextmanager
    def fake_mute(*, enabled=True):
        order.append(f"mute_enter:{enabled}")
        yield type("S", (), {"applied": bool(enabled)})()
        order.append("mute_exit")

    @contextmanager
    def fake_duck(cfg, *, enabled=True, exclude_conference=False):
        order.append(f"duck_enter:{enabled}:excl={exclude_conference}")
        yield type(
            "D",
            (),
            {
                "as_meta": lambda self: {
                    "media_ducked": True,
                    "duck_count": 1,
                }
            },
        )()
        order.append("duck_exit")

    monkeypatch.setattr("hark.conference.apply_conference_hold", fake_hold)
    monkeypatch.setattr("hark.speech.mic_muted_during_tts", fake_mute)
    monkeypatch.setattr("hark.speech.duck_media", fake_duck)
    monkeypatch.setattr("hark.speech.claim_tts_play_ticket", lambda: object())
    monkeypatch.setattr("hark.speech.abandon_tts_play_ticket", lambda *a, **k: None)

    @contextmanager
    def fake_exclusive(*, ticket=None, wait_timeout_s=None):
        order.append("exclusive")
        yield

    monkeypatch.setattr("hark.speech.exclusive_playback", fake_exclusive)
    monkeypatch.setattr("hark.speech.lookup_cached_tts", lambda *a, **k: b"\x00\x01")
    monkeypatch.setattr(
        "hark.speech.play_wav_bytes",
        lambda *a, **k: type("PR", (), {"duration_ms": 10})(),
    )
    monkeypatch.setattr(
        "hark.speech.repair_tts_mute_after_play",
        lambda **k: {"repaired": False},
    )
    monkeypatch.setattr(
        "hark.speech.wait_until_tts_play_allowed",
        lambda **k: type(
            "D",
            (),
            {
                "deferred": False,
                "gate": None,
                "as_meta": lambda self: {},
            },
        )(),
    )

    # UsageStore no-op
    class Store:
        def record_tts(self, **kw):
            return None

    monkeypatch.setattr("hark.speech.UsageStore", Store)

    result = speech_mod.run_tts(cfg, "hello stack", play=True, use_cache=True)
    assert result["ok"]
    # conference before mute/duck; mute wraps duck
    assert order.index("conference:hold") < order.index("mute_enter:True")
    assert order.index("mute_enter:True") < order.index("duck_enter:True:excl=True")
    assert order.index("duck_enter:True:excl=True") < order.index("duck_exit")
    assert order.index("duck_exit") < order.index("mute_exit")


def test_run_ask_confirm_profile_readback_silence_lexicon(monkeypatch):
    """Confirm path: readback TTS + profile=confirm silence listen + lexicon."""
    cfg = HarkConfig()
    cfg.confirm.mode = "always"
    calls: list[tuple[str, dict]] = []

    def fake_speak(cfg, prompt, **kwargs):
        calls.append(("speak", {"prompt": prompt}))
        return (
            {"ok": True, "provider": "mock"},
            ListenResult(
                text="delete the database",
                provider="mock",
                duration_ms=100,
                end_mode="silence",
                stream_id="s1",
            ),
        )

    def fake_tts(cfg, text, **kwargs):
        calls.append(("tts", {"text": text}))
        return {"ok": True, "provider": "mock"}

    def fake_listen(cfg, **kwargs):
        calls.append(("listen", dict(kwargs)))
        assert kwargs.get("profile") == "confirm"
        assert kwargs.get("end_mode") == "silence"
        assert "I heard:" in (kwargs.get("last_tts") or "")
        return ListenResult(
            text="yes",
            provider="mock",
            duration_ms=50,
            end_mode="silence",
            stream_id="s2",
        )

    monkeypatch.setattr("hark.speech.speak_and_listen", fake_speak)
    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    out = run_ask(cfg, "Should I wipe production?", risk_hint="R2")
    assert out["ok"] is True
    assert out["text"] == "delete the database"
    assert out["tts"]["ok"] is True
    assert any(c[0] == "tts" and "I heard:" in c[1]["text"] for c in calls)
    assert any(c[0] == "listen" and c[1].get("profile") == "confirm" for c in calls)


@pytest.mark.parametrize(
    "confirm_reply",
    [
        "cancel",
        "yes I cant approve this",
        "yes I wont approve this",
        "yes I dont approve this",
    ],
)
def test_run_ask_confirm_cancel_on_no(monkeypatch, confirm_reply):
    cfg = HarkConfig()
    cfg.confirm.mode = "always"

    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: (
            {"ok": True},
            ListenResult(
                text="ship it",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="x",
            ),
        ),
    )
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda *a, **k: ListenResult(
            text=confirm_reply,
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            stream_id="y",
        ),
    )

    out = run_ask(cfg, "Deploy now?", risk_hint="R2")
    assert out["ok"] is False
    assert out.get("cancelled") is True
    assert out.get("confirm_reply") == confirm_reply


def test_run_ask_confirmation_cancel_precedes_affirmative_text(monkeypatch):
    """Answer Window cancellation is authoritative even if STT text says yes."""
    cfg = HarkConfig()
    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: (
            {"ok": True},
            ListenResult(
                text="ship it",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="answer",
            ),
        ),
    )
    monkeypatch.setattr("hark.speech.run_tts", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda *a, **k: ListenResult(
            text="yes",
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            end_phrase="agent:cancel",
            cancelled=True,
            stream_id="confirm",
        ),
    )

    out = run_ask(cfg, "Publish this package?", risk_hint="R3")

    assert out["ok"] is False
    assert out["cancelled"] is True
    assert out["confirm_reply"] == "yes"
    assert out["end_phrase"] == "agent:cancel"
    assert out["exit"] != 0


@pytest.mark.parametrize(
    ("confirm", "configured_mode", "risk", "expect_confirm"),
    [
        ("never", "always", "R0", False),
        ("never", "always", "R1", False),
        ("never", "always", "R2", False),
        ("never", "always", "R3", False),
        ("auto", "always", "R0", False),
        ("auto", "always", "R1", False),
        ("auto", "always", "R2", True),
        ("auto", "always", "R3", True),
        ("always", "never", "R0", True),
        ("always", "never", "R1", True),
        ("always", "never", "R2", True),
        ("always", "never", "R3", True),
        (None, "never", "R0", False),
        (None, "never", "R1", False),
        (None, "never", "R2", True),
        (None, "never", "R3", True),
    ],
)
def test_run_ask_confirmation_policy_by_risk(
    monkeypatch, confirm, configured_mode, risk, expect_confirm
):
    cfg = HarkConfig()
    cfg.confirm.mode = configured_mode
    calls: list[str] = []

    monkeypatch.setattr(
        "hark.speech.speak_and_listen",
        lambda *a, **k: (
            {"ok": True},
            ListenResult(
                text="three slices is fine",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="answer",
            ),
        ),
    )
    monkeypatch.setattr(
        "hark.speech.run_tts",
        lambda *a, **k: calls.append("tts") or {"ok": True},
    )
    monkeypatch.setattr(
        "hark.speech.run_listen",
        lambda *a, **k: (
            calls.append("listen")
            or ListenResult(
                text="  Yes. ",
                provider="mock",
                duration_ms=10,
                end_mode="silence",
                stream_id="confirm",
            )
        ),
    )

    out = run_ask(
        cfg,
        "Should I publish this package?",
        confirm=confirm,
        risk_hint=risk,
    )

    assert out["ok"] is True
    assert out.get("cancelled") is not True
    assert ("listen" in calls) is expect_confirm


def test_run_ask_timeout_preserves_tts_info(monkeypatch):
    """Listen TimeoutError after TTS maps to run_ask JSON with tts attached."""
    cfg = HarkConfig()

    def boom(*a, **k):
        exc = TimeoutError("no speech detected")
        attach_tts_info(exc, {"ok": True, "provider": "mock", "voice": "eve"})
        raise exc

    monkeypatch.setattr("hark.speech.speak_and_listen", boom)
    out = run_ask(cfg, "hello?")
    assert out["ok"] is False
    assert out["exit"] != 0
    assert out["tts"]["provider"] == "mock"
    assert "no speech" in out["error"]


def test_half_duplex_still_via_speech_import(monkeypatch):
    """Facade path: half-duplex listen after TTS (regression for re-export)."""
    order: list[str] = []
    cfg = HarkConfig()
    cfg.audio.overlap_prearm = False
    cfg.audio.listen_pre_arm_ms = 50

    def fake_tts(cfg, text, **kwargs):
        order.append("tts")
        on_near = kwargs.get("on_near_end")
        if on_near:
            on_near()
            order.append("near")
        time.sleep(0.01)
        order.append("tts_done")
        return {"ok": True, "provider": "mock", "voice": "eve", "mic_muted": True}

    def fake_listen(cfg, **kwargs):
        order.append("listen")
        assert kwargs.get("already_armed") is True
        return ListenResult(
            text="hi",
            provider="mock",
            duration_ms=10,
            end_mode="silence",
            stream_id="z",
        )

    monkeypatch.setattr("hark.speech.run_tts", fake_tts)
    monkeypatch.setattr("hark.speech.run_listen", fake_listen)

    tts_info, listened = speak_and_listen(cfg, "prompt?")
    assert tts_info["ok"]
    assert listened.text == "hi"
    assert order == ["tts", "near", "tts_done", "listen"]

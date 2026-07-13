from types import SimpleNamespace

import hark.ambient as ambient
from hark.config import HarkConfig
from hark.wake import WakeHit


def test_wake_remainder_still_uses_cloud_listen(monkeypatch):
    listened = SimpleNamespace(
        text="cloud transcript",
        provider="xai",
        duration_ms=42,
        end_phrase=None,
        cancelled=False,
        stream_id="stest1",
        partials_emitted=0,
    )
    calls = []
    monkeypatch.setattr(
        ambient,
        "run_listen",
        lambda cfg, **kwargs: calls.append(kwargs) or listened,
    )
    hit = WakeHit(
        phrase="hey hark",
        remainder="deploy the build to production",
        raw="hey hark deploy the build to production",
        backend="vosk",
    )

    result = ambient.complete_after_wake(HarkConfig(), hit, announce=False)

    assert len(calls) == 1
    assert calls[0]["end_mode"] == "silence"
    assert result.text == "cloud transcript"
    assert result.listen["provider"] == "xai"

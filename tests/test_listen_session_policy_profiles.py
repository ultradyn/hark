"""P1.M6.E2.T002: bound_answer / post_wake / confirm profile defaults."""

from __future__ import annotations

from hark.answer_window import ListenSessionPolicy
from hark.config import HarkConfig


def _cfg_streaming_on() -> HarkConfig:
    cfg = HarkConfig()
    cfg.ambient.streaming = True
    cfg.ambient.streaming_ack_min_quiet_s = 2.5
    return cfg


def test_bound_answer_streaming_off_despite_ambient_toml() -> None:
    cfg = _cfg_streaming_on()
    pol = ListenSessionPolicy.from_config(cfg, "bound_answer")
    assert pol.profile == "bound_answer"
    assert pol.streaming is False
    assert pol.streaming_ack_min_quiet_s == 2.5  # numeric default still loaded


def test_post_wake_inherits_ambient_streaming() -> None:
    cfg = _cfg_streaming_on()
    pol = ListenSessionPolicy.from_config(cfg, "post_wake")
    assert pol.profile == "post_wake"
    assert pol.streaming is True
    assert pol.streaming_ack_min_quiet_s == 2.5


def test_confirm_streaming_off_despite_ambient_toml() -> None:
    cfg = _cfg_streaming_on()
    pol = ListenSessionPolicy.from_config(cfg, "confirm")
    assert pol.profile == "confirm"
    assert pol.streaming is False


def test_post_wake_off_when_ambient_streaming_false() -> None:
    cfg = HarkConfig()
    assert cfg.ambient.streaming is False
    pol = ListenSessionPolicy.from_config(cfg, "post_wake")
    assert pol.streaming is False

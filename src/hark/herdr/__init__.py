"""Herdr integration (CLI wrap + Unix socket)."""

from hark.herdr.client import (
    AgentInfo,
    HerdrClient,
    HerdrError,
    NamedSessionInfo,
)

__all__ = ["AgentInfo", "HerdrClient", "HerdrError", "NamedSessionInfo"]
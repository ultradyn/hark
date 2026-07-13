"""Shared pytest fixtures.

Disable GitHub release self-update checks by default so CI and unit tests never
hit the network (B088). Opt in via ``HARK_UPDATE_CHECK=1`` in specific tests.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_update_check_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARK_UPDATE_CHECK", "0")

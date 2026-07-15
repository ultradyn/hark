"""Shared pytest fixtures.

Disable GitHub release self-update checks by default so CI and unit tests never
hit the network (B088). Opt in via ``HARK_UPDATE_CHECK=1`` in specific tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_state_home(tmp_path: Path) -> Path:
    """Per-test XDG state root; never allow defaults to reach operator state."""
    return tmp_path / "xdg-state"


@pytest.fixture(autouse=True)
def _isolate_xdg_state(
    monkeypatch: pytest.MonkeyPatch,
    isolated_state_home: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_state_home))
    monkeypatch.setenv("HARK_EVENT_PROVENANCE", "test")


@pytest.fixture(autouse=True)
def _disable_update_check_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARK_UPDATE_CHECK", "0")

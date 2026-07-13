"""B070×B079: learned-alias hot-reload must not thrash Sherpa every hop."""

from __future__ import annotations

from types import SimpleNamespace

from hark.ambient import _apply_policy_to_backend
from hark.wake import WakePolicy


class _CountingBackend:
    def __init__(self) -> None:
        self.rebuilds = 0
        self.policy = None

    def rebuild_keywords(self, policy=None) -> None:
        self.rebuilds += 1
        if policy is not None:
            self.policy = policy


def test_apply_policy_routes_to_rebuild_keywords() -> None:
    b = _CountingBackend()
    pol = WakePolicy(mode="names", names=["iris"])
    _apply_policy_to_backend(b, pol)
    assert b.rebuilds == 1
    assert b.policy is pol


def test_learned_reload_gate_only_on_object_change() -> None:
    """Mirrors ambient loop: only apply when load_learned_if_changed returns new obj."""
    b = _CountingBackend()
    pol = WakePolicy(mode="names", names=["iris"], learn=True)
    learned0 = SimpleNamespace(
        name_aliases={"eyris": "iris"},
        phrase_aliases=[],
        mtime_ns=1,
    )
    # First apply (simulates changed load)
    prev = None
    learned = learned0
    if pol.learn and learned is not None and learned is not prev:
        pol = pol.merge_learned(
            name_aliases=learned.name_aliases,
            phrase_aliases=learned.phrase_aliases,
        )
        _apply_policy_to_backend(b, pol)
    assert b.rebuilds == 1

    # Same object from load_learned_if_changed → no rebuild
    prev = learned
    learned = learned0  # same identity
    if pol.learn and learned is not None and learned is not prev:
        _apply_policy_to_backend(b, pol)
    assert b.rebuilds == 1

    # New object (mtime change) → rebuild
    prev = learned
    learned = SimpleNamespace(
        name_aliases={"eyris": "iris", "irys": "iris"},
        phrase_aliases=[],
        mtime_ns=2,
    )
    if pol.learn and learned is not None and learned is not prev:
        pol = pol.merge_learned(
            name_aliases=learned.name_aliases,
            phrase_aliases=learned.phrase_aliases,
        )
        _apply_policy_to_backend(b, pol)
    assert b.rebuilds == 2

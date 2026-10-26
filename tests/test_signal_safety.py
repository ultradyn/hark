"""Signal-mask ownership ends in ordinary frames, never object finalizers."""

import signal

import pytest

from hark.signal_safety import SigintMaskGuard


def test_guard_has_no_finalizer_that_can_consume_late_interrupts():
    assert "__del__" not in SigintMaskGuard.__dict__


def test_cleanup_propagates_interrupt_without_an_unwinding_primary():
    def interrupted(_how, _mask):
        raise KeyboardInterrupt

    guard = SigintMaskGuard(interrupted)
    guard._active = True
    with pytest.raises(KeyboardInterrupt):
        guard.restore_suppressing()


def test_restore_retries_pre_effect_failure_explicitly():
    current = {signal.SIGINT}
    attempts = 0

    def mask(how, target):
        nonlocal current, attempts
        if how == signal.SIG_SETMASK:
            attempts += 1
            if attempts == 1:
                raise OSError("transient mask failure")
            current = set(target)
        return set(current)

    guard = SigintMaskGuard(mask)
    guard._active = True
    guard._previous = set()
    guard.restore_suppressing()
    assert attempts == 2
    assert current == set()
    assert guard._active is False


def test_cleanup_preserves_primary_but_finishes_mask_restoration():
    current = {signal.SIGINT}
    attempts = 0

    def mask(how, target):
        nonlocal current, attempts
        if how == signal.SIG_SETMASK:
            attempts += 1
            if attempts == 1:
                raise MemoryError("cleanup failure")
            current = set(target)
        return set(current)

    guard = SigintMaskGuard(mask)
    guard._active = True
    guard._previous = set()
    with pytest.raises(ValueError, match="primary"):
        try:
            raise ValueError("primary")
        finally:
            guard.restore_preserving_primary()
    assert attempts == 2
    assert current == set()
    assert guard._active is False

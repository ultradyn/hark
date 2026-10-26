"""Exception-safe signal-mask transitions used by interrupt lifecycles."""

from __future__ import annotations

import signal
import sys
from collections.abc import Callable
from typing import Any


MaskFunction = Callable[[int, set[Any]], set[Any]]


class SigintMaskGuard:
    """Recoverably block SIGINT after first recording the prior mask."""

    __slots__ = ("_active", "_mask_function", "_previous")

    def __init__(self, mask_function: MaskFunction | None) -> None:
        self._active = False
        self._mask_function = mask_function
        self._previous: set[Any] | None = None

    @classmethod
    def acquire(
        cls,
        mask_function: MaskFunction | None = None,
    ) -> SigintMaskGuard:
        if mask_function is None:
            mask_function = getattr(signal, "pthread_sigmask", None)
        guard = cls(mask_function)
        if mask_function is None:
            return guard

        # Query without changing state before the fallible blocking syscall.
        # If a wrapper raises after the block took effect, recovery still has
        # the exact prior mask and an already-live guard object.
        guard._previous = mask_function(signal.SIG_BLOCK, set())
        guard._active = True
        try:
            mask_function(signal.SIG_BLOCK, {signal.SIGINT})
        except BaseException:
            guard.restore_suppressing()
            raise
        return guard

    def restore(self) -> None:
        if not self._active or self._mask_function is None:
            return
        target = self._previous or set()
        try:
            self._mask_function(signal.SIG_SETMASK, target)
        finally:
            # Unmasking a pending SIGINT can raise from its Python handler
            # after the kernel has already installed ``target``. Reconcile the
            # actual mask in a finally so the guard never retries a
            # completed transition and delivers the same signal tail twice.
            primary = sys.exception()
            try:
                current = self._mask_function(signal.SIG_BLOCK, set())
            except BaseException:
                if primary is None:
                    raise
            else:
                if set(current) == set(target):
                    self._active = False

    def restore_preserving_primary(self) -> None:
        primary = sys.exception()
        try:
            self.restore()
        except BaseException:
            # Retry from this ordinary owner frame, never from a finalizer:
            # Python discards interrupts raised during object finalization.
            self.restore_suppressing()
            if primary is None:
                raise

    def restore_suppressing(self) -> None:
        primary = sys.exception()
        for _ in range(2):
            try:
                self.restore()
                return
            except (KeyboardInterrupt, SystemExit):
                if primary is None:
                    raise
            except Exception:
                # A failed mask syscall may have taken effect. Reconcile and
                # retry explicitly while the operation's owner is still live.
                continue

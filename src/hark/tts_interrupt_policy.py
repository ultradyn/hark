"""Caller context for the B153 TTS interrupt terminal policy."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator
import signal
import sys
from typing import Any

from hark.signal_safety import SigintMaskGuard


_CLI_PROCESS_EXIT_EXPECTED: ContextVar[bool] = ContextVar(
    "hark_tts_cli_process_exit_expected",
    default=False,
)


class TtsSynthesisInterrupted(KeyboardInterrupt):
    """A typed terminal interrupt owned by the TTS/CLI lifecycle."""

    exit_code = 130


class _CliSigintController:
    """Keep one stable handler installed so pool swaps have no raw tail."""

    def __init__(self) -> None:
        self._installed = False
        self._active_depth = 0
        self._previous: Any = None
        self._handler = self._handle

    def _handle(self, signum: int, frame: Any) -> None:
        if self._active_depth:
            raise TtsSynthesisInterrupted
        previous = self._previous
        if previous in (None, signal.SIG_DFL, signal.default_int_handler):
            # Keep the installed boundary handler typed through the return
            # tail of cli.main; injected/unrelated KeyboardInterrupt objects
            # do not traverse this OS-signal path and remain distinguishable.
            raise TtsSynthesisInterrupted
        if previous == signal.SIG_IGN:
            return
        previous(signum, frame)

    def activate(self) -> None:
        # Only a genuinely nested scope may rely on the outer scope's handler
        # publication. An external reset can make ``_installed`` stale between
        # separate CLI invocations in the same interpreter.
        if self._active_depth:
            self._active_depth += 1
            return
        snapshot_complete = False
        guard: SigintMaskGuard | None = None
        try:
            # A reused controller can retain the previous scope's delegation
            # target. Establish live signal-table truth before publishing an
            # active scope so an interrupt *inside* getsignal follows the live
            # handler and is never reclassified from that cached target.
            current = signal.getsignal(signal.SIGINT)
            snapshot_complete = True
            self._installed = current is self._handler
            if not self._installed:
                self._previous = current
            self._active_depth = 1
            guard = SigintMaskGuard.acquire()
            try:
                current = signal.getsignal(signal.SIGINT)
                self._installed = current is self._handler
                if not self._installed:
                    self._previous = current
                    try:
                        signal.signal(signal.SIGINT, self._handler)
                    finally:
                        # A pending SIGINT can be delivered by the unmask below.
                        # Publish actual handler truth before that is possible.
                        self._reconcile_handler_truth()
            finally:
                guard.restore_preserving_primary()
        except BaseException as exc:
            if snapshot_complete:
                self._rollback_failed_activation(guard)
            else:
                # No activation state was published or mutated, but a reused
                # controller's cache may already have been stale at entry.
                # Refresh only ownership truth while preserving the interrupt.
                primary = sys.exception()
                try:
                    self._reconcile_handler_truth()
                except BaseException:
                    if primary is None:
                        raise
            self._active_depth = 0
            if (
                snapshot_complete
                and type(exc) is KeyboardInterrupt
                and self._previous
                in (
                    None,
                    signal.SIG_DFL,
                    signal.default_int_handler,
                )
            ):
                raise TtsSynthesisInterrupted from None
            raise

    def _reconcile_handler_truth(self) -> None:
        self._installed = signal.getsignal(signal.SIGINT) is self._handler

    def _rollback_failed_activation(self, guard: SigintMaskGuard | None) -> None:
        primary = sys.exception()
        try:
            try:
                # ``_installed`` is only cached publication. Restore another
                # handler only when the live signal table proves we still own
                # it; an external reset must never be overwritten by rollback.
                if signal.getsignal(signal.SIGINT) is self._handler:
                    signal.signal(signal.SIGINT, self._previous)
            finally:
                self._reconcile_handler_truth()
        except BaseException:
            if primary is None:
                raise
        finally:
            if guard is not None:
                guard.restore_preserving_primary()

    def deactivate(self) -> None:
        if not self._active_depth:
            return
        self._active_depth -= 1
        if self._active_depth:
            return

        entry_primary = sys.exception()
        guard: SigintMaskGuard | None = None
        try:
            guard = SigintMaskGuard.acquire()
            try:
                # A still-running synth pool deliberately retains its own
                # handler for repeated-SIGINT escalation. Only restore the
                # controller when it is still the live outermost owner.
                if signal.getsignal(signal.SIGINT) is self._handler:
                    signal.signal(signal.SIGINT, self._previous)
            finally:
                self._reconcile_handler_truth()
        except (OSError, ValueError):
            # Match activate's main-thread-only degradation without replacing
            # an exception already leaving the CLI dispatch boundary.
            if entry_primary is None:
                raise
        except BaseException:
            if entry_primary is None:
                raise
        finally:
            if guard is not None:
                guard.restore_preserving_primary()


_CLI_SIGINT_CONTROLLER = _CliSigintController()


def cli_process_exit_expected() -> bool:
    """Return whether the current call stack is owned by CLI main."""
    return _CLI_PROCESS_EXIT_EXPECTED.get()


@contextmanager
def cli_tts_interrupt_scope() -> Iterator[None]:
    """Own OS SIGINT for the complete CLI dispatch boundary."""
    token = _CLI_PROCESS_EXIT_EXPECTED.set(True)
    try:
        _CLI_SIGINT_CONTROLLER.activate()
        try:
            yield
        finally:
            _CLI_SIGINT_CONTROLLER.deactivate()
    finally:
        _CLI_PROCESS_EXIT_EXPECTED.reset(token)

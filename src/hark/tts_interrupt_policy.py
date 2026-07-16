"""Caller context for the B153 TTS interrupt terminal policy."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator


_CLI_PROCESS_EXIT_EXPECTED: ContextVar[bool] = ContextVar(
    "hark_tts_cli_process_exit_expected",
    default=False,
)


def cli_process_exit_expected() -> bool:
    """Return whether the current call stack is owned by CLI main."""
    return _CLI_PROCESS_EXIT_EXPECTED.get()


@contextmanager
def cli_tts_interrupt_scope() -> Iterator[None]:
    """Mark TTS pools created during CLI dispatch as process-exit owned."""
    token = _CLI_PROCESS_EXIT_EXPECTED.set(True)
    try:
        yield
    finally:
        _CLI_PROCESS_EXIT_EXPECTED.reset(token)

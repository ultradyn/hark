"""Unix socket JSON-line Herdr client (subscribe path)."""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from typing import Any, AsyncIterator, Callable

SUBSCRIPTIONS: list[dict[str, Any]] = [
    {"type": "pane.agent_status_changed"},
    {"type": "pane.agent_detected"},
    {"type": "pane.closed"},
    {"type": "pane.exited"},
    {"type": "pane.moved"},
]

# Transient peer/socket drops: reconnect quietly rather than spam watch.error.
_EXPECTED_ERRNOS = frozenset(
    {
        errno.EPIPE,
        errno.ECONNRESET,
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ENOENT,  # socket path briefly gone while Herdr restarts
    }
)

_EXPECTED_NEEDLES = (
    "broken pipe",
    "connection reset",
    "connection refused",
    "connection aborted",
    "socket closed",
    "errno 32",
    "errno 104",
    "errno 111",
    "errno 2",
    "epipe",
    "econnreset",
    "econnrefused",
)


def is_expected_disconnect(exc: BaseException) -> bool:
    """True for transient socket drops that warrant quiet reconnect.

    Real protocol/API failures (e.g. subscribe method missing) return False
    so callers can surface them as watch.error and fall back to poll.
    """
    if isinstance(
        exc,
        (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            ConnectionRefusedError,
            EOFError,
            FileNotFoundError,
        ),
    ):
        return True
    if isinstance(exc, OSError) and exc.errno in _EXPECTED_ERRNOS:
        return True
    text = str(exc).lower()
    return any(needle in text for needle in _EXPECTED_NEEDLES)


async def _write(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
    await writer.drain()


async def _read(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await reader.readline()
    if not line:
        raise EOFError("Herdr socket closed")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("expected JSON object")
    return value


async def _close_quietly(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        # Peer already gone (Broken pipe / reset) — expected on disconnect.
        pass


async def subscribe_events(
    socket_path: str | Path,
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        await _write(writer, {"id": "hark_ping", "method": "ping", "params": {}})
        ping = await _read(reader)
        if "error" in ping:
            raise RuntimeError(f"ping failed: {ping['error']}")

        await _write(
            writer,
            {
                "id": "hark_sub",
                "method": "events.subscribe",
                "params": {"subscriptions": SUBSCRIPTIONS},
            },
        )
        ack = await _read(reader)
        if "error" in ack:
            raise RuntimeError(f"subscribe failed: {ack['error']}")

        while True:
            event = await _read(reader)
            if on_event:
                on_event(event)
            yield event
    finally:
        await _close_quietly(writer)


def run_subscribe_loop(
    socket_path: str | Path,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    async def _main() -> None:
        async for event in subscribe_events(socket_path):
            emit(event)

    asyncio.run(_main())

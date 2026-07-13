#!/usr/bin/env python3
"""Minimal Herdr raw event subscription probe.

Hark protocol reconnaissance: Herdr socket subscribe probe (not the full voice bridge).
It connects to HERDR_SOCKET_PATH, requests a session snapshot, subscribes to
pane lifecycle/state events, and writes every pushed JSON object as one JSONL
line. Validate the subscription objects against the installed schema first:

    herdr api schema --output herdr-api.schema.json

Run from a Herdr-managed pane or pass --socket explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any


SUBSCRIPTIONS: list[dict[str, Any]] = [
    {"type": "pane.agent_status_changed"},
    {"type": "pane.agent_detected"},
    {"type": "pane.closed"},
    {"type": "pane.exited"},
    {"type": "pane.moved"},
]


async def write_request(writer: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
    writer.write(json.dumps(obj, separators=(",", ":")).encode() + b"\n")
    await writer.drain()


async def read_json_line(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await reader.readline()
    if not line:
        raise EOFError("Herdr socket closed")
    try:
        value = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from Herdr: {line[:200]!r}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"expected JSON object, got {type(value).__name__}")
    return value


async def run(socket_path: str, include_snapshot: bool) -> None:
    if os.name == "nt":
        raise SystemExit("This prototype only implements Unix sockets. Production must support Windows named pipes.")

    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        await write_request(writer, {"id": "probe_ping", "method": "ping", "params": {}})
        ping = await read_json_line(reader)
        if "error" in ping:
            raise RuntimeError(f"ping failed: {ping['error']}")
        print(json.dumps({"probe": "ping", "response": ping}, separators=(",", ":")), flush=True)

        await write_request(writer, {"id": "probe_snapshot", "method": "session.snapshot", "params": {}})
        snapshot = await read_json_line(reader)
        if "error" in snapshot:
            raise RuntimeError(f"snapshot failed: {snapshot['error']}")
        if include_snapshot:
            print(json.dumps({"probe": "snapshot", "response": snapshot}, separators=(",", ":")), flush=True)

        await write_request(
            writer,
            {
                "id": "probe_subscribe",
                "method": "events.subscribe",
                "params": {"subscriptions": SUBSCRIPTIONS},
            },
        )
        ack = await read_json_line(reader)
        if "error" in ack:
            raise RuntimeError(
                "subscription failed; inspect the installed Herdr schema and adjust filters: "
                f"{ack['error']}"
            )
        print(json.dumps({"probe": "subscribed", "response": ack}, separators=(",", ":")), flush=True)

        while True:
            event = await read_json_line(reader)
            print(json.dumps(event, separators=(",", ":")), flush=True)
    finally:
        writer.close()
        await writer.wait_closed()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default=os.environ.get("HERDR_SOCKET_PATH"))
    parser.add_argument("--include-snapshot", action="store_true")
    args = parser.parse_args()
    if not args.socket:
        parser.error("set HERDR_SOCKET_PATH or pass --socket")
    try:
        asyncio.run(run(args.socket, args.include_snapshot))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # concise probe diagnostics
        print(f"herdr_event_monitor: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Exec target for one isolated TTS provider synthesis call."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import struct
import sys
import threading
from typing import Any

from hark.providers.base import ProviderError
from hark.providers.resolve import resolve_tts


_PR_SET_PDEATHSIG = 1
_MAX_METADATA_SIZE = 64 * 1024
_MAX_AUDIO_SIZE = 64 * 1024 * 1024


def _install_parent_death_signal() -> None:
    parent_pid = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    if parent_pid == 1 or os.getppid() != parent_pid:
        os._exit(1)
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})


def _write_result(message: dict[str, Any]) -> None:
    fd_text = os.environ.get("HARK_TTS_RESULT_FD")
    if not fd_text:
        raise RuntimeError("missing HARK_TTS_RESULT_FD")
    audio = message.pop("audio", b"")
    if not isinstance(audio, bytes):
        audio = bytes(audio)
    if len(audio) > _MAX_AUDIO_SIZE:
        raise RuntimeError("TTS synth audio exceeds 64 MiB")
    message["audio_size"] = len(audio)
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(payload) > _MAX_METADATA_SIZE:
        raise RuntimeError("TTS synth metadata exceeds 64 KiB")
    with os.fdopen(int(fd_text), "wb", closefd=True) as result_file:
        result_file.write(struct.pack("!I", len(payload)))
        result_file.write(payload)
        result_file.write(audio)


def _exception_message(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ProviderError):
        return {
            "status": "error",
            "kind": "provider",
            "message": str(exc),
            "code": exc.code,
        }
    message: dict[str, Any] = {
        "status": "error",
        "kind": "exception",
        "type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message": str(exc),
    }
    return message


def _read_request(request_file: Any) -> dict[str, Any]:
    header = request_file.read(4)
    if len(header) != 4:
        raise ValueError("missing TTS synth request header")
    payload_size = struct.unpack("!I", header)[0]
    if payload_size > _MAX_METADATA_SIZE:
        raise ValueError("oversize TTS synth request")
    payload = request_file.read(payload_size)
    if len(payload) != payload_size:
        raise ValueError("truncated TTS synth request")
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid TTS synth request") from exc
    if not isinstance(request, dict):
        raise ValueError("invalid TTS synth request message")

    provider = request.get("provider")
    voice = request.get("voice")
    language = request.get("language")
    text = request.get("text")
    if not isinstance(provider, str):
        raise ValueError("invalid TTS synth request provider")
    if not isinstance(voice, str):
        raise ValueError("invalid TTS synth request voice")
    if language is not None and not isinstance(language, str):
        raise ValueError("invalid TTS synth request language")
    if not isinstance(text, str):
        raise ValueError("invalid TTS synth request text")
    return {
        "provider": provider,
        "voice": voice,
        "language": language,
        "text": text,
    }


def _run_test_mode(mode: str) -> int:
    if mode == "--test-success":
        _write_result(
            {
                "status": "ok",
                "audio": b"test-audio",
                "provider": "test-worker",
                "content_type": "audio/mpeg",
                "voice": "test-voice",
            }
        )
        return 0
    if mode == "--test-provider-error":
        _write_result(
            {
                "status": "error",
                "kind": "provider",
                "message": "worker provider failed",
                "code": 42,
            }
        )
        return 1
    if mode == "--test-unknown-error":
        _write_result(
            {
                "status": "error",
                "kind": "exception",
                "type": "test.UnknownFailure",
                "message": "unknown worker failure",
            }
        )
        return 1
    if mode == "--test-gil-hang":
        print(f"GIL_READY {os.getpid()}", flush=True)
        ctypes.PyDLL(None).sleep(10)
        return 1
    if mode == "--test-hang":
        print("APPEND_READY", flush=True)
        threading.Event().wait()
        return 1
    return 2


def main(argv: list[str] | None = None) -> int:
    _install_parent_death_signal()
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        return _run_test_mode(args[0])
    try:
        request = _read_request(sys.stdin.buffer)
        provider = resolve_tts(
            request["provider"],
            voice=request["voice"],
            language=request.get("language"),
        )
        result = provider.synthesize(
            request["text"],
            voice=request["voice"],
        )
        _write_result(
            {
                "status": "ok",
                "audio": result.audio,
                "provider": result.provider,
                "content_type": result.content_type,
                "voice": result.voice or request["voice"],
            }
        )
        return 0
    except BaseException as exc:
        try:
            _write_result(_exception_message(exc))
        except BaseException:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

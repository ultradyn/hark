"""Clean-interpreter process transport for TTS provider synthesis."""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from hark.providers.base import ProviderError


_MAX_METADATA_SIZE = 64 * 1024
_MAX_AUDIO_SIZE = 64 * 1024 * 1024


@dataclass(frozen=True)
class SynthRequest:
    provider: str
    voice: str
    language: str | None
    text: str


@dataclass(frozen=True)
class SynthResponse:
    audio: bytes
    provider: str
    content_type: str
    voice: str


class SynthWorkerError(RuntimeError):
    """A synth worker failed without a reconstructable provider exception."""


class ProcessOwner(Protocol):
    def register_synth_process(self, process: subprocess.Popen[bytes]) -> None: ...

    def unregister_synth_process(self, process: subprocess.Popen[bytes]) -> None: ...


class SynthTransport(Protocol):
    def synthesize(self, request: SynthRequest) -> SynthResponse: ...


def synth_worker_command() -> list[str]:
    return [sys.executable, "-m", "hark.tts_worker"]


class SubprocessSynthTransport:
    """Execute one provider call in a clean child interpreter."""

    def __init__(
        self,
        owner: ProcessOwner,
        *,
        command_factory: Callable[[], list[str]] = synth_worker_command,
    ) -> None:
        self._owner = owner
        self._command_factory = command_factory

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        pid = getattr(process, "pid", None)
        if not isinstance(pid, int) or pid <= 0:
            return
        try:
            running = process.poll() is None
        except BaseException:
            running = True
        if running:
            try:
                process.terminate()
                process.wait(timeout=0.5)
            except BaseException:
                try:
                    os.kill(pid, signal.SIGKILL)
                except BaseException:
                    pass
        try:
            process.wait(timeout=0.5)
        except BaseException:
            pass

    @staticmethod
    def _decode(result_file: Any, returncode: int) -> SynthResponse:
        header = result_file.read(4)
        if len(header) != 4:
            raise SynthWorkerError(
                f"TTS synth worker exited {returncode} without a result"
            )
        metadata_size = struct.unpack("!I", header)[0]
        if metadata_size > _MAX_METADATA_SIZE:
            raise SynthWorkerError("oversize TTS synth worker metadata")
        metadata_payload = result_file.read(metadata_size)
        if len(metadata_payload) != metadata_size:
            raise SynthWorkerError("truncated TTS synth worker metadata")
        try:
            message = json.loads(metadata_payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SynthWorkerError("invalid TTS synth worker result") from exc
        if not isinstance(message, dict):
            raise SynthWorkerError("invalid TTS synth worker message")

        status = message.get("status")
        if not isinstance(status, str) or status not in {"ok", "error"}:
            raise SynthWorkerError("invalid TTS synth worker status")

        if status == "ok":
            audio_size = message.get("audio_size")
            if not isinstance(audio_size, int) or isinstance(audio_size, bool):
                raise SynthWorkerError("invalid TTS synth worker audio size")
            if audio_size < 0 or audio_size > _MAX_AUDIO_SIZE:
                raise SynthWorkerError("invalid TTS synth worker audio size")
            provider = message.get("provider")
            content_type = message.get("content_type")
            voice = message.get("voice")
            if not all(
                isinstance(value, str) for value in (provider, content_type, voice)
            ):
                raise SynthWorkerError("invalid TTS synth worker response fields")
            audio = result_file.read(audio_size)
            if len(audio) != audio_size:
                raise SynthWorkerError("truncated TTS synth worker audio")
            return SynthResponse(
                audio=audio,
                provider=provider,
                content_type=content_type,
                voice=voice,
            )

        kind = message.get("kind")
        if kind == "provider":
            remote_message = message.get("message")
            code = message.get("code")
            if not isinstance(remote_message, str):
                raise SynthWorkerError("invalid TTS provider error message")
            if (
                not isinstance(code, int)
                or isinstance(code, bool)
                or code < 0
                or code > 255
            ):
                raise SynthWorkerError("invalid TTS provider error code")
            raise ProviderError(remote_message, code=code)
        if kind != "exception":
            raise SynthWorkerError("invalid TTS synth worker error kind")
        remote_type = message.get("type")
        remote_message = message.get("message")
        if not isinstance(remote_type, str) or not isinstance(remote_message, str):
            raise SynthWorkerError("invalid TTS synth worker exception fields")
        raise SynthWorkerError(f"TTS synth worker {remote_type}: {remote_message}")

    def synthesize(self, request: SynthRequest) -> SynthResponse:
        read_fd, write_fd = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        read_file = None
        try:
            env = os.environ.copy()
            env["HARK_TTS_RESULT_FD"] = str(write_fd)
            import_path = os.pathsep.join(path for path in sys.path if path)
            if import_path:
                current_path = env.get("PYTHONPATH")
                env["PYTHONPATH"] = import_path + (
                    os.pathsep + current_path if current_path else ""
                )
            process = subprocess.Popen.__new__(subprocess.Popen)
            self._owner.register_synth_process(process)
            subprocess.Popen.__init__(
                process,
                self._command_factory(),
                stdin=subprocess.PIPE,
                pass_fds=(write_fd,),
                env=env,
            )
            os.close(write_fd)
            write_fd = -1

            assert process.stdin is not None
            request_payload = json.dumps(asdict(request), separators=(",", ":")).encode(
                "utf-8"
            )
            if len(request_payload) > _MAX_METADATA_SIZE:
                raise SynthWorkerError("oversize TTS synth worker request")
            process.stdin.write(struct.pack("!I", len(request_payload)))
            process.stdin.write(request_payload)
            process.stdin.close()
            read_file = os.fdopen(read_fd, "rb", closefd=True)
            read_fd = -1
            response = self._decode(read_file, process.poll() or -1)
            returncode = process.wait()
            if returncode != 0:
                raise SynthWorkerError(f"TTS synth worker exited {returncode}")
            return response
        except BaseException:
            if process is not None:
                self._stop(process)
            raise
        finally:
            if process is not None:
                try:
                    process_stdin = getattr(process, "stdin", None)
                    if process_stdin is not None and not process_stdin.closed:
                        process_stdin.close()
                except BaseException:
                    pass
            if read_file is not None:
                try:
                    read_file.close()
                except BaseException:
                    pass
            if read_fd >= 0:
                try:
                    os.close(read_fd)
                except BaseException:
                    pass
            if write_fd >= 0:
                try:
                    os.close(write_fd)
                except BaseException:
                    pass
            if process is not None:
                try:
                    self._owner.unregister_synth_process(process)
                except BaseException:
                    # Ownership bookkeeping must not replace the primary or
                    # prevent cleanup of the transport resources above.
                    pass


class InProcessSynthTransport:
    """Injectable deterministic transport for unit tests."""

    def __init__(self, resolver: Callable[..., Any]) -> None:
        self._resolver = resolver

    def synthesize(self, request: SynthRequest) -> SynthResponse:
        provider = self._resolver(
            request.provider,
            voice=request.voice,
            language=request.language,
        )
        result = provider.synthesize(request.text, voice=request.voice)
        return SynthResponse(
            audio=result.audio,
            provider=result.provider,
            content_type=result.content_type,
            voice=result.voice or request.voice,
        )

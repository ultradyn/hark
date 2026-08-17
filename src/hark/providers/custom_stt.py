"""Custom STT — OpenAI-compatible batch transcription against a configurable base URL.

Product name: **Custom STT**. Talks to any HTTP gateway that implements the
OpenAI audio transcriptions surface (and optionally a native ``/stt`` alias).

See ``docs/PROVIDERS.md`` § Custom STT for the normative client contract.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from hark.providers.base import ProviderError, Transcript, provider_operation

DEFAULT_CUSTOM_PATH = "/audio/transcriptions"
DEFAULT_NATIVE_PATH = "/stt"
_KEY_COMMAND_TIMEOUT_S = 10.0
_KEY_COMMAND_ERR_LIMIT = 200


def _run_custom_api_key_command(command: str) -> str | None:
    """Run a shell command and return its stripped stdout (first non-empty line)."""
    cmd = (command or "").strip()
    if not cmd:
        return None
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=_KEY_COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError(
            f"custom STT api key command timed out after {_KEY_COMMAND_TIMEOUT_S:.0f}s"
        ) from exc
    except OSError as exc:
        raise ProviderError(
            f"custom STT api key command failed to start ({exc.__class__.__name__})"
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")
        if len(err) > _KEY_COMMAND_ERR_LIMIT:
            err = err[: _KEY_COMMAND_ERR_LIMIT - 3].rstrip() + "..."
        detail = f": {err}" if err else ""
        raise ProviderError(
            f"custom STT api key command failed (exit {proc.returncode}){detail}"
        )
    text = (proc.stdout or "").strip()
    if not text:
        return None
    # Multi-line helpers (e.g. `pass show`) — first non-empty line is the secret.
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def resolve_custom_api_key(
    api_key: str | None,
    api_key_file: str | None = None,
    api_key_command: str | None = None,
) -> str | None:
    """Bearer key resolution (first hit wins):

    1. literal ``api_key`` (config / ``HARK_STT_CUSTOM_API_KEY``)
    2. file ``api_key_file`` (config / ``HARK_STT_CUSTOM_API_KEY_FILE``)
    3. command ``api_key_command`` (config / ``HARK_STT_CUSTOM_API_KEY_COMMAND``)

    File and command keep the secret out of config.toml. File = whole contents
    stripped. Command = shell stdout, first non-empty line, timeout 10s.
    """
    key = (api_key or "").strip()
    if key:
        return key
    raw_path = (api_key_file or "").strip()
    if raw_path:
        path = Path(raw_path).expanduser()
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProviderError(
                f"custom STT api key file unreadable: {path} ({exc.__class__.__name__})"
            ) from exc
        if key:
            return key
    return _run_custom_api_key_command(api_key_command or "")


def normalize_custom_base_url(base_url: str | None) -> str:
    """Return a trailing-slash-free OpenAI-style root (…/v1)."""
    raw = (base_url or "").strip()
    if not raw:
        raise ProviderError(
            "custom STT requires custom_base_url "
            "(config [stt].custom_base_url or HARK_STT_CUSTOM_BASE_URL)"
        )
    return raw.rstrip("/")


def normalize_custom_path(path: str | None) -> str:
    """Return a leading-slash path; default OpenAI transcriptions path."""
    raw = (path or DEFAULT_CUSTOM_PATH).strip() or DEFAULT_CUSTOM_PATH
    if not raw.startswith("/"):
        raise ProviderError(
            f"custom_path must start with '/' (got {path!r}); "
            f"examples: {DEFAULT_CUSTOM_PATH!r}, {DEFAULT_NATIVE_PATH!r}"
        )
    # Collapse accidental double slashes in the path portion only.
    while "//" in raw:
        raw = raw.replace("//", "/")
    return raw


def join_custom_url(base_url: str, path: str) -> str:
    base = normalize_custom_base_url(base_url)
    p = normalize_custom_path(path)
    # urljoin needs a trailing slash on base to replace path correctly when
    # path is absolute-with-leading-slash… actually absolute paths replace.
    # Manual join is clearer: base is root, path is absolute under that host root.
    return f"{base}{p}"


def _extract_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload or "").strip()
    text = (
        payload.get("text")
        or payload.get("transcript")
        or (
            payload.get("result", {}).get("text")
            if isinstance(payload.get("result"), dict)
            else None
        )
        or ""
    )
    if isinstance(payload.get("words"), list) and not text:
        text = " ".join(
            str(w.get("word") or w.get("text") or "")
            for w in payload["words"]
            if isinstance(w, dict)
        )
    return str(text).strip()


@dataclass(frozen=True)
class CustomSttStatus:
    name: str
    available: bool
    detail: str
    base_url: str | None = None
    path: str | None = None
    model: str | None = None


def custom_stt_status(
    *,
    base_url: str | None,
    api_key: str | None,
    model: str | None,
    path: str | None,
    api_key_file: str | None = None,
    api_key_command: str | None = None,
) -> CustomSttStatus:
    """Soft readiness: base URL + API key present (no live probe)."""
    bu = (base_url or "").strip()
    md = (model or "").strip() or None
    try:
        key = (
            resolve_custom_api_key(api_key, api_key_file, api_key_command) or ""
        )
    except ProviderError as exc:
        return CustomSttStatus(
            name="custom",
            available=False,
            detail=str(exc),
            base_url=bu.rstrip("/") or None,
            path=path,
            model=md,
        )
    try:
        p = normalize_custom_path(path)
    except ProviderError as exc:
        return CustomSttStatus(
            name="custom",
            available=False,
            detail=str(exc),
            base_url=bu or None,
            path=path,
            model=md,
        )
    if not bu:
        return CustomSttStatus(
            name="custom",
            available=False,
            detail=(
                "not configured — set [stt].custom_base_url + key via "
                "HARK_STT_CUSTOM_API_KEY / custom_api_key_file / "
                "custom_api_key_command (provider=custom is explicit-only)"
            ),
            base_url=None,
            path=p,
            model=md,
        )
    if not key:
        return CustomSttStatus(
            name="custom",
            available=False,
            detail=(
                f"base_url set ({bu.rstrip('/')}) but API key missing — "
                "HARK_STT_CUSTOM_API_KEY, custom_api_key_file, "
                "or custom_api_key_command"
            ),
            base_url=bu.rstrip("/"),
            path=p,
            model=md,
        )
    # OpenAI surface requires model; native /stt may inject synthetic model.
    if p.rstrip("/") == DEFAULT_CUSTOM_PATH.rstrip("/") or p.endswith(
        "/audio/transcriptions"
    ):
        if not md:
            return CustomSttStatus(
                name="custom",
                available=False,
                detail=(
                    f"base_url+key ok but custom_model required for path {p} "
                    "(OpenAI-compatible transcriptions)"
                ),
                base_url=bu.rstrip("/"),
                path=p,
                model=None,
            )
    host = bu.rstrip("/")
    model_bit = f", model={md}" if md else ", model=(server default)"
    return CustomSttStatus(
        name="custom",
        available=True,
        detail=f"configured → POST {host}{p}{model_bit}",
        base_url=host,
        path=p,
        model=md,
    )


class CustomStt:
    """Batch STT client for OpenAI-compatible (or native /stt dual-mount) gateways."""

    name = "custom"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str | None = None,
        path: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = normalize_custom_base_url(base_url)
        key = (api_key or "").strip()
        if not key:
            raise ProviderError(
                "custom STT API key missing — set HARK_STT_CUSTOM_API_KEY, "
                "[stt].custom_api_key_file, or [stt].custom_api_key_command"
            )
        self.api_key = key
        self.path = normalize_custom_path(path)
        self.model = (model or "").strip() or None
        self.timeout = timeout
        # OpenAI transcriptions require model; native dual-mount may omit it.
        if self._requires_model() and not self.model:
            raise ProviderError(
                "custom STT requires custom_model for OpenAI-compatible path "
                f"{self.path} (config [stt].custom_model or HARK_STT_CUSTOM_MODEL)"
            )
        self.url = join_custom_url(self.base_url, self.path)

    def _requires_model(self) -> bool:
        p = self.path.rstrip("/")
        return p.endswith("/audio/transcriptions") or p.endswith("/audio/translations")

    @provider_operation("Custom STT")
    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data: dict[str, Any] = {}
        if self.model:
            data["model"] = self.model
        if language:
            data["language"] = language
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                self.url,
                headers=headers,
                files=files,
                data=data or None,
            )
            if r.status_code == 401:
                raise ProviderError(
                    "Custom STT 401 — check HARK_STT_CUSTOM_API_KEY / "
                    "custom_api_key_file / custom_api_key_command"
                )
            if r.status_code >= 400:
                raise ProviderError(
                    f"Custom STT HTTP {r.status_code}: {r.text[:300]}"
                )
            try:
                payload = r.json()
            except Exception:
                # Some gateways may return plain text on response_format=text
                text = (r.text or "").strip()
                return Transcript(text=text, provider=self.name)
        return Transcript(text=_extract_text(payload), provider=self.name)

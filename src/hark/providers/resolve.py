"""Provider auto-resolution."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from hark.providers.auth import google_auth, minimax_auth, openai_auth, xai_auth
from hark.providers.base import ProviderError, ProviderUnsupported, SttProvider, TtsProvider
from hark.providers.google_p import GoogleStt, GoogleTts
from hark.providers.minimax import MinimaxStt, MinimaxTts
from hark.providers.openai_p import OpenAIStt, OpenAITts
from hark.providers.xai import XaiStt, XaiTts

if TYPE_CHECKING:
    from hark.config import SttConfig, TtsConfig

log = logging.getLogger("hark.providers")

# Local full-STT names (B072). Cloud remains default under "auto".
_LOCAL_STT_NAMES = frozenset(
    {
        "faster_whisper",
        "faster-whisper",
        "whisper",  # alias → faster-whisper
        "local",  # alias → faster-whisper
        "moonshine",
    }
)

_MINIMAX_OK_HINT = (
    "MiniMax TTS requires consent — set tts.minimax_ok = true in config.toml, "
    "or HARK_TTS_MINIMAX_OK=1, or run an interactive hark tts once and answer yes"
)


def _normalize_provider_name(name: str) -> str:
    n = (name or "").lower().strip()
    if n in ("gemini",):
        return "google"
    if n in ("faster-whisper", "whisper", "local"):
        return "faster_whisper"
    return n


def _normalize_stt_name(name: str) -> str:
    n = (name or "auto").lower().strip()
    if n in ("faster-whisper", "whisper", "local"):
        return "faster_whisper"
    return n


def _disabled_set(names: list[str] | None) -> set[str]:
    return {_normalize_provider_name(x) for x in (names or []) if str(x).strip()}


def _env_disabled(env_key: str) -> set[str] | None:
    raw = os.environ.get(env_key)
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return _disabled_set(parts)


def _env_truthy(env_key: str) -> bool | None:
    raw = os.environ.get(env_key)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _stt_disabled(stt_cfg: SttConfig | None) -> set[str]:
    disabled = _disabled_set(getattr(stt_cfg, "disabled", None) if stt_cfg else None)
    env = _env_disabled("HARK_STT_DISABLED")
    if env is not None:
        disabled |= env
    if stt_cfg is None:
        try:
            from hark.config import load_config

            disabled |= _disabled_set(load_config().stt.disabled)
        except Exception:
            pass
    return disabled


def _tts_policy(tts_cfg: TtsConfig | None) -> tuple[set[str], bool]:
    disabled = _disabled_set(getattr(tts_cfg, "disabled", None) if tts_cfg else None)
    minimax_ok = bool(getattr(tts_cfg, "minimax_ok", False)) if tts_cfg else False
    if tts_cfg is None:
        try:
            from hark.config import load_config

            loaded = load_config().tts
            disabled |= _disabled_set(loaded.disabled)
            minimax_ok = bool(loaded.minimax_ok)
        except Exception:
            pass
    env_dis = _env_disabled("HARK_TTS_DISABLED")
    if env_dis is not None:
        disabled |= env_dis
    env_ok = _env_truthy("HARK_TTS_MINIMAX_OK")
    if env_ok is not None:
        minimax_ok = env_ok
    return disabled, minimax_ok


def _reject_if_disabled(name: str, disabled: set[str], *, kind: str) -> None:
    n = _normalize_provider_name(name)
    if n in disabled:
        section = "stt" if kind == "stt" else "tts"
        raise ProviderError(
            f"{kind} provider {n!r} is disabled in config "
            f"([{section}] disabled / HARK_{kind.upper()}_DISABLED)"
        )


def persist_tts_minimax_ok(config_path: Path | None = None) -> Path:
    """Write ``tts.minimax_ok = true`` into the active config.toml."""
    from hark.config import write_default_config
    from hark.paths import default_config_path
    from hark.setup_flow import _set_toml_key

    path = Path(config_path) if config_path is not None else default_config_path()
    if not path.is_file():
        write_default_config(path=path, force=False)
    text = path.read_text(encoding="utf-8")
    text = _set_toml_key(text, "tts", "minimax_ok", "true")
    path.write_text(text, encoding="utf-8")
    return path


def _prompt_minimax_ok() -> bool:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return False
    try:
        sys.stderr.write(
            "MiniMax credentials are available for TTS.\n"
            "Allow Hark to use MiniMax TTS? [y/N] "
        )
        sys.stderr.flush()
        line = sys.stdin.readline()
    except Exception:
        return False
    return (line or "").strip().lower() in ("y", "yes")


def _ensure_minimax_tts_allowed(
    *,
    minimax_ok: bool,
    allow_prompt: bool,
    config_path: Path | None,
    pinned: bool,
) -> bool:
    """Return True if MiniMax TTS may be used; False to skip in auto.

    Raises ProviderError when pinned/required and consent is missing.
    """
    if minimax_ok:
        return True
    if allow_prompt and _prompt_minimax_ok():
        try:
            persist_tts_minimax_ok(config_path)
        except Exception as exc:
            log.warning("failed to persist tts.minimax_ok: %s", exc)
        os.environ["HARK_TTS_MINIMAX_OK"] = "1"
        return True
    if pinned:
        raise ProviderError(_MINIMAX_OK_HINT)
    return False


def _local_opts(stt_cfg: SttConfig | None) -> dict:
    """Defaults when no SttConfig is passed (tests / direct resolve)."""
    if stt_cfg is None:
        return {
            "model": "tiny.en",
            "device": "cpu",
            "compute_type": "int8",
            "model_path": None,
            "fail_open": True,
            "download": True,
        }
    return {
        "model": getattr(stt_cfg, "local_model", None) or "tiny.en",
        "device": getattr(stt_cfg, "local_device", None) or "cpu",
        "compute_type": getattr(stt_cfg, "local_compute_type", None) or "int8",
        "model_path": getattr(stt_cfg, "local_model_path", None),
        "fail_open": bool(getattr(stt_cfg, "local_fail_open", True)),
        "download": bool(getattr(stt_cfg, "local_download", True)),
    }


def _resolve_cloud_stt(name: str = "auto", *, disabled: set[str] | None = None) -> SttProvider:
    name = (name or "auto").lower()
    disabled = disabled or set()
    if name == "anthropic":
        raise ProviderUnsupported(
            "anthropic: no public STT API; use xai|openai|google"
        )
    if name == "minimax":
        _reject_if_disabled("minimax", disabled, kind="stt")
        return MinimaxStt()
    if name == "xai":
        _reject_if_disabled("xai", disabled, kind="stt")
        return XaiStt()
    if name == "openai":
        _reject_if_disabled("openai", disabled, kind="stt")
        return OpenAIStt()
    if name in ("google", "gemini"):
        _reject_if_disabled("google", disabled, kind="stt")
        return GoogleStt()
    if name != "auto":
        raise ProviderError(f"unknown STT provider: {name}")

    candidates: list[tuple[str, type]] = [
        ("xai", XaiStt),
        ("openai", OpenAIStt),
        ("google", GoogleStt),
    ]
    for pname, cls in candidates:
        if pname in disabled:
            continue
        if pname == "xai" and xai_auth().available:
            return cls()
        if pname == "openai" and openai_auth().available:
            return cls()
        if pname == "google" and google_auth().available:
            return cls()
    raise ProviderError(
        "no STT provider available — grok login / agy / XAI_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY"
        + (f" (disabled: {sorted(disabled)})" if disabled else "")
    )


def _try_local_stt(name: str, opts: dict) -> SttProvider:
    """Build a local STT provider; raises ProviderError on missing deps/models."""
    from hark.providers.local_stt import FasterWhisperStt, MoonshineStt

    if name == "moonshine":
        m = opts["model"] or "moonshine/tiny"
        # SttConfig defaults to tiny.en (faster-whisper); map to Moonshine tiny.
        if m.endswith(".en") or m in ("tiny", "base", "small"):
            m = "moonshine/tiny" if "base" not in m and "small" not in m else "moonshine/base"
        return MoonshineStt(model=m)

    # Probe load so fail-open can kick in before first listen.
    stt = FasterWhisperStt(
        model=opts["model"],
        device=opts["device"],
        compute_type=opts["compute_type"],
        model_path=opts["model_path"],
        download=opts["download"],
    )
    stt._ensure_model()
    return stt


def resolve_stt(
    name: str = "auto",
    *,
    stt_cfg: SttConfig | None = None,
) -> SttProvider:
    """Resolve STT provider.

    Cloud providers are the default (``auto`` / xai / openai / google).
    Optional local engines (``faster_whisper``, ``moonshine``) require the
    ``local-stt`` extra (or Moonshine stretch install). When local is selected
    and unavailable, ``stt.local_fail_open`` (default True) falls back to cloud
    auto-resolution.
    """
    disabled = _stt_disabled(stt_cfg)
    raw = (name or "auto").lower().strip()
    if raw in _LOCAL_STT_NAMES or _normalize_stt_name(raw) in (
        "faster_whisper",
        "moonshine",
    ):
        local_name = _normalize_stt_name(raw)
        _reject_if_disabled(local_name, disabled, kind="stt")
        opts = _local_opts(stt_cfg)
        try:
            return _try_local_stt(local_name, opts)
        except ProviderError as exc:
            if opts["fail_open"]:
                log.warning(
                    "local STT %s unavailable (%s); fail-open to cloud",
                    local_name,
                    exc,
                )
                return _resolve_cloud_stt("auto", disabled=disabled)
            raise

    return _resolve_cloud_stt(raw, disabled=disabled)


def resolve_tts(
    name: str = "auto",
    *,
    voice: str | None = None,
    language: str | None = None,
    tts_cfg: TtsConfig | None = None,
    allow_prompt: bool = False,
    config_path: Path | None = None,
) -> TtsProvider:
    name = (name or "auto").lower()
    disabled, minimax_ok = _tts_policy(tts_cfg)
    if name == "anthropic":
        raise ProviderUnsupported(
            "anthropic: no public TTS API for hark; use xai|openai|minimax|google"
        )

    def _xai() -> XaiTts:
        return XaiTts(
            voice=voice or "eve",
            language=language or "en",
        )

    def _minimax(*, pinned: bool) -> MinimaxTts | None:
        _reject_if_disabled("minimax", disabled, kind="tts")
        if not _ensure_minimax_tts_allowed(
            minimax_ok=minimax_ok,
            allow_prompt=allow_prompt,
            config_path=config_path,
            pinned=pinned,
        ):
            return None
        return MinimaxTts()

    if name == "xai":
        _reject_if_disabled("xai", disabled, kind="tts")
        return _xai()
    if name == "openai":
        _reject_if_disabled("openai", disabled, kind="tts")
        return OpenAITts()
    if name == "minimax":
        got = _minimax(pinned=True)
        assert got is not None
        return got
    if name in ("google", "gemini"):
        _reject_if_disabled("google", disabled, kind="tts")
        return GoogleTts()
    if name != "auto":
        raise ProviderError(f"unknown TTS provider: {name}")

    if "xai" not in disabled and xai_auth().available:
        return _xai()
    if "openai" not in disabled and openai_auth().available:
        return OpenAITts()
    if "minimax" not in disabled and minimax_auth().available:
        got = _minimax(pinned=False)
        if got is not None:
            return got
    if "google" not in disabled and google_auth().available:
        return GoogleTts()
    raise ProviderError(
        "no TTS provider available — grok login / XAI_API_KEY / OPENAI_API_KEY / MINIMAX_API_KEY"
        + (f" (disabled: {sorted(disabled)})" if disabled else "")
        + ("" if minimax_ok or "minimax" in disabled else f"; {_MINIMAX_OK_HINT}")
    )

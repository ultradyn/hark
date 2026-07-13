"""Optional local full-STT providers (B072 / I004).

Cloud STT remains the product default (ADR-004). These engines are for
**post-wake utterance** transcription (answer windows / offline privacy),
never continuous ambient wake. See docs/plans/B069-local-stt-survey.md:

- Prefer **faster-whisper** ``tiny.en`` / ``base.en`` int8 on CPU
  (measured RTF ≈ 0.10–0.15 on short clips, mid laptop).
- **Moonshine** is a stretch edge path (better short-audio latency curve).

Install::

    pip install 'hark[local-stt]'   # faster-whisper
    # moonshine: pip install useful-moonshine (stretch; not in default extra)

GPU is optional and never required (``device=cpu`` default).
"""

from __future__ import annotations

import io
import time
import wave
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from hark.providers.base import ProviderError, Transcript

# Measured on B069 probe host (Ryzen 7 class CPU, no discrete NVIDIA).
# Documented for operators; not enforced at runtime.
FASTER_WHISPER_RTF_NOTES = {
    "tiny.en": "B069: ~0.10–0.14 RTF on 2.5 s clips (int8 CPU); cold load ~5.5 s",
    "base.en": "B069: ~0.19–0.23 RTF on 2.5 s clips (int8 CPU); cold load ~13.5 s",
}

DEFAULT_FW_MODEL = "tiny.en"
DEFAULT_FW_DEVICE = "cpu"
DEFAULT_FW_COMPUTE = "int8"
DEFAULT_MOONSHINE_MODEL = "moonshine/tiny"


def wav_bytes_to_float32(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Decode WAV bytes → mono float32 in [-1, 1] and sample rate."""
    if not wav_bytes:
        raise ProviderError("local STT: empty audio")
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate()
            nframes = wf.getnframes()
            raw = wf.readframes(nframes)
    except wave.Error as exc:
        raise ProviderError(f"local STT: invalid WAV: {exc}") from exc

    if sw == 1:
        pcm = np.frombuffer(raw, dtype=np.uint8).astype(np.int16) - 128
        pcm = (pcm * 256).astype(np.int16)
    elif sw == 2:
        pcm = np.frombuffer(raw, dtype=np.int16)
    elif sw == 4:
        pcm = np.frombuffer(raw, dtype=np.int32)
        pcm = (pcm // 65536).astype(np.int16)
    else:
        raise ProviderError(f"local STT: unsupported sample width {sw}")

    if nch > 1:
        pcm = pcm.reshape(-1, nch).mean(axis=1).astype(np.int16)
    audio = pcm.astype(np.float32) / 32768.0
    return audio, int(sr)


def _duration_ms(audio: np.ndarray, sample_rate: int) -> int:
    if sample_rate <= 0 or audio.size == 0:
        return 0
    return int(round(1000.0 * float(audio.size) / float(sample_rate)))


# ---------------------------------------------------------------------------
# faster-whisper
# ---------------------------------------------------------------------------


def import_faster_whisper() -> Any:
    """Import faster_whisper or raise ProviderError with install hint."""
    try:
        import faster_whisper  # type: ignore
    except ImportError as exc:
        raise ProviderError(
            "faster-whisper not installed — pip install 'hark[local-stt]' "
            "or: pip install faster-whisper"
        ) from exc
    return faster_whisper


def load_faster_whisper_model(
    model_size: str = DEFAULT_FW_MODEL,
    *,
    device: str = DEFAULT_FW_DEVICE,
    compute_type: str = DEFAULT_FW_COMPUTE,
    model_path: str | None = None,
    download: bool = True,
) -> Any:
    """Load a CTranslate2 Whisper model (lazy; may download on first use)."""
    fw = import_faster_whisper()
    size_or_path = model_path or model_size
    try:
        return fw.WhisperModel(
            size_or_path,
            device=device,
            compute_type=compute_type,
            local_files_only=not download,
        )
    except Exception as exc:  # model missing, ctranslate2, download, etc.
        raise ProviderError(
            f"faster-whisper model load failed ({size_or_path!r}, "
            f"device={device}, compute_type={compute_type}): {exc}"
        ) from exc


class FasterWhisperStt:
    """Local batch STT via faster-whisper (optional extra ``local-stt``).

    **Not for ambient wake** — open-vocab Whisper mangles rare names and is a
    poor always-on path (B069). Use for post-wake prompts / offline windows.
    """

    name = "faster_whisper"

    def __init__(
        self,
        *,
        model: str = DEFAULT_FW_MODEL,
        device: str = DEFAULT_FW_DEVICE,
        compute_type: str = DEFAULT_FW_COMPUTE,
        model_path: str | None = None,
        download: bool = True,
        beam_size: int = 1,
        model_loader: Callable[..., Any] | None = None,
        # Injected model for tests (skips load)
        model_instance: Any | None = None,
    ) -> None:
        self.model_size = model
        self.device = device
        self.compute_type = compute_type
        self.model_path = model_path
        self.download = download
        self.beam_size = beam_size
        self._model_loader = model_loader or load_faster_whisper_model
        self._model = model_instance

    def _ensure_model(self) -> Any:
        if self._model is None:
            self._model = self._model_loader(
                self.model_size,
                device=self.device,
                compute_type=self.compute_type,
                model_path=self.model_path,
                download=self.download,
            )
        return self._model

    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        t0 = time.monotonic()
        audio, sr = wav_bytes_to_float32(wav_bytes)
        duration_ms = _duration_ms(audio, sr)
        try:
            model = self._ensure_model()
            # language=None lets multilingual models auto-detect; .en models ignore.
            kwargs: dict[str, Any] = {
                "beam_size": self.beam_size,
                "vad_filter": False,
            }
            if language:
                kwargs["language"] = language
            elif self.model_size.endswith(".en") or (
                self.model_path and str(self.model_path).endswith(".en")
            ):
                kwargs["language"] = "en"
            segments, _info = model.transcribe(audio, **kwargs)
            parts: list[str] = []
            for seg in segments:
                text = getattr(seg, "text", None)
                if text is None and isinstance(seg, dict):
                    text = seg.get("text")
                if text:
                    parts.append(str(text))
            text_out = " ".join(p.strip() for p in parts if p and str(p).strip()).strip()
            # collapse double spaces from join
            while "  " in text_out:
                text_out = text_out.replace("  ", " ")
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"faster-whisper transcribe failed: {exc}") from exc

        _ = t0  # wall time available for future metrics; duration_ms is audio
        return Transcript(
            text=text_out,
            provider=self.name,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Moonshine (stretch)
# ---------------------------------------------------------------------------


def import_moonshine() -> Any:
    """Best-effort import of a Moonshine Python package (stretch)."""
    # Historical / community package names vary.
    for mod_name in ("moonshine", "useful_moonshine"):
        try:
            return __import__(mod_name)
        except ImportError:
            continue
    raise ProviderError(
        "Moonshine not installed — stretch local STT; try: "
        "pip install useful-moonshine  (or moonshine). "
        "Prefer provider=faster_whisper for the supported path."
    )


def _moonshine_transcribe_fn(mod: Any) -> Callable[..., Any]:
    if hasattr(mod, "transcribe") and callable(mod.transcribe):
        return mod.transcribe
    # older layouts
    for path in (
        ("transcribe", "transcribe"),
        ("model", "transcribe"),
    ):
        obj: Any = mod
        try:
            for part in path:
                obj = getattr(obj, part)
            if callable(obj):
                return obj
        except AttributeError:
            continue
    raise ProviderError(
        "Moonshine package loaded but no transcribe() entrypoint found"
    )


class MoonshineStt:
    """Stretch local STT via Moonshine (edge short-form).

    Packaging is less stable than faster-whisper; fail-open is recommended.
    Not for continuous ambient wake.
    """

    name = "moonshine"

    def __init__(
        self,
        *,
        model: str = DEFAULT_MOONSHINE_MODEL,
        model_loader: Callable[[], Any] | None = None,
        transcribe_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self._model_loader = model_loader or import_moonshine
        self._transcribe_fn = transcribe_fn
        self._mod: Any | None = None

    def _ensure(self) -> Callable[..., Any]:
        if self._transcribe_fn is not None:
            return self._transcribe_fn
        if self._mod is None:
            self._mod = self._model_loader()
        self._transcribe_fn = _moonshine_transcribe_fn(self._mod)
        return self._transcribe_fn

    def transcribe(self, wav_bytes: bytes, *, language: str | None = None) -> Transcript:
        _ = language  # English-first models; ignored for stretch path
        audio, sr = wav_bytes_to_float32(wav_bytes)
        duration_ms = _duration_ms(audio, sr)
        try:
            fn = self._ensure()
            # APIs vary: (audio, sr), path-only, or kwargs.
            try:
                result = fn(audio, sr)
            except TypeError:
                try:
                    result = fn(audio, sample_rate=sr)
                except TypeError:
                    result = fn(wav_bytes)
            if isinstance(result, str):
                text_out = result.strip()
            elif isinstance(result, dict):
                text_out = str(result.get("text") or result.get("transcript") or "").strip()
            elif isinstance(result, (list, tuple)) and result:
                text_out = " ".join(str(x) for x in result).strip()
            else:
                text_out = str(result or "").strip()
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"moonshine transcribe failed: {exc}") from exc

        return Transcript(
            text=text_out,
            provider=self.name,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# Status / doctor helpers
# ---------------------------------------------------------------------------


@dataclass
class LocalSttStatus:
    name: str
    available: bool
    detail: str
    rtf_note: str = ""


def faster_whisper_status(
    *,
    model: str = DEFAULT_FW_MODEL,
) -> LocalSttStatus:
    note = FASTER_WHISPER_RTF_NOTES.get(model, FASTER_WHISPER_RTF_NOTES["tiny.en"])
    try:
        import_faster_whisper()
    except ProviderError as exc:
        return LocalSttStatus(
            name="faster_whisper",
            available=False,
            detail=str(exc),
            rtf_note=note,
        )
    return LocalSttStatus(
        name="faster_whisper",
        available=True,
        detail=f"faster-whisper importable; default model={model} (loads on first use)",
        rtf_note=note,
    )


def moonshine_status() -> LocalSttStatus:
    try:
        import_moonshine()
    except ProviderError as exc:
        return LocalSttStatus(
            name="moonshine",
            available=False,
            detail=str(exc),
            rtf_note="B069 cited: short-clip latency tens–hundreds of ms (edge-focused)",
        )
    return LocalSttStatus(
        name="moonshine",
        available=True,
        detail="Moonshine package importable (stretch path)",
        rtf_note="B069 cited: short-clip latency tens–hundreds of ms (edge-focused)",
    )


def local_stt_statuses(*, model: str = DEFAULT_FW_MODEL) -> list[LocalSttStatus]:
    return [faster_whisper_status(model=model), moonshine_status()]

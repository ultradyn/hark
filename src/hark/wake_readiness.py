"""Wake dependencies and model preflight without opening audio."""

from __future__ import annotations

from typing import Any
from hark.config import HarkConfig


def ambient_readiness(cfg: HarkConfig) -> dict[str, Any]:
    """Engine-aware ambient/wake model readiness for doctor (B070)."""
    from pathlib import Path

    eng = (cfg.ambient.engine or "vosk").strip().lower()
    model_path = cfg.ambient.model_path
    model_ok = False
    status = "unknown"
    warnings: list[str] = []
    hints: list[str] = []
    package_ok: bool | None = None

    if eng in ("text_probe", "mock", "test", "off", "none", "disabled"):
        model_ok = True
        status = "probe"
        package_ok = True
    elif eng == "vosk":
        try:
            import vosk  # noqa: F401

            package_ok = True
        except (ImportError, OSError):
            package_ok = False
            warnings.append("vosk package missing (uv sync --extra wake)")
        p = Path(model_path) if model_path else None
        model_ok = bool(p and p.is_dir() and (p / "am" / "final.mdl").is_file()
                        and (p / "conf" / "mfcc.conf").is_file()
                        and (p / "graph").is_dir())
        if model_ok:
            status = "ready" if package_ok else "package_missing"
        else:
            status = "missing_model"
            hints.append("run ./scripts/setup-ambient.sh or download-vosk-model.sh")
    elif eng in ("sherpa_kws", "sherpa", "kws"):
        try:
            import sherpa_onnx  # noqa: F401

            package_ok = True
        except (ImportError, OSError):
            package_ok = False
            warnings.append(
                "sherpa-onnx package missing (uv sync --extra wake-sherpa)"
            )
        try:
            from hark.wake import is_sherpa_kws_model_dir

            model_ok = is_sherpa_kws_model_dir(model_path)
        except Exception:
            model_ok = bool(
                model_path and Path(model_path).is_dir()
                and (Path(model_path) / "tokens.txt").is_file()
            )
        if model_ok and package_ok:
            status = "ready"
        elif not model_ok:
            status = "missing_model"
            hints.append(
                "run ./scripts/download-sherpa-kws-model.sh "
                "(English GigaSpeech 3.3M int8 KWS)"
            )
        else:
            status = "package_missing"
    else:
        status = "unknown_engine"
        warnings.append(f"unknown ambient.engine={eng!r}")
        p = Path(model_path) if model_path else None
        model_ok = bool(p and p.is_dir())

    return {
        "enabled": cfg.ambient.enabled,
        "engine": cfg.ambient.engine,
        "wake_mode": getattr(cfg.ambient, "wake_mode", "names"),
        "names": list(getattr(cfg.ambient, "names", []) or []),
        "activation_count": len(cfg.ambient.activation_phrases),
        "learn_from_near_misses": bool(
            getattr(cfg.ambient, "learn_from_near_misses", True)
        ),
        "model_path": model_path,
        "model_ok": model_ok,
        "package_ok": package_ok,
        "status": status,
        "warnings": warnings,
        "hints": hints,
        "snippet_s": cfg.ambient.snippet_s,
    }


"""Conservative risk classification for questions (R0–R3)."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Conservative: when unsure R1 vs R2, treat as R2.

_R3 = re.compile(
    r"\b("
    r"rm\s+-rf|delete\s+all|drop\s+table|force[\s-]?push|"
    r"production|prod\s+deploy|publish\s+package|credential|secret|api[\s_-]?key|"
    r"private[\s_-]?key|password|token\s*=|wire\s*transfer"
    r")\b",
    re.I,
)

_R2 = re.compile(
    r"\b("
    r"allow\b|permission|approve|yes\s*/\s*no|do\s+you\s+want|"
    r"run\s+this\s+command|bash\s+command|network\s+access|"
    r"write\s+to\s+(disk|file)|edit\s+file|install\s+package|"
    r"sudo|dangerous|destructive"
    r")\b",
    re.I,
)

_MENU_HINT = re.compile(r"^\s*\d+[\).\]]\s+\S", re.M)


@dataclass(frozen=True)
class RiskResult:
    risk: str  # R0 | R1 | R2 | R3
    kind: str  # informational | free_text | choice | permission | destructive
    confidence: float


def classify_question(text: str, choices: list[str] | None = None) -> RiskResult:
    t = text or ""
    if _R3.search(t):
        return RiskResult(risk="R3", kind="destructive", confidence=0.85)
    if _R2.search(t) or (
        choices
        and len(choices) >= 2
        and any(
            re.search(r"yes|no|allow|deny|approve|reject", c, re.I) for c in choices
        )
    ):
        return RiskResult(risk="R2", kind="permission", confidence=0.8)
    if choices or _MENU_HINT.search(t):
        return RiskResult(risk="R1", kind="choice", confidence=0.7)
    if len(t.strip()) < 8:
        return RiskResult(risk="R0", kind="informational", confidence=0.5)
    return RiskResult(risk="R1", kind="free_text", confidence=0.65)


def confirm_required(
    risk: str,
    mode: str,
    *,
    explicit_override: bool = False,
) -> bool:
    """Whether spoken confirmation is required before delivery.

    Configured policy keeps R2/R3 mandatory even when ``mode=never``. An
    explicit per-call/CLI ``never`` is operator authority to bypass the second
    confirmation for this ask only. ``always`` applies to every risk class.
    """
    if explicit_override and mode == "never":
        return False
    if mode == "always":
        return True
    if risk in ("R2", "R3"):
        return True
    if risk == "R0":
        return False
    if mode == "never":
        return False
    # auto: library marks "unsure" cases separately; default no force
    return False

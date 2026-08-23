"""Scoped TTS retry + provider fallback (B177 / gh-9).

Gemini TTS answers question-shaped input with HTTP 400 ``Model tried to
generate text, but it should only be used for TTS``. Plain statements
synthesize; substantive questions fail. It is content-dependent and
nondeterministic — the first question of a filing session succeeded, then six
consecutive ones failed — and the shipped Gemini model is *already* TTS-only,
so pinning a model cannot be the fix. With ``xai``/``openai`` disabled, ``auto``
lands on ``google`` and ``hark ask`` dead-ends at exit 4 while the operator is
already waiting for the question. The fallback is what keeps ``ask`` usable.

**Scope is deliberately narrow.** Only the "generated text instead of audio"
class is eligible. Auth (401/403), quota (429), missing credentials, and every
other failure still fail fast, so a bad token can never be papered over as a
working setup (cf. B175, where ``doctor`` reported "ready" off the mere presence
of ``auth.json``). Generalizing fallback to auth errors is a separate open task,
**B164** — do not widen the classifier for it here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

__all__ = [
    "NOT_RETRYABLE",
    "tts_error_http_status",
    "TEXT_INSTEAD_OF_AUDIO",
    "TTS_FALLBACK_ORDER",
    "FallbackStep",
    "classify_tts_error",
    "harden_tts_text",
    "is_fallback_eligible",
    "plan_synth_attempts",
    "synthesize_with_fallback",
]

_R = TypeVar("_R")

#: The one eligible error class.
TEXT_INSTEAD_OF_AUDIO = "text_instead_of_audio"

#: A later rung's own failure. Not eligible on the *first* rung — it is only
#: recorded when a fallback target turns out to be unusable and the walk
#: continues past it.
NOT_RETRYABLE = "not_retryable"

#: Fallback walk order. Mirrors the ``auto`` order in
#: :func:`hark.providers.resolve.resolve_tts`, so a fallback never lands
#: somewhere ``auto`` would not have gone on its own. Explicit-pin-only
#: gateways (Custom TTS) are excluded as *targets* for the same reason; a run
#: that started on one can still fall back into this chain.
TTS_FALLBACK_ORDER: tuple[str, ...] = ("xai", "openai", "minimax", "google")

# Substrings of the upstream 400 body. Matched case-insensitively against the
# rendered provider error; the message crosses the isolated-synth-worker
# boundary intact (``ProviderError(remote_message, code=code)``).
_TEXT_NOT_AUDIO_MARKERS: tuple[str, ...] = (
    "should only be used for tts",
    "tried to generate text",
    "missing audio data",
)

# Never treat these as content-shaped even if a body happens to also carry one
# of the markers above — an auth or quota failure must fail fast.
_FAIL_FAST_MARKERS: tuple[str, ...] = (
    " 401",
    " 403",
    " 429",
    "unauthorized",
    "permission denied",
    "api key",
    "quota",
    "rate limit",
)

# Gemini treats a leading natural-language directive as style guidance and does
# not speak it (that is how "Say cheerfully: …" works upstream). Used ONLY on
# the retry of an attempt that already failed, so the happy path is untouched
# and a mis-spoken prefix can only ever replace a hard failure.
_GEMINI_READ_ALOUD_PREFIX = "Read the following text aloud verbatim: "

# Providers render failures as "<label> HTTP <status>: <body>".
_HTTP_STATUS_RE = re.compile(r"HTTP\s+(\d{3})\b")


@dataclass(frozen=True)
class SynthAttempt:
    """One rung of the ladder: who to ask, with what text, and why."""

    provider: str
    text: str
    hardened: bool = False
    step: str = "primary"  # primary | retry | fallback


@dataclass(frozen=True)
class FallbackStep:
    """One hop: a rung that failed, and the rung being tried next.

    ``from_provider`` / ``error_class`` / ``error`` describe the **failure**;
    ``to_provider`` / ``step`` / ``hardened`` describe the **next attempt**.
    Never contains audio.
    """

    from_provider: str
    to_provider: str
    step: str
    hardened: bool
    error_class: str
    error: str

    def as_meta(self) -> dict[str, Any]:
        return {
            "from_provider": self.from_provider,
            "to_provider": self.to_provider,
            "step": self.step,
            "hardened": self.hardened,
            "error_class": self.error_class,
            "error": self.error,
        }


def classify_tts_error(exc: BaseException) -> str | None:
    """Name the fallback-eligible class of *exc*, or ``None`` to fail fast."""
    from hark.providers.base import ProviderUnsupported, safe_exception_detail

    if isinstance(exc, ProviderUnsupported):
        # A provider that structurally cannot do TTS will not do it on retry.
        return None
    # Generous limit: classify on the whole upstream body, not on whatever
    # survives the default operator-facing truncation.
    detail = safe_exception_detail(exc, fallback="", limit=4000).lower()
    if not detail:
        return None
    if any(marker in detail for marker in _FAIL_FAST_MARKERS):
        return None
    if any(marker in detail for marker in _TEXT_NOT_AUDIO_MARKERS):
        return TEXT_INSTEAD_OF_AUDIO
    return None


def is_fallback_eligible(exc: BaseException) -> bool:
    """True when *exc* is one of the narrow classes worth retrying elsewhere."""
    return classify_tts_error(exc) is not None


def tts_error_http_status(exc: BaseException) -> int | None:
    """HTTP status embedded in a provider error message, when there is one.

    Every TTS provider renders failures as ``… HTTP <status>: <body>``, so the
    status is recoverable without a typed error. Used only to decide whether a
    classification *refusal* is worth surfacing.
    """
    from hark.providers.base import safe_exception_detail

    match = _HTTP_STATUS_RE.search(safe_exception_detail(exc, fallback="", limit=4000))
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - the regex only matches digits
        return None


def harden_tts_text(
    provider: str,
    text: str,
    *,
    language: str | None = None,
) -> str | None:
    """Request-shaping variant of *text* for a retry, or ``None`` if none exists.

    Best-effort half of B177. Returns ``None`` for providers with no known
    shaping lever, which leaves the retry an identical request — still worth
    one attempt, because the upstream failure is nondeterministic.

    The directive is English, so it is only used for English output; injecting
    it into another language risks it being spoken rather than obeyed.
    """
    name = (provider or "").lower().strip()
    if name not in ("google", "gemini"):
        return None
    lang = (language or "en").lower().strip()
    if lang and not (lang == "en" or lang.startswith("en-") or lang.startswith("en_")):
        return None
    if text.startswith(_GEMINI_READ_ALOUD_PREFIX):
        return None
    return _GEMINI_READ_ALOUD_PREFIX + text


def plan_synth_attempts(
    provider: str,
    text: str,
    *,
    chain: Sequence[str] = (),
    language: str | None = None,
) -> list[SynthAttempt]:
    """Ladder for one chunk: primary, one same-provider retry, then *chain*.

    The retry comes before any fallback because the failure is nondeterministic:
    the cheapest fix is asking the provider the operator actually chose again.
    """
    attempts = [SynthAttempt(provider=provider, text=text, step="primary")]
    hardened = harden_tts_text(provider, text, language=language)
    attempts.append(
        SynthAttempt(
            provider=provider,
            text=hardened if hardened is not None else text,
            hardened=hardened is not None,
            step="retry",
        )
    )
    for name in chain:
        if name == provider:
            continue
        attempts.append(SynthAttempt(provider=name, text=text, step="fallback"))
    return attempts


def _tts_cfg_without(tts_cfg: Any | None, skip: set[str]) -> Any:
    """A ``[tts]`` view with *skip* added to ``disabled`` (never mutates input)."""
    import dataclasses

    from hark.config import TtsConfig

    base = tts_cfg
    if base is None:
        try:
            from hark.config import load_config

            base = load_config().tts
        except Exception:
            base = TtsConfig()
    disabled = sorted(set(getattr(base, "disabled", None) or []) | skip)
    try:
        return dataclasses.replace(base, disabled=disabled)
    except Exception:
        # Not a dataclass (a test double): only the policy fields are read.
        return TtsConfig(
            disabled=disabled,
            minimax_ok=bool(getattr(base, "minimax_ok", False)),
        )


def fallback_provider_chain(
    current: str,
    *,
    voice: str | None = None,
    language: str | None = None,
    tts_cfg: Any | None = None,
    config_path: Any | None = None,
) -> list[str]:
    """Providers after *current* that ``auto`` would pick, in its own order.

    Built by re-running ``resolve_tts("auto")`` with each already-tried provider
    added to ``disabled``, rather than by reimplementing the walk. That is what
    keeps credential discovery, the operator's disabled list, and MiniMax
    consent identical to plain ``auto`` — pinning names directly would not, since
    an explicit pin skips the availability check and would hand back a provider
    with no credentials, turning a content-shaped 400 into an auth error.

    ``allow_prompt`` is **False**: a fallback must never block on a TTY consent
    prompt while the operator is already waiting mid-turn.
    """
    from hark.providers.base import ProviderError
    from hark.providers.resolve import _normalize_provider_name, resolve_tts

    chain: list[str] = []
    skip = {_normalize_provider_name(current)} if current else set()
    for _ in range(len(TTS_FALLBACK_ORDER)):
        try:
            provider = resolve_tts(
                "auto",
                voice=voice,
                language=language,
                tts_cfg=_tts_cfg_without(tts_cfg, skip),
                allow_prompt=False,
                config_path=config_path,
            )
        except ProviderError:
            break
        except Exception:
            break
        name = _normalize_provider_name(getattr(provider, "name", "") or "")
        if not name or name in skip:
            break
        chain.append(name)
        skip.add(name)
    return chain


def synthesize_with_fallback(
    *,
    provider: str,
    text: str,
    call: Callable[[str, str], _R],
    chain: Callable[[], Sequence[str]] | None = None,
    on_step: Callable[[FallbackStep], None] | None = None,
    enabled: bool = True,
    language: str | None = None,
) -> tuple[_R, list[FallbackStep]]:
    """Run *call* down the ladder; return its result plus the steps taken.

    ``call(provider, text)`` performs one synthesis. ``chain`` is invoked at
    most once, and only once the same-provider retry is spent, so neither the
    happy path nor a retry-rescued run pays a credential-probe cost.

    An ineligible failure on the **first** rung fails fast, unchanged. Past
    that, an ineligible failure only disqualifies that fallback target and the
    walk continues — ``auto`` picks on credential presence, not viability, so a
    stale key sitting in the chain must not be able to truncate it.

    When every rung fails, the **first** exception is re-raised: that is the
    real trigger, and the last provider's unrelated complaint would misdirect
    the operator. The exhausted ladder is attached to it as
    ``tts_fallback_steps``, and the last provider asked as
    ``tts_last_provider``.
    """
    from hark.providers.base import safe_exception_detail

    if not enabled:
        return call(provider, text), []

    steps: list[FallbackStep] = []
    first_exc: BaseException | None = None
    attempts = plan_synth_attempts(provider, text, language=language)
    resolved_chain: list[str] | None = None
    index = 0
    last_provider = provider

    while index < len(attempts):
        attempt = attempts[index]
        index += 1
        last_provider = attempt.provider
        try:
            return call(attempt.provider, attempt.text), steps
        except BaseException as exc:  # noqa: BLE001 - classified below
            if not isinstance(exc, Exception):
                # KeyboardInterrupt / SystemExit / CancelledError are never a
                # provider verdict, whichever rung they land on. Checked before
                # classification so the invariant holds by construction rather
                # than by no marker happening to appear in their str().
                raise
            error_class = classify_tts_error(exc)
            if first_exc is None:
                # The chosen provider's OWN first failure decides fail-fast: a
                # bad token or exhausted quota there must surface immediately
                # rather than be papered over by a provider switch (B175).
                if error_class is None:
                    raise
                first_exc = exc
            # Past the first rung, ANY provider failure only means that
            # fallback target is unusable — `auto` resolves on credential
            # presence, not viability, so a present-but-revoked key can sit in
            # the chain. Keep walking; the original trigger is still what gets
            # re-raised if nothing works.
            if index >= len(attempts) and resolved_chain is None and chain is not None:
                # Resolve the chain only once the same-provider retry is spent,
                # so a run the retry rescues never pays for credential probes
                # while the operator waits. The ladder's first two rungs are
                # prefix-stable, so the index still points at the next
                # unattempted rung after re-planning.
                resolved_chain = list(chain())
                attempts = plan_synth_attempts(
                    provider,
                    text,
                    chain=resolved_chain,
                    language=language,
                )
            if index >= len(attempts):
                break
            step = FallbackStep(
                from_provider=attempt.provider,
                to_provider=attempts[index].provider,
                step=attempts[index].step,
                hardened=attempts[index].hardened,
                error_class=error_class or NOT_RETRYABLE,
                error=safe_exception_detail(exc),
            )
            steps.append(step)
            if on_step is not None:
                on_step(step)

    if first_exc is None:  # pragma: no cover - unreachable: the ladder is >=2 rungs
        # Not an `assert`: `python -O` strips those, and the stripped form
        # would fall through to `raise None` -> TypeError.
        raise RuntimeError("TTS fallback ladder ended with no attempt and no error")
    try:
        setattr(first_exc, "tts_fallback_steps", [s.as_meta() for s in steps])
        # Attribution for the usage/audit trail: the ladder ends without
        # emitting a step for its final rung, so the caller cannot otherwise
        # tell who was asked last.
        setattr(first_exc, "tts_last_provider", last_provider)
    except BaseException:
        pass
    raise first_exc

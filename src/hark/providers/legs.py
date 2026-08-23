"""Per-leg TTS/STT provider selection (B187).

A speak-then-listen turn has two independent provider legs. MiniMax is
TTS-only, xAI/OpenAI/Google do both, and Custom STT / Custom TTS are separate
gateways — so a single provider name cannot describe both legs.

Selection rules (see ``docs/PROVIDERS.md`` § Per-leg provider selection):

* ``--provider`` / ``provider=`` remains a **both-legs shorthand**. Narrowing
  it to TTS only would silently change the meaning of existing invocations.
* ``--tts-provider`` / ``--stt-provider`` win for their own leg when given.
* ``listen`` is STT-only, so there the two knobs name the same leg; supplying
  both is not an error (the specific flag still wins).

Blank strings normalize to ``None`` so an empty flag value never pins a
nonexistent provider named ``""``.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["LegProviders", "split_leg_providers"]


def _clean(name: str | None) -> str | None:
    if name is None:
        return None
    stripped = str(name).strip()
    return stripped or None


@dataclass(frozen=True)
class LegProviders:
    """Resolved provider name per leg (``None`` = config / auto)."""

    tts: str | None = None
    stt: str | None = None


def split_leg_providers(
    *,
    provider: str | None = None,
    tts_provider: str | None = None,
    stt_provider: str | None = None,
) -> LegProviders:
    """Fold a both-legs ``provider`` and per-leg overrides into one selection.

    The per-leg value wins whenever it is given, independent of argument or
    command-line order — argparse ``dest`` aliasing would instead make the
    result depend on which flag the operator typed last.
    """
    shared = _clean(provider)
    return LegProviders(
        tts=_clean(tts_provider) or shared,
        stt=_clean(stt_provider) or shared,
    )

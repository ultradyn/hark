"""Answer Window policy: everything the session needs that is not a runtime dep.

Built at the call seam (profile + overrides). Session loops must not re-read
``cfg.ambient`` — streaming / idle clamps live on this object (M6 alignment).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

from hark.listen_end import (
    DEFAULT_CANCEL_PHRASES,
    DEFAULT_END_PHRASES,
    DEFAULT_SOFT_END_PHRASES,
    EndMode,
    parse_end_mode,
)

AnswerWindowProfile = Literal["bound_answer", "post_wake", "confirm"]


@dataclass(frozen=True)
class AnswerWindowPolicy:
    """Frozen inputs for one ``open(policy)`` capture."""

    profile: AnswerWindowProfile
    end_mode: EndMode
    max_listen_s: float
    stream_id: str | None = None
    partial_kind: str = "ambient.partial"
    stt_provider: str | None = None

    # Half-duplex / echo
    last_tts: str | None = None
    post_tts_guard_s: float = 0.0
    already_armed: bool = False
    discard_leading_ms: int = 0

    # Energy gate
    abs_open_db: float = -48.0
    open_margin_db: float = 8.0
    initial_timeout_s: float = 45.0
    pre_roll_ms: int = 300
    mute_edge_pad_ms: int = 300
    lead_in_ms: int = 0
    arm_cue: bool = False

    # Silence recovery
    no_open_retry: bool = True
    no_open_nudge: bool = True
    no_open_nudge_text: str = "I didn't catch that."
    empty_stt_retry: bool = True
    empty_stt_nudge: bool = True

    # Endpointing (silence)
    endpoint_strategy_name: str = "energy"
    smart_turn_model_path: str | None = None
    smart_turn_threshold: float | None = None
    endpoint_probe_silence_s: float = 0.4
    endpoint_max_silence_s: float = 6.0

    # Radio product knobs
    stream_partials: bool = True
    radio_partial_silence_s: float = 0.6
    radio_segment_overlap_ms: int = 300
    radio_segment_pad_ms: int = 250
    radio_idle_end_silence_s: float = 0.0  # 0 → derive 3× end_silence_s
    end_silence_s: float = 2.1
    end_phrases: tuple[str, ...] = DEFAULT_END_PHRASES
    cancel_phrases: tuple[str, ...] = DEFAULT_CANCEL_PHRASES
    soft_end_phrases: tuple[str, ...] = DEFAULT_SOFT_END_PHRASES
    soft_end_phrases_enabled: bool = True
    strip_phrase: bool = True

    # Streaming / idle — policy fields only (no ambient leak in session)
    streaming: bool = False
    streaming_ack_min_quiet_s: float = 2.0
    suppress_stop_cue: bool | None = None  # None → True when streaming

    # Media duck during STT
    duck_media_during_stt: bool = True
    pause_media_during_stt: bool = False

    def stop_cue_suppressed(self) -> bool:
        if self.suppress_stop_cue is not None:
            return bool(self.suppress_stop_cue)
        return bool(self.streaming)


def effective_radio_idle_s(policy: AnswerWindowPolicy) -> float:
    """Post-speech quiet before radio auto-finish (B074 + B112 streaming clamp).

    Classic radio uses ``radio_idle_end_silence_s`` (default 3× ``end_silence_s``).
    With ``policy.streaming``, idle is clamped toward
    ``max(end_silence_s, streaming_ack_min_quiet_s)`` without raising the window.
    """
    idle = float(policy.radio_idle_end_silence_s or 0.0)
    if idle <= 0:
        idle = 3.0 * float(policy.end_silence_s or 2.1)
    if not policy.streaming:
        return idle
    floor = max(
        float(policy.end_silence_s or 0.0),
        float(policy.streaming_ack_min_quiet_s or 0.0),
    )
    if floor <= 0:
        return idle
    # Prefer the tighter window so streaming finals land after a natural pause.
    return min(idle, floor) if idle > 0 else floor


def _profile_streaming_default(
    profile: AnswerWindowProfile, ambient_streaming: bool
) -> bool:
    """bound_answer / confirm: streaming off unless overridden; post_wake may inherit ambient."""
    if profile == "post_wake":
        return bool(ambient_streaming)
    return False


def policy_from_config(
    cfg: Any,
    profile: AnswerWindowProfile = "bound_answer",
    **overrides: Any,
) -> AnswerWindowPolicy:
    """Build policy from config at the call seam.

    ``**overrides`` are field names of :class:`AnswerWindowPolicy` only.
    Ambient streaming is read **here** (once), never inside a session loop.
    """
    listen = getattr(cfg, "listen", None)
    audio = getattr(cfg, "audio", None)
    ambient = getattr(cfg, "ambient", None)
    stt = getattr(cfg, "stt", None)

    end_mode_raw = overrides.pop("end_mode", None)
    if end_mode_raw is None:
        end_mode_raw = getattr(listen, "end_mode", "silence") if listen else "silence"
    if isinstance(end_mode_raw, EndMode):
        end_mode = end_mode_raw
    else:
        end_mode = parse_end_mode(str(end_mode_raw))

    ambient_streaming = bool(getattr(ambient, "streaming", False)) if ambient else False
    streaming_default = _profile_streaming_default(profile, ambient_streaming)

    def _g(obj: Any, name: str, default: Any) -> Any:
        if obj is None:
            return default
        return getattr(obj, name, default)

    soft = _g(listen, "soft_end_phrases", DEFAULT_SOFT_END_PHRASES)
    if soft is None:
        soft = DEFAULT_SOFT_END_PHRASES

    base = AnswerWindowPolicy(
        profile=profile,
        end_mode=end_mode,
        max_listen_s=float(_g(listen, "max_listen_s", 120.0)),
        stt_provider=getattr(stt, "provider", None) if stt else None,
        abs_open_db=float(_g(listen, "abs_open_db", -48.0)),
        open_margin_db=float(_g(listen, "open_margin_db", 8.0)),
        initial_timeout_s=float(_g(listen, "initial_timeout_s", 45.0)),
        pre_roll_ms=int(_g(listen, "pre_roll_ms", 300) or 300),
        mute_edge_pad_ms=int(_g(audio, "mute_edge_pad_ms", 300) or 300),
        no_open_retry=bool(_g(listen, "no_open_retry", True)),
        no_open_nudge=bool(_g(listen, "no_open_nudge", True)),
        empty_stt_retry=bool(_g(listen, "empty_stt_retry", True)),
        empty_stt_nudge=bool(_g(listen, "empty_stt_nudge", True)),
        endpoint_strategy_name=str(_g(listen, "endpoint_strategy", "energy") or "energy"),
        smart_turn_model_path=_g(listen, "smart_turn_model_path", None),
        smart_turn_threshold=_g(listen, "smart_turn_threshold", None),
        endpoint_probe_silence_s=float(_g(listen, "endpoint_probe_silence_s", 0.4)),
        endpoint_max_silence_s=float(_g(listen, "endpoint_max_silence_s", 6.0)),
        stream_partials=bool(_g(listen, "stream_partials", True)),
        radio_partial_silence_s=float(_g(listen, "radio_partial_silence_s", 0.6)),
        radio_segment_overlap_ms=int(_g(listen, "radio_segment_overlap_ms", 300) or 0),
        radio_segment_pad_ms=int(_g(listen, "radio_segment_pad_ms", 250) or 0),
        radio_idle_end_silence_s=float(_g(listen, "radio_idle_end_silence_s", 0.0) or 0.0),
        end_silence_s=float(_g(listen, "end_silence_s", 2.1)),
        end_phrases=tuple(_g(listen, "end_phrases", DEFAULT_END_PHRASES) or DEFAULT_END_PHRASES),
        cancel_phrases=tuple(
            _g(listen, "cancel_phrases", DEFAULT_CANCEL_PHRASES) or DEFAULT_CANCEL_PHRASES
        ),
        soft_end_phrases=tuple(soft),
        soft_end_phrases_enabled=bool(_g(listen, "soft_end_phrases_enabled", True)),
        strip_phrase=bool(_g(listen, "strip_phrase", True)),
        streaming=streaming_default,
        streaming_ack_min_quiet_s=float(
            _g(ambient, "streaming_ack_min_quiet_s", 2.0) or 2.0
        ),
        duck_media_during_stt=bool(_g(audio, "duck_media_during_stt", True)),
        pause_media_during_stt=bool(_g(audio, "pause_media_during_stt", False)),
        arm_cue=bool(_g(audio, "answer_arm_cue", False)) if profile == "bound_answer" else False,
    )

    # Profile-specific defaults applied before explicit overrides.
    if profile == "post_wake" and ambient is not None:
        post_abs = getattr(ambient, "post_wake_abs_open_db", None)
        if post_abs is not None:
            base = replace(base, abs_open_db=float(post_abs))
        post_timeout = getattr(ambient, "post_wake_timeout_s", None)
        if post_timeout is not None:
            base = replace(base, initial_timeout_s=float(post_timeout))
        base = replace(
            base,
            lead_in_ms=int(getattr(ambient, "post_wake_lead_in_ms", 150) or 0),
            arm_cue=bool(getattr(ambient, "post_wake_arm_cue", True)),
            no_open_nudge=bool(getattr(ambient, "post_wake_no_open_nudge", True)),
            no_open_nudge_text=str(
                getattr(
                    ambient,
                    "post_wake_no_open_tts",
                    "I heard the wake but not your prompt.",
                )
                or "I heard the wake but not your prompt."
            ),
        )
    elif profile == "confirm":
        # Confirm turns are short; streaming stays off (profile default).
        pass

    if overrides:
        base = replace(base, **overrides)
    return base

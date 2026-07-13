"""Local DSP: capture, gate, playback (no local neural dictation)."""

from hark.audio.capture import (
    ContinuousMicStream,
    MicLease,
    PcmRingBuffer,
    capture_utterance,
    clamp_pre_roll_ms,
    record_seconds,
    score_window_plan,
)
from hark.audio.media import (
    DuckState,
    MediaMatch,
    detect_media,
    duck_media,
    duck_media_during,
    is_media_active,
)
from hark.audio.playback import play_wav_bytes, write_wav

__all__ = [
    "ContinuousMicStream",
    "DuckState",
    "MicLease",
    "MediaMatch",
    "PcmRingBuffer",
    "capture_utterance",
    "clamp_pre_roll_ms",
    "detect_media",
    "duck_media",
    "duck_media_during",
    "is_media_active",
    "record_seconds",
    "score_window_plan",
    "play_wav_bytes",
    "write_wav",
]

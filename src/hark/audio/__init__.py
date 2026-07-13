"""Local DSP: capture, gate, playback (no local neural dictation)."""

from hark.audio.capture import MicLease, capture_utterance, record_seconds
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
    "DuckState",
    "MicLease",
    "MediaMatch",
    "capture_utterance",
    "detect_media",
    "duck_media",
    "duck_media_during",
    "is_media_active",
    "record_seconds",
    "play_wav_bytes",
    "write_wav",
]

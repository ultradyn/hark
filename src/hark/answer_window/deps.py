"""Injectable runtime dependencies for Answer Window (test fakes welcome)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class AnswerWindowDeps:
    """Hardware / provider / control seams — not part of the product policy."""

    stt: Any | None = None
    capture: Any | None = None
    clock: Callable[[], float] = field(default_factory=lambda: time.monotonic)
    sleep: Callable[[float], None] = field(default_factory=lambda: time.sleep)
    on_partial: Callable[[dict[str, Any]], None] | None = None
    audio_ok_after: Callable[[], float | None] | None = None
    play_record_start: Callable[[], None] | None = None
    play_record_stop: Callable[[], None] | None = None
    poll_listen_action: Callable[[str], str | None] | None = None
    consume_listen_action: Callable[[str], str | None] | None = None
    clear_active_listen: Callable[[str], None] | None = None
    register_active_listen: Callable[..., None] | None = None
    touch_voice_activity: Callable[..., None] | None = None
    duck_media: Any | None = None
    run_tts_nudge: Callable[..., None] | None = None
    syslog: Callable[..., None] | None = None
    usage_store: Any | None = None
    endpoint_strategy: Any | None = None
    # Temporary until E4.T002 fully injects STT/cues/duck without config.
    cfg: Any | None = None

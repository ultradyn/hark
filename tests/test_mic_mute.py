from hark.audio.mic_mute import mic_muted_during_tts


def test_mute_context_disabled_is_noop():
    with mic_muted_during_tts(enabled=False) as st:
        assert st.applied is False


def test_mute_nested_depth():
    # When pactl unavailable or no source, still no crash
    with mic_muted_during_tts(enabled=True) as outer:
        with mic_muted_during_tts(enabled=True) as inner:
            assert inner.source == outer.source

"""B093: echo filter must not wipe radio assembly on short question-echo answers.

Uses the public Answer Window ``echo_overlap`` helper (deep seam). The private
``_echo_overlap`` alias remains for speech back-compat only.
"""

from __future__ import annotations

from hark.answer_window import echo_overlap, join_radio_stt_segments


def test_bitlocker_one_word_not_echo():
    """Dogfood: user answers 'BitLocker.' after prompt that contains BitLocker."""
    tts = (
        "Please answer: one, Windows version. Two, BitLocker on, off, or unsure? "
        "Three, do you have a local admin account?"
    )
    assert echo_overlap("BitLocker.", tts) is False
    assert echo_overlap("BitLocker on.", tts) is False
    assert echo_overlap("on", tts) is False


def test_windows_answer_not_echo():
    tts = (
        "I'm on backup tuf. Please answer Windows version — ten or eleven, Home or Pro? "
        "Two, BitLocker. Three, local admin."
    )
    ans = "For one, I think it's Windows 10 Pro or Windows 11 Pro. I need to check."
    assert echo_overlap(ans, tts) is False


def test_long_residual_tts_still_echo():
    """A long re-reading of the prompt should still be treated as echo."""
    tts = (
        "Please answer what you know about the laptop state including Windows version "
        "BitLocker encryption local admin disk size free space and dual boot status "
        "when you are ready to continue with the backup plan to the NAS device."
    )
    # Near-full re-speak of the prompt
    assert echo_overlap(tts, tts) is True
    almost = tts[10:-10]
    assert len(almost) >= 40
    assert echo_overlap(almost, tts) is True


def test_join_keeps_windows_and_bitlocker_segments():
    segs = [
        "For one, I think it's Windows 10 Pro or Windows 11 Pro. I need to check.",
        "BitLocker.",
        "I do have local admin.",
    ]
    joined = join_radio_stt_segments(segs)
    assert "Windows" in joined
    assert "BitLocker" in joined
    assert "local admin" in joined

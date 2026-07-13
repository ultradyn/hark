from hark.audio.playback import sniff_audio_format


def test_sniff_wav():
    # minimal RIFF/WAVE header stub
    data = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 8
    assert sniff_audio_format(data) == "wav"


def test_sniff_mp3_adts():
    # xAI-style MPEG ADTS sync
    data = bytes.fromhex("fff3c4c4004e1439") + b"\x00" * 32
    assert sniff_audio_format(data) == "mp3"


def test_sniff_id3():
    assert sniff_audio_format(b"ID3" + b"\x00" * 20) == "mp3"


def test_mp3_not_treated_as_silent_empty():
    # regression: previous code played MP3 as int16 PCM → static
    data = bytes.fromhex("fff3c4c4") + b"\x00" * 100
    assert sniff_audio_format(data) != "pcm"
    assert sniff_audio_format(data) != "wav"

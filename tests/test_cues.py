from hark.audio.cues import (
    RECORD_START_LEAD_SILENCE_MS,
    build_beep_pair,
    ensure_cue_files,
    phrase_slug,
    play_record_start,
    tts_cache_path,
)


def test_beep_is_wav():
    data = build_beep_pair((880, 1175))
    assert data[:4] == b"RIFF"
    assert len(data) > 500


def test_ensure_cue_files(tmp_path, monkeypatch):
    import hark.audio.cues as cues

    monkeypatch.setattr(cues, "CUES_DIR", tmp_path)
    paths = ensure_cue_files()
    assert paths["record_start"].is_file()
    assert paths["record_stop"].is_file()


def test_phrase_slug_and_cache_path():
    assert phrase_slug("Okay.") == "okay"
    p = tts_cache_path("eve", "Okay.")
    assert "eve" in str(p)
    assert p.suffix == ".mp3"


def test_record_start_lead_silence_ms_is_searchable_odd():
    # Dogfood: odd ms so clip/kick-in tuning is easy to find in code/logs.
    assert RECORD_START_LEAD_SILENCE_MS == 117


def test_play_record_start_sleeps_lead_then_plays(monkeypatch):
    import hark.audio.cues as cues

    sleeps: list[float] = []
    played: list[str] = []

    monkeypatch.setattr(cues.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(
        cues, "play_cue", lambda name, **kw: played.append(name)
    )

    play_record_start()
    assert sleeps == [0.117]
    assert played == ["record_start"]

from hark.audio.cues import build_beep_pair, ensure_cue_files, phrase_slug, tts_cache_path


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

from hark.usage import UsageStore, _word_count


def test_word_count():
    assert _word_count("hello world") == 2
    assert _word_count("") == 0


def test_usage_agg(tmp_path):
    store = UsageStore(tmp_path / "u.jsonl")
    store.record_tts(text="hello world", provider="xai", voice="eve", audio_ms=500)
    store.record_tts(text="hi", provider="xai", voice="eve", audio_ms=200)
    store.record_stt(text="yes please", provider="xai", audio_ms=800)
    s = store.summary()
    assert s["tts"]["instances"] == 2
    assert s["tts"]["total_chars"] == len("hello world") + len("hi")
    assert s["tts"]["total_words"] == 3
    assert s["tts"]["total_audio_ms"] == 700
    assert s["stt"]["instances"] == 1
    assert s["stt"]["total_words"] == 2

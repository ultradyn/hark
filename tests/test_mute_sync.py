from hark.audio import mic_mute as mm


def test_mute_sync_tick_alsa_unmute_edge(monkeypatch):
    calls = []

    def snap():
        # After ensure, report OS unmuted
        if calls:
            return False, True
        return True, True  # ALSA on, pulse still muted this tick

    monkeypatch.setattr(mm, "_read_mute_snapshot", snap)
    monkeypatch.setattr(
        mm, "ensure_unmuted", lambda **k: calls.append("ensure") or {}
    )

    # prev ALSA was off → now on triggers cascade
    p, a, did = mm.mute_sync_tick(True, False)
    assert did is True
    assert calls == ["ensure"]
    assert p is False
    assert a is True


def test_mute_sync_tick_pulse_unmute_edge(monkeypatch):
    calls = []

    def snap():
        return False, True

    monkeypatch.setattr(mm, "_read_mute_snapshot", snap)
    monkeypatch.setattr(
        mm, "ensure_unmuted", lambda **k: calls.append("ensure") or {}
    )

    p, a, did = mm.mute_sync_tick(True, True)  # prev pulse muted → now unmuted
    assert did is True
    assert calls == ["ensure"]


def test_mute_sync_tick_no_edge(monkeypatch):
    monkeypatch.setattr(mm, "_read_mute_snapshot", lambda: (False, True))
    monkeypatch.setattr(
        mm, "ensure_unmuted", lambda **k: (_ for _ in ()).throw(AssertionError())
    )
    p, a, did = mm.mute_sync_tick(False, True)
    assert did is False


def test_release_tts_mute_hold(monkeypatch):
    monkeypatch.setattr(mm, "set_source_mute", lambda *a, **k: True)
    monkeypatch.setattr(mm, "set_alsa_mic_capture", lambda *a, **k: True)
    monkeypatch.setattr(mm, "_which", lambda n: n == "pactl")
    monkeypatch.setattr(mm, "default_source", lambda: "src")
    monkeypatch.setattr(mm, "find_wave_alsa_card", lambda: None)
    mm._depth = 1
    mm._saved = mm.MuteState(source="src", was_muted=False, applied=True)
    mm._user_unmuted_override = False
    assert mm.release_tts_mute_hold() is True
    # B086: full clear (depth 0) so listen clocks unfreeze
    assert mm._depth == 0
    assert mm._saved is None
    assert mm._user_unmuted_override is False


def test_find_wave_card_parses(monkeypatch):
    sample = """
**** List of CAPTURE Hardware Devices ****
card 1: Wave3 [Elgato Wave:3], device 0: USB Audio [USB Audio]
"""
    monkeypatch.setattr(
        mm,
        "_run",
        lambda args, **k: type(
            "R", (), {"returncode": 0, "stdout": sample, "stderr": ""}
        )(),
    )
    monkeypatch.setattr(mm, "_which", lambda n: True)
    found = mm.find_wave_alsa_card()
    assert found is not None
    assert found[0] == "Wave3"
    assert found[1] == 1

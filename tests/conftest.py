"""Shared pytest fixtures.

Disable GitHub release self-update checks by default so CI and unit tests never
hit the network (B088). Opt in via ``HARK_UPDATE_CHECK=1`` in specific tests.

Block external audio control by default so the suite never ducks the operator's
volume or pauses their music (B188). Opt in via ``HARK_MEDIA_CONTROL=1``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hark.audio import external_audio


@pytest.fixture
def isolated_state_home(tmp_path: Path) -> Path:
    """Per-test XDG state root; never allow defaults to reach operator state."""
    return tmp_path / "xdg-state"


@pytest.fixture(autouse=True)
def _isolate_xdg_state(
    monkeypatch: pytest.MonkeyPatch,
    isolated_state_home: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_state_home))
    monkeypatch.setenv("HARK_EVENT_PROVENANCE", "test")


@pytest.fixture(autouse=True)
def _disable_update_check_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARK_UPDATE_CHECK", "0")


@pytest.fixture(autouse=True)
def _block_external_audio_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the operator's speakers, players or mic light (B188).

    ``monkeypatch.setenv`` writes ``os.environ``, so this is inherited by every
    subprocess the suite spawns — which is the case that matters. A spawned
    ``hark ask`` cannot see an in-process patch, and by default it ducks media
    (``duck_media_during_stt`` and ``pause_media_during_stt`` are both True), so
    without this the suite really does pause the operator's music.

    A test that needs the real code path can opt in with
    ``monkeypatch.setenv("HARK_MEDIA_CONTROL", "1")`` — and is then responsible
    for stubbing ``subprocess.run`` itself. See :mod:`hark.audio.external_audio`.
    """
    monkeypatch.setenv(external_audio.ENV_FLAG, "0")
    external_audio.reset_blocked_calls()


@pytest.fixture(autouse=True)
def _tripwire_no_mutating_audio_argv(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Fail the test if a mutating audio argv reaches ``subprocess.run``.

    The gate in :mod:`hark.audio.external_audio` covers the call sites that ask
    it. This covers the ones that *forget* to — a new ``pactl set-…`` added
    somewhere that bypasses ``_run_pactl_cmd`` would otherwise silently reach
    the operator's desktop, and the failure would show up as "my music keeps
    pausing" rather than as a red test.

    Deliberate limits, so nobody reads more assurance into this than it gives:

    * In-process only. A mutating command inside a **spawned child** is not
      visible here; the ``HARK_MEDIA_CONTROL`` env var is what covers that.
    * A test that monkeypatches ``subprocess.run`` itself replaces this wrapper
      (last patch wins), so it is not covered — which is harmless, since
      nothing real executes.

    Opt out with ``@pytest.mark.allow_media_control`` when a test genuinely has
    to drive the real argv path.
    """
    if request.node.get_closest_marker("allow_media_control"):
        return

    real_run = subprocess.run

    def guarded(*args, **kwargs):  # type: ignore[no-untyped-def]
        argv = kwargs.get("args") if not args else args[0]
        try:
            mutating = external_audio.is_mutating_argv(
                argv if isinstance(argv, (list, tuple)) else [argv]
            )
        except Exception:
            mutating = False
        if mutating:
            raise AssertionError(
                "B188: a mutating audio command reached subprocess.run during a "
                f"test: {list(argv)!r}. This would change the operator's real "
                "audio state. Route it through hark.audio.media._run_pactl_cmd "
                "or hark.audio.mic_mute._run_mutating (which consult "
                "hark.audio.external_audio), or mark the test with "
                "@pytest.mark.allow_media_control if it must drive the argv "
                "directly."
            )
        try:
            playback = external_audio.is_audio_playback_argv(
                argv if isinstance(argv, (list, tuple)) else [argv]
            )
        except Exception:
            playback = False
        if playback:
            raise AssertionError(
                "B190: an audio player was spawned during a test: "
                f"{list(argv)!r}. This makes real noise on the operator's "
                "machine. Stub the playback layer (hark.audio.playback) or "
                "hark.speech.run_tts, or mark the test with "
                "@pytest.mark.allow_media_control."
            )
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)


@pytest.fixture(autouse=True)
def _tripwire_no_real_audio_device(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Fail the test if playback reaches the real audio device (B190).

    ``playback.py:1338`` plays WAV/PCM through ``sounddevice`` rather than a
    subprocess, so the argv tripwire cannot see it. This covers that path.

    A test that swaps ``hark.audio.playback.sd`` for its own fake bypasses this
    (it patches the name, not the module) — which is fine, since a fake makes no
    sound. Skipped entirely when sounddevice is absent.
    """
    if request.node.get_closest_marker("allow_media_control"):
        return
    try:
        from hark.audio import playback
    except Exception:
        return
    sd = getattr(playback, "sd", None)
    if sd is None:
        return

    def blocked(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(
            "B190: playback reached the real audio device (sounddevice.play) "
            "during a test. This makes real noise on the operator's machine. "
            "Stub hark.audio.playback or hark.speech.run_tts, or mark the test "
            "with @pytest.mark.allow_media_control."
        )

    for name in ("play", "wait"):
        if hasattr(sd, name):
            monkeypatch.setattr(sd, name, blocked, raising=False)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "allow_media_control: test drives real audio-control or playback argvs; "
        "exempt from the B188 and B190 tripwires",
    )

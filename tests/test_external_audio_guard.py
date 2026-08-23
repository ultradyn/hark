"""B188: external audio control is gated so tests never touch the operator.

The bug this pins: agents running the suite were pausing the operator's media
and changing his volume, because ``duck_media_during_stt`` /
``pause_media_during_stt`` default to True and nothing stopped a spawned
``hark`` child from acting on them.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from hark.audio import external_audio, media, mic_mute
from hark.audio.external_audio import (
    ENV_FLAG,
    is_mutating_argv,
    media_control_blocked,
)


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------


def test_blocked_under_pytest_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the flag absent, an in-process test still must not actuate audio."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    assert media_control_blocked() == "pytest detected"


def test_conftest_sets_the_env_var_so_children_inherit_it() -> None:
    """The autouse fixture must use os.environ, not just an in-process patch.

    This is the load-bearing layer: a spawned ``hark ask`` sees the environment
    and nothing else.
    """
    assert os.environ.get(ENV_FLAG) == "0"


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "OFF", " 0 "])
def test_falsy_values_block(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv(ENV_FLAG, val)
    assert media_control_blocked() is not None


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "ON"])
def test_truthy_values_allow_even_under_pytest(
    monkeypatch: pytest.MonkeyPatch, val: str
) -> None:
    """Explicit opt-in wins, so a test can still drive the real path."""
    monkeypatch.setenv(ENV_FLAG, val)
    assert media_control_blocked() is None


def test_unparseable_value_blocks_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wrong 'allow' costs the operator's audio; a wrong 'block' costs a duck."""
    monkeypatch.setenv(ENV_FLAG, "maybe")
    reason = media_control_blocked()
    assert reason is not None
    assert "unrecognised" in reason


def test_unset_outside_pytest_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production default is unchanged: real Hark runs still duck media."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(external_audio, "_under_pytest", lambda: False)
    assert media_control_blocked() is None


# ---------------------------------------------------------------------------
# argv classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["pactl", "set-sink-input-volume", "3", "40%"],
        ["pactl", "set-source-mute", "alsa_input.x", "1"],
        ["playerctl", "-p", "spotify", "pause"],
        ["playerctl", "-p", "spotify", "play"],
        ["amixer", "-c", "1", "-q", "sset", "Mic", "nocap"],
        ["wpctl", "set-volume", "@DEFAULT_SINK@", "0.4"],
        ["/usr/bin/pactl", "set-sink-volume", "0", "50%"],
    ],
)
def test_mutating_argvs_are_recognised(argv: list[str]) -> None:
    assert is_mutating_argv(argv) is True


@pytest.mark.parametrize(
    "argv",
    [
        ["pactl", "list", "sink-inputs"],
        ["pactl", "get-default-source"],
        ["pactl", "get-source-mute", "alsa_input.x"],
        ["playerctl", "status"],
        ["playerctl", "-p", "spotify", "status"],
        ["playerctl", "-l"],
        ["amixer", "-c", "1", "sget", "Mic"],
        ["arecord", "-l"],
        ["git", "status"],
        [],
    ],
)
def test_read_only_argvs_are_not_mutating(argv: list[str]) -> None:
    """Probes tell Hark what is playing without touching it — never gated."""
    assert is_mutating_argv(argv) is False


# ---------------------------------------------------------------------------
# the call sites actually consult the gate
# ---------------------------------------------------------------------------


def test_set_sink_input_volume_is_blocked_and_records_why() -> None:
    external_audio.reset_blocked_calls()
    assert media.set_sink_input_volume(3, 40.0) is False
    calls = external_audio.blocked_calls()
    assert len(calls) == 1
    assert calls[0].argv[:2] == ("pactl", "set-sink-input-volume")
    assert calls[0].reason


def test_pause_mpris_players_is_blocked() -> None:
    """The operator's music must keep playing through the whole suite.

    ``which_fn`` is forced so the assertion holds on a box without playerctl —
    otherwise the availability check short-circuits and proves nothing.
    """
    external_audio.reset_blocked_calls()
    paused = media.pause_mpris_players(
        ["spotify"], which_fn=lambda _name: "/usr/bin/playerctl"
    )
    assert paused == []
    argvs = [c.argv for c in external_audio.blocked_calls()]
    assert ("playerctl", "-p", "spotify", "pause") in argvs


def test_resume_mpris_players_is_blocked() -> None:
    external_audio.reset_blocked_calls()
    assert media.resume_mpris_players(["spotify"]) == []
    argvs = [c.argv for c in external_audio.blocked_calls()]
    assert ("playerctl", "-p", "spotify", "play") in argvs


def test_set_source_mute_is_blocked() -> None:
    """No flipping the mute light on the operator's microphone."""
    external_audio.reset_blocked_calls()
    assert mic_mute.set_source_mute("alsa_input.example", True) is False
    argvs = [c.argv for c in external_audio.blocked_calls()]
    assert ("pactl", "set-source-mute", "alsa_input.example", "1") in argvs


def test_injected_run_cmd_still_bypasses_the_gate() -> None:
    """Existing tests inject fakes; the gate must not break them.

    ``run_cmd=`` never reaches a real subprocess, so there is nothing to guard.
    """
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> bool:
        seen.append(argv)
        return True

    assert media.set_sink_input_volume(3, 40.0, run_cmd=fake) is True
    assert seen and seen[0][1] == "set-sink-input-volume"


def test_gate_allows_when_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in reaches subprocess — so an opted-in test must stub it itself."""
    monkeypatch.setenv(ENV_FLAG, "1")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert media.set_sink_input_volume(3, 40.0) is True
    assert calls[0][:2] == ["pactl", "set-sink-input-volume"]


# ---------------------------------------------------------------------------
# the tripwire
# ---------------------------------------------------------------------------


def test_tripwire_fails_on_a_bypassing_mutating_argv() -> None:
    """A new call site that skips the gate must show up as a red test."""
    with pytest.raises(AssertionError, match="B188"):
        subprocess.run(["pactl", "set-sink-volume", "0", "10%"], capture_output=True)


def test_tripwire_allows_read_only_argvs() -> None:
    # Does not need the binary to exist — only that the tripwire lets it past.
    try:
        subprocess.run(["pactl", "list", "sink-inputs"], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


@pytest.mark.allow_media_control
def test_tripwire_opt_out_marker_disables_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The marker exempts a test that must drive real argvs."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    subprocess.run(["pactl", "set-sink-volume", "0", "10%"])
    assert calls == [["pactl", "set-sink-volume", "0", "10%"]]


def test_blocked_call_log_is_bounded() -> None:
    external_audio.reset_blocked_calls()
    for i in range(external_audio._MAX_LOGGED + 20):
        external_audio.note_blocked(["pactl", "set-sink-volume", str(i)], "test")
    assert len(external_audio.blocked_calls()) == external_audio._MAX_LOGGED

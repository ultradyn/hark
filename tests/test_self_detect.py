"""Unit tests for self-detection (B029): hark running inside a herdr pane."""

from __future__ import annotations

from hark.herdr.client import AgentInfo
from hark.self_detect import SelfIdentity, detect_self


def _agent(pane_id: str, session_id: str = "local") -> AgentInfo:
    return AgentInfo(
        session_id=session_id,
        pane_id=pane_id,
        agent="claude",
        status="blocked",
    )


# --- detect_self -----------------------------------------------------------


def test_detect_self_none_outside_herdr():
    assert detect_self({}) is None
    # HERDR_ENV unset but pane id present -> still not self.
    assert detect_self({"HERDR_PANE_ID": "wG:p3"}) is None


def test_detect_self_reads_env():
    ident = detect_self(
        {
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": "wG:p3",
            "HERDR_SOCKET_PATH": "/run/herdr.sock",
            "HERDR_SESSION": "work",
        }
    )
    assert ident == SelfIdentity(
        pane_id="wG:p3", socket_path="/run/herdr.sock", session="work"
    )
    assert ident.target == "work/wG:p3"


def test_detect_self_requires_pane_id():
    assert detect_self({"HERDR_ENV": "1"}) is None
    assert detect_self({"HERDR_ENV": "1", "HERDR_PANE_ID": "  "}) is None


def test_detect_self_escape_hatch_disables():
    env = {
        "HERDR_ENV": "1",
        "HERDR_PANE_ID": "wG:p3",
        "HARK_WATCH_INCLUDE_SELF": "1",
    }
    assert detect_self(env) is None


def test_detect_self_target_defaults_to_local():
    ident = detect_self({"HERDR_ENV": "1", "HERDR_PANE_ID": "wG:p3"})
    assert ident is not None
    assert ident.session is None
    assert ident.target == "local/wG:p3"


# --- SelfIdentity.matches_agent -------------------------------------------


def test_matches_agent_requires_pane_id_match():
    ident = SelfIdentity(pane_id="wG:p3", socket_path="/run/h.sock")
    assert not ident.matches_agent(
        _agent("wG:p9"), session_socket="/run/h.sock", session_is_remote=False
    )
    assert ident.matches_agent(
        _agent("wG:p3"), session_socket="/run/h.sock", session_is_remote=False
    )


def test_matches_agent_socket_mismatch_is_not_self():
    ident = SelfIdentity(pane_id="wG:p3", socket_path="/run/a.sock")
    assert not ident.matches_agent(
        _agent("wG:p3"), session_socket="/run/b.sock", session_is_remote=False
    )


def test_matches_agent_unknown_socket_local_trusts_pane():
    ident = SelfIdentity(pane_id="wG:p3", socket_path=None)
    assert ident.matches_agent(
        _agent("wG:p3"), session_socket=None, session_is_remote=False
    )
    assert ident.matches_agent(
        _agent("wG:p3"), session_socket="/run/h.sock", session_is_remote=False
    )


def test_matches_agent_unknown_socket_remote_never_self():
    ident = SelfIdentity(pane_id="wG:p3", socket_path=None)
    assert not ident.matches_agent(
        _agent("wG:p3"), session_socket=None, session_is_remote=True
    )
    # Even with a session socket on our side, if self socket unknown and remote:
    assert not ident.matches_agent(
        _agent("wG:p3"), session_socket="/tunnel/h.sock", session_is_remote=True
    )


def test_matches_agent_socket_realpath_equivalent(tmp_path):
    real = tmp_path / "herdr.sock"
    real.write_text("")
    link = tmp_path / "link.sock"
    link.symlink_to(real)
    ident = SelfIdentity(pane_id="wG:p3", socket_path=str(link))
    assert ident.matches_agent(
        _agent("wG:p3"), session_socket=str(real), session_is_remote=False
    )

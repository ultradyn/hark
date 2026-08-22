"""State files hold full HEP; compaction belongs only at the monitor read edge.

The daemon's watch worker owns ``watch.jsonl``, and the dashboard reads that file
back as HEP (docs/DASHBOARD.md) for register-on-demand answering. Compacting at
the write edge breaks that contract and drops the fields the read edge needs.
CONTEXT.md pins compaction to ``present_for_monitor`` at the read edge only.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest

import hark.cli as cli
import hark.daemon as daemon
import hark.dashboard.api as api
import hark.watch as watch
from hark.config import HarkConfig, SessionConfig
from hark.delivery import DeliveryStore
from hark.events import make_agent_status_event, monitor_profile
from hark.herdr.client import AgentInfo
from hark.paths import state_dir
from hark.state_feed.present import present_for_monitor

SESSION = "lab"
PANE = "w1:p6"
QUESTION = "Allow running rm -rf /tmp/scratch?"


class _FakeWatchClient:
    """Herdr stand-in: one blocked agent whose pane shows a menu."""

    def __init__(self, session: SessionConfig) -> None:
        self.session = session

    def socket_exists(self) -> bool:
        return False

    def _agent(self) -> AgentInfo:
        return AgentInfo(
            session_id=self.session.id,
            pane_id=PANE,
            agent="claude",
            status="blocked",
            revision=3,
        )

    def list_agents(self) -> list[AgentInfo]:
        return [self._agent()]

    def get_agent(self, pane_id: str) -> AgentInfo:
        return self._agent()

    def read_pane(self, pane_id: str, lines: int = 60) -> str:
        return f"{QUESTION}\n1. Yes\n2. No\nReply with a number."


def _daemon_watch_argv(monkeypatch: pytest.MonkeyPatch, root: Path) -> list[str]:
    """The exact watch-worker argv the daemon spawns, captured before fork."""
    captured: list[list[str]] = []

    def spawn(argv, *, _claim, **_kwargs):
        captured.append(list(argv))
        raise OSError("captured before fork")

    monkeypatch.setattr(daemon, "_spawn_owned_popen", spawn)
    with pytest.raises(daemon.WorkerSpawnError):
        daemon.spawn_mode_a_workers(root=root, session=SESSION, do_ambient=False)
    assert captured, "daemon did not attempt to spawn the watch worker"
    argv = captured[0]
    return argv[argv.index("watch") :]


def _run_watch_worker(
    monkeypatch: pytest.MonkeyPatch, argv_tail: list[str]
) -> tuple[Path, list[dict]]:
    """Run the captured worker argv (plus ``--once``) into ``watch.jsonl``."""
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda *_a, **_k: HarkConfig(sessions=[SessionConfig(id=SESSION)]),
    )
    monkeypatch.setattr(watch, "HerdrClient", _FakeWatchClient)
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "watch.jsonl"
    with path.open("w", encoding="utf-8") as fh, contextlib.redirect_stdout(fh):
        assert cli.main([*argv_tail, "--once"]) == 0
    events = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    return path, events


def _blocked_hep() -> dict:
    agent = AgentInfo(
        session_id=SESSION,
        pane_id=PANE,
        agent="claude",
        status="blocked",
        revision=3,
    )
    return make_agent_status_event(
        agent,
        from_status="working",
        to_status="blocked",
        question_text=QUESTION,
    )


def test_daemon_watch_worker_writes_uncompacted_hep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    argv = _daemon_watch_argv(monkeypatch, tmp_path)

    assert argv[:3] == ["watch", "--session", SESSION]
    assert "--statuses" in argv
    # watch.jsonl is a state file, not a Monitor stdout: it must stay full HEP.
    assert "--for-monitor" not in argv


def test_watch_state_file_round_trips_through_dashboard_answer_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    argv = _daemon_watch_argv(monkeypatch, tmp_path)
    _, events = _run_watch_worker(monkeypatch, argv)

    blocked = next(e for e in events if e["kind"] == "agent.blocked")
    hep = api.find_hep_event(blocked["event_id"])
    assert hep is not None

    bound = DeliveryStore().register_from_hep(hep)
    assert bound.event_id == blocked["event_id"]
    assert bound.session_id == SESSION
    assert bound.pane_id == PANE
    assert bound.question_fingerprint
    assert bound.question_text and QUESTION in bound.question_text
    assert bound.risk


def test_pending_question_panel_reads_choices_from_watch_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    argv = _daemon_watch_argv(monkeypatch, tmp_path)
    _, events = _run_watch_worker(monkeypatch, argv)
    blocked = next(e for e in events if e["kind"] == "agent.blocked")

    monkeypatch.setattr(api, "HerdrClient", _FakeWatchClient)
    cfg = HarkConfig(sessions=[SessionConfig(id=SESSION)])
    snapshot = api.context_snapshot(cfg, SESSION, PANE)

    pending = snapshot["pending_question"]
    assert pending is not None
    assert pending["event_id"] == blocked["event_id"]
    assert pending["fingerprint"]
    assert QUESTION in (pending["text"] or "")


def test_monitor_compact_of_full_hep_keeps_every_answerable_field():
    """Pin the single-pass monitor line: the read edge is the only compaction."""
    compact = present_for_monitor(_blocked_hep())

    assert compact["kind"] == "agent.blocked"
    assert compact["session_id"] == SESSION
    assert compact["pane_id"] == PANE
    assert compact["agent"] == "claude"
    assert compact["status_to"] == "blocked"
    assert compact["question"] == QUESTION
    assert compact["fingerprint"]
    assert compact["risk"]
    assert "invent" in compact["instructions"].lower()
    assert f"{SESSION}/{PANE}" in compact["instructions"]


def test_legacy_compact_watch_line_still_feeds_the_monitor():
    """Files already on disk hold compact lines; keep reading them (lossily)."""
    legacy = monitor_profile(_blocked_hep())
    again = present_for_monitor(legacy)

    assert again["kind"] == "agent.blocked"
    assert again["event_id"] == legacy["event_id"]
    assert again["question"] == QUESTION
    # A compact line cannot round-trip target/question metadata; that loss is
    # exactly why watch.jsonl now holds full HEP.
    assert "pane_id" not in again


def test_legacy_compact_watch_line_does_not_crash_the_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    legacy = monitor_profile(_blocked_hep())
    root = state_dir()
    root.mkdir(parents=True, exist_ok=True)
    (root / "watch.jsonl").write_text(
        json.dumps(legacy, separators=(",", ":")) + "\n", encoding="utf-8"
    )

    hep = api.find_hep_event(legacy["event_id"])
    assert hep is not None

    # question is a bare string here; registering must degrade, not raise.
    bound = DeliveryStore().register_from_hep(hep)
    assert bound.event_id == legacy["event_id"]
    assert bound.session_id == SESSION
    assert bound.question_text == QUESTION

    monkeypatch.setattr(api, "HerdrClient", _FakeWatchClient)
    cfg = HarkConfig(sessions=[SessionConfig(id=SESSION)])
    snapshot = api.context_snapshot(cfg, SESSION, bound.pane_id or PANE)
    assert snapshot["ok"] is True

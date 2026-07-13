"""B009 criteria 1 & 3: queue count TTS announcement and no cross-pane merge."""

import argparse
import json
import time

import hark.cli as cli
import hark.speech as speech
from hark.delivery import (
    BoundEvent,
    DeliveryStore,
    queue_announcement,
    summarize_pending,
)
from hark.herdr.client import AgentInfo


# ---------------------------------------------------------------------------
# Criterion 1: TTS announces N waiting
# ---------------------------------------------------------------------------


def _seed(store: DeliveryStore, event_id, session_id, pane_id):
    store.save_event(
        BoundEvent(
            event_id=event_id,
            session_id=session_id,
            pane_id=pane_id,
            pane_revision=1,
            question_fingerprint=f"blake2b:{event_id}",
        )
    )


def test_pending_events_excludes_resolved(tmp_path):
    store = DeliveryStore(tmp_path / "events.jsonl")
    _seed(store, "a", "local", "w1:p1")
    _seed(store, "b", "local", "w1:p2")
    _seed(store, "c", "work", "w1:p1")
    store.mark("b", "delivered")
    store.mark("c", "skipped")  # skip/next must drop it from the queue

    pending = {e["event_id"] for e in store.pending_events()}
    assert pending == {"a"}


def test_summarize_counts_distinct_targets():
    pending = [
        {"event_id": "a", "session_id": "local", "pane_id": "w1:p1"},
        {"event_id": "b", "session_id": "work", "pane_id": "w1:p1"},
        # duplicate target must not inflate the count
        {"event_id": "c", "session_id": "local", "pane_id": "w1:p1"},
    ]
    summary = summarize_pending(pending)
    assert summary["count"] == 2
    assert set(summary["targets"]) == {"local/w1:p1", "work/w1:p1"}


def test_queue_announcement_wording():
    assert "No agents" in queue_announcement(0)
    assert queue_announcement(1) == "One agent is waiting for input."
    assert queue_announcement(2) == "2 agents are waiting for input."
    assert queue_announcement(5) == "5 agents are waiting for input."


def _queue_args(announce=False, **extra):
    base = dict(
        json=True,
        announce=announce,
        all=False,
        prune=False,
        live=False,
        offline=True,  # unit tests avoid Herdr; age/supersede still apply
        max_age=None,
    )
    base.update(extra)
    return argparse.Namespace(**base)


def test_cmd_queue_announce_speaks_when_more_than_one(tmp_path, monkeypatch, capsys):
    store = DeliveryStore(tmp_path / "events.jsonl")
    _seed(store, "a", "local", "w1:p1")
    _seed(store, "b", "work", "w1:p2")
    monkeypatch.setattr(cli, "DeliveryStore", lambda: store)

    spoken = []
    monkeypatch.setattr(
        speech, "run_tts", lambda cfg, text, **kw: spoken.append(text) or {"ok": True}
    )

    rc = cli.cmd_queue(_queue_args(announce=True), cfg=object())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 2
    assert out["announced"] is True
    assert spoken == ["2 agents are waiting for input."]


def test_cmd_queue_announce_silent_when_one(tmp_path, monkeypatch, capsys):
    store = DeliveryStore(tmp_path / "events.jsonl")
    _seed(store, "a", "local", "w1:p1")
    monkeypatch.setattr(cli, "DeliveryStore", lambda: store)

    spoken = []
    monkeypatch.setattr(
        speech, "run_tts", lambda cfg, text, **kw: spoken.append(text) or {"ok": True}
    )

    rc = cli.cmd_queue(_queue_args(announce=True), cfg=object())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["announced"] is False
    assert spoken == []  # only announce when more than one is waiting


# ---------------------------------------------------------------------------
# Criterion 3: no cross-pane merge
# ---------------------------------------------------------------------------


class RecordingClient:
    """Per-session fake client; records which pane received text."""

    def __init__(self, session_id):
        self.session_id = session_id
        self.sent = []

    def get_agent(self, pane_id):
        return AgentInfo(
            session_id=self.session_id,
            pane_id=pane_id,
            agent="codex",
            status="blocked",
            revision=1,
        )

    def read_pane(self, pane_id, lines=60):
        return "Allow this action?"

    def send_text(self, pane_id, text):
        self.sent.append((pane_id, text))

    def send_keys(self, pane_id, keys):
        self.sent.append((pane_id, keys))


def test_answer_delivers_only_to_bound_pane(tmp_path, monkeypatch):
    """Answering event A must never bleed into pane B or another session."""
    store = DeliveryStore(tmp_path / "events.jsonl")
    _seed(store, "evtA", "local", "w1:p1")
    _seed(store, "evtB", "work", "w9:p9")

    clients = {"local": RecordingClient("local"), "work": RecordingClient("work")}
    monkeypatch.setattr(cli, "DeliveryStore", lambda: store)
    monkeypatch.setattr(cli, "_client_for", lambda cfg, session_id: clients[session_id])
    # Fingerprint of live pane must equal the bound fingerprint to deliver.
    # (the check lives in the shared answer core since hark serve reuses it)
    import hark.answering as answering
    monkeypatch.setattr(answering, "question_fingerprint", lambda excerpt: "blake2b:evtA")

    args = argparse.Namespace(event_id="evtA", text="use option two", keys=None)
    rc = cli.cmd_answer(args, cfg=object())
    assert rc == 0

    # Only pane A of session 'local' received the answer.
    assert clients["local"].sent == [("w1:p1", "use option two")]
    assert clients["work"].sent == []
    # Event B is untouched and still pending in the queue.
    assert {e["event_id"] for e in store.pending_events()} == {"evtB"}


def test_meta_skip_removes_target_from_queue(tmp_path, monkeypatch):
    """End-to-end: a 'skip' meta-command (via hark skip) drops that event."""
    store = DeliveryStore(tmp_path / "events.jsonl")
    _seed(store, "evtA", "local", "w1:p1")
    _seed(store, "evtB", "work", "w9:p9")
    monkeypatch.setattr(cli, "DeliveryStore", lambda: store)

    rc = cli.cmd_skip(argparse.Namespace(event_id="evtA"))
    assert rc == 0

    # evtA gone from the queue; the other target still waiting and countable.
    remaining = {e["event_id"] for e in store.pending_events()}
    assert remaining == {"evtB"}
    assert summarize_pending(store.pending_events())["count"] == 1


def test_cmd_queue_hides_stale_and_prune_expires(tmp_path, monkeypatch, capsys):
    """B101: queue lists only fresh targets; --prune expires the rest."""
    store = DeliveryStore(tmp_path / "events.jsonl")
    now = time.time()
    store.save_event(
        BoundEvent(
            event_id="stale",
            session_id="local",
            pane_id="w1:p1",
            pane_revision=1,
            question_fingerprint="blake2b:stale",
            created_at=now - 50_000,
        )
    )
    store.save_event(
        BoundEvent(
            event_id="fresh",
            session_id="work",
            pane_id="w1:p2",
            pane_revision=1,
            question_fingerprint="blake2b:fresh",
            created_at=now - 10,
        )
    )
    monkeypatch.setattr(cli, "DeliveryStore", lambda: store)

    rc = cli.cmd_queue(_queue_args(announce=False, max_age=3600), cfg=None)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["event_id"] for e in out["queue"]] == ["fresh"]
    assert out["count"] == 1
    assert out["stale_count"] >= 1
    assert out["pruned"] == []

    rc = cli.cmd_queue(
        _queue_args(announce=False, prune=True, max_age=3600), cfg=None
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert any(p["event_id"] == "stale" for p in out["pruned"])
    assert store.get("stale").status == "expired"
    assert store.get("fresh").status == "pending"


def test_cmd_queue_announce_ignores_stale_only(tmp_path, monkeypatch, capsys):
    """Announce must not claim agents are waiting when only stale remain."""
    store = DeliveryStore(tmp_path / "events.jsonl")
    now = time.time()
    for i, pane in enumerate(("w1:p1", "w1:p2", "w1:p3")):
        store.save_event(
            BoundEvent(
                event_id=f"old{i}",
                session_id="local",
                pane_id=pane,
                pane_revision=1,
                question_fingerprint=f"blake2b:old{i}",
                created_at=now - 50_000,
            )
        )
    monkeypatch.setattr(cli, "DeliveryStore", lambda: store)

    spoken = []
    monkeypatch.setattr(
        speech, "run_tts", lambda cfg, text, **kw: spoken.append(text) or {"ok": True}
    )

    rc = cli.cmd_queue(
        _queue_args(announce=True, max_age=3600), cfg=object()
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 0
    assert out["announced"] is False  # only announce when count > 1
    assert spoken == []
    assert "No agents" in out["announcement"]


def test_cmd_queue_live_soft_filter_drops_idle_panes(tmp_path, monkeypatch, capsys):
    """B101: live soft-filter excludes not-blocked panes from announce/count."""
    store = DeliveryStore(tmp_path / "events.jsonl")
    now = time.time()
    for eid, pane in (("blocked", "w1:p1"), ("idle", "w1:p2")):
        store.save_event(
            BoundEvent(
                event_id=eid,
                session_id="local",
                pane_id=pane,
                pane_revision=1,
                question_fingerprint=f"blake2b:{eid}",
                created_at=now - 10,
            )
        )
    monkeypatch.setattr(cli, "DeliveryStore", lambda: store)

    class FakeClient:
        def get_agent(self, pane_id):
            status = "blocked" if pane_id == "w1:p1" else "idle"
            return AgentInfo(
                session_id="local",
                pane_id=pane_id,
                agent="codex",
                status=status,
                revision=1,
            )

    monkeypatch.setattr(cli, "_client_for", lambda cfg, session_id: FakeClient())

    rc = cli.cmd_queue(
        _queue_args(announce=False, offline=False, max_age=3600), cfg=object()
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert [e["event_id"] for e in out["queue"]] == ["blocked"]
    assert out["count"] == 1
    assert out["stale_count"] >= 1
    assert out["live"] is True


def test_listen_echoes_for_event_binding(tmp_path, monkeypatch, capsys):
    """The captured reply is tagged with the event it answers (no cross-assoc)."""
    from hark.speech import ListenResult

    monkeypatch.setattr(
        speech,
        "run_listen",
        lambda cfg, **kw: ListenResult(
            text="use option two", provider="test", duration_ms=10, end_mode="silence"
        ),
    )
    args = argparse.Namespace(
        provider=None, end_mode=None, json=True, event_id="evtA"
    )
    rc = cli.cmd_listen(args, cfg=object())
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["for_event"] == "evtA"
    assert out["meta_command"] is None


def test_listen_control_actions_do_not_cross_streams(tmp_path, monkeypatch):
    """A finish/cancel targeted at stream A must not end stream B (no audio merge)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    from hark.listen_control import (
        poll_listen_action,
        register_active_listen,
        request_listen_action,
    )

    register_active_listen("streamA", mode="silence")
    request_listen_action("finish", stream_id="streamA")

    assert poll_listen_action("streamA") == "finish"
    assert poll_listen_action("streamB") is None

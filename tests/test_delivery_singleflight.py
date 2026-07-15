from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import stat
import threading
from pathlib import Path
from typing import Any

import hark.answering as answering
import hark.cli as cli
import hark.dashboard.api as dashboard_api
import hark.delivery as delivery_module
from hark.answering import answer_bound_event
from hark.delivery import BoundEvent, DeliveryStore
from hark.events import extract_question_excerpt
from hark.exitcodes import ABORT, OK
from hark.fingerprint import question_fingerprint
from hark.herdr.client import AgentInfo, HerdrError


MENU = "Allow this action?\n1. Yes\n2. No\n"


class _LiveClient:
    def __init__(
        self,
        *,
        send_entered: Any | None = None,
        release_send: Any | None = None,
        send_count: Any | None = None,
        send_error: Exception | None = None,
    ) -> None:
        self.send_entered = send_entered
        self.release_send = release_send
        self.send_count = send_count
        self.send_error = send_error
        self.sent: list[tuple[str, str]] = []

    def get_agent(self, pane_id: str) -> AgentInfo:
        return AgentInfo(
            session_id="local",
            pane_id=pane_id,
            agent="codex",
            status="blocked",
            revision=1,
        )

    def read_pane(self, pane_id: str, lines: int = 60) -> str:
        return MENU

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))
        if self.send_count is not None:
            with self.send_count.get_lock():
                self.send_count.value += 1
        if self.send_entered is not None:
            self.send_entered.set()
        if self.release_send is not None:
            assert self.release_send.wait(5), "timed out waiting to release send"
        if self.send_error is not None:
            raise self.send_error

    def send_keys(self, pane_id: str, keys: list[str]) -> None:
        raise AssertionError("test uses text delivery")


def _seed(store: DeliveryStore, event_id: str = "evt") -> None:
    store.save_event(
        BoundEvent(
            event_id=event_id,
            session_id="local",
            pane_id="w1:p1",
            pane_revision=1,
            question_fingerprint=question_fingerprint(extract_question_excerpt(MENU)),
            meta={"kind": "agent.blocked"},
        )
    )


def _records(path: Path) -> list[dict[str, Any]]:
    deliveries = path.parent / "deliveries.jsonl"
    return [json.loads(line) for line in deliveries.read_text().splitlines()]


def _process_answer(
    path: str,
    send_entered: Any,
    release_send: Any,
    send_count: Any,
    results: Any,
) -> None:
    client = _LiveClient(
        send_entered=send_entered,
        release_send=release_send,
        send_count=send_count,
    )
    result = answer_bound_event(
        "evt",
        text="yes",
        store=DeliveryStore(Path(path)),
        client_for=lambda _sid: client,
    )
    results.put((result.status, result.reason))


def _crash_owner(path: str, state: str, ready: Any) -> None:
    store = DeliveryStore(Path(path))
    claim = store.acquire_delivery("evt")
    assert claim.owned and claim.token
    if state in ("validating", "sending"):
        assert store.advance_delivery("evt", claim.token, "validating")
    if state == "sending":
        assert store.advance_delivery("evt", claim.token, "sending")
    ready.set()
    os._exit(0)


def test_thread_race_has_one_sender_and_stable_competitor(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    send_entered = threading.Event()
    release_send = threading.Event()
    client = _LiveClient(send_entered=send_entered, release_send=release_send)
    results: list[Any] = []

    owner = threading.Thread(
        target=lambda: results.append(
            answer_bound_event(
                "evt", text="yes", store=store, client_for=lambda _sid: client
            )
        )
    )
    owner.start()
    assert send_entered.wait(5)

    competitor = answer_bound_event(
        "evt", text="again", store=DeliveryStore(path), client_for=lambda _sid: client
    )
    assert competitor.status == "in_progress"
    assert competitor.reason == "delivery_in_progress"

    release_send.set()
    owner.join(5)
    assert not owner.is_alive()
    assert results[0].status == "delivered"
    assert client.sent == [("w1:p1", "yes")]
    assert [record["status"] for record in _records(path)] == [
        "acquired",
        "validating",
        "sending",
        "delivered",
    ]


def test_process_race_has_one_sender(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "events.jsonl"
    _seed(DeliveryStore(path))
    send_entered = ctx.Event()
    release_send = ctx.Event()
    send_count = ctx.Value("i", 0)
    results = ctx.Queue()

    owner = ctx.Process(
        target=_process_answer,
        args=(str(path), send_entered, release_send, send_count, results),
    )
    owner.start()
    assert send_entered.wait(5)
    competitor = ctx.Process(
        target=_process_answer,
        args=(str(path), send_entered, release_send, send_count, results),
    )
    competitor.start()
    competitor.join(5)
    assert competitor.exitcode == 0

    release_send.set()
    owner.join(5)
    assert owner.exitcode == 0
    outcomes = {results.get(timeout=2)[0], results.get(timeout=2)[0]}
    assert outcomes == {"delivered", "in_progress"}
    assert send_count.value == 1


def test_crashed_pre_send_owners_recover_but_sending_becomes_uncertain(
    tmp_path: Path,
) -> None:
    ctx = multiprocessing.get_context("spawn")
    for state in ("acquired", "validating", "sending"):
        path = tmp_path / state / "events.jsonl"
        _seed(DeliveryStore(path))
        ready = ctx.Event()
        process = ctx.Process(target=_crash_owner, args=(str(path), state, ready))
        process.start()
        assert ready.wait(5)
        process.join(5)
        assert process.exitcode == 0

        claim = DeliveryStore(path).acquire_delivery("evt")
        if state == "sending":
            assert claim.owned is False
            assert claim.status == "uncertain"
            assert claim.reason == "owner_lost_after_send_started"
        else:
            assert claim.owned is True
            assert claim.status == "acquired"


def test_expired_pre_send_owner_is_replaced_with_compare_and_set(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    old = store.acquire_delivery("evt", now=10, stale_after_s=5)
    assert old.owned and old.token

    replacement = store.acquire_delivery("evt", now=16, stale_after_s=5)
    assert replacement.owned and replacement.token
    assert replacement.token != old.token
    assert store.advance_delivery("evt", old.token, "validating", now=17) is False
    assert store.advance_delivery("evt", replacement.token, "validating", now=17)


def test_transport_error_is_durable_uncertain_and_never_retried(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    client = _LiveClient(send_error=HerdrError("socket closed"))

    first = answer_bound_event(
        "evt", text="yes", store=store, client_for=lambda _sid: client
    )
    second = answer_bound_event(
        "evt",
        text="yes",
        store=DeliveryStore(path),
        client_for=lambda _sid: (_ for _ in ()).throw(
            AssertionError("uncertain delivery must not create a client")
        ),
    )

    assert first.status == "uncertain"
    assert second.status == "uncertain"
    assert client.sent == [("w1:p1", "yes")]
    assert [record["status"] for record in _records(path)][-2:] == [
        "sending",
        "uncertain",
    ]


def test_live_gate_rejection_is_owned_and_durable(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    client = _LiveClient()

    def working_agent(pane_id: str) -> AgentInfo:
        return AgentInfo(
            session_id="local",
            pane_id=pane_id,
            agent="codex",
            status="working",
            revision=1,
        )

    client.get_agent = working_agent  # type: ignore[method-assign]
    result = answer_bound_event(
        "evt", text="yes", store=store, client_for=lambda _sid: client
    )

    assert result.status == "rejected"
    assert client.sent == []
    assert [record["status"] for record in _records(path)] == [
        "acquired",
        "validating",
        "rejected",
    ]


def test_duplicate_cli_and_dashboard_submissions_do_not_resend(
    tmp_path: Path, monkeypatch: Any
) -> None:
    cli_path = tmp_path / "cli" / "events.jsonl"
    cli_store = DeliveryStore(cli_path)
    _seed(cli_store, "cli-event")
    cli_client = _LiveClient()
    monkeypatch.setattr(cli, "DeliveryStore", lambda: cli_store)
    monkeypatch.setattr(cli, "_client_for", lambda _cfg, _sid: cli_client)
    args = argparse.Namespace(event_id="cli-event", text="yes", keys=None)

    assert cli.cmd_answer(args, cfg=None) == OK
    assert cli.cmd_answer(args, cfg=None) == ABORT
    assert cli_client.sent == [("w1:p1", "yes")]

    dash_path = tmp_path / "dashboard" / "events.jsonl"
    dash_store = DeliveryStore(dash_path)
    _seed(dash_store, "dash-event")
    dash_client = _LiveClient()
    monkeypatch.setattr(answering, "DeliveryStore", lambda: dash_store)
    monkeypatch.setattr(dashboard_api, "_client_for", lambda _cfg, _sid: dash_client)
    body = {"event_id": "dash-event", "text": "yes"}

    first_status, first_payload = dashboard_api.answer_action(object(), body)
    second_status, second_payload = dashboard_api.answer_action(object(), body)
    assert (first_status, first_payload["status"]) == (200, "delivered")
    assert (second_status, second_payload["detail"]) == (409, "already_delivered")
    assert dash_client.sent == [("w1:p1", "yes")]


def test_skip_cannot_replace_sending_and_ambiguous_send_stays_uncertain(
    tmp_path: Path, monkeypatch: Any
) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    send_entered = threading.Event()
    release_send = threading.Event()
    client = _LiveClient(
        send_entered=send_entered,
        release_send=release_send,
        send_error=HerdrError("socket closed"),
    )
    results: list[Any] = []
    owner = threading.Thread(
        target=lambda: results.append(
            answer_bound_event(
                "evt", text="yes", store=store, client_for=lambda _sid: client
            )
        )
    )
    owner.start()
    assert send_entered.wait(5)

    monkeypatch.setattr(cli, "DeliveryStore", lambda: DeliveryStore(path))
    assert cli.cmd_skip(argparse.Namespace(event_id="evt")) == ABORT

    release_send.set()
    owner.join(5)
    assert not owner.is_alive()
    assert results[0].status == "uncertain"
    assert [record["status"] for record in _records(path)][-2:] == [
        "sending",
        "uncertain",
    ]


def _race_external_writer_against_send(
    store: DeliveryStore,
    action: Any,
) -> tuple[list[Any], list[Any]]:
    mark_entered = threading.Event()
    release_mark = threading.Event()
    send_entered = threading.Event()
    release_send = threading.Event()
    original_mark = store.mark

    def paused_mark(event_id: str, status: str, **extra: Any) -> bool:
        mark_entered.set()
        assert release_mark.wait(5)
        return original_mark(event_id, status, **extra)

    store.mark = paused_mark  # type: ignore[method-assign]
    action_results: list[Any] = []
    action_thread = threading.Thread(target=lambda: action_results.append(action()))
    action_thread.start()
    assert mark_entered.wait(5)

    client = _LiveClient(send_entered=send_entered, release_send=release_send)
    answer_results: list[Any] = []
    owner = threading.Thread(
        target=lambda: answer_results.append(
            answer_bound_event(
                "evt", text="yes", store=store, client_for=lambda _sid: client
            )
        )
    )
    owner.start()
    assert send_entered.wait(5)

    release_mark.set()
    action_thread.join(5)
    assert not action_thread.is_alive()
    release_send.set()
    owner.join(5)
    assert not owner.is_alive()
    assert client.sent == [("w1:p1", "yes")]
    return action_results, answer_results


def test_prune_snapshot_cannot_expire_event_after_send_started(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    actions, answers = _race_external_writer_against_send(
        store,
        lambda: store.prune(now=10**12, max_age_s=1),
    )

    assert actions == [[]]
    assert answers[0].status == "delivered"
    assert [record["status"] for record in _records(path)][-2:] == [
        "sending",
        "delivered",
    ]


def test_invalidation_snapshot_cannot_replace_sending(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    actions, answers = _race_external_writer_against_send(
        store,
        lambda: store.invalidate_target("local", "w1:p1", reason="pane.closed"),
    )

    assert actions == [[]]
    assert answers[0].status == "delivered"
    assert [record["status"] for record in _records(path)][-2:] == [
        "sending",
        "delivered",
    ]


def test_post_send_failed_cas_forces_durable_uncertain(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    original_advance = store.advance_delivery

    def conflict_after_send(
        event_id: str, owner_token: str, status: str, **extra: Any
    ) -> bool:
        if status == "delivered":
            with store._locked_delivery_state():
                store._append_delivery_unlocked(
                    {"event_id": event_id, "status": "skipped", "ts": 1.0}
                )
        return original_advance(event_id, owner_token, status, **extra)

    store.advance_delivery = conflict_after_send  # type: ignore[method-assign]
    result = answer_bound_event(
        "evt", text="yes", store=store, client_for=lambda _sid: _LiveClient()
    )

    assert result.status == "uncertain"
    assert [record["status"] for record in _records(path)][-2:] == [
        "skipped",
        "uncertain",
    ]


def test_external_terminal_during_validation_fences_rejection_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    validation_entered = threading.Event()
    release_validation = threading.Event()
    client = _LiveClient()

    def working_agent(pane_id: str) -> AgentInfo:
        return AgentInfo(
            session_id="local",
            pane_id=pane_id,
            agent="codex",
            status="working",
            revision=1,
        )

    def paused_read(pane_id: str, lines: int = 60) -> str:
        validation_entered.set()
        assert release_validation.wait(5)
        return MENU

    client.get_agent = working_agent  # type: ignore[method-assign]
    client.read_pane = paused_read  # type: ignore[method-assign]
    results: list[Any] = []
    owner = threading.Thread(
        target=lambda: results.append(
            answer_bound_event(
                "evt", text="yes", store=store, client_for=lambda _sid: client
            )
        )
    )
    owner.start()
    assert validation_entered.wait(5)
    assert store.mark("evt", "skipped", reason="operator_skip") is True
    release_validation.set()
    owner.join(5)

    assert not owner.is_alive()
    assert results[0].status == "rejected"
    assert results[0].reason == "operator_skip"
    assert [record["status"] for record in _records(path)][-1] == "skipped"


def test_cold_delivery_file_fsyncs_file_then_parent_directory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    calls: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        mode = os.fstat(fd).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(delivery_module.os, "fsync", recording_fsync)
    claim = store.acquire_delivery("evt")

    assert claim.owned and claim.token
    assert calls == ["file", "directory"]
    calls.clear()
    assert store.advance_delivery("evt", claim.token, "validating")
    assert calls == ["file"]


def test_external_terminal_cannot_replace_existing_terminal(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    store = DeliveryStore(path)
    _seed(store)
    assert store.mark("evt", "delivered", text="yes") is True
    assert store.mark("evt", "skipped") is False
    assert [record["status"] for record in _records(path)] == ["delivered"]

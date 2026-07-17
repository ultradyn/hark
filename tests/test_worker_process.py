"""PID-reuse-safe worker identity and signalling regression tests (B127)."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import hark.worker_process as worker_process


@pytest.mark.parametrize(
    ("argv", "role"),
    [
        (["hark", "ambient"], "ambient"),
        (["/venv/bin/hark", "watch", "--for-monitor"], "watch"),
        (["python3", "/venv/bin/hark", "ambient"], "ambient"),
        (["python", "-m", "hark", "watch", "--session", "lab"], "watch"),
        (
            ["python", "-m", "hark", "watch", "--session", "run-mode-a-lab"],
            "watch",
        ),
        (["/usr/bin/uv", "run", "hark", "ambient"], "ambient"),
    ],
)
def test_worker_role_accepts_real_launch_shapes(argv: list[str], role: str):
    assert worker_process.worker_role_from_argv(argv) == role


def test_worker_request_compatibility_requires_exact_roles_and_watch_session(
    monkeypatch: pytest.MonkeyPatch,
):
    watch = worker_process.WorkerRecord(pid=101, start_time="w", role="watch")
    ambient = worker_process.WorkerRecord(pid=202, start_time="a", role="ambient")
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(
        worker_process,
        "_proc_argv",
        lambda pid: (
            ["hark", "watch", "--session", "requested"]
            if pid == watch.pid
            else ["hark", "ambient"]
        ),
    )
    monkeypatch.setattr(worker_process, "_proc_environ", lambda _pid: dict(os.environ))

    assert worker_process.worker_records_match_request(
        [watch, ambient], watch=True, ambient=True, session="requested"
    )
    assert not worker_process.worker_records_match_request(
        [watch, ambient], watch=True, ambient=True, session="different"
    )
    assert not worker_process.worker_records_match_request(
        [watch], watch=False, ambient=True, session="requested"
    )
    monkeypatch.setattr(
        worker_process,
        "_proc_environ",
        lambda _pid: {**os.environ, "HARK_CONFIG": "/different/config.toml"},
    )
    assert not worker_process.worker_records_match_request(
        [watch, ambient], watch=True, ambient=True, session="requested"
    )


def test_pidfile_lock_cleanup_preserves_body_primary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    class HostileHandle:
        def fileno(self) -> int:
            return 123

        def close(self) -> None:
            raise RuntimeError("close failed")

    handle = HostileHandle()
    monkeypatch.setattr(worker_process.Path, "open", lambda *_a, **_k: handle)

    def hostile_flock(_fd: int, operation: int) -> None:
        if operation == worker_process.fcntl.LOCK_UN:
            raise OSError("unlock failed")

    monkeypatch.setattr(worker_process.fcntl, "flock", hostile_flock)
    primary = ValueError("body failed")
    with pytest.raises(ValueError) as caught:
        with worker_process.worker_pidfile_lock(tmp_path / "mode-a.pids"):
            raise primary

    assert caught.value is primary
    notes = " ".join(getattr(primary, "__notes__", []))
    assert "unlock failed" in notes
    assert "close failed" in notes


def test_pidfile_lock_acquisition_close_preserves_primary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    class HostileHandle:
        def fileno(self) -> int:
            return 123

        def close(self) -> None:
            raise RuntimeError("close failed")

    primary = KeyboardInterrupt("lock interrupted")
    monkeypatch.setattr(worker_process.Path, "open", lambda *_a, **_k: HostileHandle())
    monkeypatch.setattr(
        worker_process.fcntl,
        "flock",
        lambda *_a, **_k: (_ for _ in ()).throw(primary),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        with worker_process.worker_pidfile_lock(tmp_path / "mode-a.pids"):
            raise AssertionError("unreachable")

    assert caught.value is primary
    assert "close failed" in " ".join(getattr(primary, "__notes__", []))


def test_direct_publication_keeps_structured_worker_identity(tmp_path: Path):
    path = tmp_path / "mode-a.pids"
    record = worker_process.WorkerRecord(
        pid=4321, start_time="captured-start", role="ambient"
    )

    assert (
        worker_process.main(["publish", str(path), "--direct", record.to_json()]) == 0
    )

    assert json.loads(path.read_text(encoding="utf-8")) == json.loads(record.to_json())


def test_capture_role_poll_rejects_pid_lifetime_change(
    monkeypatch: pytest.MonkeyPatch,
):
    record = worker_process.WorkerRecord(pid=4321, start_time="original", role="watch")
    lifetimes = iter([True, False])
    monkeypatch.setattr(
        worker_process,
        "_record_matches_kernel_lifetime",
        lambda _record: next(lifetimes),
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: False)
    monkeypatch.setattr(worker_process.time, "sleep", lambda _seconds: None)

    assert not worker_process.wait_for_worker_role(record, timeout_s=1.0)


def test_capture_and_wait_allow_pre_exec_child_before_token_is_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    child_pid = 4321
    parent_pid = os.getpid()
    monkeypatch.setattr(worker_process, "_proc_stat", lambda _pid: ("S", "captured"))
    monkeypatch.setattr(worker_process, "_proc_ppid", lambda _pid: parent_pid)
    monkeypatch.setattr(worker_process, "_current_boot_id", lambda: "boot")
    monkeypatch.setattr(worker_process, "_proc_environ", lambda _pid: {})
    record = worker_process.capture_worker_identity(
        child_pid,
        role="watch",
        expected_parent_pid=parent_pid,
        pidfile=tmp_path / "mode-a.pids",
        spawn_token="preclaim",
    )
    assert record is not None
    assert record.provisional

    ready_checks = iter([False, True])
    monkeypatch.setattr(
        worker_process, "record_matches_process", lambda _record: next(ready_checks)
    )
    monkeypatch.setattr(worker_process.time, "sleep", lambda _seconds: None)
    assert worker_process.wait_for_worker_role(record, timeout_s=1)


def test_capture_rejects_pid_that_is_not_the_launchers_child(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(worker_process, "_proc_stat", lambda _pid: ("S", "start"))
    monkeypatch.setattr(worker_process, "_proc_ppid", lambda _pid: 999)

    assert (
        worker_process.capture_worker_identity(
            4321, role="watch", expected_parent_pid=111
        )
        is None
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", "sleep 60", "hark", "ambient"],
        ["python", "-c", "import time", "hark", "watch"],
        ["python", "script.py", "hark", "ambient"],
        ["python-malware", "-m", "hark", "watch"],
        ["pypymalware", "/venv/bin/hark", "ambient"],
        ["python3", "/repo/scripts/run-mode-a.sh", "ambient"],
        ["uv", "tool", "hark", "watch"],
    ],
)
def test_worker_role_rejects_unrelated_trailing_hark_args(argv: list[str]):
    assert worker_process.worker_role_from_argv(argv) is None


def test_inspect_rejects_unrelated_python_script_named_hark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    pidfile = tmp_path / "mode-a.pids"
    monkeypatch.setattr(worker_process, "_proc_stat", lambda _pid: ("S", "123"))
    monkeypatch.setattr(
        worker_process,
        "_proc_argv",
        lambda _pid: ["python3", str(tmp_path / "hark"), "ambient"],
    )
    monkeypatch.setattr(worker_process, "_proc_environ", lambda _pid: {})

    assert (
        worker_process.worker_role_from_argv(
            ["python3", str(tmp_path / "hark"), "ambient"]
        )
        == "ambient"
    )
    assert (
        worker_process.inspect_worker(
            4321, expected_role="ambient", expected_pidfile=pidfile
        )
        is None
    )


def test_discovery_is_scoped_and_deduplicates_only_wrapper_ancestry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    pidfile = tmp_path / "one" / "mode-a.pids"
    other_pidfile = tmp_path / "two" / "mode-a.pids"
    config = tmp_path / "config" / "hark" / "config.toml"
    candidates = {
        101: worker_process.WorkerRecord(
            101, "a", "watch", pidfile=str(pidfile), config=str(config)
        ),
        102: worker_process.WorkerRecord(
            102, "b", "watch", pidfile=str(pidfile), config=str(config)
        ),
        201: worker_process.WorkerRecord(
            201,
            "c",
            "ambient",
            pidfile=str(other_pidfile),
            config=str(config),
        ),
    }

    class Entry:
        def __init__(self, name: str) -> None:
            self.name = name

    monkeypatch.setattr(
        worker_process.Path,
        "iterdir",
        lambda _path: [Entry(str(pid)) for pid in candidates],
    )
    monkeypatch.setattr(
        worker_process,
        "inspect_worker",
        lambda pid, *, expected_pidfile=None, **_kwargs: (
            candidates[pid]
            if candidates[pid].pidfile == str(expected_pidfile)
            else None
        ),
    )
    monkeypatch.setattr(
        worker_process,
        "_proc_ppid",
        lambda pid: {102: 101, 101: 1}.get(pid),
    )

    assert worker_process._discover_workers(pidfile, config) == [candidates[102]]


def test_live_provisional_record_is_retained_but_never_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    path = tmp_path / "mode-a.pids"
    provisional = worker_process.WorkerRecord(
        4321,
        "captured",
        "watch",
        pidfile=str(path.resolve()),
        config=str(worker_process._config_path_from_environ(dict(os.environ))),
        provisional=True,
        boot_id=worker_process._current_boot_id(),
        spawn_token="preclaimed-token",
    )
    path.write_text(provisional.to_json() + "\n", encoding="utf-8")
    monkeypatch.setattr(worker_process, "record_matches_lifetime", lambda _r: True)
    monkeypatch.setattr(
        worker_process,
        "record_matches_process",
        lambda _r: (_ for _ in ()).throw(
            AssertionError("provisional record was treated as healthy")
        ),
    )

    assert worker_process.collect_worker_records(path) == [provisional]
    assert not worker_process.worker_records_match_request(
        [provisional], watch=True, ambient=False, session="default"
    )


def spawn_process(
    directory: Path,
    *,
    role: str | None = None,
    legacy: bool = False,
    spawn_token: str | None = None,
) -> subprocess.Popen[bytes]:
    if role:
        launcher = directory / "hark"
        launcher.write_text(
            "#!/usr/bin/env python3\n"
            "import signal\n"
            "import time\n"
            "def stop(*_args):\n"
            "    raise SystemExit(0)\n"
            "signal.signal(signal.SIGTERM, stop)\n"
            "while True:\n"
            "    time.sleep(1)\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        pidfile = directory / "mode-a.pids"
        env = dict(os.environ)
        if not legacy:
            env.update(
                {
                    worker_process.WORKER_PIDFILE_ENV: str(pidfile.resolve()),
                    worker_process.WORKER_ROLE_ENV: role,
                }
            )
        if spawn_token is not None:
            env[worker_process.WORKER_SPAWN_TOKEN_ENV] = spawn_token
        child = subprocess.Popen(
            [str(launcher), role],
            start_new_session=True,
            env=env,
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if (
                worker_process.inspect_worker(
                    child.pid,
                    expected_role=role,
                    expected_pidfile=pidfile,
                    require_markers=not legacy,
                )
                is not None
            ):
                return child
            time.sleep(0.01)
        kill_child(child)
        raise AssertionError(f"worker argv did not become ready for role {role}")
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )


def kill_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            child.kill()
    child.wait(timeout=2)


def test_bare_pid_of_live_scoped_worker_is_retained_without_migration(
    tmp_path: Path,
):
    child = spawn_process(tmp_path, role="ambient")
    path = tmp_path / "mode-a.pids"
    original = f"pid={child.pid}\n"
    try:
        path.write_text(original, encoding="utf-8")

        with pytest.raises(
            worker_process.WorkerStateUnavailableError,
            match="no historical start time",
        ) as caught:
            worker_process.collect_worker_records(path)

        assert caught.value.pids == (child.pid,)
        assert child.poll() is None
        assert path.read_text(encoding="utf-8") == original
    finally:
        kill_child(child)


def test_structured_pre_scope_markerless_worker_is_migrated_from_historical_lifetime(
    tmp_path: Path,
):
    shim = tmp_path / "shim" / "hark"
    shim.mkdir(parents=True)
    (shim / "__init__.py").write_text("", encoding="utf-8")
    (shim / "__main__.py").write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: exit(0))\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONPATH": str(shim.parent)}
    child = subprocess.Popen(
        [sys.executable, "-m", "hark", "ambient"],
        env=env,
        start_new_session=True,
    )
    path = tmp_path / "mode-a.pids"
    try:
        live = None
        deadline = time.monotonic() + 2.0
        while live is None and time.monotonic() < deadline:
            live = worker_process.inspect_worker(
                child.pid,
                expected_role="ambient",
                expected_pidfile=path,
                require_markers=False,
            )
            if live is None:
                time.sleep(0.01)
        assert live is not None
        historical = worker_process.WorkerRecord(
            pid=live.pid, start_time=live.start_time, role=live.role
        )
        path.write_text(historical.to_json() + "\n", encoding="utf-8")

        records = worker_process.collect_worker_records(path)

        assert [(record.pid, record.role) for record in records] == [
            (child.pid, "ambient")
        ]
        assert records[0].legacy is True
        assert records[0].pidfile == str(path.resolve())
    finally:
        kill_child(child)


def test_forged_provisional_record_without_provenance_never_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    child = spawn_process(tmp_path)
    path = tmp_path / "mode-a.pids"
    try:
        stat = worker_process._proc_stat(child.pid)
        assert stat is not None
        forged = worker_process.WorkerRecord(
            pid=child.pid,
            start_time=stat[1],
            role="ambient",
            pidfile=str(path.resolve()),
            config=str(worker_process._config_path_from_environ(dict(os.environ))),
            provisional=True,
        )
        path.write_text(forged.to_json() + "\n", encoding="utf-8")
        sent: list[int] = []
        monkeypatch.setattr(
            worker_process,
            "_send_pidfd_signal",
            lambda _fd, sig: sent.append(sig),
        )
        assert worker_process.collect_worker_records(path) == []
        assert sent == []
        assert child.poll() is None
    finally:
        kill_child(child)


def test_procfs_unknown_retains_pidfile_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "mode-a.pids"
    record = worker_process.WorkerRecord(
        pid=4321,
        start_time="known",
        role="ambient",
        pidfile=str(path.resolve()),
        config=str(worker_process._config_path_from_environ(dict(os.environ))),
        boot_id=worker_process._current_boot_id(),
    )
    original = record.to_json() + "\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        worker_process,
        "_proc_stat",
        lambda _pid: (_ for _ in ()).throw(
            worker_process._ProcfsUnavailableError(13, "denied")
        ),
    )
    with pytest.raises(worker_process.WorkerStateUnavailableError):
        worker_process.collect_worker_records(path)
    assert path.read_text(encoding="utf-8") == original


def _deny_boot_id_read(monkeypatch: pytest.MonkeyPatch) -> None:
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    real_read_text = Path.read_text

    def deny_boot_id(candidate: Path, *args, **kwargs):
        if candidate == boot_id_path:
            raise PermissionError("boot identity unavailable")
        return real_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_boot_id)


def test_boot_id_unavailable_retains_valid_structured_record_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    child = spawn_process(tmp_path, role="ambient")
    path = tmp_path / "mode-a.pids"
    try:
        record = worker_process.inspect_worker(
            child.pid, expected_role="ambient", expected_pidfile=path
        )
        assert record is not None
        original = record.to_json().encode() + b"\n# retain this exact body\n"
        path.write_bytes(original)
        sent: list[int] = []
        monkeypatch.setattr(
            worker_process,
            "_send_pidfd_signal",
            lambda _fd, sig: sent.append(sig),
        )
        _deny_boot_id_read(monkeypatch)

        with pytest.raises(
            worker_process.WorkerStateUnavailableError,
            match="retaining ownership state",
        ) as caught:
            worker_process.collect_worker_records(path)

        assert caught.value.pids == (child.pid,)
        assert path.read_bytes() == original
        assert sent == []
        assert child.poll() is None
    finally:
        kill_child(child)


def test_boot_id_unavailable_retains_valid_provisional_record_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    spawn_token = "boot-unavailable-provisional"
    child = spawn_process(tmp_path, role="watch", spawn_token=spawn_token)
    path = tmp_path / "mode-a.pids"
    try:
        actual = worker_process.inspect_worker(
            child.pid, expected_role="watch", expected_pidfile=path
        )
        assert actual is not None
        provisional = worker_process.WorkerRecord(
            pid=actual.pid,
            start_time=actual.start_time,
            role=actual.role,
            pidfile=actual.pidfile,
            config=actual.config,
            provisional=True,
            boot_id=actual.boot_id,
            spawn_token=spawn_token,
        )
        original = b"# provisional authority\n" + provisional.to_json().encode() + b"\n"
        path.write_bytes(original)
        sent: list[int] = []
        monkeypatch.setattr(
            worker_process,
            "_send_pidfd_signal",
            lambda _fd, sig: sent.append(sig),
        )
        _deny_boot_id_read(monkeypatch)

        with pytest.raises(
            worker_process.WorkerStateUnavailableError,
            match="retaining ownership state",
        ) as caught:
            worker_process.collect_worker_records(path)

        assert caught.value.pids == (child.pid,)
        assert path.read_bytes() == original
        assert sent == []
        assert child.poll() is None
    finally:
        kill_child(child)


def test_discovery_fails_closed_when_boot_provenance_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    child = spawn_process(tmp_path, role="ambient")
    path = tmp_path / "mode-a.pids"
    try:
        _deny_boot_id_read(monkeypatch)

        with pytest.raises(
            worker_process.WorkerStateUnavailableError,
            match="boot provenance",
        ):
            worker_process.collect_worker_records(path, discover=True)

        assert not path.exists()
        assert child.poll() is None
    finally:
        kill_child(child)


@pytest.mark.parametrize("operation", ["create", "capture", "recover"])
def test_new_identity_authority_reports_boot_id_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
):
    unavailable = worker_process._ProcfsUnavailableError(
        13, "boot identity unavailable"
    )
    monkeypatch.setattr(
        worker_process,
        "_current_boot_id",
        lambda: (_ for _ in ()).throw(unavailable),
    )
    path = tmp_path / "mode-a.pids"

    if operation == "create":
        invoke = lambda: worker_process.create_worker_spawn_claim(
            role="ambient", pidfile=path
        )
    elif operation == "capture":
        monkeypatch.setattr(
            worker_process, "_proc_stat", lambda _pid: ("S", "captured")
        )
        invoke = lambda: worker_process.capture_worker_identity(
            4321, role="ambient", pidfile=path
        )
    else:
        claim = worker_process.WorkerSpawnClaim(
            role="ambient",
            pidfile=str(path.resolve()),
            config=str(tmp_path / "config.toml"),
            boot_id="known-boot",
            parent_pid=os.getpid(),
            parent_start_time="known-parent",
            token="known-token",
        )
        invoke = lambda: worker_process.recover_worker_spawn_claim(claim)

    with pytest.raises(
        worker_process.WorkerStateUnavailableError,
        match="boot identity unavailable",
    ):
        invoke()

    assert not path.exists()


def test_other_config_record_is_preserved_and_verified_against_its_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "mode-a.pids"
    config_a = tmp_path / "config-a.toml"
    config_b = tmp_path / "config-b.toml"
    record = worker_process.WorkerRecord(
        pid=4321,
        start_time="known",
        role="ambient",
        pidfile=str(path.resolve()),
        config=str(config_a.resolve()),
        boot_id=worker_process._current_boot_id(),
    )
    path.write_text(record.to_json() + "\n", encoding="utf-8")
    monkeypatch.setenv("HARK_CONFIG", str(config_b))
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _r: True)
    assert worker_process.collect_worker_records(path) == [record]
    assert json.loads(path.read_text(encoding="utf-8"))["config"] == str(
        config_a.resolve()
    )


def test_pre_scope_structured_worker_is_migrated_only_from_live_provenance(
    tmp_path: Path,
):
    child = spawn_process(tmp_path, role="watch")
    path = tmp_path / "mode-a.pids"
    try:
        live = worker_process.inspect_worker(
            child.pid, expected_role="watch", expected_pidfile=path
        )
        assert live is not None
        old = worker_process.WorkerRecord(
            pid=live.pid, start_time=live.start_time, role=live.role
        )
        path.write_text(old.to_json() + "\n", encoding="utf-8")

        assert worker_process.collect_worker_records(path) == [live]
        assert json.loads(path.read_text(encoding="utf-8"))["pidfile"] == str(
            path.resolve()
        )
    finally:
        kill_child(child)


def test_live_unrelated_legacy_pid_is_removed_without_signal(tmp_path: Path):
    child = spawn_process(tmp_path)
    path = tmp_path / "mode-a.pids"
    try:
        path.write_text(f"{child.pid}\n", encoding="utf-8")
        assert worker_process.collect_worker_records(path) == []
        assert child.poll() is None
        assert not path.exists()
    finally:
        kill_child(child)


def test_unrelated_suffix_is_ignored_by_migration_and_discovery(tmp_path: Path):
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            "hark",
            "ambient",
        ],
        start_new_session=True,
    )
    path = tmp_path / "mode-a.pids"
    try:
        path.write_text(f"{child.pid}\n", encoding="utf-8")
        records = worker_process.collect_worker_records(path, discover=True)
        assert child.pid not in {record.pid for record in records}
        assert child.poll() is None
    finally:
        kill_child(child)


def test_orphan_discovery_records_untracked_worker(tmp_path: Path):
    child = spawn_process(tmp_path, role="watch")
    path = tmp_path / "mode-a.pids"
    try:
        records = worker_process.collect_worker_records(path, discover=True)
        matching = [record for record in records if record.pid == child.pid]
        assert len(matching) == 1
        assert matching[0].role == "watch"
        assert json.loads(path.read_text(encoding="utf-8").splitlines()[0])[
            "start_time"
        ]
    finally:
        kill_child(child)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda record: worker_process.WorkerRecord(
            pid=record.pid, start_time="reused-pid-start-time", role=record.role
        ),
        lambda record: worker_process.WorkerRecord(
            pid=record.pid, start_time=record.start_time, role="watch"
        ),
    ],
    ids=["reused-pid", "role-mismatch"],
)
def test_mismatched_structured_record_is_removed_without_signal(
    tmp_path: Path,
    mutation,
):
    child = spawn_process(tmp_path, role="ambient")
    path = tmp_path / "mode-a.pids"
    try:
        actual = worker_process.inspect_worker(child.pid)
        assert actual is not None
        worker_process.write_worker_records(path, [mutation(actual)])
        records = worker_process.collect_worker_records(path)
        assert records == []
        assert child.poll() is None
        assert not path.exists()
    finally:
        kill_child(child)


def test_malformed_and_stale_entries_are_removed(tmp_path: Path):
    path = tmp_path / "mode-a.pids"
    path.write_text(
        "\n".join(
            [
                "not-json-or-pid",
                '{"pid":true,"role":"watch","start_time":"1","version":1}',
                '{"pid":123,"role":[],"start_time":"1","version":1}',
                '{"pid":999999999,"role":"watch","start_time":"1","version":1}',
            ]
        ),
        encoding="utf-8",
    )
    assert worker_process.collect_worker_records(path) == []
    assert not path.exists()


def test_unreadable_pidfile_fails_closed_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "mode-a.pids"
    original = b'{"pid":123,"role":"watch","start_time":"1","version":1}\n'
    path.write_bytes(original)
    real_read_text = Path.read_text

    def deny_target_read(candidate: Path, *args, **kwargs):
        if candidate == path:
            raise PermissionError("ownership state unreadable")
        return real_read_text(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_target_read)

    with pytest.raises(PermissionError, match="ownership state unreadable"):
        worker_process.collect_worker_records(path)

    assert path.read_bytes() == original


def test_paused_empty_collector_cannot_erase_concurrent_fresh_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "mode-a.pids"
    path.write_text("malformed-old-state\n", encoding="utf-8")
    fresh = worker_process.WorkerRecord(
        pid=4321, start_time="fresh-owner", role="watch"
    )
    collector_at_rewrite = threading.Event()
    release_collector = threading.Event()
    writer_entered_unlocked = threading.Event()
    failures: list[BaseException] = []
    real_write = worker_process._write_worker_records_unlocked

    def controlled_write(target: Path, records):
        materialized = list(records)
        if threading.current_thread().name == "paused-collector":
            collector_at_rewrite.set()
            if not release_collector.wait(timeout=2):
                raise TimeoutError("collector was not released")
        else:
            writer_entered_unlocked.set()
        real_write(target, materialized)

    monkeypatch.setattr(
        worker_process, "_write_worker_records_unlocked", controlled_write
    )

    def collect_stale() -> None:
        try:
            assert worker_process.collect_worker_records(path) == []
        except BaseException as exc:
            failures.append(exc)

    def write_fresh() -> None:
        try:
            worker_process.write_worker_records(path, [fresh])
        except BaseException as exc:
            failures.append(exc)

    collector = threading.Thread(target=collect_stale, name="paused-collector")
    collector.start()
    assert collector_at_rewrite.wait(timeout=2)
    writer = threading.Thread(target=write_fresh, name="fresh-writer")
    writer.start()

    # The writer cannot reach its unlocked replace while the collector owns
    # the transaction lock.  Without serialization it writes now and the
    # resumed empty collector unlinks that fresh ownership.
    assert not writer_entered_unlocked.wait(timeout=0.1)
    release_collector.set()
    collector.join(timeout=2)
    writer.join(timeout=2)

    assert not collector.is_alive()
    assert not writer.is_alive()
    assert failures == []
    assert worker_process.read_worker_records(path) == [fresh]


def test_owned_writer_preserves_other_live_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "mode-a.pids"
    scope = str(path.resolve())
    config = str(worker_process._config_path_from_environ(dict(os.environ)))
    other = worker_process.WorkerRecord(
        pid=1111,
        start_time="other-owner",
        role="ambient",
        pidfile=scope,
        config=config,
        boot_id=worker_process._current_boot_id(),
    )
    old_owned = worker_process.WorkerRecord(
        pid=2222,
        start_time="old-owned",
        role="watch",
        pidfile=scope,
        config=config,
        boot_id=worker_process._current_boot_id(),
    )
    refreshed_owned = worker_process.WorkerRecord(
        pid=2222,
        start_time="refreshed-owned",
        role="watch",
        pidfile=scope,
        config=config,
        boot_id=worker_process._current_boot_id(),
    )
    worker_process.write_worker_records(path, [other, old_owned])
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: True)

    worker_process.replace_owned_worker_records(
        path,
        owned_pids={old_owned.pid},
        records=[refreshed_owned],
    )

    assert worker_process.read_worker_records(path) == [other, refreshed_owned]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL])
def test_signal_reverifies_after_opening_pidfd(
    monkeypatch: pytest.MonkeyPatch, sig: int
):
    record = worker_process.WorkerRecord(
        pid=os.getpid(), start_time="old", role="watch"
    )
    read_fd, write_fd = os.pipe()
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        worker_process.os, "pidfd_open", lambda _pid: read_fd, raising=False
    )
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda fd, sig: sent.append((fd, sig)),
        raising=False,
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: False)
    try:
        assert worker_process.signal_worker(record, sig) is False
        assert sent == []
    finally:
        os.close(write_fd)


def test_signal_worker_records_treats_identity_mismatch_as_benign(
    monkeypatch: pytest.MonkeyPatch,
):
    record = worker_process.WorkerRecord(pid=4321, start_time="old", role="watch")
    read_fd, write_fd = os.pipe()
    sent: list[int] = []
    monkeypatch.setattr(
        worker_process.os, "pidfd_open", lambda _pid: read_fd, raising=False
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: False)
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda _fd, sig: sent.append(sig),
        raising=False,
    )
    try:
        result = worker_process.signal_worker_records([record], signal.SIGTERM)
    finally:
        os.close(write_fd)

    assert result.errors == ()
    assert result.sent_records == ()
    assert sent == []


def test_signal_worker_records_continues_after_procfs_verification_error(
    monkeypatch: pytest.MonkeyPatch,
):
    first = worker_process.WorkerRecord(pid=41001, start_time="first", role="watch")
    second = worker_process.WorkerRecord(
        pid=41002, start_time="second", role="ambient"
    )
    attempted: list[int] = []
    sent: list[int] = []
    write_fds: list[int] = []

    def open_pidfd(_pid: int) -> int:
        read_fd, write_fd = os.pipe()
        write_fds.append(write_fd)
        return read_fd

    def matches(record: worker_process.WorkerRecord) -> bool:
        attempted.append(record.pid)
        if record is first:
            raise worker_process._ProcfsUnavailableError("procfs denied")
        return True

    monkeypatch.setattr(worker_process.os, "pidfd_open", open_pidfd, raising=False)
    monkeypatch.setattr(worker_process, "record_matches_process", matches)
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda _fd, _sig: sent.append(second.pid),
        raising=False,
    )
    try:
        result = worker_process.signal_worker_records(
            [first, second], signal.SIGTERM
        )
    finally:
        for descriptor in write_fds:
            os.close(descriptor)

    assert attempted == [first.pid, second.pid]
    assert sent == [second.pid]
    assert result.sent_records == (second,)
    assert len(result.errors) == 1
    assert result.errors[0].record == first
    assert "identity verification" in (result.errors[0].error or "")
    assert "procfs denied" in (result.errors[0].error or "")


def test_signal_worker_reports_pidfd_close_error_after_signal(
    monkeypatch: pytest.MonkeyPatch,
):
    record = worker_process.WorkerRecord(
        pid=41003, start_time="verified", role="watch"
    )
    read_fd, write_fd = os.pipe()
    sent: list[int] = []
    real_close = os.close

    monkeypatch.setattr(
        worker_process.os, "pidfd_open", lambda _pid: read_fd, raising=False
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _r: True)
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda _fd, sig: sent.append(sig),
        raising=False,
    )

    def fail_close(descriptor: int) -> None:
        real_close(descriptor)
        raise OSError("close denied")

    monkeypatch.setattr(worker_process.os, "close", fail_close)
    try:
        result = worker_process.signal_worker_records([record], signal.SIGTERM)
    finally:
        real_close(write_fd)

    assert sent == [signal.SIGTERM]
    assert result.sent_records == (record,)
    assert len(result.errors) == 1
    assert "pidfd close failed" in (result.errors[0].error or "")
    assert "close denied" in (result.errors[0].error or "")


def test_signal_records_cli_does_not_signal_reused_spawn_identity(
    monkeypatch: pytest.MonkeyPatch,
):
    """A captured attempt record is not authority after its PID lifetime changes."""
    record = worker_process.WorkerRecord(
        pid=4321, start_time="spawn-time", role="watch"
    )
    read_fd, write_fd = os.pipe()
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        worker_process.os, "pidfd_open", lambda _pid: read_fd, raising=False
    )
    monkeypatch.setattr(
        worker_process, "record_matches_lifetime", lambda _record: False
    )
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda fd, sig: sent.append((fd, sig)),
        raising=False,
    )
    try:
        assert (
            worker_process.main(
                ["signal-records", "TERM", "--lifetime-only", record.to_json()]
            )
            == 0
        )
    finally:
        os.close(write_fd)

    assert sent == []


@pytest.mark.parametrize(
    "failure_stage", ["pidfd_open", "pidfd_send_signal", "pidfd_unavailable"]
)
def test_signal_cli_fails_when_verified_worker_cannot_be_signalled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_stage: str,
):
    record = worker_process.WorkerRecord(pid=1234, start_time="verified", role="watch")
    monkeypatch.setattr(
        worker_process, "collect_worker_records", lambda *_args, **_kwargs: [record]
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: True)

    write_fd: int | None = None
    if failure_stage == "pidfd_open":
        monkeypatch.setattr(
            worker_process.os,
            "pidfd_open",
            lambda _pid: (_ for _ in ()).throw(PermissionError("denied")),
            raising=False,
        )
        monkeypatch.setattr(
            worker_process.signal,
            "pidfd_send_signal",
            lambda _fd, _sig: None,
            raising=False,
        )
    elif failure_stage == "pidfd_send_signal":
        read_fd, write_fd = os.pipe()
        monkeypatch.setattr(
            worker_process.os, "pidfd_open", lambda _pid: read_fd, raising=False
        )
        monkeypatch.setattr(
            worker_process.signal,
            "pidfd_send_signal",
            lambda _fd, _sig: (_ for _ in ()).throw(PermissionError("denied")),
            raising=False,
        )
    else:
        monkeypatch.delattr(worker_process.os, "pidfd_open", raising=False)
        monkeypatch.delattr(worker_process.signal, "pidfd_send_signal", raising=False)
        monkeypatch.setattr(worker_process, "_LIBC_PIDFD_OPEN", None)
        monkeypatch.setattr(worker_process, "_LIBC_PIDFD_SEND_SIGNAL", None)
        monkeypatch.setattr(
            worker_process.os,
            "kill",
            lambda _pid, _sig: (_ for _ in ()).throw(
                AssertionError("unsafe PID fallback used")
            ),
        )

    try:
        assert worker_process.main(["signal", "/tmp/mode-a.pids", "KILL"]) == 1
    finally:
        if write_fd is not None:
            os.close(write_fd)

    captured = capsys.readouterr()
    assert captured.out == ""
    expected = (
        "pidfd unavailable"
        if failure_stage == "pidfd_unavailable"
        else f"{failure_stage} failed"
    )
    assert expected in captured.err
    assert "worker pid 1234" in captured.err


def test_missing_stdlib_pidfd_uses_libc_adapter_without_pid_kill(
    monkeypatch: pytest.MonkeyPatch,
):
    record = worker_process.WorkerRecord(pid=1234, start_time="verified", role="watch")
    read_fd, write_fd = os.pipe()
    opened: list[tuple[int, int]] = []
    sent: list[tuple[int, int, object, int]] = []
    monkeypatch.delattr(worker_process.os, "pidfd_open", raising=False)
    monkeypatch.delattr(worker_process.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(
        worker_process,
        "_LIBC_PIDFD_OPEN",
        lambda pid, flags: opened.append((pid, flags)) or read_fd,
    )
    monkeypatch.setattr(
        worker_process,
        "_LIBC_PIDFD_SEND_SIGNAL",
        lambda fd, sig, info, flags: sent.append((fd, sig, info, flags)) or 0,
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: True)
    monkeypatch.setattr(
        worker_process.os,
        "kill",
        lambda _pid, _sig: (_ for _ in ()).throw(
            AssertionError("unsafe PID fallback used")
        ),
    )
    try:
        assert worker_process.signal_worker(record, signal.SIGTERM) is True
    finally:
        os.close(write_fd)

    assert opened == [(record.pid, 0)]
    assert sent == [(read_fd, signal.SIGTERM, None, 0)]


def test_missing_stdlib_pidfd_rejects_swapped_occupant_after_open(
    monkeypatch: pytest.MonkeyPatch,
):
    record = worker_process.WorkerRecord(pid=1234, start_time="old", role="watch")
    read_fd, write_fd = os.pipe()
    sent: list[int] = []
    monkeypatch.delattr(worker_process.os, "pidfd_open", raising=False)
    monkeypatch.delattr(worker_process.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(
        worker_process, "_LIBC_PIDFD_OPEN", lambda _pid, _flags: read_fd
    )
    monkeypatch.setattr(
        worker_process,
        "_LIBC_PIDFD_SEND_SIGNAL",
        lambda _fd, _sig, _info, _flags: sent.append(_sig) or 0,
    )
    monkeypatch.setattr(worker_process, "record_matches_process", lambda _record: False)
    monkeypatch.setattr(
        worker_process.os,
        "kill",
        lambda _pid, _sig: (_ for _ in ()).throw(
            AssertionError("unsafe PID fallback used")
        ),
    )
    try:
        assert worker_process.signal_worker(record, signal.SIGKILL) is False
    finally:
        os.close(write_fd)

    assert sent == []


@pytest.mark.parametrize("benign_stage", ["gone", "identity-mismatch"])
def test_signal_cli_ignores_workers_that_are_no_longer_the_recorded_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    benign_stage: str,
):
    record = worker_process.WorkerRecord(pid=1234, start_time="old", role="watch")
    monkeypatch.setattr(
        worker_process, "collect_worker_records", lambda *_args, **_kwargs: [record]
    )
    monkeypatch.setattr(
        worker_process.signal,
        "pidfd_send_signal",
        lambda _fd, _sig: None,
        raising=False,
    )
    if benign_stage == "gone":
        monkeypatch.setattr(
            worker_process.os,
            "pidfd_open",
            lambda _pid: (_ for _ in ()).throw(ProcessLookupError()),
            raising=False,
        )
    else:
        monkeypatch.setattr(
            worker_process.os,
            "pidfd_open",
            lambda _pid: (_ for _ in ()).throw(PermissionError("denied")),
            raising=False,
        )
        monkeypatch.setattr(
            worker_process, "record_matches_process", lambda _record: False
        )

    assert worker_process.main(["signal", "/tmp/mode-a.pids", "TERM"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_shell_signal_adapter_delegates_to_identity_module(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    trace = tmp_path / "trace"
    command = f"""
set -euo pipefail
export HARK_RUN_MODE_A_SOURCE_ONLY=1
export XDG_STATE_HOME={tmp_path!s}
source {script!s}
worker_identity() {{ printf '%s\\n' "$*" >> {trace!s}; }}
signal_verified_workers TERM
"""
    subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    expected_pidfile = tmp_path / "hark" / "mode-a.pids"
    assert trace.read_text(encoding="utf-8").strip() == (
        f"signal {expected_pidfile} TERM --discover"
    )


def test_shell_stop_retains_pidfile_when_identity_collection_fails(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    pidfile = state / "hark" / "mode-a.pids"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("sentinel\n", encoding="utf-8")
    command = f"""
set -euo pipefail
export HARK_RUN_MODE_A_SOURCE_ONLY=1
export XDG_STATE_HOME={state!s}
source {script!s}
worker_identity() {{ return 42; }}
set +e
graceful_stop 0 stop
status=$?
set -e
printf '%s\n' "$status"
"""
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "1"
    assert "refusing to stop" in result.stderr
    assert pidfile.read_text(encoding="utf-8") == "sentinel\n"


def test_shell_force_stop_retains_pidfile_when_kill_signal_fails(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    pidfile = state / "hark" / "mode-a.pids"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("sentinel\n", encoding="utf-8")
    command = f"""
set -euo pipefail
export HARK_RUN_MODE_A_SOURCE_ONLY=1
export XDG_STATE_HOME={state!s}
export HARK_STOP_GRACE_S=0
source {script!s}
worker_identity() {{
  case "$1" in
    collect) printf '1234\\n' ;;
    signal) [[ "$3" == TERM ]] ;;
  esac
}}
set +e
graceful_stop 1 stop
status=$?
set -e
printf '%s\n' "$status"
"""
    result = subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().endswith("1")
    assert "force-killing remaining processes: 1234" in result.stdout
    assert "retaining pidfile" in result.stderr
    assert pidfile.read_text(encoding="utf-8") == "sentinel\n"


def test_shell_start_refuses_when_initial_identity_collection_fails(tmp_path: Path):
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    state = tmp_path / "state"
    pidfile = state / "hark" / "mode-a.pids"
    pidfile.parent.mkdir(parents=True)
    pidfile.write_text("sentinel\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    uv.chmod(0o755)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(state)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        [str(script), "--no-ambient"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode != 0
    assert "refusing to start" in result.stderr
    assert pidfile.read_text(encoding="utf-8") == "sentinel\n"


def test_real_uv_shell_start_records_one_scoped_logical_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The real uv controller must not become a second/global worker identity."""
    repo = Path(__file__).resolve().parents[1]
    script = repo / "scripts" / "run-mode-a.sh"
    real_uv = shutil.which("uv")
    assert real_uv is not None
    state = tmp_path / "state"
    other_state = tmp_path / "other-state"
    config = tmp_path / "config"
    fake_bin = tmp_path / "workers"
    fake_bin.mkdir()
    fake_hark = fake_bin / "hark"
    fake_hark.write_text(
        "#!/usr/bin/env python3\n"
        "import signal\n"
        "import time\n"
        "signal.signal(signal.SIGTERM, lambda *_args: exit(0))\n"
        "while True:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
    )
    fake_hark.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "XDG_STATE_HOME": str(state),
            "XDG_CONFIG_HOME": str(config),
            "HARK_TEST_WORKER_EXECUTABLE": str(fake_hark),
            "PATH": f"{Path(real_uv).parent}{os.pathsep}{env['PATH']}",
        }
    )
    pidfile = state / "hark" / "mode-a.pids"
    records: list[worker_process.WorkerRecord] = []
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    try:
        result = subprocess.run(
            [str(script), "--no-ambient", "--session", "uv-regression"],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        records = worker_process.collect_worker_records(pidfile, discover=True)
        assert [(record.role, record.provisional) for record in records] == [
            ("watch", False)
        ]
        assert records[0].pidfile == str(pidfile.resolve())
        assert (
            worker_process.collect_worker_records(
                other_state / "hark" / "mode-a.pids", discover=True
            )
            == []
        )
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "other-config"))
        assert (
            worker_process.collect_worker_records(pidfile, discover=True, rewrite=False)
            == records
        )
    finally:
        if not records and pidfile.exists():
            records = worker_process.read_worker_records(pidfile)
        for record in records:
            worker_process.signal_worker(record, signal.SIGTERM)

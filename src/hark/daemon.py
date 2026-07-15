"""Experimental harkd process scaffold (optional always-on vs handsfree CLI).

Not required for handsfree v1. See docs/HARKD.md for the full boundary spec.

v0 provides:
  - single-instance pidfile under the shared XDG state dir
  - status / stop via that pidfile
  - refuse start when handsfree workers (mode-a.pids) or another harkd is live
  - optional --workers to supervise the same ambient/watch processes the launcher uses

It does **not** auto-deliver answers (no silent double-send with the skill path).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from hark import __version__
from hark.exitcodes import ERROR, OK, USAGE
from hark.paths import state_dir

HARKD_PID_NAME = "harkd.pid"
MODE_A_PIDS_NAME = "mode-a.pids"
BUSY_NAME = "busy.lock"
MIC_LOCK_NAME = "mic.lock"


class DaemonConflict(RuntimeError):
    """Another owner already holds always-on workers or harkd."""


@dataclass
class ProcessProbe:
    running: bool
    pids: list[int] = field(default_factory=list)
    pidfile: str | None = None


@dataclass
class DaemonStatus:
    state_dir: str
    harkd: ProcessProbe
    mode_a: ProcessProbe
    busy_lock: bool
    mic_lock: bool
    version: str = __version__

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_dir": self.state_dir,
            "harkd": asdict(self.harkd),
            "mode_a": asdict(self.mode_a),
            "busy_lock": self.busy_lock,
            "mic_lock": self.mic_lock,
            "version": self.version,
        }


def harkd_pid_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / HARKD_PID_NAME


def mode_a_pids_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / MODE_A_PIDS_NAME


def busy_lock_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / BUSY_NAME


def mic_lock_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / MIC_LOCK_NAME


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but not signalable by us — treat as live.
        return True
    except OSError:
        return False
    # Zombies remain in the process table until reaped; treat as not running
    # so stop/status do not hang waiting on a dead child we are not parenting.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # format: pid (comm) state ... — comm may contain spaces/parens
        rparen = stat.rfind(")")
        if rparen != -1:
            rest = stat[rparen + 2 :].split()
            if rest and rest[0] == "Z":
                return False
    except OSError:
        pass
    return True


def read_pids_file(path: Path) -> list[int]:
    if not path.is_file():
        return []
    pids: list[int] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # allow "pid=123" or bare "123"
        if line.startswith("pid="):
            line = line.split("=", 1)[1].strip()
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def live_pids_from_file(path: Path) -> list[int]:
    return sorted({p for p in read_pids_file(path) if pid_alive(p)})


def write_pid_file(path: Path, pids: Sequence[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    live = [p for p in pids if pid_alive(p)]
    if not live:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return
    path.write_text("".join(f"{p}\n" for p in live), encoding="utf-8")


def clear_pid_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def probe_harkd(root: Path | None = None) -> ProcessProbe:
    root = root or state_dir()
    path = harkd_pid_path(root)
    live = live_pids_from_file(path)
    return ProcessProbe(
        running=bool(live),
        pids=live,
        pidfile=str(path) if path.exists() else None,
    )


def probe_mode_a(root: Path | None = None) -> ProcessProbe:
    root = root or state_dir()
    path = mode_a_pids_path(root)
    live = live_pids_from_file(path)
    return ProcessProbe(
        running=bool(live),
        pids=live,
        pidfile=str(path) if path.exists() else None,
    )


def collect_status(root: Path | None = None) -> DaemonStatus:
    root = root or state_dir()
    return DaemonStatus(
        state_dir=str(root),
        harkd=probe_harkd(root),
        mode_a=probe_mode_a(root),
        busy_lock=busy_lock_path(root).is_file(),
        mic_lock=mic_lock_path(root).is_file(),
    )


def assert_can_start(root: Path | None = None, *, self_pid: int | None = None) -> None:
    """Refuse if another harkd or handsfree workers already own always-on role."""
    root = root or state_dir()
    harkd = probe_harkd(root)
    for pid in harkd.pids:
        if self_pid is not None and pid == self_pid:
            continue
        if pid_alive(pid):
            raise DaemonConflict(
                f"harkd already running (pid {pid}); stop it first: hark daemon stop"
            )
    mode_a = probe_mode_a(root)
    if mode_a.running:
        pids = ", ".join(str(p) for p in mode_a.pids)
        raise DaemonConflict(
            f"Hark workers are running (pids: {pids} via {MODE_A_PIDS_NAME}); "
            "stop them first (./scripts/run-mode-a.sh --stop) so harkd does not "
            "race ambient/watch or delivery ownership — see docs/HARKD.md"
        )


def acquire_harkd_pidfile(root: Path | None = None, *, pid: int | None = None) -> Path:
    """Write harkd.pid after exclusivity checks. Returns path."""
    root = root or state_dir()
    pid = os.getpid() if pid is None else pid
    assert_can_start(root, self_pid=pid)
    path = harkd_pid_path(root)
    # Stale pidfile (dead PID): replace
    existing = live_pids_from_file(path)
    if existing and existing != [pid]:
        raise DaemonConflict(
            f"harkd already running (pid {existing[0]}); stop it first: hark daemon stop"
        )
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")
    return path


def release_harkd_pidfile(root: Path | None = None, *, pid: int | None = None) -> None:
    root = root or state_dir()
    path = harkd_pid_path(root)
    if not path.is_file():
        return
    recorded = read_pids_file(path)
    if pid is not None and recorded and pid not in recorded:
        return
    clear_pid_file(path)


def stop_harkd(
    root: Path | None = None,
    *,
    timeout_s: float = 15.0,
    force: bool = False,
) -> dict[str, Any]:
    """SIGTERM the pid in harkd.pid; optionally SIGKILL after timeout."""
    root = root or state_dir()
    path = harkd_pid_path(root)
    live = live_pids_from_file(path)
    if not live:
        clear_pid_file(path)
        return {"ok": True, "stopped": [], "message": "harkd not running"}

    stopped: list[int] = []
    for pid in live:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            return {
                "ok": False,
                "stopped": stopped,
                "error": f"cannot signal pid {pid}: {exc}",
            }

    deadline = time.monotonic() + max(0.1, timeout_s)
    while time.monotonic() < deadline:
        still = [p for p in live if pid_alive(p)]
        if not still:
            clear_pid_file(path)
            return {"ok": True, "stopped": stopped, "message": "harkd stopped"}
        time.sleep(0.05)

    still = [p for p in live if pid_alive(p)]
    if still and force:
        for pid in still:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        clear_pid_file(path)
        return {
            "ok": True,
            "stopped": stopped,
            "killed": still,
            "message": "harkd force-killed",
        }

    if still:
        write_pid_file(path, still)
        return {
            "ok": False,
            "stopped": stopped,
            "still_running": still,
            "error": f"still running after {timeout_s:.0f}s; use --force",
        }

    clear_pid_file(path)
    return {"ok": True, "stopped": stopped, "message": "harkd stopped"}


def _hark_argv() -> list[str]:
    """Build argv prefix to re-enter the hark CLI (uv-friendly)."""
    # Prefer the same interpreter running this package.
    return [sys.executable, "-m", "hark"]


def spawn_mode_a_workers(
    *,
    session: str = "default",
    do_watch: bool = True,
    do_ambient: bool = True,
    root: Path | None = None,
    log_dir: Path | None = None,
) -> list[subprocess.Popen[Any]]:
    """Start ambient/watch children and record mode-a.pids (shared with handsfree launcher)."""
    root = root or state_dir()
    log_dir = log_dir or root
    log_dir.mkdir(parents=True, exist_ok=True)
    base = _hark_argv()
    children: list[subprocess.Popen[Any]] = []
    pids: list[int] = []

    if do_watch:
        watch_log = open(log_dir / "watch.jsonl", "a", encoding="utf-8")  # noqa: SIM115
        proc = subprocess.Popen(
            [
                *base,
                "watch",
                "--session",
                session,
                "--for-monitor",
                "--statuses",
                "blocked,done",
            ],
            stdout=watch_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        children.append(proc)
        if proc.pid:
            pids.append(proc.pid)

    if do_ambient:
        ambient_log = open(log_dir / "ambient.jsonl", "a", encoding="utf-8")  # noqa: SIM115
        proc = subprocess.Popen(
            [*base, "ambient"],
            stdout=ambient_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        children.append(proc)
        if proc.pid:
            pids.append(proc.pid)

    write_pid_file(mode_a_pids_path(root), pids)
    return children


def terminate_children(
    children: Sequence[subprocess.Popen[Any]],
    *,
    root: Path | None = None,
    timeout_s: float = 10.0,
) -> None:
    root = root or state_dir()
    for proc in children:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.terminate()
                except OSError:
                    pass
    deadline = time.monotonic() + timeout_s
    for proc in children:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining if remaining > 0 else 0.1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
    clear_pid_file(mode_a_pids_path(root))


def run_foreground(
    *,
    root: Path | None = None,
    workers: bool = False,
    do_watch: bool = True,
    do_ambient: bool = True,
    session: str = "default",
    idle_sleep_s: float = 0.5,
) -> int:
    """Foreground supervisor: hold harkd.pid; optional ambient/watch workers; wait for SIGTERM."""
    root = root or state_dir()
    children: list[subprocess.Popen[Any]] = []
    shutting_down = {"flag": False}

    def _handle_stop(signum: int, _frame: object) -> None:
        shutting_down["flag"] = True

    prev_term = signal.signal(signal.SIGTERM, _handle_stop)
    prev_int = signal.signal(signal.SIGINT, _handle_stop)

    try:
        acquire_harkd_pidfile(root, pid=os.getpid())
    except DaemonConflict as exc:
        print(f"harkd: {exc}", file=sys.stderr)
        return ERROR

    try:
        if workers:
            try:
                children = spawn_mode_a_workers(
                    session=session,
                    do_watch=do_watch,
                    do_ambient=do_ambient,
                    root=root,
                )
            except OSError as exc:
                print(f"harkd: failed to spawn workers: {exc}", file=sys.stderr)
                release_harkd_pidfile(root, pid=os.getpid())
                return ERROR
            print(
                f"harkd: started with workers pids={[c.pid for c in children]} "
                f"(state={root})",
                flush=True,
            )
        else:
            print(
                f"harkd: running foreground supervisor pid={os.getpid()} "
                f"(no workers; state={root}) — see docs/HARKD.md",
                flush=True,
            )

        while not shutting_down["flag"]:
            # If we own workers and they all exited, leave.
            if children and all(c.poll() is not None for c in children):
                print("harkd: all workers exited", flush=True)
                break
            # Refresh mode-a.pids with still-live children
            if children:
                write_pid_file(
                    mode_a_pids_path(root),
                    [c.pid for c in children if c.pid and c.poll() is None],
                )
            # Keep our own pidfile honest
            if not harkd_pid_path(root).is_file():
                # External clear — re-assert ownership while we run
                try:
                    acquire_harkd_pidfile(root, pid=os.getpid())
                except DaemonConflict:
                    print("harkd: lost pidfile exclusivity", file=sys.stderr)
                    break
            time.sleep(idle_sleep_s)

        return OK
    finally:
        if children:
            terminate_children(children, root=root)
        release_harkd_pidfile(root, pid=os.getpid())
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
        print("harkd: stopped", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="harkd",
        description=(
            "Experimental Hark always-on daemon scaffold (not required for handsfree v1). "
            "See docs/HARKD.md."
        ),
    )
    p.add_argument("--version", action="version", version=f"harkd {__version__}")
    sub = p.add_subparsers(dest="daemon_cmd", required=True)

    st = sub.add_parser("start", help="run foreground supervisor (single-instance)")
    st.add_argument(
        "--workers",
        action="store_true",
        help="also supervise ambient + watch (same pieces as run-mode-a.sh)",
    )
    st.add_argument("--no-watch", action="store_true", help="with --workers: skip watch")
    st.add_argument(
        "--no-ambient", action="store_true", help="with --workers: skip ambient"
    )
    st.add_argument("--session", default="default", help="Herdr session for watch")

    status = sub.add_parser("status", help="show harkd / workers / locks")
    status.add_argument("--json", action="store_true")

    stop = sub.add_parser("stop", help="SIGTERM harkd via pidfile")
    stop.add_argument(
        "--force",
        action="store_true",
        help="SIGKILL if still running after grace",
    )
    stop.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="seconds to wait after SIGTERM (default 15)",
    )
    stop.add_argument("--json", action="store_true")

    return p


def dispatch_daemon(args: argparse.Namespace) -> int:
    cmd = args.daemon_cmd
    if cmd == "start":
        if args.workers and args.no_watch and args.no_ambient:
            print("harkd: --workers with both --no-watch and --no-ambient is empty", file=sys.stderr)
            return USAGE
        return run_foreground(
            workers=bool(args.workers),
            do_watch=not bool(args.no_watch),
            do_ambient=not bool(args.no_ambient),
            session=str(args.session or "default"),
        )
    if cmd == "status":
        status = collect_status()
        if args.json:
            print(json.dumps(status.to_dict(), indent=2))
        else:
            h = status.harkd
            m = status.mode_a
            print(f"state_dir: {status.state_dir}")
            if h.running:
                print(f"harkd: running (pid {', '.join(str(p) for p in h.pids)})")
            else:
                print("harkd: not running")
            if m.running:
                print(f"workers: running (pids {', '.join(str(p) for p in m.pids)})")
            else:
                print("workers: not running")
            print(f"busy.lock: {'yes' if status.busy_lock else 'no'}")
            print(f"mic.lock: {'yes' if status.mic_lock else 'no'}")
        return OK
    if cmd == "stop":
        result = stop_harkd(timeout_s=float(args.timeout), force=bool(args.force))
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(result.get("message") or result.get("error") or json.dumps(result))
        return OK if result.get("ok") else ERROR
    return USAGE


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        from hark.stdio import configure_stdio

        configure_stdio()
    except Exception:
        pass
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else USAGE
    try:
        return dispatch_daemon(args)
    except DaemonConflict as exc:
        print(f"harkd: {exc}", file=sys.stderr)
        return ERROR
    except KeyboardInterrupt:
        return ABORT_SOFT


# local alias so KeyboardInterrupt maps cleanly without circular imports
ABORT_SOFT = 130


def run_cli_subcommand(argv: Sequence[str]) -> int:
    """Entry used by `hark daemon …` (argv without the 'daemon' word)."""
    return main(list(argv))

"""Keep SSH tied to the owning process, including pre-marker owner death.

A process-identity supervisor survives the creating Python thread's exit. SSH
executes in its own helper with PDEATHSIG attached to the supervisor's durable
main thread. Native setup runs in a fresh interpreter, never a preexec_fn.
"""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time

from hark.herdr.tunnel import ProcessIdentity, capture_process_identity


def supervise(parent: ProcessIdentity, argv: list[str]) -> None:
    """Own SSH until the exact Hark process dies or cleanup signals us."""
    if not argv or capture_process_identity(parent.pid) != parent:
        raise RuntimeError("tunnel owner exited before child startup")
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if stopping:
        return
    child = subprocess.Popen(
        [sys.executable, "-m", "hark.herdr.tunnel_child", "exec", str(os.getpid()), *argv],
        stdin=subprocess.DEVNULL,
    )
    try:
        while not stopping and capture_process_identity(parent.pid) == parent:
            status = child.poll()
            if status is not None:
                raise RuntimeError(f"SSH child exited with status {status}")
            time.sleep(0.05)
    finally:
        # This handle still owns an unreaped child, so its PID cannot be reused.
        if child.poll() is None:
            child.kill()
        child.wait()


def exec_owned(expected_parent: int, argv: list[str]) -> None:
    if sys.platform != "linux":
        raise RuntimeError("tunnel parent-death protection requires Linux")
    if expected_parent <= 1 or not argv:
        raise ValueError("expected a parent PID and SSH command")

    libc = ctypes.CDLL(None, use_errno=True)
    prctl = getattr(libc, "prctl", None)
    if prctl is None:
        raise RuntimeError("Linux parent-death protection is unavailable")
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    if os.getppid() != expected_parent:
        # The creating process has already exited. No SSH child may be started.
        raise RuntimeError("tunnel owner exited before child startup")
    os.execvp(argv[0], argv)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if args and args[0] == "supervise" and len(args) >= 5:
            supervise(ProcessIdentity(int(args[1]), args[2], args[3]), args[4:])
        elif args and args[0] == "exec" and len(args) >= 3:
            exec_owned(int(args[1]), args[2:])
        else:
            raise ValueError("expected helper mode, parent identity and SSH command")
    except Exception as exc:
        print(f"hark tunnel: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

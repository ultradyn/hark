"""Linux pidfd operations for Python builds that omit the stdlib wrappers.

Use libc's named interfaces rather than architecture-specific syscall numbers.
An exported interface can still fail (for example ENOSYS on an old kernel);
callers retain their existing policy for unavailable or failed operations.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import sys
from typing import Any


def _load_libc_functions() -> tuple[Any, Any]:
    if sys.platform != "linux":
        return None, None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        opener = libc.pidfd_open
        sender = libc.pidfd_send_signal
    except (AttributeError, OSError):
        return None, None
    opener.argtypes = [ctypes.c_int, ctypes.c_uint]
    opener.restype = ctypes.c_int
    sender.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    sender.restype = ctypes.c_int
    return opener, sender


_LIBC_OPEN, _LIBC_SEND = _load_libc_functions()


def available() -> bool:
    """Whether both operations have an implementation on this host."""
    return sys.platform == "linux" and (
        callable(getattr(os, "pidfd_open", None)) or _LIBC_OPEN is not None
    ) and (
        callable(getattr(signal, "pidfd_send_signal", None)) or _LIBC_SEND is not None
    )


def open_pidfd(pid: int, flags: int = 0) -> int:
    """Open a process descriptor, raising OSError if the host cannot do so."""
    native = getattr(os, "pidfd_open", None)
    if callable(native):
        return native(pid, flags)
    if sys.platform != "linux" or _LIBC_OPEN is None:
        raise OSError(errno.ENOSYS, "pidfd_open is unavailable")
    ctypes.set_errno(0)
    result = _LIBC_OPEN(pid, flags)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result)


def send_signal(pidfd: int, signum: int) -> None:
    """Signal exactly the process represented by a descriptor."""
    native = getattr(signal, "pidfd_send_signal", None)
    if callable(native):
        native(pidfd, signum)
        return
    if sys.platform != "linux" or _LIBC_SEND is None:
        raise OSError(errno.ENOSYS, "pidfd_send_signal is unavailable")
    ctypes.set_errno(0)
    result = _LIBC_SEND(pidfd, signum, None, 0)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))

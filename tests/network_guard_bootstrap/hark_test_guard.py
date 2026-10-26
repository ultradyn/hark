"""Test-only socket policy shared by pytest and ordinary Python children."""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path

BOOTSTRAP = str(Path(__file__).resolve().parent)
ACTIVE = "HARK_TEST_NETWORK_GUARD"
ALLOW = "HARK_TEST_PROVIDER_NETWORK_ENABLED"
_installed = False


class ProviderNetworkBlocked(AssertionError):
    """An ordinary test attempted external network IO."""


def live_provider_allowed(*, marker: bool, environ=None) -> bool:
    env = os.environ if environ is None else environ
    return marker and env.get("HARK_TEST_PROVIDER_NETWORK") == "1"


def guarded_subprocess_env(overrides=None) -> dict[str, str]:
    """Preserve active test policy when a child needs a custom environment."""
    env = dict(os.environ if overrides is None else overrides)
    env[ACTIVE] = "1"
    env[ALLOW] = os.environ.get(ALLOW, "0")
    paths = env.get("PYTHONPATH", "").split(os.pathsep)
    env["PYTHONPATH"] = os.pathsep.join(
        [BOOTSTRAP, *(path for path in paths if path and path != BOOTSTRAP)]
    )
    return env


def check_host(host) -> None:
    if os.environ.get(ALLOW) == "1":
        return
    if host is None or host == "" or host == b"":
        return  # lookup for a local bind
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    value = str(host).lower().rstrip(".")
    if value == "localhost":
        return
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        address = None
    if address is not None:
        mapped = getattr(address, "ipv4_mapped", None)
        if address.is_loopback or (mapped is not None and mapped.is_loopback):
            return
    raise ProviderNetworkBlocked(
        f"B190: external provider/network IO blocked for {host!r}. "
        "Use a mocked transport or loopback fixture. Live verification requires "
        "both @pytest.mark.allow_provider_network and HARK_TEST_PROVIDER_NETWORK=1."
    )


def install() -> None:
    """Install once; pytest sets the per-test policy through inherited env."""
    global _installed
    if _installed:
        return
    _installed = True
    real_lookup = socket.getaddrinfo

    def lookup(host, *args, **kwargs):
        check_host(host)
        return real_lookup(host, *args, **kwargs)

    socket.getaddrinfo = lookup
    for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        real = getattr(socket, name)

        def resolve(host, *args, _real=real, **kwargs):
            check_host(host)
            return _real(host, *args, **kwargs)

        setattr(socket, name, resolve)

    for name in ("connect", "connect_ex"):
        real = getattr(socket.socket, name)

        def connect(self, address, *args, _real=real, **kwargs):
            if self.family in (socket.AF_INET, socket.AF_INET6):
                check_host(address[0])
            return _real(self, address, *args, **kwargs)

        setattr(socket.socket, name, connect)

    real_sendto = socket.socket.sendto

    def sendto(self, data, *args):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            check_host(args[-1][0])
        return real_sendto(self, data, *args)

    socket.socket.sendto = sendto
    if hasattr(socket.socket, "sendmsg"):
        real_sendmsg = socket.socket.sendmsg

        def sendmsg(self, buffers, ancdata=(), flags=0, address=None):
            if address is not None and self.family in (socket.AF_INET, socket.AF_INET6):
                check_host(address[0])
            if address is None:
                return real_sendmsg(self, buffers, ancdata, flags)
            return real_sendmsg(self, buffers, ancdata, flags, address)

        socket.socket.sendmsg = sendmsg

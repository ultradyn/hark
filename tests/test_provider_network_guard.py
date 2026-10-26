"""B190: deny accidental spend before DNS/socket IO, including Python children."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from network_guard_bootstrap import hark_test_guard as guard


@pytest.mark.parametrize("marker,flag,expected", [
    (False, "0", False), (False, "1", False),
    (True, "0", False), (True, "1", True),
])
def test_live_optin_requires_marker_and_environment(marker, flag, expected):
    assert guard.live_provider_allowed(
        marker=marker, environ={"HARK_TEST_PROVIDER_NETWORK": flag},
    ) is expected


@pytest.mark.parametrize("host", ["api.example.invalid", "203.0.113.1", "2001:db8::1"])
def test_external_lookup_and_connections_denied_before_io(host):
    with pytest.raises(guard.ProviderNetworkBlocked, match="B190"):
        socket.getaddrinfo(host, 443)
    with socket.socket() as sock:
        with pytest.raises(guard.ProviderNetworkBlocked, match="B190"):
            sock.connect((host, 443))
        with pytest.raises(guard.ProviderNetworkBlocked, match="B190"):
            sock.connect_ex((host, 443))


def test_external_udp_denied_before_io():
    with socket.socket(type=socket.SOCK_DGRAM) as sock:
        with pytest.raises(guard.ProviderNetworkBlocked, match="B190"):
            sock.sendto(b"no packets", ("203.0.113.1", 12345))


def test_httpx_default_transport_denied_but_mocktransport_allowed():
    with httpx.Client(trust_env=False) as client:
        with pytest.raises(guard.ProviderNetworkBlocked, match="B190"):
            client.get("https://provider.example.invalid")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
    with httpx.Client(transport=transport) as client:
        assert client.get("https://provider.example.invalid").json() == {"ok": True}


def test_loopback_and_unix_connections_still_work(tmp_path):
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        with socket.create_connection(server.getsockname(), timeout=1) as client:
            peer, _ = server.accept()
            with peer:
                client.sendall(b"local")
                assert peer.recv(5) == b"local"
    if hasattr(socket, "AF_UNIX"):
        with socket.socket(socket.AF_UNIX) as server:
            path = str(tmp_path / "local.sock")
            server.bind(path)
            server.listen()
            with socket.socket(socket.AF_UNIX) as client:
                client.connect(path)
                peer, _ = server.accept()
                peer.close()


_CHILD_PROBE = """
import socket
try:
    socket.create_connection(('provider.example.invalid', 443), timeout=0.1)
except AssertionError as exc:
    assert 'B190' in str(exc)
    print('BLOCKED_BEFORE_IO')
else:
    raise AssertionError('network policy did not reach Python child')
"""


@pytest.mark.parametrize("custom_env", [False, True])
def test_guard_reaches_spawned_python_children(guarded_subprocess_env, custom_env):
    env = guarded_subprocess_env({}) if custom_env else None
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE], env=env,
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BLOCKED_BEFORE_IO"


def test_guard_reaches_nested_python_children():
    launcher = "import subprocess,sys; sys.exit(subprocess.run([sys.executable, '-c', sys.argv[1]]).returncode)"
    result = subprocess.run(
        [sys.executable, "-c", launcher, _CHILD_PROBE],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "BLOCKED_BEFORE_IO"


def test_bootstrap_preserves_other_sitecustomize(tmp_path, guarded_subprocess_env):
    (tmp_path / "sitecustomize.py").write_text("print('ORIGINAL_STARTUP_PROBE')\n")
    env = guarded_subprocess_env({**os.environ, "PYTHONPATH": str(tmp_path)})
    result = subprocess.run(
        [sys.executable, "-c", _CHILD_PROBE], env=env,
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["ORIGINAL_STARTUP_PROBE", "BLOCKED_BEFORE_IO"]


@pytest.mark.allow_provider_network
def test_marked_test_obeys_explicit_environment_without_opening_network():
    expected = (
        os.environ.get("HARK_TEST_PROVIDER_NETWORK") == "1"
    )
    assert (os.environ[guard.ALLOW] == "1") is expected
    result = subprocess.run(
        [sys.executable, "-c", f"import os; print(os.environ[{guard.ALLOW!r}])"],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("1" if expected else "0")


@pytest.mark.allow_media_control
def test_media_permission_does_not_enable_provider_or_native_playback():
    with pytest.raises(guard.ProviderNetworkBlocked):
        guard.check_host("provider.example.invalid")
    from hark.audio import playback

    if playback.sd is not None:
        with pytest.raises(AssertionError, match="B190"):
            playback.sd.RawOutputStream(samplerate=16000, channels=1, dtype="int16")


def test_guard_does_not_replace_native_process_authority():
    from hark import tts_isolation, tts_worker

    assert subprocess.Popen.__init__ is tts_isolation._NATIVE_SYNTH_POPEN_INIT
    assert subprocess.Popen.__init__ is tts_worker._NATIVE_PAYLOAD_POPEN_INIT
    assert Path(guard.BOOTSTRAP).is_dir()

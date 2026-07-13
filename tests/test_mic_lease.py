import os
from pathlib import Path
import subprocess
import sys

from hark.audio.capture import MicLease


def _child_acquire(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from hark.audio.capture import MicBusyError, MicLease\n"
            "try:\n"
            "    with MicLease('child'):\n"
            "        pass\n"
            "except MicBusyError:\n"
            "    raise SystemExit(1)\n",
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_mic_lease_is_exclusive_across_processes_and_releases(tmp_path, monkeypatch):
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    env = os.environ.copy()
    source_dir = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_dir), env.get("PYTHONPATH")) if part
    )

    with MicLease("parent"):
        assert (state_home / "hark" / "mic.lock").is_file()
        assert _child_acquire(env).returncode == 1

    assert _child_acquire(env).returncode == 0

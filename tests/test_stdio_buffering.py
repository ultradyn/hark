"""B109: line-buffered stdout when piped; warn on interactive | tail traps."""

from __future__ import annotations

import io
import sys
from types import SimpleNamespace

import pytest

from hark.stdio import (
    _looks_interactive,
    _reset_for_tests,
    configure_stdio,
    is_tty,
    maybe_warn_non_tty_stdout,
)


@pytest.fixture(autouse=True)
def _reset_stdio_latches():
    _reset_for_tests()
    yield
    _reset_for_tests()


def test_configure_stdio_calls_reconfigure_with_line_buffering():
    calls: list[dict] = []

    class FakeStream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    out = FakeStream()
    err = FakeStream()
    result = configure_stdio(stdout=out, stderr=err, force=True)
    assert result == {"stdout": True, "stderr": True}
    assert calls[0]["line_buffering"] is True
    assert calls[0].get("write_through") is True
    assert calls[1]["line_buffering"] is True


def test_configure_stdio_tolerates_streams_without_reconfigure():
    buf = io.StringIO()
    result = configure_stdio(stdout=buf, stderr=buf, force=True)
    assert result == {"stdout": False, "stderr": False}


def test_configure_stdio_idempotent_without_force():
    class FakeStream:
        def __init__(self):
            self.n = 0

        def reconfigure(self, **kwargs):
            self.n += 1

    out = FakeStream()
    configure_stdio(stdout=out, stderr=out, force=True)
    n_after_first = out.n
    configure_stdio(stdout=out, stderr=out, force=False)
    assert out.n == n_after_first


def test_is_tty_false_for_stringio():
    assert is_tty(io.StringIO()) is False


def test_looks_interactive_detects_listen_paths():
    assert _looks_interactive(["monitor", "--for-monitor"]) is True
    assert _looks_interactive(["watch", "--for-monitor"]) is True
    assert _looks_interactive(["ambient"]) is True
    assert _looks_interactive(["listen"]) is True
    assert _looks_interactive(["ask", "hello"]) is True
    assert _looks_interactive(["tts", "--listen", "hi"]) is True
    assert _looks_interactive(["tts", "--listen-for-user-response", "hi"]) is True
    assert _looks_interactive(["tts", "just speak"]) is False
    assert _looks_interactive(["doctor"]) is False
    assert _looks_interactive(["status"]) is False
    assert _looks_interactive(["--config", "/tmp/c.toml", "monitor"]) is True
    assert _looks_interactive(["agentapi", "deliver", "--follow-monitor"]) is True
    assert _looks_interactive(["watch-logs", "--follow"]) is True
    assert _looks_interactive(["watch-logs"]) is False


def test_maybe_warn_non_tty_for_monitor(monkeypatch):
    err = io.StringIO()
    out = io.StringIO()  # not a tty
    assert maybe_warn_non_tty_stdout(
        ["monitor", "--for-monitor"], stream=out, err=err, force=True
    )
    text = err.getvalue()
    assert "not a TTY" in text
    assert "tail" in text.lower()
    assert "monitor" in text


def test_maybe_warn_skips_tty_stdout():
    err = io.StringIO()
    tty = SimpleNamespace(isatty=lambda: True)
    assert (
        maybe_warn_non_tty_stdout(["monitor"], stream=tty, err=err, force=True)
        is False
    )
    assert err.getvalue() == ""


def test_maybe_warn_skips_non_interactive():
    err = io.StringIO()
    assert (
        maybe_warn_non_tty_stdout(
            ["doctor", "--json"], stream=io.StringIO(), err=err, force=True
        )
        is False
    )
    assert err.getvalue() == ""


def test_maybe_warn_respects_env_silence(monkeypatch):
    monkeypatch.setenv("HARK_NO_PIPE_WARN", "1")
    err = io.StringIO()
    assert (
        maybe_warn_non_tty_stdout(
            ["listen"], stream=io.StringIO(), err=err, force=True
        )
        is False
    )
    assert err.getvalue() == ""


def test_main_invokes_configure_stdio(monkeypatch):
    """cli.main must call configure_stdio before dispatch (B109 wiring)."""
    from hark import cli as cli_mod

    called: list[str] = []

    def fake_configure():
        called.append("configure")
        return {"stdout": True, "stderr": True}

    def fake_warn(argv=None):
        called.append("warn")
        return False

    monkeypatch.setattr("hark.stdio.configure_stdio", fake_configure)
    monkeypatch.setattr("hark.stdio.maybe_warn_non_tty_stdout", fake_warn)

    # doctor --json needs config; stub load + dispatch light path
    monkeypatch.setattr(
        cli_mod,
        "load_config",
        lambda *a, **k: SimpleNamespace(warnings=[]),
    )
    monkeypatch.setattr(cli_mod, "dispatch", lambda args, cfg: 0)
    monkeypatch.setattr(
        cli_mod,
        "build_parser",
        lambda: SimpleNamespace(
            parse_args=lambda argv: SimpleNamespace(cmd="doctor", json=True, config_path=None)
        ),
    )

    rc = cli_mod.main(["doctor", "--json"])
    assert rc == 0
    assert called == ["configure", "warn"]


def test_line_buffered_pipe_streams_progressively(tmp_path):
    """Integration: child with configure_stdio flushes lines through a pipe promptly.

    Without reconfigure, a short fully-buffered print may stay invisible until
    exit; with line_buffering + write_through, the reader sees the line before
    the writer sleeps.
    """
    import os
    import select
    import subprocess
    import textwrap
    import time
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "producer.py"
    script.write_text(
        textwrap.dedent(
            """\
            import time
            from hark.stdio import configure_stdio
            configure_stdio(force=True)
            print("PROGRESS")
            time.sleep(2.0)
            print("DONE")
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(root / "src"), "HARK_UPDATE_CHECK": "0"},
    )
    assert proc.stdout is not None
    deadline = time.monotonic() + 1.0
    buf = ""
    try:
        while time.monotonic() < deadline and "PROGRESS" not in buf:
            r, _, _ = select.select([proc.stdout], [], [], 0.1)
            if r:
                chunk = proc.stdout.readline()
                if not chunk:
                    break
                buf += chunk
        assert "PROGRESS" in buf, f"expected progressive line before sleep; got {buf!r}"
    finally:
        proc.kill()
        proc.wait(timeout=2)

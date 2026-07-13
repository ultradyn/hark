"""B055 — coding CLI argv resolver."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hark.agents.resolve import (
    ResolveError,
    is_safe_executable,
    resolve_adhoc_argv,
    resolve_agent_argv,
)


def _fake_path(tmp_path: Path, entries: dict[str, str | Path]) -> str:
    """Create executables; values may be symlink targets (relative names)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, target in entries.items():
        path = bindir / name
        if isinstance(target, Path) or (isinstance(target, str) and target.startswith("/")):
            path.symlink_to(target)
        elif target == "self":
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        else:
            # symlink to sibling name
            path.symlink_to(target)
            sibling = bindir / Path(target).name
            if not sibling.exists() and target != name:
                sibling.write_text("#!/bin/sh\nexit 0\n")
                sibling.chmod(0o755)
    return str(bindir)


def test_prefer_alias_when_safe(tmp_path: Path):
    path = _fake_path(
        tmp_path,
        {
            "cc": "self",  # safe claude-like shim (not under /usr/bin)
            "claude": "self",
        },
    )
    # Place under tmp so /usr/bin rule does not fire; real_name cc without gcc
    r = resolve_agent_argv("claude", path=path)
    assert r.source == "alias"
    assert r.agent_key == "claude"
    assert Path(r.command).name == "cc"


def test_reject_gcc_as_cc(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gcc = bindir / "gcc"
    gcc.write_text("#!/bin/sh\n")
    gcc.chmod(0o755)
    cc = bindir / "cc"
    cc.symlink_to(gcc)
    claude = bindir / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    path = str(bindir)
    r = resolve_agent_argv("claude", path=path)
    assert r.source == "canonical"
    assert Path(r.command).name == "claude"
    assert is_safe_executable("cc", path=path) is None


def test_reject_coderabbit_as_cr(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cr = bindir / "cr"
    # path contains coderabbit
    rabbit = bindir / "coderabbit"
    rabbit.write_text("#!/bin/sh\n")
    rabbit.chmod(0o755)
    cr.symlink_to(rabbit)
    cursor = bindir / "cursor-agent"
    cursor.write_text("#!/bin/sh\n")
    cursor.chmod(0o755)
    path = str(bindir)
    r = resolve_agent_argv("cursor-agent", path=path)
    assert r.source == "canonical"
    assert Path(r.command).name == "cursor-agent"


def test_override_wins(tmp_path: Path):
    path = _fake_path(tmp_path, {"my-claude": "self", "claude": "self"})
    r = resolve_agent_argv(
        "claude",
        overrides={"claude": "my-claude"},
        path=path,
    )
    assert r.source == "override"
    assert Path(r.command).name == "my-claude"


def test_adhoc_argv(tmp_path: Path):
    path = _fake_path(tmp_path, {"opencode": "self"})
    r = resolve_adhoc_argv("opencode", extra_args=["--foo"], path=path)
    assert r.source == "adhoc"
    assert r.agent_key == "adhoc"
    assert r.argv[-1] == "--foo"


def test_unknown_agent_raises():
    with pytest.raises(ResolveError, match="unknown agent"):
        resolve_agent_argv("not-a-real-agent-xyz")


def test_extra_args_appended(tmp_path: Path):
    path = _fake_path(tmp_path, {"grok": "self"})
    r = resolve_agent_argv("gk", extra_args=["-p", "hi"], path=path)
    assert r.agent_key == "grok"
    assert r.argv[-2:] == ["-p", "hi"]

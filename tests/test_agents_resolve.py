"""B055 — coding CLI argv resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from hark.agents.resolve import (
    ResolveError,
    ResolveFailureReason,
    is_safe_executable,
    resolve_adhoc_argv,
    resolve_agent_argv,
    resolve_flexible,
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
    with pytest.raises(ResolveError, match="unknown agent") as exc_info:
        resolve_agent_argv("not-a-real-agent-xyz")
    assert exc_info.value.reason is ResolveFailureReason.UNKNOWN_AGENT


def test_empty_agent_has_structured_reason():
    with pytest.raises(ResolveError, match="empty agent") as exc_info:
        resolve_agent_argv("   ")
    assert exc_info.value.reason is ResolveFailureReason.EMPTY_COMMAND


def test_flexible_only_falls_back_for_unknown_catalog_token(tmp_path: Path):
    path = _fake_path(tmp_path, {"custom-agent": "self"})

    resolved = resolve_flexible("custom-agent", path=path)

    assert resolved.agent_key == "adhoc"
    assert Path(resolved.command).name == "custom-agent"


def test_flexible_preserves_known_agent_failure(tmp_path: Path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gcc = bindir / "gcc"
    gcc.write_text("#!/bin/sh\n")
    gcc.chmod(0o755)
    (bindir / "cc").symlink_to(gcc)

    with pytest.raises(ResolveError, match="no safe executable") as exc_info:
        resolve_flexible("cc", path=str(bindir))

    assert exc_info.value.reason is ResolveFailureReason.NO_SAFE_EXECUTABLE


def test_malformed_override_has_structured_reason(tmp_path: Path):
    path = _fake_path(tmp_path, {"codex": "self"})

    with pytest.raises(ResolveError, match="malformed override") as exc_info:
        resolve_flexible("codex", overrides={"codex": "'bad"}, path=path)

    assert exc_info.value.reason is ResolveFailureReason.INVALID_OVERRIDE


def test_unknown_unsafe_binary_reports_adhoc_failure_reason(tmp_path: Path):
    path = _fake_path(tmp_path, {"unrelated": "self"})

    with pytest.raises(ResolveError, match="not found on PATH") as exc_info:
        resolve_flexible("custom-agent", path=path)

    assert exc_info.value.reason is ResolveFailureReason.INVALID_ADHOC_COMMAND


def test_extra_args_appended(tmp_path: Path):
    path = _fake_path(tmp_path, {"grok": "self"})
    r = resolve_agent_argv("gk", extra_args=["-p", "hi"], path=path)
    assert r.agent_key == "grok"
    assert r.argv[-2:] == ["-p", "hi"]

"""B055 — coding CLI argv resolver."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from hark.agents.resolve import (
    AGENT_CATALOG,
    ResolveError,
    ResolveFailureReason,
    is_safe_executable,
    resolve_adhoc_argv,
    resolve_agent_argv,
    resolve_flexible,
)


MULTIWORD_CATALOG_NAMES = tuple(
    (name, spec.key, spec.canonical)
    for spec in AGENT_CATALOG
    for name in (*spec.aliases, *spec.names)
    if " " in name
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


@pytest.mark.parametrize(
    ("agent", "override", "target_name"),
    (("claude", "cc", "gcc"), ("cursor-agent", "cr", "coderabbit")),
)
def test_override_path_token_applies_collision_rejects(
    tmp_path: Path, agent: str, override: str, target_name: str
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = bindir / target_name
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    (bindir / override).symlink_to(target)

    with pytest.raises(
        ResolveError, match=rf"agent {agent!r}.*{override!r}"
    ) as exc_info:
        resolve_agent_argv(agent, overrides={agent: override}, path=str(bindir))

    assert exc_info.value.reason is ResolveFailureReason.INVALID_OVERRIDE


@pytest.mark.parametrize("relative", (False, True))
@pytest.mark.parametrize(
    "invalid_kind", ("missing", "broken", "directory", "non-executable")
)
def test_override_path_requires_regular_executable(
    tmp_path: Path, monkeypatch, relative: bool, invalid_kind: str
):
    target = tmp_path / "override-agent"
    if invalid_kind == "broken":
        target.symlink_to(tmp_path / "missing-target")
    elif invalid_kind == "directory":
        target.mkdir()
    elif invalid_kind == "non-executable":
        target.write_text("not executable\n")
        target.chmod(0o644)

    monkeypatch.chdir(tmp_path)
    override = f"./{target.name}" if relative else str(target)

    with pytest.raises(
        ResolveError, match=rf"agent 'codex'.*{target.name}"
    ) as exc_info:
        resolve_agent_argv("codex", overrides={"codex": override})

    assert exc_info.value.reason is ResolveFailureReason.INVALID_OVERRIDE


@pytest.mark.parametrize("relative", (False, True))
@pytest.mark.parametrize("suffix", ("/", "/."))
def test_override_rejects_file_path_with_directory_syntax(
    tmp_path: Path, monkeypatch, relative: bool, suffix: str
):
    target = tmp_path / "custom-codex"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    base = f"./{target.name}" if relative else str(target)
    override = f"{base}{suffix}"

    with pytest.raises(
        ResolveError, match=rf"agent 'codex'.*{target.name}"
    ) as exc_info:
        resolve_agent_argv("codex", overrides={"codex": override})

    assert exc_info.value.reason is ResolveFailureReason.INVALID_OVERRIDE


@pytest.mark.parametrize("relative", (False, True))
def test_override_safe_path_preserves_prefix_and_extra_args(
    tmp_path: Path, monkeypatch, relative: bool
):
    target = tmp_path / "custom-codex"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    override = f"./{target.name}" if relative else str(target)

    resolved = resolve_agent_argv(
        "codex",
        overrides={"codex": [override, "--configured"]},
        extra_args=["--requested"],
    )

    selected = (
        os.path.join(str(tmp_path), override) if relative else override
    )
    assert resolved.argv == [selected, "--configured", "--requested"]
    assert resolved.source == "override"


def test_override_relative_path_entry_is_pinned_absolute(
    tmp_path: Path, monkeypatch
):
    bindir = tmp_path / "relative-bin"
    bindir.mkdir()
    target = bindir / "custom-codex"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    monkeypatch.chdir(tmp_path)

    resolved = resolve_agent_argv(
        "codex",
        overrides={"codex": "custom-codex --configured"},
        path="relative-bin",
    )

    assert resolved.argv == [
        os.path.join(str(tmp_path), "relative-bin/custom-codex"),
        "--configured",
    ]


def test_override_symlink_preserves_selected_argv0_dispatch(
    tmp_path: Path, monkeypatch
):
    target = tmp_path / "agent-dispatcher"
    target.write_text(
        "#!/bin/sh\n"
        'if [ "$(basename "$0")" = "custom-codex" ]; then\n'
        "  printf selected\n"
        "else\n"
        "  printf target\n"
        "fi\n"
    )
    target.chmod(0o755)
    shim = tmp_path / "custom-codex"
    shim.symlink_to(target)
    monkeypatch.chdir(tmp_path)

    resolved = resolve_agent_argv(
        "codex",
        overrides={"codex": ["./custom-codex", "--configured"]},
    )

    assert resolved.argv == [
        os.path.join(str(tmp_path), "./custom-codex"),
        "--configured",
    ]
    executed = subprocess.run(
        resolved.argv[:1],
        check=True,
        capture_output=True,
        text=True,
    )
    assert executed.stdout == "selected"


def test_override_preserves_symlink_before_dotdot_execution(
    tmp_path: Path, monkeypatch
):
    elsewhere = tmp_path / "elsewhere"
    nested = elsewhere / "nested"
    nested.mkdir(parents=True)
    (tmp_path / "linkdir").symlink_to(nested, target_is_directory=True)

    actual = elsewhere / "custom-codex"
    actual.write_text("#!/bin/sh\nprintf actual\n")
    actual.chmod(0o755)
    sentinel = tmp_path / "custom-codex"
    sentinel.write_text("#!/bin/sh\nprintf sentinel\n")
    sentinel.chmod(0o755)
    monkeypatch.chdir(tmp_path)

    command = "./linkdir/../custom-codex"
    resolved = resolve_agent_argv(
        "codex",
        overrides={"codex": [command, "--configured"]},
    )

    assert resolved.argv[0] == os.path.join(str(tmp_path), command)
    executed = subprocess.run(
        resolved.argv[:1],
        check=True,
        capture_output=True,
        text=True,
    )
    assert executed.stdout == "actual"
    assert Path(resolved.argv[0]).resolve() == actual.resolve()
    assert Path(resolved.argv[0]).resolve() != sentinel.resolve()


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


@pytest.mark.parametrize(
    ("command", "target_name"),
    (("cc --version", "gcc"), ("cr review", "coderabbit")),
)
def test_flexible_classifies_quoted_command_head_before_fallback(
    tmp_path: Path, command: str, target_name: str
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = bindir / target_name
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    (bindir / command.split()[0]).symlink_to(target)

    with pytest.raises(ResolveError, match="no safe executable") as exc_info:
        resolve_flexible(command, path=str(bindir))

    assert exc_info.value.reason is ResolveFailureReason.NO_SAFE_EXECUTABLE


def test_flexible_quoted_safe_catalog_command_preserves_embedded_args(tmp_path: Path):
    path = _fake_path(tmp_path, {"codex": "self"})

    resolved = resolve_flexible("codex --version", path=path)

    assert resolved.agent_key == "codex"
    assert resolved.argv[-1] == "--version"


@pytest.mark.parametrize(
    ("catalog_name", "expected_key", "canonical"),
    MULTIWORD_CATALOG_NAMES,
)
def test_flexible_prefers_longest_multiword_catalog_name(
    tmp_path: Path, catalog_name: str, expected_key: str, canonical: str
):
    path = _fake_path(
        tmp_path,
        {
            canonical: "self",
            catalog_name.split()[0]: "self",
        },
    )

    resolved = resolve_flexible(f"{catalog_name} --probe", path=path)

    assert resolved.agent_key == expected_key
    assert resolved.source == "canonical"
    assert resolved.argv == [str(Path(path) / canonical), "--probe"]


def test_flexible_catalog_prefix_ambiguity_keeps_unknown_command_adhoc(tmp_path: Path):
    path = _fake_path(tmp_path, {"open": "self"})

    resolved = resolve_flexible("open sesame", path=path)

    assert resolved.agent_key == "adhoc"
    assert resolved.argv == [str(Path(path) / "open"), "sesame"]


def test_flexible_singleword_catalog_prefix_keeps_remaining_args(tmp_path: Path):
    path = _fake_path(tmp_path, {"claude": "self"})

    resolved = resolve_flexible("claude custom", path=path)

    assert resolved.agent_key == "claude"
    assert resolved.argv == [str(Path(path) / "claude"), "custom"]


@pytest.mark.parametrize(
    ("relative", "directory"),
    ((False, False), (True, False), (False, True), (True, True)),
)
def test_implicit_path_requires_regular_executable(
    tmp_path: Path, monkeypatch, relative: bool, directory: bool
):
    target = tmp_path / ("plain-dir" if directory else "plain-file")
    if directory:
        target.mkdir()
    else:
        target.write_text("not executable\n")
        target.chmod(0o644)
    monkeypatch.chdir(tmp_path)
    command = f"./{target.name}" if relative else str(target)

    with pytest.raises(ResolveError, match="regular executable") as exc_info:
        resolve_flexible(command)

    assert exc_info.value.reason is ResolveFailureReason.INVALID_ADHOC_COMMAND


@pytest.mark.parametrize("relative", (False, True))
def test_implicit_unknown_executable_path_is_allowed(
    tmp_path: Path, monkeypatch, relative: bool
):
    target = tmp_path / "custom-agent"
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    command = f"./{target.name}" if relative else str(target)

    resolved = resolve_flexible(command)

    assert resolved.source == "adhoc"
    assert resolved.command == command


def test_explicit_adhoc_preserves_operator_selected_non_executable_path(tmp_path: Path):
    target = tmp_path / "plain-file"
    target.write_text("not executable\n")
    target.chmod(0o644)

    resolved = resolve_flexible(str(target), adhoc=True)

    assert resolved.source == "adhoc"
    assert resolved.command == str(target)


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

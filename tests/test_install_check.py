"""Install freshness checks (B100) — stale PATH/tool vs source tree."""

from __future__ import annotations

import io
import json
import textwrap
from pathlib import Path

import pytest

from hark.install_check import (
    _file_url_to_path,
    classify_install,
    compare_to_source,
    discover_source_trees,
    install_status,
    is_hark_source_tree,
    path_hark_probe,
    reinstall_hint,
)


def _make_source_tree(root: Path, *, cli_extra: str = "") -> Path:
    """Minimal hark checkout layout for discovery + compare."""
    (root / "src" / "hark").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "hark"
            version = "0.1.0"
            """
        ),
        encoding="utf-8",
    )
    (root / "src" / "hark" / "__init__.py").write_text(
        '__version__ = "0.1.0"\n', encoding="utf-8"
    )
    (root / "src" / "hark" / "speech.py").write_text(
        "# speech\n", encoding="utf-8"
    )
    cli = textwrap.dedent(
        f"""\
        def build_parser():
            sub = type("S", (), {{}})()
            sub.add_parser("doctor", help="d")
            sub.add_parser("start", help="start workers")
            sub.add_parser("stop", help="stop workers")
            sub.add_parser("restart", help="restart workers")
            {cli_extra}
        """
    )
    (root / "src" / "hark" / "cli.py").write_text(cli, encoding="utf-8")
    (root / "src" / "hark" / "workers.py").write_text(
        textwrap.dedent(
            """\
            def add_lifecycle_parsers(sub):
                sub.add_parser("start", help="s")
                sub.add_parser("stop", help="t")
                sub.add_parser("restart", help="r")
            """
        ),
        encoding="utf-8",
    )
    return root


def _make_installed_pkg(root: Path, *, with_start: bool = False) -> Path:
    """Fake site-packages/hark tree."""
    pkg = root / "site-packages" / "hark"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (pkg / "speech.py").write_text("# speech\n", encoding="utf-8")
    cmds = ['sub.add_parser("doctor", help="d")']
    if with_start:
        cmds.extend(
            [
                'sub.add_parser("start", help="s")',
                'sub.add_parser("stop", help="t")',
                'sub.add_parser("restart", help="r")',
            ]
        )
    (pkg / "cli.py").write_text(
        "def build_parser():\n    class S: pass\n    sub = S()\n    "
        + "\n    ".join(cmds)
        + "\n",
        encoding="utf-8",
    )
    return pkg


def test_is_hark_source_tree(tmp_path: Path) -> None:
    assert is_hark_source_tree(tmp_path) is False
    _make_source_tree(tmp_path / "hark")
    assert is_hark_source_tree(tmp_path / "hark") is True


def test_discover_source_trees_env_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "src"
    _make_source_tree(home)
    cwd_tree = tmp_path / "work" / "nested"
    _make_source_tree(tmp_path / "work")
    cwd_tree.mkdir(parents=True, exist_ok=True)

    found = discover_source_trees(
        cwd=cwd_tree,
        env={"HARK_HOME": str(home)},
        direct_url=None,
    )
    paths = [p.resolve() for p in found]
    assert home.resolve() in paths
    assert (tmp_path / "work").resolve() in paths
    # HARK_HOME first
    assert paths[0] == home.resolve()


def test_discover_from_direct_url(tmp_path: Path) -> None:
    tree = _make_source_tree(tmp_path / "checkout")
    found = discover_source_trees(
        cwd=tmp_path / "elsewhere",
        env={},
        direct_url={"url": tree.resolve().as_uri(), "dir_info": {}},
    )
    assert tree.resolve() in [p.resolve() for p in found]


def test_classify_editable_from_direct_url(tmp_path: Path) -> None:
    tree = _make_source_tree(tmp_path / "checkout")
    pkg = tree / "src" / "hark"
    info = classify_install(
        pkg_root=pkg,
        direct_url={
            "url": tree.resolve().as_uri(),
            "dir_info": {"editable": True},
        },
    )
    assert info["editable"] is True
    assert info["mode"] == "editable"


def test_classify_tool_copy(tmp_path: Path) -> None:
    tree = _make_source_tree(tmp_path / "checkout")
    # Mimic uv tools layout path segment
    pkg = tmp_path / "uv" / "tools" / "hark" / "lib" / "site-packages" / "hark"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    info = classify_install(
        pkg_root=pkg,
        direct_url={"url": tree.resolve().as_uri(), "dir_info": {}},
    )
    assert info["editable"] is False
    assert info["mode"] == "tool-copy"
    assert info["install_source_path"] == str(tree.resolve())


def test_compare_to_source_detects_cli_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _make_source_tree(tmp_path / "src")
    pkg = _make_installed_pkg(tmp_path / "inst", with_start=False)
    # Installed speech matches; cli differs
    (source / "src" / "hark" / "cli.py").write_text(
        (source / "src" / "hark" / "cli.py").read_text(encoding="utf-8") + "\n# newer\n",
        encoding="utf-8",
    )

    # Stub installed commands (running package still has full CLI in test env)
    monkeypatch.setattr(
        "hark.install_check._installed_cli_commands",
        lambda: {"doctor", "config", "status"},
    )
    cmp = compare_to_source(source, pkg_root=pkg)
    assert cmp["behind"] is True
    assert "start" in cmp["missing_commands"]
    assert "stop" in cmp["missing_commands"]
    assert any(d["path"] == "cli.py" for d in cmp["file_diffs"])


def test_hash_only_diff_is_different_not_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content mismatch without missing notable commands → different, not stale."""
    source = _make_source_tree(tmp_path / "src")
    pkg = _make_installed_pkg(tmp_path / "inst", with_start=True)
    (pkg / "cli.py").write_text(
        (source / "src" / "hark" / "cli.py").read_text(encoding="utf-8") + "\n# drift\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hark.install_check._installed_cli_commands",
        lambda: {"doctor", "start", "stop", "restart"},
    )
    cmp = compare_to_source(source, pkg_root=pkg)
    assert cmp["behind"] is False
    assert cmp["different"] is True
    assert cmp["missing_commands"] == []


def test_compare_matching_files_not_behind_when_cmds_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source_tree(tmp_path / "src")
    pkg = _make_installed_pkg(tmp_path / "inst", with_start=True)
    # Sync cli content with source
    (pkg / "cli.py").write_text(
        (source / "src" / "hark" / "cli.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hark.install_check._installed_cli_commands",
        lambda: {"doctor", "start", "stop", "restart"},
    )
    cmp = compare_to_source(source, pkg_root=pkg)
    assert cmp["behind"] is False
    assert cmp["missing_commands"] == []
    assert cmp["file_diffs"] == []


def test_install_status_stale_non_editable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _make_source_tree(tmp_path / "src")
    pkg = tmp_path / "uv" / "tools" / "hark" / "lib" / "python3.13" / "site-packages" / "hark"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (pkg / "speech.py").write_text("# old speech\n", encoding="utf-8")
    (pkg / "cli.py").write_text("# old cli\n", encoding="utf-8")
    # Source speech/cli differ
    (source / "src" / "hark" / "speech.py").write_text("# new speech\n", encoding="utf-8")

    monkeypatch.setattr(
        "hark.install_check._installed_cli_commands",
        lambda: {"doctor"},
    )
    st = install_status(
        cwd=tmp_path / "empty",
        env={"HARK_HOME": str(source)},
        pkg_root=pkg,
        direct_url={"url": source.resolve().as_uri(), "dir_info": {}},
        probe_path_hark=False,
    )
    assert st["stale"] is True
    assert st["status"] == "stale"
    assert st["editable"] is False
    assert any("behind" in w for w in st["warnings"])
    assert any("uv tool install -e ." in h for h in st["hints"])
    assert "start" in (st.get("comparison") or {}).get("missing_commands", [])


def test_install_status_frozen_tool_copy_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-editable but currently matching → frozen advisory, not stale."""
    source = _make_source_tree(tmp_path / "src")
    pkg = tmp_path / "uv" / "tools" / "hark" / "lib" / "python3.13" / "site-packages" / "hark"
    pkg.mkdir(parents=True)
    for name in ("__init__.py", "cli.py", "speech.py"):
        (pkg / name).write_text(
            (source / "src" / "hark" / name).read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "hark.install_check._installed_cli_commands",
        lambda: {"doctor", "start", "stop", "restart"},
    )
    st = install_status(
        cwd=tmp_path,
        env={"HARK_HOME": str(source)},
        pkg_root=pkg,
        direct_url={"url": source.resolve().as_uri(), "dir_info": {}},
        probe_path_hark=False,
    )
    assert st["stale"] is False
    assert st["status"] == "frozen"
    assert any("non-editable" in w for w in st["warnings"])
    assert any("uv tool install -e ." in h for h in st["hints"])


def test_install_status_editable_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _make_source_tree(tmp_path / "src")
    pkg = source / "src" / "hark"
    monkeypatch.setattr(
        "hark.install_check._installed_cli_commands",
        lambda: {"doctor", "start", "stop", "restart"},
    )
    st = install_status(
        cwd=source,
        env={"HARK_HOME": str(source)},
        pkg_root=pkg,
        direct_url={
            "url": source.resolve().as_uri(),
            "dir_info": {"editable": True},
        },
        probe_path_hark=False,
    )
    assert st["stale"] is False
    assert st["editable"] is True
    assert st["status"] in ("editable", "ok")
    assert st["warnings"] == []


def test_path_hark_stale_while_running_editable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv run doctor (editable) must still warn when PATH hark lacks start/stop."""
    source = _make_source_tree(tmp_path / "src")
    pkg = source / "src" / "hark"
    tool_pkg = (
        tmp_path / "uv" / "tools" / "hark" / "lib" / "python3.13" / "site-packages" / "hark"
    )
    tool_pkg.mkdir(parents=True)
    (tool_pkg / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (tool_pkg / "cli.py").write_text("# old\n", encoding="utf-8")
    (tool_pkg / "speech.py").write_text("# old\n", encoding="utf-8")

    fake_bin = tmp_path / "bin" / "hark"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_text(
        "#!/bin/sh\necho 'usage: hark {doctor,config,status}'\n",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)

    monkeypatch.setattr(
        "hark.install_check._installed_cli_commands",
        lambda: {"doctor", "start", "stop", "restart"},
    )
    monkeypatch.setattr(
        "hark.install_check.path_hark_probe",
        lambda **_kw: {
            "ok": True,
            "path": str(fake_bin),
            "resolved": str(fake_bin),
            "package_root": str(tool_pkg),
            "commands": ["doctor", "config", "status"],
            "version": "hark 0.1.0",
            "same_as_running": False,
            "error": None,
        },
    )
    st = install_status(
        cwd=source,
        env={"HARK_HOME": str(source)},
        pkg_root=pkg,
        direct_url={
            "url": source.resolve().as_uri(),
            "dir_info": {"editable": True},
        },
        probe_path_hark=True,
    )
    assert st["stale"] is True
    assert st["status"] == "path-stale"
    assert any("PATH hark" in w for w in st["warnings"])
    assert "start" in (st.get("path_comparison") or {}).get("missing_commands", [])


def test_pypi_install_without_source_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "site-packages" / "hark"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (pkg / "cli.py").write_text("# release\n", encoding="utf-8")
    (pkg / "speech.py").write_text("# release\n", encoding="utf-8")
    monkeypatch.setattr(
        "hark.install_check._installed_cli_commands",
        lambda: {"doctor", "start", "stop"},
    )
    st = install_status(
        cwd=tmp_path / "not-a-repo",
        env={},  # no HARK_HOME; no default tree under tmp home
        pkg_root=pkg,
        direct_url={"url": "https://pypi.org/simple/hark/"},
        probe_path_hark=False,
    )
    assert st["stale"] is False
    assert st["warnings"] == []
    assert st["status"] in ("ok", "unknown")


def test_file_url_unquotes_spaces(tmp_path: Path) -> None:
    tree = tmp_path / "hark checkout"
    _make_source_tree(tree)
    uri = tree.resolve().as_uri()
    assert "%20" in uri or " " in str(tree)
    got = _file_url_to_path(uri)
    assert got == tree.resolve()
    assert is_hark_source_tree(got)


def test_reinstall_hint_quotes_spaces(tmp_path: Path) -> None:
    tree = tmp_path / "hark checkout"
    _make_source_tree(tree)
    hint = reinstall_hint(tree)
    assert "uv tool install -e ." in hint
    assert "'/ " in hint or "hark checkout" in hint or "hark\\ checkout" in hint
    # paste-safe: shlex.quote wraps the path
    assert "cd " in hint


def test_run_doctor_includes_install_section(monkeypatch: pytest.MonkeyPatch) -> None:
    from hark.config import AudioConfig, HarkConfig
    from hark.doctor import run_doctor
    from hark.exitcodes import OK

    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: f"/usr/bin/{name}" if name != "herdr" else None,
    )
    monkeypatch.setattr(
        "hark.doctor._install_report",
        lambda: {
            "ok": True,
            "status": "stale",
            "stale": True,
            "mode": "tool-copy",
            "editable": False,
            "package_version": "0.1.0",
            "hark_version": "0.1.0",
            "which_hark": "/home/u/.local/bin/hark",
            "compared_source": "/home/u/src/hark",
            "comparison": {
                "git_describe": "v0.1.7-10-gabc",
                "missing_commands": ["start", "stop", "restart"],
            },
            "warnings": ["PATH/tool hark install is behind local source"],
            "hints": ["cd /home/u/src/hark && uv tool install -e . --force"],
        },
    )
    # Soft checks that may touch network / env
    monkeypatch.setattr(
        "hark.doctor._update_report",
        lambda _cfg: {"disabled": True},
    )
    monkeypatch.setattr(
        "hark.doctor._tts_play_queue_report",
        lambda: {"status": "idle", "serving": None, "next": 0, "pending": 0, "healed_count": 0, "warnings": []},
    )
    out = io.StringIO()
    code = run_doctor(
        HarkConfig(audio=AudioConfig(), sessions=[]),
        as_json=False,
        out=out,
        err=io.StringIO(),
    )
    assert code == OK  # soft — does not fail doctor
    text = out.getvalue()
    assert "install:" in text
    assert "stale" in text
    assert "missing cmds: start, stop, restart" in text
    assert "uv tool install -e ." in text
    assert "warn:" in text


def test_run_doctor_json_install(monkeypatch: pytest.MonkeyPatch) -> None:
    from hark.config import AudioConfig, HarkConfig
    from hark.doctor import run_doctor
    from hark.exitcodes import OK

    monkeypatch.setattr("hark.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "hark.doctor._install_report",
        lambda: {
            "ok": True,
            "status": "editable",
            "stale": False,
            "mode": "editable",
            "editable": True,
            "package_version": "0.1.0",
            "warnings": [],
            "hints": [],
        },
    )
    monkeypatch.setattr("hark.doctor._update_report", lambda _cfg: {"disabled": True})
    monkeypatch.setattr(
        "hark.doctor._tts_play_queue_report",
        lambda: {"status": "idle", "serving": None, "next": 0, "pending": 0, "healed_count": 0, "warnings": []},
    )
    out = io.StringIO()
    code = run_doctor(
        HarkConfig(audio=AudioConfig(), sessions=[]),
        as_json=True,
        out=out,
        err=io.StringIO(),
    )
    assert code == OK
    report = json.loads(out.getvalue())
    assert "install" in report
    assert report["install"]["status"] == "editable"
    assert report["ok"] is True

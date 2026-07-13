"""B088: GitHub release self-update check (cache, compare, fail-soft)."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from typing import Any

import pytest

from hark.update_check import (
    TTL_SECONDS,
    UpdateStatus,
    check_for_update,
    format_update_notice,
    is_newer,
    maybe_print_update_notice,
    normalize_version,
    version_tuple,
)


@pytest.fixture(autouse=True)
def _enable_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Override suite-wide HARK_UPDATE_CHECK=0 from conftest."""
    monkeypatch.setenv("HARK_UPDATE_CHECK", "1")


class _FakeResp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                "err",
                request=httpx.Request("GET", "https://api.github.com"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        status: int = 200,
        error: Exception | None = None,
    ) -> None:
        self.payload = payload
        self.status = status
        self.error = error
        self.calls = 0

    def get(self, url: str, headers: dict | None = None, timeout: float | None = None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert "releases/latest" in url
        return _FakeResp(self.payload or {}, status=self.status)


def test_normalize_and_compare() -> None:
    assert normalize_version("v1.2.3") == "1.2.3"
    assert normalize_version("1.2.3") == "1.2.3"
    assert version_tuple("v0.1.0") == (0, 1, 0)
    assert is_newer("0.2.0", "0.1.0")
    assert is_newer("v1.0.0", "0.9.9")
    assert not is_newer("0.1.0", "0.1.0")
    assert not is_newer("0.1.0", "0.2.0")
    assert is_newer("0.1.10", "0.1.9")


def test_fresh_fetch_writes_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "update_check.json"
    client = _FakeClient(
        {
            "tag_name": "v0.9.0",
            "html_url": "https://github.com/ultradyn/hark/releases/tag/v0.9.0",
            "name": "0.9.0",
        }
    )
    status = check_for_update(
        current="0.1.0",
        repo="ultradyn/hark",
        cache_file=cache,
        client=client,
        now=1_700_000_000.0,
        ttl_s=TTL_SECONDS,
    )
    assert client.calls == 1
    assert status.update_available is True
    assert status.latest_version == "0.9.0"
    assert status.from_cache is False
    assert cache.is_file()
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["latest_version"] == "0.9.0"
    assert data["tag_name"] == "v0.9.0"


def test_fresh_cache_skips_network(tmp_path: Path) -> None:
    cache = tmp_path / "update_check.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": 1_700_000_000.0,
                "repo": "ultradyn/hark",
                "tag_name": "v0.5.0",
                "latest_version": "0.5.0",
                "html_url": "https://example.com/r",
            }
        ),
        encoding="utf-8",
    )
    client = _FakeClient(error=RuntimeError("should not be called"))
    status = check_for_update(
        current="0.1.0",
        cache_file=cache,
        client=client,
        now=1_700_000_000.0 + 60,  # well within 24h
        ttl_s=TTL_SECONDS,
    )
    assert client.calls == 0
    assert status.from_cache is True
    assert status.stale is False
    assert status.update_available is True
    assert status.latest_version == "0.5.0"


def test_stale_cache_refreshes(tmp_path: Path) -> None:
    cache = tmp_path / "update_check.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": 1_700_000_000.0,
                "repo": "ultradyn/hark",
                "tag_name": "v0.2.0",
                "latest_version": "0.2.0",
                "html_url": "https://example.com/old",
            }
        ),
        encoding="utf-8",
    )
    client = _FakeClient(
        {
            "tag_name": "v0.3.0",
            "html_url": "https://example.com/new",
        }
    )
    status = check_for_update(
        current="0.1.0",
        cache_file=cache,
        client=client,
        now=1_700_000_000.0 + TTL_SECONDS + 10,
        ttl_s=TTL_SECONDS,
    )
    assert client.calls == 1
    assert status.latest_version == "0.3.0"
    assert status.from_cache is False


def test_offline_falls_back_to_stale_cache(tmp_path: Path) -> None:
    cache = tmp_path / "update_check.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": 1_700_000_000.0,
                "repo": "ultradyn/hark",
                "tag_name": "v0.4.0",
                "latest_version": "0.4.0",
                "html_url": "https://example.com/r",
            }
        ),
        encoding="utf-8",
    )
    client = _FakeClient(error=OSError("network down"))
    status = check_for_update(
        current="0.1.0",
        cache_file=cache,
        client=client,
        now=1_700_000_000.0 + TTL_SECONDS + 100,
        ttl_s=TTL_SECONDS,
    )
    assert status.from_cache is True
    assert status.stale is True
    assert status.update_available is True
    assert status.latest_version == "0.4.0"
    assert status.error is not None


def test_offline_no_cache_no_raise(tmp_path: Path) -> None:
    cache = tmp_path / "missing.json"
    client = _FakeClient(error=OSError("offline"))
    status = check_for_update(
        current="0.1.0",
        cache_file=cache,
        client=client,
        now=1.0,
    )
    assert status.update_available is False
    assert status.error is not None
    assert not cache.is_file()


def test_disabled_skips_everything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No env override — config/API ``enabled=False`` alone must silence checks
    monkeypatch.delenv("HARK_UPDATE_CHECK", raising=False)
    client = _FakeClient(error=RuntimeError("nope"))
    status = check_for_update(
        enabled=False,
        current="0.1.0",
        cache_file=tmp_path / "c.json",
        client=client,
    )
    assert status.disabled is True
    assert status.update_available is False
    assert client.calls == 0


def test_env_disables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARK_UPDATE_CHECK", "0")  # overrides module autouse enable
    client = _FakeClient({"tag_name": "v9.0.0"})
    status = check_for_update(
        enabled=True,
        current="0.1.0",
        cache_file=tmp_path / "c.json",
        client=client,
    )
    assert status.disabled is True
    assert client.calls == 0


def test_current_already_latest(tmp_path: Path) -> None:
    cache = tmp_path / "update_check.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": 1_700_000_000.0,
                "tag_name": "v0.1.0",
                "latest_version": "0.1.0",
                "html_url": "https://example.com/r",
            }
        ),
        encoding="utf-8",
    )
    status = check_for_update(
        current="0.1.0",
        cache_file=cache,
        client=_FakeClient(error=RuntimeError("no")),
        now=1_700_000_000.0 + 10,
    )
    assert status.update_available is False
    assert format_update_notice(status) is None


def test_print_notice(tmp_path: Path) -> None:
    cache = tmp_path / "update_check.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": 1_700_000_000.0,
                "tag_name": "v1.0.0",
                "latest_version": "1.0.0",
                "html_url": "https://github.com/ultradyn/hark/releases/tag/v1.0.0",
                "repo": "ultradyn/hark",
            }
        ),
        encoding="utf-8",
    )
    buf = io.StringIO()
    status = maybe_print_update_notice(
        current="0.1.0",
        cache_file=cache,
        client=_FakeClient(error=RuntimeError("no")),
        now=1_700_000_000.0 + 10,
        file=buf,
    )
    assert status.update_available is True
    text = buf.getvalue()
    assert "update available" in text
    assert "0.1.0" in text
    assert "1.0.0" in text


def test_up_to_date_after_upgrade_uses_cache(tmp_path: Path) -> None:
    """Cache says 0.5 is latest; installed already 0.5 → no notice."""
    cache = tmp_path / "update_check.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": 1_700_000_000.0,
                "tag_name": "v0.5.0",
                "latest_version": "0.5.0",
                "html_url": "https://example.com/r",
            }
        ),
        encoding="utf-8",
    )
    status = check_for_update(
        current="0.5.0",
        cache_file=cache,
        client=_FakeClient(error=RuntimeError("no")),
        now=1_700_000_000.0 + 10,
    )
    assert status.update_available is False


def test_update_status_to_dict() -> None:
    s = UpdateStatus(
        current_version="0.1.0",
        latest_version="0.2.0",
        update_available=True,
        checked_at=1_700_000_000.0,
    )
    d = s.to_dict()
    assert d["update_available"] is True
    assert d["checked_at_iso"] is not None
    assert d["checked_at_iso"].endswith("Z")


def test_health_snapshot_includes_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hark.config import HarkConfig, UpdateConfig
    from hark.dashboard import api

    cache = tmp_path / "update_check.json"
    # Fresh cache so doctor/health never hit the network
    cache.write_text(
        json.dumps(
            {
                "checked_at": time.time(),
                "tag_name": "v9.9.9",
                "latest_version": "9.9.9",
                "html_url": "https://example.com/r",
                "repo": "ultradyn/hark",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("hark.update_check.cache_path", lambda: cache)
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)
    monkeypatch.setattr("hark.doctor.package_version", lambda: "0.1.0", raising=False)
    # Pin package version comparison
    monkeypatch.setattr("hark.update_check.package_version", lambda: "0.1.0")
    # doctor talks to herdr; empty sessions keeps it soft-ok
    cfg = HarkConfig(sessions=[], update=UpdateConfig(enabled=True, repo="ultradyn/hark"))
    monkeypatch.setattr(
        "hark.doctor.all_provider_status",
        lambda: [],
    )
    snap = api.health_snapshot(
        cfg,
        {"name": "hark-serve-py", "version": "0.1.0", "started_at": "t0"},
    )
    assert "update" in snap
    assert snap["update"]["update_available"] is True
    assert snap["update"]["latest_version"] == "9.9.9"


def test_config_update_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hark.config import load_config

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        'version = 1\n[update]\nenabled = false\nrepo = "other/hark"\n',
        encoding="utf-8",
    )
    # Config load also honors HARK_UPDATE_CHECK; clear so TOML wins
    monkeypatch.delenv("HARK_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("HARK_UPDATE_REPO", raising=False)
    monkeypatch.setenv("HARK_CONFIG", str(cfg_path))
    cfg = load_config(cfg_path)
    assert cfg.update.enabled is False
    assert cfg.update.repo == "other/hark"


def test_monitor_prints_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hark.monitor_feed import run_monitor

    cache = tmp_path / "update_check.json"
    cache.write_text(
        json.dumps(
            {
                "checked_at": time.time(),
                "tag_name": "v2.0.0",
                "latest_version": "2.0.0",
                "html_url": "https://example.com/r",
                "repo": "ultradyn/hark",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("hark.update_check.cache_path", lambda: cache)
    monkeypatch.setattr("hark.update_check.package_version", lambda: "0.1.0")
    monkeypatch.setattr("hark.paths.state_dir", lambda: tmp_path)
    monkeypatch.setattr("hark.monitor_feed.state_dir", lambda: tmp_path)
    # Avoid following forever: empty feed files, replay 0, then break follow
    monkeypatch.setattr(
        "hark.monitor_feed.follow_state_files",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr("hark.monitor_feed.default_feed_paths", lambda: [])
    code = run_monitor(replay=0, state_root=tmp_path)
    assert code == 0
    err = capsys.readouterr().err
    assert "update available" in err
    assert "2.0.0" in err

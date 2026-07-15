"""hark doctor — media ducking readiness (B047) soft checks."""

from __future__ import annotations

import io
import json

from hark.config import AudioConfig, HarkConfig
from hark.doctor import _media_duck_report, run_doctor
from hark.exitcodes import OK


def _cfg(**audio_kw) -> HarkConfig:
    return HarkConfig(audio=AudioConfig(**audio_kw), sessions=[])


def test_media_duck_report_ready(monkeypatch):
    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: f"/usr/bin/{name}" if name in ("pactl", "playerctl") else None,
    )
    report = _media_duck_report(_cfg())
    assert report["status"] == "ready"
    assert report["pactl_ok"] is True
    assert report["playerctl_ok"] is True
    assert report["duck_level"] == 0.15
    assert report["duck_media_during_tts"] is True
    assert report["duck_media_during_stt"] is True
    assert report["pause_media_during_stt"] is True
    assert report["warnings"] == []


def test_media_duck_report_pactl_missing_degraded(monkeypatch):
    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: "/usr/bin/playerctl" if name == "playerctl" else None,
    )
    report = _media_duck_report(_cfg())
    assert report["status"] == "degraded"
    assert report["pactl_ok"] is False
    assert report["playerctl_ok"] is True
    assert any("pactl missing" in w for w in report["warnings"])
    # Soft only — does not imply hard doctor failure


def test_media_duck_report_playerctl_missing_warns(monkeypatch):
    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: "/usr/bin/pactl" if name == "pactl" else None,
    )
    report = _media_duck_report(_cfg())
    assert report["status"] == "ready"  # volume duck still works
    assert report["pactl_ok"] is True
    assert report["playerctl_ok"] is False
    assert any("playerctl missing" in w for w in report["warnings"])


def test_media_duck_report_disabled_when_all_off(monkeypatch):
    monkeypatch.setattr("hark.doctor.shutil.which", lambda _name: None)
    report = _media_duck_report(
        _cfg(
            duck_media_during_tts=False,
            duck_media_during_stt=False,
            pause_media_during_tts=False,
            pause_media_during_stt=False,
            media_check_mpris=False,
        )
    )
    assert report["status"] == "disabled"
    # No tool warnings when ducking is fully off and MPRIS unused
    assert report["warnings"] == []


def test_run_doctor_includes_media_duck_soft(monkeypatch):
    """Missing pactl → warn in human + JSON; still exit OK when herdr ok."""
    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: None if name in ("pactl", "playerctl", "herdr") else f"/bin/{name}",
    )
    # No herdr sessions → herdr_ok stays True
    cfg = _cfg()
    out = io.StringIO()
    err = io.StringIO()
    code = run_doctor(cfg, as_json=False, out=out, err=err)
    assert code == OK
    text = out.getvalue()
    assert "media duck:" in text
    assert "degraded" in text
    assert "pactl" in text.lower()
    # overall may be DEGRADED from speech keys, but exit is not HERDR from ducking
    assert "warn:" in text


def test_run_doctor_json_media_duck(monkeypatch):
    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "pactl" else None,
    )
    out = io.StringIO()
    code = run_doctor(_cfg(), as_json=True, out=out, err=io.StringIO())
    assert code == OK
    report = json.loads(out.getvalue())
    assert "media_duck" in report
    assert report["media_duck"]["pactl_ok"] is True
    assert report["media_duck"]["status"] == "ready"
    # Soft warnings must not flip overall herdr/ok solely for playerctl
    assert report["ok"] is True


def test_run_doctor_setup_incomplete_soft(tmp_path, monkeypatch):
    """B116: missing setup-complete → setup incomplete warn; doctor still OK."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "pactl" else None,
    )
    out = io.StringIO()
    code = run_doctor(_cfg(), as_json=True, out=out, err=io.StringIO())
    assert code == OK
    report = json.loads(out.getvalue())
    assert "setup" in report
    assert report["setup"]["needs_run"] is True
    assert report["setup"]["complete"] is False
    assert report["setup"]["status"] == "incomplete"
    assert any("setup incomplete" in w for w in report["setup"]["warnings"])
    # Soft only — does not flip overall ok by itself
    assert report["ok"] is True


def test_run_doctor_setup_complete_with_sessions(tmp_path, monkeypatch):
    from hark.config import SessionConfig
    from hark.setup_flow import SetupAnswers, write_setup_complete

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    write_setup_complete(
        SetupAnswers(
            persona="feminine",
            wake_engine="vosk",
            tts_voice="eve",
            wake_names=["iris"],
            sessions=[{"id": "local"}],
        ),
        hark_version="0.0.0",
    )
    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "pactl" else None,
    )
    cfg = HarkConfig(
        audio=AudioConfig(),
        sessions=[SessionConfig(id="local")],
    )
    out = io.StringIO()
    code = run_doctor(cfg, as_json=False, out=out, err=io.StringIO())
    assert code == OK
    text = out.getvalue()
    assert "setup:" in text
    assert "complete" in text
    report_out = io.StringIO()
    run_doctor(cfg, as_json=True, out=report_out, err=io.StringIO())
    report = json.loads(report_out.getvalue())
    assert report["setup"]["complete"] is True
    assert report["setup"]["needs_run"] is False
    assert report["setup"]["config_session_ids"] == ["local"]


# ---------------------------------------------------------------------------
# dashboard report (B066)
# ---------------------------------------------------------------------------


def test_dashboard_report_localhost_ok(monkeypatch):
    from hark.config import DashboardConfig
    from hark.doctor import _dashboard_report

    monkeypatch.setattr("hark.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    cfg = HarkConfig(sessions=[], dashboard=DashboardConfig())
    report = _dashboard_report(cfg)
    assert report["status"] == "ok"
    assert report["localhost"] is True and not report["errors"]


def test_dashboard_report_remote_without_token_errors(monkeypatch):
    from hark.config import DashboardConfig
    from hark.doctor import _dashboard_report

    monkeypatch.setattr("hark.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    cfg = HarkConfig(sessions=[], dashboard=DashboardConfig(host="0.0.0.0"))
    report = _dashboard_report(cfg)
    assert report["status"] == "error"
    assert any("refuse" in e for e in report["errors"])
    # remote + no tls also warns about secure-context features
    assert any("tailscale serve" in w for w in report["warnings"])


def test_dashboard_report_remote_token_tls_ok(monkeypatch):
    from hark.config import DashboardConfig
    from hark.doctor import _dashboard_report

    monkeypatch.setattr("hark.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    cfg = HarkConfig(
        sessions=[],
        dashboard=DashboardConfig(host="100.64.0.5", token="t", tls_terminated=True),
    )
    report = _dashboard_report(cfg)
    assert report["status"] == "ok"


def test_dashboard_report_ffmpeg_missing_warns(monkeypatch):
    from hark.config import DashboardConfig
    from hark.doctor import _dashboard_report

    monkeypatch.setattr("hark.doctor.shutil.which", lambda name: None)
    cfg = HarkConfig(sessions=[], dashboard=DashboardConfig())
    report = _dashboard_report(cfg)
    assert report["status"] == "warn"
    assert any("ffmpeg" in w for w in report["warnings"])


def test_run_doctor_flags_dashboard_error(monkeypatch):
    from hark.config import DashboardConfig

    monkeypatch.setattr("hark.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    cfg = HarkConfig(sessions=[], dashboard=DashboardConfig(host="0.0.0.0"))
    out = io.StringIO()
    run_doctor(cfg, as_json=True, out=out, err=io.StringIO())
    report = json.loads(out.getvalue())
    assert report["dashboard"]["status"] == "error"
    assert report["ok"] is False


def test_tts_play_queue_report_heals_abandoned(tmp_path, monkeypatch):
    """Doctor auto-heals stuck TTS play queue (B099)."""
    import time

    from hark.audio import playback as pb
    from hark.doctor import _tts_play_queue_report

    lock = tmp_path / "tts_play.lock"
    queue = tmp_path / "tts_play_queue.json"
    monkeypatch.setattr(pb, "tts_play_lock_path", lambda: lock)
    monkeypatch.setattr(pb, "tts_play_queue_path", lambda: queue)
    queue.write_text(
        json.dumps(
            {
                "next": 10,
                "serving": 8,
                "cancelled": [],
                "holders": {
                    "8": {"pid": 999_999_981, "claimed_at": time.time() - 120},
                    "9": {"pid": 999_999_982, "claimed_at": time.time() - 100},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pb, "_pid_alive", lambda _pid: False)
    report = _tts_play_queue_report()
    assert report["status"] == "healed"
    assert report["healed_count"] == 2
    assert report["serving"] == 10
    assert any("healed" in w for w in report["warnings"])

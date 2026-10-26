"""Offline speech capability diagnostics and startup repair contracts."""

import io
import json
from types import SimpleNamespace

import pytest

from hark.config import HarkConfig, SttConfig, TtsConfig, load_config
from hark.speech_readiness import speech_readiness


def readiness(cfg=None, *, providers=(), events=()):
    return speech_readiness(cfg or HarkConfig(), providers=list(providers), events=list(events),
                            custom_stt={}, custom_tts={}, local_stt=[])


def auth(name, source="env"):
    return {"name": name, "source": source, "available": True, "detail": "credential present"}


def test_credentials_do_not_verify_audio_access():
    result = readiness(providers=[auth("xai")])
    assert result["configured"] is True
    assert result["stt"]["validation"] == result["tts"]["validation"] == "untested"
    assert "untested" in result["stt"]["hint"]


def test_codex_audio_scope_is_unverified_per_modality():
    result = readiness(providers=[auth("openai", "codex")])
    for leg in ("tts", "stt"):
        row = next(row for row in result[leg]["candidates"] if row["provider"] == "openai")
        assert row["supported"] is True
        assert "audio scope" in row["hint"]


def test_stt_policy_and_tts_consent_are_independent():
    cfg = HarkConfig(stt=SttConfig(provider="minimax"), tts=TtsConfig(provider="minimax"))
    result = readiness(cfg, providers=[auth("minimax")])
    assert result["configured"] is False
    assert result["stt"]["candidates"][0]["status"] == "unsupported"
    assert result["tts"]["candidates"][0]["status"] == "consent_required"
    assert "setup --minimax-ok" in result["tts"]["hint"]


def test_disabled_candidates_cannot_make_stt_ready():
    cfg = HarkConfig(stt=SttConfig(disabled=["xai", "openai", "google"]))
    result = readiness(cfg, providers=[auth("xai")])
    assert result["stt"]["configured"] is False
    assert "No configured STT" in result["stt"]["hint"]


def test_local_alias_and_fail_open_match_runtime_policy():
    cfg = HarkConfig(stt=SttConfig(provider="whisper", local_fail_open=True))
    result = readiness(cfg, providers=[auth("google")])
    assert result["stt"]["provider"] == "faster_whisper"
    assert result["stt"]["configured"] is True
    assert any(row["provider"] == "google" for row in result["stt"]["candidates"])
    cfg.stt.local_fail_open = False
    result = readiness(cfg, providers=[auth("google")])
    assert result["stt"]["configured"] is False


def test_recent_stt_failure_metadata_is_sanitized_and_later_success_clears_it():
    failure = {"event": "stt.response", "message": "HTTP 403 secret-token audio payload",
               "data": {"provider": "xai", "ok": False, "error": "secret-token"}}
    result = readiness(providers=[auth("xai")], events=[failure])
    row = result["stt"]["candidates"][0]
    assert row["last_failure"] == {"event": "stt.response", "http_status": 403}
    assert "secret-token" not in json.dumps(result)
    assert row["status"] == "last_auth_failed"
    success = {"event": "stt.response", "data": {"provider": "xai", "ok": True}}
    result = readiness(providers=[auth("xai")], events=[failure, success])
    assert "last_failure" not in result["stt"]["candidates"][0]


@pytest.mark.parametrize("provider", ["minimax", "anthropic"])
def test_unsupported_stt_config_warns_immediately(tmp_path, monkeypatch, provider):
    monkeypatch.delenv("HARK_STT_PROVIDER", raising=False)
    path = tmp_path / "config.toml"
    path.write_text(f'[stt]\nprovider="{provider}"\n')
    assert any("does not support STT" in warning for warning in load_config(path).warnings)
    monkeypatch.setenv("HARK_STT_PROVIDER", "google")
    assert not any("does not support STT" in warning for warning in load_config(path).warnings)


def test_setup_explicit_consent_persists_without_resetting_provider(tmp_path, monkeypatch):
    from hark.setup_flow import run_setup
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    path = tmp_path / "config.toml"
    path.write_text('[tts]\nprovider="minimax"\nminimax_ok=false\n')
    monkeypatch.setattr("hark.speech_readiness.speech_readiness", lambda cfg: {
        "configured": True, "stt": {"hint": "untested"}, "tts": {"hint": "untested"}})
    out = io.StringIO()
    assert run_setup(non_interactive=True, skip_doctor=True, skip_download=True,
                     minimax_ok=True, force=True, config_path=path, out=out) == 0
    cfg = load_config(path)
    assert cfg.tts.minimax_ok is True
    assert cfg.tts.provider == "minimax"
    assert "has not been verified" in out.getvalue()


def test_setup_keeps_interview_marker_but_reports_impossible_speech(tmp_path, monkeypatch):
    from hark.setup_flow import run_setup, load_setup_complete
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = readiness()
    monkeypatch.setattr("hark.speech_readiness.speech_readiness", lambda cfg: result)
    out, err = io.StringIO(), io.StringIO()
    assert run_setup(non_interactive=True, skip_doctor=True, skip_download=True,
                     force=True, config_path=tmp_path / "config.toml", out=out, err=err) == 4
    assert load_setup_complete() is not None
    assert "incomplete" in err.getvalue()
    assert "No configured STT" in out.getvalue()
    # Saved interview answers must not turn the same impossible policy into
    # success on the next invocation, including explicit consent-only updates.
    for extra in ({}, {"minimax_ok": True}):
        assert run_setup(non_interactive=True, skip_doctor=True, skip_download=True,
                         config_path=tmp_path / "config.toml", out=out, err=err, **extra) == 4


def test_missing_wake_preflight_starts_no_workers(tmp_path, monkeypatch):
    from hark import workers
    monkeypatch.setattr(workers, "assert_no_live_harkd", lambda root: None)
    monkeypatch.setattr(workers, "collect_worker_records", lambda *a, **kw: [])
    monkeypatch.setattr("hark.wake_readiness.ambient_readiness", lambda cfg: {
        "status": "missing_model", "warnings": [], "hints": ["install wake model"]})
    monkeypatch.setattr(workers, "spawn_mode_a_workers", lambda **kw: pytest.fail("spawned before preflight"))
    result = workers.start_workers(tmp_path, do_watch=True, do_ambient=True)
    assert result["ok"] is False
    assert "missing_model" in result["error"]
    assert "install wake model" in result["error"]


def test_doctor_plan_and_conference_are_truthful(monkeypatch):
    from hark import doctor
    from hark.conference import ConferenceMatch
    monkeypatch.setattr(doctor, "all_provider_status", lambda: [])
    monkeypatch.setattr("hark.conference.conference_status", lambda cfg: ConferenceMatch(True, matched=("zoom",)))
    cfg = HarkConfig(sessions=[])
    output = io.StringIO()
    assert doctor.run_doctor(cfg, out=output, plan=True) == 0
    value = output.getvalue()
    assert "speech: ready" not in value
    assert "No configured STT" in value
    assert "conference hold: ACTIVE" in value
    assert "zoom" in value
    assert "Ordered next actions" in value
    output = io.StringIO()
    doctor.run_doctor(cfg, out=output, as_json=True)
    report = json.loads(output.getvalue())
    assert report["speech_ok"] is False
    assert report["next_actions"]
    from pathlib import Path
    for action in report["next_actions"]:
        assert Path(action["doc"]).is_file()


def test_diagnostic_parser_flags():
    from hark.cli import build_parser
    parser = build_parser()
    assert parser.parse_args(["doctor", "--plan"]).plan is True
    assert parser.parse_args(["setup", "--minimax-ok"]).minimax_ok is True

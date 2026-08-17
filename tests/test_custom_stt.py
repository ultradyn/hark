"""Custom STT provider — OpenAI-compatible /audio/transcriptions (+ optional /stt)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from hark.config import SttConfig, load_config, config_to_dict
from hark.providers.base import ProviderError, Transcript
from hark.providers.custom_stt import CustomStt, custom_stt_status, normalize_custom_base_url
from hark.providers.resolve import resolve_stt


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | str) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else str(payload)

    def json(self) -> dict:
        assert isinstance(self._payload, dict)
        return self._payload


def test_normalize_custom_base_url_strips_trailing_slash() -> None:
    assert (
        normalize_custom_base_url("https://gw.example.com/v1/")
        == "https://gw.example.com/v1"
    )
    assert (
        normalize_custom_base_url("https://gw.example.com/v1")
        == "https://gw.example.com/v1"
    )


def test_normalize_custom_base_url_rejects_empty() -> None:
    with pytest.raises(ProviderError, match="custom_base_url"):
        normalize_custom_base_url("")
    with pytest.raises(ProviderError, match="custom_base_url"):
        normalize_custom_base_url("   ")


def test_custom_stt_transcribe_openai_path(monkeypatch) -> None:
    captured: dict = {}

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a) -> None:
            return None

        def post(self, url, headers=None, files=None, data=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            captured["files"] = files
            captured["data"] = dict(data or {})
            return _FakeResponse(200, {"text": "  hello operator  "})

    monkeypatch.setattr(httpx, "Client", _Client)
    stt = CustomStt(
        base_url="https://gw.example.com/v1/",
        api_key="tok-abc",
        model="gpt-4o-mini-transcribe",
    )
    tr = stt.transcribe(b"RIFF....", language="en")
    assert isinstance(tr, Transcript)
    assert tr.provider == "custom"
    assert tr.text == "hello operator"
    assert captured["url"] == "https://gw.example.com/v1/audio/transcriptions"
    assert captured["headers"]["Authorization"] == "Bearer tok-abc"
    assert captured["data"]["model"] == "gpt-4o-mini-transcribe"
    assert captured["data"]["language"] == "en"
    name, blob, ctype = captured["files"]["file"]
    assert name == "audio.wav"
    assert blob == b"RIFF...."
    assert ctype == "audio/wav"


def test_custom_stt_native_stt_path(monkeypatch) -> None:
    captured: dict = {}

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a) -> None:
            return None

        def post(self, url, headers=None, files=None, data=None):
            captured["url"] = url
            captured["data"] = dict(data or {})
            return _FakeResponse(
                200, {"transcript": "native path", "language": "en", "duration": 1.2}
            )

    monkeypatch.setattr(httpx, "Client", _Client)
    stt = CustomStt(
        base_url="https://gw.example.com/v1",
        api_key="tok",
        model="grok-stt",
        path="/stt",
    )
    tr = stt.transcribe(b"wav")
    assert tr.text == "native path"
    assert captured["url"] == "https://gw.example.com/v1/stt"
    assert captured["data"]["model"] == "grok-stt"


def test_custom_stt_path_must_start_with_slash() -> None:
    with pytest.raises(ProviderError, match="custom_path"):
        CustomStt(
            base_url="https://gw.example.com/v1",
            api_key="tok",
            model="m",
            path="audio/transcriptions",
        )


def test_custom_stt_openai_path_requires_model() -> None:
    with pytest.raises(ProviderError, match="custom_model"):
        CustomStt(
            base_url="https://gw.example.com/v1",
            api_key="tok",
            model="",
            path="/audio/transcriptions",
        )


def test_custom_stt_native_path_allows_missing_model(monkeypatch) -> None:
    """Native dual-mount aliases may inject a synthetic model server-side."""

    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a) -> None:
            return None

        def post(self, url, headers=None, files=None, data=None):
            assert "model" not in (data or {})
            return _FakeResponse(200, {"text": "ok"})

    monkeypatch.setattr(httpx, "Client", _Client)
    stt = CustomStt(
        base_url="https://gw.example.com/v1",
        api_key="tok",
        model=None,
        path="/stt",
    )
    assert stt.transcribe(b"x").text == "ok"


def test_custom_stt_http_error(monkeypatch) -> None:
    class _Client:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, *a) -> None:
            return None

        def post(self, url, headers=None, files=None, data=None):
            return _FakeResponse(501, "not_supported")

    monkeypatch.setattr(httpx, "Client", _Client)
    stt = CustomStt(
        base_url="https://gw.example.com/v1",
        api_key="tok",
        model="m1",
    )
    with pytest.raises(ProviderError, match="HTTP 501"):
        stt.transcribe(b"x")


def test_custom_stt_requires_api_key() -> None:
    with pytest.raises(ProviderError, match="api key|API key|custom_api_key"):
        CustomStt(
            base_url="https://gw.example.com/v1",
            api_key="",
            model="m1",
        )


def test_custom_stt_status_reports_readiness() -> None:
    st = custom_stt_status(
        base_url="https://gw.example.com/v1",
        api_key="tok",
        model="m1",
        path="/audio/transcriptions",
    )
    assert st.available is True
    assert "gw.example.com" in st.detail
    assert "m1" in st.detail

    st2 = custom_stt_status(base_url=None, api_key=None, model=None, path=None)
    assert st2.available is False
    assert "base_url" in st2.detail.lower() or "not configured" in st2.detail.lower()


def test_resolve_custom_stt(monkeypatch) -> None:
    cfg = SttConfig(
        provider="custom",
        custom_base_url="https://gw.example.com/v1",
        custom_api_key="tok",
        custom_model="whisper-1",
        custom_path="/audio/transcriptions",
    )
    stt = resolve_stt("custom", stt_cfg=cfg)
    assert stt.name == "custom"
    assert isinstance(stt, CustomStt)
    assert stt.base_url == "https://gw.example.com/v1"
    assert stt.model == "whisper-1"


def test_resolve_custom_missing_base_url() -> None:
    with pytest.raises(ProviderError, match="custom_base_url"):
        resolve_stt(
            "custom",
            stt_cfg=SttConfig(provider="custom", custom_api_key="tok", custom_model="m"),
        )


def test_auto_does_not_select_custom(monkeypatch) -> None:
    monkeypatch.setattr(
        "hark.providers.resolve.xai_auth",
        lambda: MagicMock(available=False),
    )
    monkeypatch.setattr(
        "hark.providers.resolve.openai_auth",
        lambda: MagicMock(available=True),
    )
    monkeypatch.setattr(
        "hark.providers.resolve.google_auth",
        lambda: MagicMock(available=False),
    )
    stt = resolve_stt(
        "auto",
        stt_cfg=SttConfig(
            custom_base_url="https://gw.example.com/v1",
            custom_api_key="tok",
            custom_model="m",
        ),
    )
    assert stt.name == "openai"


def test_config_loads_custom_stt_fields(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[stt]
provider = "custom"
custom_base_url = "https://gw.example.com/v1"
custom_api_key = "cfg-key"
custom_model = "grok-stt"
custom_path = "/stt"
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("HARK_STT_PROVIDER", raising=False)
    monkeypatch.delenv("HARK_STT_CUSTOM_BASE_URL", raising=False)
    monkeypatch.delenv("HARK_STT_CUSTOM_API_KEY", raising=False)
    monkeypatch.delenv("HARK_STT_CUSTOM_MODEL", raising=False)
    monkeypatch.delenv("HARK_STT_CUSTOM_PATH", raising=False)
    cfg = load_config(path)
    assert cfg.stt.provider == "custom"
    assert cfg.stt.custom_base_url == "https://gw.example.com/v1"
    assert cfg.stt.custom_api_key == "cfg-key"
    assert cfg.stt.custom_model == "grok-stt"
    assert cfg.stt.custom_path == "/stt"
    d = config_to_dict(cfg)
    assert d["stt"]["provider"] == "custom"
    assert d["stt"]["custom_base_url"] == "https://gw.example.com/v1"
    assert d["stt"]["custom_model"] == "grok-stt"
    assert d["stt"]["custom_path"] == "/stt"
    # Never leak the secret into redacted config dumps.
    assert "custom_api_key" not in d["stt"] or d["stt"].get("custom_api_key") in (
        None,
        True,
        False,
        "***",
    )
    assert d["stt"].get("custom_api_key_configured") is True


def test_env_overrides_custom_stt(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[stt]
provider = "auto"
custom_base_url = "https://cfg.example/v1"
custom_model = "from-cfg"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARK_STT_PROVIDER", "custom")
    monkeypatch.setenv("HARK_STT_CUSTOM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("HARK_STT_CUSTOM_API_KEY", "env-key")
    monkeypatch.setenv("HARK_STT_CUSTOM_MODEL", "from-env")
    monkeypatch.setenv("HARK_STT_CUSTOM_PATH", "/stt")
    cfg = load_config(path)
    assert cfg.stt.provider == "custom"
    assert cfg.stt.custom_base_url == "https://env.example/v1"
    assert cfg.stt.custom_api_key == "env-key"
    assert cfg.stt.custom_model == "from-env"
    assert cfg.stt.custom_path == "/stt"


def test_doctor_speech_ok_when_custom_pinned(monkeypatch) -> None:
    import io
    import json

    from hark.config import HarkConfig
    from hark.doctor import run_doctor
    from hark.exitcodes import OK

    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: None if name == "herdr" else f"/bin/{name}",
    )
    monkeypatch.setattr("hark.doctor.all_provider_status", lambda: [])
    cfg = HarkConfig(
        sessions=[],
        stt=SttConfig(
            provider="custom",
            custom_base_url="https://gw.example/v1",
            custom_api_key="tok",
            custom_model="grok-stt",
            custom_path="/stt",
        ),
    )
    out = io.StringIO()
    code = run_doctor(cfg, as_json=True, out=out, err=io.StringIO())
    assert code == OK
    report = json.loads(out.getvalue())
    assert report["speech_ok"] is True
    assert report["custom_stt"]["available"] is True
    assert any(p["name"] == "custom" and p["available"] for p in report["providers"])


def test_doctor_speech_not_ok_when_custom_incomplete(monkeypatch) -> None:
    import io
    import json

    from hark.config import HarkConfig
    from hark.doctor import run_doctor

    monkeypatch.setattr(
        "hark.doctor.shutil.which",
        lambda name: None if name == "herdr" else f"/bin/{name}",
    )
    monkeypatch.setattr("hark.doctor.all_provider_status", lambda: [])
    cfg = HarkConfig(
        sessions=[],
        stt=SttConfig(provider="custom", custom_base_url="https://gw.example/v1"),
    )
    out = io.StringIO()
    run_doctor(cfg, as_json=True, out=out, err=io.StringIO())
    report = json.loads(out.getvalue())
    assert report["speech_ok"] is False
    assert "custom" in (report.get("speech_hint") or "").lower()


def test_resolve_custom_api_key_literal_wins(tmp_path: Path) -> None:
    from hark.providers.custom_stt import resolve_custom_api_key

    kf = tmp_path / "key"
    kf.write_text("file-tok\n", encoding="utf-8")
    assert resolve_custom_api_key("lit-tok", str(kf)) == "lit-tok"
    assert resolve_custom_api_key(None, str(kf)) == "file-tok"
    assert resolve_custom_api_key("  ", str(kf)) == "file-tok"
    assert resolve_custom_api_key(None, None) is None
    assert resolve_custom_api_key(None, "") is None


def test_resolve_custom_api_key_file_missing_raises(tmp_path: Path) -> None:
    from hark.providers.custom_stt import resolve_custom_api_key

    with pytest.raises(ProviderError, match="key file unreadable"):
        resolve_custom_api_key(None, str(tmp_path / "nope"))


def test_resolve_custom_api_key_empty_file_is_none(tmp_path: Path) -> None:
    from hark.providers.custom_stt import resolve_custom_api_key

    kf = tmp_path / "key"
    kf.write_text("   \n", encoding="utf-8")
    assert resolve_custom_api_key(None, str(kf)) is None


def test_custom_stt_status_with_key_file(tmp_path: Path) -> None:
    kf = tmp_path / "key"
    kf.write_text("file-tok\n", encoding="utf-8")
    st = custom_stt_status(
        base_url="https://gw.example/v1",
        api_key=None,
        api_key_file=str(kf),
        model="grok-stt",
        path=None,
    )
    assert st.available is True

    st_missing = custom_stt_status(
        base_url="https://gw.example/v1",
        api_key=None,
        api_key_file=str(tmp_path / "nope"),
        model="grok-stt",
        path=None,
    )
    assert st_missing.available is False
    assert "unreadable" in st_missing.detail


def test_resolve_stt_custom_key_file_via_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HARK_STT_CUSTOM_API_KEY", raising=False)
    monkeypatch.delenv("HARK_STT_CUSTOM_API_KEY_FILE", raising=False)
    kf = tmp_path / "key"
    kf.write_text("file-tok\n", encoding="utf-8")
    cfg = SttConfig(
        provider="custom",
        custom_base_url="https://gw.example/v1",
        custom_api_key_file=str(kf),
        custom_model="grok-stt",
    )
    stt = resolve_stt("custom", stt_cfg=cfg)
    assert stt.api_key == "file-tok"


def test_config_round_trip_custom_api_key_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HARK_STT_CUSTOM_API_KEY_FILE", raising=False)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[stt]\nprovider = "custom"\n'
        'custom_base_url = "https://gw.example/v1"\n'
        'custom_api_key_file = "~/.llmp"\n'
        'custom_model = "grok-stt"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.stt.custom_api_key_file == "~/.llmp"
    dumped = config_to_dict(cfg)
    assert dumped["stt"]["custom_api_key_file"] == "~/.llmp"
    assert "custom_api_key" not in dumped["stt"]  # only *_configured is exposed
    assert dumped["stt"]["custom_api_key_configured"] is True


def test_resolve_custom_api_key_command(tmp_path: Path) -> None:
    from hark.providers.custom_stt import resolve_custom_api_key

    kf = tmp_path / "key"
    kf.write_text("file-tok\n", encoding="utf-8")
    # command used when no literal/file key
    assert (
        resolve_custom_api_key(None, None, f"printf '%s\\n' cmd-tok") == "cmd-tok"
    )
    # file wins over command
    assert (
        resolve_custom_api_key(None, str(kf), f"printf '%s\\n' cmd-tok") == "file-tok"
    )
    # literal wins over both
    assert (
        resolve_custom_api_key("lit", str(kf), f"printf '%s\\n' cmd-tok") == "lit"
    )
    # first non-empty line only
    assert resolve_custom_api_key(None, None, "printf 'line1\\nline2\\n'") == "line1"


def test_resolve_custom_api_key_command_nonzero_raises() -> None:
    from hark.providers.custom_stt import resolve_custom_api_key

    with pytest.raises(ProviderError, match="api key command failed"):
        resolve_custom_api_key(None, None, "false")


def test_resolve_stt_custom_key_command_via_config(monkeypatch) -> None:
    monkeypatch.delenv("HARK_STT_CUSTOM_API_KEY", raising=False)
    monkeypatch.delenv("HARK_STT_CUSTOM_API_KEY_FILE", raising=False)
    monkeypatch.delenv("HARK_STT_CUSTOM_API_KEY_COMMAND", raising=False)
    cfg = SttConfig(
        provider="custom",
        custom_base_url="https://gw.example/v1",
        custom_api_key_command="printf '%s\\n' cmd-tok",
        custom_model="grok-stt",
    )
    stt = resolve_stt("custom", stt_cfg=cfg)
    assert getattr(stt, "api_key") == "cmd-tok"


def test_config_round_trip_custom_api_key_command(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HARK_STT_CUSTOM_API_KEY_COMMAND", raising=False)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[stt]\nprovider = "custom"\n'
        'custom_base_url = "https://gw.example/v1"\n'
        'custom_api_key_command = "cat ~/.llmp"\n'
        'custom_model = "grok-stt"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg.stt.custom_api_key_command == "cat ~/.llmp"
    dumped = config_to_dict(cfg)
    assert dumped["stt"]["custom_api_key_command"] == "cat ~/.llmp"
    assert dumped["stt"]["custom_api_key_configured"] is True

import json

import httpx
import pytest

from hark.providers.base import ProviderError
from hark.providers.openai_p import OpenAIStt
from hark.providers.xai import XaiStt
import hark.providers.openai_p as openai_p
import hark.providers.xai as xai


class _TransportFailure:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        raise httpx.ConnectError("offline")


class _BadJsonResponse:
    status_code = 200
    text = "not json"
    content = b""
    headers = {}

    def json(self):
        raise json.JSONDecodeError("bad response", self.text, 0)


class _BadJsonClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return _BadJsonResponse()


def test_xai_transport_failure_is_provider_error(monkeypatch):
    monkeypatch.setattr(xai, "resolve_xai_token", lambda: "token")
    monkeypatch.setattr(xai.httpx, "Client", lambda **kwargs: _TransportFailure())

    with pytest.raises(ProviderError, match="xAI STT failed"):
        XaiStt().transcribe(b"wav")


def test_openai_malformed_json_is_provider_error(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "token")
    monkeypatch.setattr(
        openai_p.httpx, "Client", lambda **kwargs: _BadJsonClient()
    )

    with pytest.raises(ProviderError, match="OpenAI STT failed"):
        OpenAIStt().transcribe(b"wav")

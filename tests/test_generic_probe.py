"""Tests for ``/api/generic/probe``.

The probe issues HEAD requests against likely tokenize / token-count
paths. A 404 means "not present"; anything else (200, 401, 405, …)
counts as "present" since the path responded.
"""
from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from server.main import app
from server.routers import generic as generic_router


def _install_mock_transport(monkeypatch, handler):
    transport = httpx.MockTransport(handler)

    class _Wrap(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(generic_router.httpx, "AsyncClient", _Wrap)


def _named_payload(token: str = "") -> dict:
    return {
        "base_url": "https://openrouter.ai/api",
        "api_token": token,
        "model_id": "",
        "cache_minutes": None,
        "streaming": True,
        "brain_message_role": "system",
        "context_preset_id": None,
    }


def _set_or(client: TestClient):
    r = client.put("/api/settings", json={
        "provider_mode": "generic",
        "generic": {
            "provider": "openrouter",
            "novelai": _named_payload() | {"base_url": "https://text.novelai.net/oa"},
            "openrouter": _named_payload("or-key"),
            "nanogpt": _named_payload() | {"base_url": "https://nano-gpt.com/api"},
            "openai_compatible": {"custom_providers": [], "active_id": None},
        },
    })
    assert r.status_code == 200, r.text


def test_both_paths_404_yields_no_capabilities(tmp_storage, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404)
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or(client)
    r = client.get("/api/generic/probe?provider=openrouter")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "has_tokenize": False,
        "has_token_count": False,
        "tokenize_path": None,
        "token_count_path": None,
    }


def test_tokenize_200_token_count_404(tmp_storage, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if "tokenize" in req.url.path:
            return httpx.Response(200)
        return httpx.Response(404)
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or(client)
    r = client.get("/api/generic/probe?provider=openrouter")
    body = r.json()
    assert body["has_tokenize"] is True
    assert body["tokenize_path"] in ("/v1/tokenize", "/tokenize")
    assert body["has_token_count"] is False
    assert body["token_count_path"] is None


def test_tokenize_405_counts_as_present(tmp_storage, monkeypatch):
    """A path that exists but rejects HEAD with 405 still counts."""
    def handler(req: httpx.Request) -> httpx.Response:
        if "/v1/tokenize" in req.url.path:
            return httpx.Response(405)
        return httpx.Response(404)
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or(client)
    r = client.get("/api/generic/probe?provider=openrouter")
    body = r.json()
    assert body["has_tokenize"] is True
    assert body["tokenize_path"] == "/v1/tokenize"


def test_tokenize_401_counts_as_present(tmp_storage, monkeypatch):
    """Path requires auth — still 'present' (just gated)."""
    def handler(req: httpx.Request) -> httpx.Response:
        if "tokenize" in req.url.path:
            return httpx.Response(401)
        return httpx.Response(404)
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or(client)
    r = client.get("/api/generic/probe?provider=openrouter")
    assert r.json()["has_tokenize"] is True


def test_token_count_only(tmp_storage, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        if "token-count" in req.url.path:
            return httpx.Response(200)
        return httpx.Response(404)
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or(client)
    body = client.get("/api/generic/probe?provider=openrouter").json()
    assert body["has_tokenize"] is False
    assert body["has_token_count"] is True
    assert body["token_count_path"] == "/v1/token-count"


def test_openai_compatible_no_active_returns_empty(tmp_storage, monkeypatch):
    """Missing config doesn't throw — the Settings page would surface
    a misleading error. Returns the no-capabilities shape instead."""
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200)  # would say "present" if reached
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    r = client.get("/api/generic/probe?provider=openai_compatible")
    assert r.status_code == 200
    body = r.json()
    assert body["has_tokenize"] is False
    assert body["has_token_count"] is False


def test_network_error_returns_empty(tmp_storage, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or(client)
    r = client.get("/api/generic/probe?provider=openrouter")
    assert r.status_code == 200
    assert r.json() == {
        "has_tokenize": False,
        "has_token_count": False,
        "tokenize_path": None,
        "token_count_path": None,
    }

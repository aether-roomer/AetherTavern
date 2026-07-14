"""End-to-end test: the TTS model-discovery routes cache through
``app.state.discovery_cache``.

Mocks the upstream httpx client via ``MockTransport`` (same pattern
as ``tests/test_tts.py``). Counts upstream hits across multiple GETs
to verify:

  - second request reads cache,
  - ``?refresh=1`` forces a fresh upstream fetch,
  - OR and NGPT cache entries don't collide.
"""
from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from server.main import app
from server.routers import tts as tts_router


def _install_mock_transport(monkeypatch, handler):
    """Patch ``tts_router.httpx.AsyncClient`` to use a MockTransport.
    Same pattern as ``tests/test_tts.py:_install_mock_transport``."""
    transport = httpx.MockTransport(handler)

    class _Wrap(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(tts_router.httpx, "AsyncClient", _Wrap)
    return transport


def _or_handler_factory(counter, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        counter["openrouter"] = counter.get("openrouter", 0) + 1
        return httpx.Response(200, json=payload)
    return handler


def _ngpt_handler_factory(counter, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        counter["nanogpt"] = counter.get("nanogpt", 0) + 1
        return httpx.Response(200, json=payload)
    return handler


def _mux_handler(counter):
    """One handler covers both upstreams; counts per-host."""
    or_payload = {"data": [{"id": "or-model-1", "name": "OR Model"}]}
    ngpt_payload = {"data": [{"id": "ngpt-model-1", "name": "NGPT Model"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "openrouter" in host:
            counter["openrouter"] = counter.get("openrouter", 0) + 1
            return httpx.Response(200, json=or_payload)
        counter["nanogpt"] = counter.get("nanogpt", 0) + 1
        return httpx.Response(200, json=ngpt_payload)

    return handler


def test_openrouter_models_cached_on_second_request(tmp_storage, monkeypatch):
    counter: dict[str, int] = {}
    _install_mock_transport(monkeypatch, _or_handler_factory(
        counter, {"data": [{"id": "m1", "name": "M1"}]}
    ))

    client = TestClient(app)
    r1 = client.get("/api/tts/openrouter/models")
    assert r1.status_code == 200
    assert r1.json()["models"][0]["id"] == "m1"

    r2 = client.get("/api/tts/openrouter/models")
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert counter["openrouter"] == 1


def test_refresh_query_forces_refetch(tmp_storage, monkeypatch):
    counter: dict[str, int] = {}
    _install_mock_transport(monkeypatch, _or_handler_factory(
        counter, {"data": [{"id": "m1", "name": "M1"}]}
    ))

    client = TestClient(app)
    client.get("/api/tts/openrouter/models")
    client.get("/api/tts/openrouter/models?refresh=1")
    assert counter["openrouter"] == 2

    # And subsequent non-refresh again reads cache.
    client.get("/api/tts/openrouter/models")
    assert counter["openrouter"] == 2


def test_or_and_ngpt_cache_entries_independent(tmp_storage, monkeypatch):
    counter: dict[str, int] = {}
    _install_mock_transport(monkeypatch, _mux_handler(counter))

    client = TestClient(app)
    client.get("/api/tts/openrouter/models")
    client.get("/api/tts/nanogpt/models")
    client.get("/api/tts/openrouter/models")
    client.get("/api/tts/nanogpt/models")
    assert counter["openrouter"] == 1
    assert counter["nanogpt"] == 1

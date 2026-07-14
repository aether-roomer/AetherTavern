"""End-to-end tests for ``/api/generic/models``.

Mocks the upstream ``/v1/models`` via ``httpx.MockTransport`` (same
pattern as ``tests/test_tts_models_cache.py``). Verifies the cache
hits, ``?refresh=1`` forces a fresh fetch, and ``(provider, base_url,
token_hash)`` keys don't collide.
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


def _set_or_token(client: TestClient, token: str = "or-real-key"):
    """Configure generic.openrouter so the cache key has a stable token hash."""
    r = client.put("/api/settings", json={
        "provider_mode": "generic",
        "generic": {
            "provider": "openrouter",
            "novelai": _named_payload(""),
            "openrouter": _named_payload(token),
            "nanogpt": _named_payload(""),
            "openai_compatible": {"custom_providers": [], "active_id": None},
        },
    })
    assert r.status_code == 200, r.text


def _named_payload(token: str, base: str | None = None) -> dict:
    return {
        "base_url": base or "https://openrouter.ai/api",
        "api_token": token,
        "model_id": "",
        "cache_minutes": None,
        "streaming": True,
        "brain_message_role": "system",
        "context_preset_id": None,
    }


def test_first_call_hits_upstream_second_reads_cache(tmp_storage, monkeypatch):
    counter = {"or": 0}
    def handler(req: httpx.Request) -> httpx.Response:
        counter["or"] += 1
        return httpx.Response(200, json={"data": [{"id": "claude", "name": "Claude"}]})
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or_token(client)

    r1 = client.get("/api/generic/models?provider=openrouter")
    assert r1.status_code == 200, r1.text
    assert r1.json()["models"][0]["id"] == "claude"

    r2 = client.get("/api/generic/models?provider=openrouter")
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    assert counter["or"] == 1, "second call should read cache"


def test_refresh_forces_upstream_refetch(tmp_storage, monkeypatch):
    counter = {"or": 0}
    def handler(req: httpx.Request) -> httpx.Response:
        counter["or"] += 1
        return httpx.Response(200, json={"data": [{"id": "m"}]})
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or_token(client)
    client.get("/api/generic/models?provider=openrouter")
    client.get("/api/generic/models?provider=openrouter?refresh=1")  # malformed — still gets cached
    # ^ the previous line accidentally encodes ``?refresh=1`` as part of the query value;
    # the canonical form is below. Both expectations hold: only the canonical form
    # forces a refetch.
    client.get("/api/generic/models?provider=openrouter&refresh=1")
    assert counter["or"] == 2

    # And subsequent non-refresh again reads cache.
    client.get("/api/generic/models?provider=openrouter")
    assert counter["or"] == 2


def test_different_tokens_use_different_cache_entries(tmp_storage, monkeypatch):
    counter = {"or": 0}
    def handler(req: httpx.Request) -> httpx.Response:
        counter["or"] += 1
        return httpx.Response(200, json={"data": [{"id": "m"}]})
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or_token(client, token="key-a")
    client.get("/api/generic/models?provider=openrouter")
    # Rotate to a different token — cache key SHA changes, so a fresh
    # upstream fetch is required.
    _set_or_token(client, token="key-b")
    client.get("/api/generic/models?provider=openrouter")
    assert counter["or"] == 2


def test_upstream_500_surfaced_as_502(tmp_storage, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or_token(client)
    r = client.get("/api/generic/models?provider=openrouter")
    assert r.status_code == 502


def test_network_error_surfaced_as_504(tmp_storage, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _set_or_token(client)
    r = client.get("/api/generic/models?provider=openrouter")
    assert r.status_code == 504


def test_openai_compatible_no_active_is_400(tmp_storage, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    r = client.get("/api/generic/models?provider=openai_compatible")
    assert r.status_code == 400


def test_unknown_provider_400(tmp_storage, monkeypatch):
    client = TestClient(app)
    r = client.get("/api/generic/models?provider=bogus")
    assert r.status_code == 400


# --- /models?custom_id=<non-active> -----------------------------------------


def _custom_provider_payload(pid: str, base: str, token: str | None = None) -> dict:
    return {
        "id": pid,
        "label": pid,
        "base_url": base,
        "api_token": token,
        "model_id": "",
        "cache_minutes": None,
        "streaming": True,
        "brain_message_role": "system",
        "context_preset_id": None,
    }


def _configure_two_customs(client: TestClient) -> None:
    """Two custom openai_compatible providers; the first is active."""
    r = client.put("/api/settings", json={
        "provider_mode": "generic",
        "generic": {
            "provider": "openai_compatible",
            "novelai": _named_payload(""),
            "openrouter": _named_payload(""),
            "nanogpt": _named_payload(""),
            "openai_compatible": {
                "active_id": "custom_a",
                "custom_providers": [
                    _custom_provider_payload(
                        "custom_a", "https://a.example/v1", token="tok-a"),
                    _custom_provider_payload(
                        "custom_b", "https://b.example/v1", token="tok-b"),
                ],
            },
        },
    })
    assert r.status_code == 200, r.text


def test_custom_id_resolves_non_active_entry(tmp_storage, monkeypatch):
    """Refreshing a non-active custom provider must hit *its* base URL,
    not the active one's. Without ``custom_id`` support the route would
    400 (active doesn't match) or quietly return the active's models.
    """
    seen_urls: list[str] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen_urls.append(str(req.url))
        return httpx.Response(200, json={"data": [{"id": "from-b"}]})
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _configure_two_customs(client)

    r = client.get("/api/generic/models?provider=openai_compatible&custom_id=custom_b")
    assert r.status_code == 200, r.text
    assert r.json()["models"][0]["id"] == "from-b"
    # The fetch went to b.example, not a.example (URL routing). The token
    # used is asserted separately in test_custom_id_uses_own_token.
    assert any("b.example" in u for u in seen_urls), seen_urls
    assert not any("a.example" in u for u in seen_urls), seen_urls


def test_custom_id_uses_own_token(tmp_storage, monkeypatch):
    """A non-active custom provider's discovery must authenticate with *its
    own* token, not the globally-active entry's — so a per-chat override to a
    non-active custom provider lists models against the right credentials."""
    seen_auth: list[str | None] = []
    def handler(req: httpx.Request) -> httpx.Response:
        seen_auth.append(req.headers.get("authorization"))
        return httpx.Response(200, json={"data": [{"id": "m"}]})
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _configure_two_customs(client)  # active=custom_a (tok-a), custom_b=tok-b

    r = client.get(
        "/api/generic/models?provider=openai_compatible&custom_id=custom_b")
    assert r.status_code == 200, r.text
    assert seen_auth == ["Bearer tok-b"]


def test_custom_id_unknown_400(tmp_storage, monkeypatch):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _configure_two_customs(client)
    r = client.get(
        "/api/generic/models?provider=openai_compatible&custom_id=nope",
    )
    assert r.status_code == 400


def test_custom_id_keys_cache_separately(tmp_storage, monkeypatch):
    """Cache key is (provider, base_url, token_hash) — two customs with
    different base_urls don't share a slot, so refreshing one doesn't
    invalidate or leak into the other's response."""
    handler_call_log: list[tuple[str, int]] = []
    responses_by_host = {
        "a.example": {"id": "model-a"},
        "b.example": {"id": "model-b"},
    }
    def handler(req: httpx.Request) -> httpx.Response:
        host = req.url.host
        handler_call_log.append((host, len(handler_call_log) + 1))
        return httpx.Response(200, json={"data": [responses_by_host[host]]})
    _install_mock_transport(monkeypatch, handler)

    client = TestClient(app)
    _configure_two_customs(client)

    r_a = client.get(
        "/api/generic/models?provider=openai_compatible&custom_id=custom_a")
    r_b = client.get(
        "/api/generic/models?provider=openai_compatible&custom_id=custom_b")
    assert r_a.json()["models"][0]["id"] == "model-a"
    assert r_b.json()["models"][0]["id"] == "model-b"
    # Each upstream got exactly one call.
    hosts = [h for h, _ in handler_call_log]
    assert hosts.count("a.example") == 1
    assert hosts.count("b.example") == 1

"""Typed-error preflight on the generation routes in Generic mode.

Hitting ``/generate`` in Generic mode without provider/preset/token set
yields a typed SSE error (``no_provider`` / ``no_preset`` / ``no_token``)
rather than a 4xx or an opaque connection failure — EventSource can't
read non-2xx response bodies, and we want errors to land on the same
channel the client already handles. The global busy slot must always
release so a subsequent call also reaches the preflight.

The deletion-response route still uses AER (no Generic analogue). In
Generic mode without AER configured, it yields ``no_aer_configured``.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from server.main import app


def _create_chat(client: TestClient) -> tuple[str, str]:
    r = client.post("/api/contacts", json={"id": "", "name": "Test"})
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "Test chat",
    })
    return contact_id, r.json()["id"]


def _flip_to_generic(client: TestClient) -> None:
    r = client.put("/api/settings", json={"provider_mode": "generic"})
    assert r.status_code == 200, r.text


def test_chat_generate_yields_typed_no_provider_when_unconfigured(tmp_storage):
    """Default Generic state has no provider model set; preflight surfaces
    a typed ``no_provider`` SSE error."""
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    _flip_to_generic(client)

    r = client.get(f"/api/chats/{chat_id}/generate")
    assert r.status_code == 200, r.text
    assert "event: error" in r.text
    assert "no_provider" in r.text


def test_chat_generate_releases_slot_for_subsequent_call(tmp_storage):
    """A second call must also reach the preflight (not be denied as busy)."""
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    _flip_to_generic(client)

    first = client.get(f"/api/chats/{chat_id}/generate")
    second = client.get(f"/api/chats/{chat_id}/generate")
    assert "event: error" in first.text
    assert "event: error" in second.text
    assert "busy" not in second.text.lower()


def test_deletion_response_uses_aer_even_in_generic_mode(tmp_storage):
    """The deletion-response route always uses AER. In Generic mode
    without AER credentials, it surfaces ``no_aer_configured`` rather
    than attempting Generic generation."""
    client = TestClient(app)
    contact_id, _chat_id = _create_chat(client)
    _flip_to_generic(client)

    r = client.get(f"/api/contacts/{contact_id}/deletion-response")
    assert r.status_code == 200, r.text
    assert "event: error" in r.text
    assert "no_aer_configured" in r.text


def test_aer_mode_does_not_emit_generic_preflight_errors(tmp_storage):
    """In AER mode the Generic preflight must not fire."""
    client = TestClient(app)
    _contact_id, chat_id = _create_chat(client)
    # provider_mode defaults to aetherroom — no PUT needed.

    r = client.get(f"/api/chats/{chat_id}/generate")
    assert r.status_code == 200, r.text
    assert "no_provider" not in r.text
    assert "no_preset" not in r.text
    assert "no_token" not in r.text


def _create_chat_with(client: TestClient, **overrides) -> str:
    contact_id = client.post("/api/contacts", json={"id": "", "name": "Test"}).json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id,
        "title": "Test chat", **overrides,
    })
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_override_to_generic_in_aer_global_emits_generic_preflight(tmp_storage):
    """Global mode is AER, but the chat pins a Generic provider — generation
    branches on the RESOLVED provider, so the Generic preflight fires."""
    client = TestClient(app)
    # Global stays aetherroom; chat overrides to (unconfigured) novelai.
    chat_id = _create_chat_with(client, provider_override="novelai")
    r = client.get(f"/api/chats/{chat_id}/generate")
    assert r.status_code == 200, r.text
    assert "event: error" in r.text
    assert "no_provider" in r.text  # novelai has no model configured


def test_override_to_aetherroom_in_generic_global_skips_generic_preflight(tmp_storage):
    """Global mode is Generic, but the chat pins AER — the Generic preflight
    must NOT fire (it runs the AER path instead)."""
    client = TestClient(app)
    _flip_to_generic(client)
    chat_id = _create_chat_with(client, provider_override="aetherroom")
    r = client.get(f"/api/chats/{chat_id}/generate")
    assert r.status_code == 200, r.text
    assert "no_provider" not in r.text
    assert "no_preset" not in r.text
    assert "no_token" not in r.text


def test_deleted_custom_override_emits_no_provider_and_releases_slot(tmp_storage):
    """A provider_override pointing at a missing custom entry fails safe with a
    typed ``no_provider`` and still releases the global slot."""
    client = TestClient(app)
    chat_id = _create_chat_with(client, provider_override="openai_compatible:ghost")
    first = client.get(f"/api/chats/{chat_id}/generate")
    second = client.get(f"/api/chats/{chat_id}/generate")
    assert "no_provider" in first.text
    assert "event: error" in second.text
    assert "busy" not in second.text.lower()

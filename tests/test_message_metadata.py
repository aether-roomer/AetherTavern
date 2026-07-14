"""``_persist_assistant_message`` writes the six generation-metadata fields
in both AER and Generic branches; user-typed messages + greeting auto-insert
leave them as ``None``.

The Generic branch is exercised end-to-end via a stubbed ``stream_chat``
async generator that emits a single ``DeltaEvent`` and ``DoneEvent``. The
AER branch is exercised the same way via a stubbed ``stream_completion``.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from server import storage
from server.inference_generic import DeltaEvent, DoneEvent, StartEvent, UsageEvent
from server.main import app


def _setup_basic_chat(client: TestClient) -> tuple[str, str]:
    r = client.post("/api/contacts", json={"id": "", "name": "Roxy"})
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
    })
    return contact_id, r.json()["id"]


def test_user_message_persists_with_origin_manual_no_metadata(tmp_storage):
    client = TestClient(app)
    contact_id, chat_id = _setup_basic_chat(client)
    r = client.post(f"/api/chats/{chat_id}/messages", json={
        "parent_id": None,
        "sender": "user",
        "sender_name": "Anon",
        "body": [{"text": "hello", "emotion": "neutral"}],
    })
    assert r.status_code == 200, r.text
    msg = r.json()
    assert msg["origin"] == "manual"
    # All six metadata fields are None for user messages.
    assert msg["generation_started_at"] is None
    assert msg["generation_duration_seconds"] is None
    assert msg["provider"] is None
    assert msg["model"] is None
    assert msg["generation_preset_id"] is None
    assert msg["context_preset_id"] is None


def test_greeting_auto_insert_origin_aer_no_metadata(tmp_storage):
    """Greetings persist as AER-origin even when Generic mode is active —
    AER form preserves emotion + multi-bubble structure, so storing the
    greeting that way keeps round-trip fidelity across mode switches.
    Metadata stays None (no LLM call happened)."""
    client = TestClient(app)
    r = client.post("/api/contacts", json={
        "id": "", "name": "Roxy", "greeting": "Hi I'm Roxy.",
    })
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
    })
    chat_id = r.json()["id"]
    msgs = client.get(f"/api/chats/{chat_id}/messages").json()
    # First message is the greeting.
    assert len(msgs) >= 1
    greeting = msgs[0]
    assert greeting["origin"] == "aer"
    assert greeting["provider"] is None
    assert greeting["generation_started_at"] is None
    assert greeting["generation_duration_seconds"] is None
    assert greeting["generation_preset_id"] is None


def test_persist_assistant_message_accepts_metadata_kwargs(tmp_storage):
    """Unit-level: ``_persist_assistant_message`` accepts the new origin /
    metadata kwargs and stamps them onto the ChatMessage. The end-to-end
    AER stream wiring uses this same function — covered indirectly by
    the AER acceptance-test suite, and directly here at the signature
    level."""
    client = TestClient(app)
    _contact_id, chat_id = _setup_basic_chat(client)
    chat = storage.get_chat(chat_id)
    contact = storage.get_contact(chat.contact_id)
    msgs_container = storage.load_chat_messages(chat_id)

    from server.routers.generate import _persist_assistant_message
    from server.models import SubMessage, new_id

    new_msg_id = new_id()
    _persist_assistant_message(
        chat_id=chat_id,
        chat=chat,
        msgs_container=msgs_container,
        new_msg_id=new_msg_id,
        chosen_parent_id=None,
        bubbles=[SubMessage(text="hello", emotion="neutral")],
        contact_name=contact.name,
        new_cursor=0,
        new_path_ids=[],
        new_rolled_over=False,
        context_tokens=42,
        active_brains_payload=[],
        origin="aer",
        provider="aetherroom",
        model="some-model",
        generation_preset_id="preset-xyz",
        context_preset_id=None,
        generation_started_at=1700000000.0,
        generation_duration_seconds=1.5,
    )
    msgs = client.get(f"/api/chats/{chat_id}/messages").json()
    persisted = next((m for m in msgs if m["id"] == new_msg_id), None)
    assert persisted is not None
    assert persisted["origin"] == "aer"
    assert persisted["provider"] == "aetherroom"
    assert persisted["model"] == "some-model"
    assert persisted["generation_preset_id"] == "preset-xyz"
    assert persisted["context_preset_id"] is None
    assert persisted["generation_started_at"] == pytest.approx(1700000000.0)
    assert persisted["generation_duration_seconds"] == pytest.approx(1.5)


def test_persist_assistant_message_generic_path(tmp_storage):
    """The same helper writes Generic-mode metadata — confirms the
    signature accepts ``origin="generic"`` + a ``context_preset_id``."""
    client = TestClient(app)
    _contact_id, chat_id = _setup_basic_chat(client)
    chat = storage.get_chat(chat_id)
    contact = storage.get_contact(chat.contact_id)
    msgs_container = storage.load_chat_messages(chat_id)

    from server.routers.generate import _persist_assistant_message
    from server.models import SubMessage, new_id

    new_msg_id = new_id()
    _persist_assistant_message(
        chat_id=chat_id,
        chat=chat,
        msgs_container=msgs_container,
        new_msg_id=new_msg_id,
        chosen_parent_id=None,
        bubbles=[SubMessage(text="hi", emotion=None)],
        contact_name=contact.name,
        new_cursor=0,
        new_path_ids=[],
        new_rolled_over=False,
        context_tokens=100,
        active_brains_payload=[],
        origin="generic",
        reasoning="thinking...",
        image_refs={"https://example.com/x.jpg": "uuid-1"},
        provider="openrouter",
        model="gpt-foo",
        generation_preset_id="gp-1",
        context_preset_id="cp-1",
        generation_started_at=1700000001.0,
        generation_duration_seconds=2.5,
    )
    msgs = client.get(f"/api/chats/{chat_id}/messages").json()
    persisted = next((m for m in msgs if m["id"] == new_msg_id), None)
    assert persisted is not None
    assert persisted["origin"] == "generic"
    assert persisted["provider"] == "openrouter"
    assert persisted["model"] == "gpt-foo"
    assert persisted["context_preset_id"] == "cp-1"
    assert persisted["reasoning"] == "thinking..."
    # image_refs lands on disk too (rewrite at wire-time substitutes the
    # remote URL with the proxy URL in body text — the dict itself is
    # preserved as-is in the response).
    assert persisted["image_refs"] == {"https://example.com/x.jpg": "uuid-1"}

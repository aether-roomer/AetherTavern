"""Concurrency: routes that load-modify-write the same file must serialize
through ``storage.lock(key)``. Without the lock, two concurrent
``POST /chats/{id}/messages`` calls would both load messages.yaml, each
append, and the second save would clobber the first.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
import httpx
from httpx import ASGITransport

from server import storage
from server.main import app
from server.models import Contact, SubMessage, User


@pytest.mark.asyncio
async def test_concurrent_create_message_does_not_lose_a_message(tmp_storage):
    """Two simultaneous POST /messages on the same chat must both land.

    Without the lock, the second writer reads messages.yaml at the same
    moment as the first, both append a new message in-memory, both save —
    the slower writer's save wipes the faster one out.
    """
    contact = storage.save_contact(Contact(name="Alice"))
    user = storage.save_user(User(name="Me"))
    # Use the API to create the chat so it goes through the same lock-aware
    # path as a real client. Using TestClient so the FastAPI lifespan runs.
    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
        chat_id = chat["id"]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as ac:
        # Fire two concurrent message creates.
        async def post_one(text: str):
            return await ac.post(
                f"/api/chats/{chat_id}/messages",
                json={
                    "sender": "user",
                    "body": [{"text": text, "emotion": "neutral"}],
                },
            )

        results = await asyncio.gather(post_one("hello"), post_one("world"))

    for r in results:
        assert r.status_code == 200, r.text

    # Both messages must be present. Without the lock, only one survives.
    msgs = storage.load_chat_messages(chat_id).messages
    # Greeting may or may not be present depending on contact.greeting; we
    # assert AT LEAST the two we just sent are in the file (plus optional
    # seed greeting from create_chat).
    user_msgs = [m for m in msgs if m.sender == "user"]
    bodies = {m.body[0].text for m in user_msgs}
    assert "hello" in bodies
    assert "world" in bodies


@pytest.mark.asyncio
async def test_concurrent_select_child_serializes(tmp_storage):
    """Two simultaneous /select calls must both apply (last-writer-wins is
    OK; what's NOT OK is a half-written chat.yaml). Sanity-check that the
    lock at least allows both calls to succeed without a 5xx."""
    contact = storage.save_contact(Contact(name="Bob"))
    user = storage.save_user(User(name="Me"))
    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
        chat_id = chat["id"]
        m1 = client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "sender": "user",
                "body": [{"text": "first", "emotion": "neutral"}],
            },
        ).json()
        m2 = client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "parent_id": m1["id"],
                "sender": "user",
                "body": [{"text": "second", "emotion": "neutral"}],
            },
        ).json()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
    ) as ac:
        async def select(child_id: str):
            return await ac.post(
                f"/api/chats/{chat_id}/select",
                json={"parent_id": m1["id"], "child_id": child_id},
            )
        # Two opposing selects fired together; both must finish 200.
        r1, r2 = await asyncio.gather(select(m2["id"]), select("__empty__"))

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text


def test_chat_tree_mutation_doesnt_bump_chat_version_id(tmp_storage):
    """Tree mutations (create_message, select, etc.) save with
    bump_version=False so a chat-info-modal draft elsewhere doesn't get
    spuriously 409'd just because another tab generated or navigated."""
    contact = storage.save_contact(Contact(name="Carol"))
    user = storage.save_user(User(name="Me"))
    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
        original_vid = chat["version_id"]
        client.post(
            f"/api/chats/{chat['id']}/messages",
            json={"sender": "user",
                  "body": [{"text": "hi", "emotion": "neutral"}]},
        )
        refreshed = client.get(f"/api/chats/{chat['id']}").json()
        assert refreshed["version_id"] == original_vid, (
            "create_message must not bump chat.version_id (otherwise the "
            "chat info modal's draft elsewhere would 409 on its next "
            "autosave). bump_version=False is load-bearing."
        )

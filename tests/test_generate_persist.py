"""Generation persists messages before the final SSE yield.

Ordering: persist immediately after parser.end(), then yield the
diagnostics + done. A late yield raising after persist still leaves the
message on disk.

This test verifies the basic happy path: a generation lands on disk and
the SSE stream emits ``done`` last. Structural ordering (persist BEFORE
the final yield) is enforced by code review — the test below catches a
regression where the persist starts failing entirely.
"""
from __future__ import annotations

import json
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from server import storage
from server.main import app
from server.models import Contact, User


async def _fake_stream(*_args, **_kwargs):
    """Minimal upstream stub: yield a single AER-formatted bubble."""
    yield " hello there\n  Emotion: happy\n"


def test_generate_persists_message_on_success(tmp_storage, monkeypatch):
    """End-to-end: a successful generation produces a persisted assistant
    message. Catches a regression where the persist starts failing
    entirely."""
    contact = storage.save_contact(Contact(name="Alice", greeting="hi"))
    user = storage.save_user(User(name="Me"))

    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
        chat_id = chat["id"]

        # Patch the upstream streamer.
        monkeypatch.setattr(
            "server.routers.generate.stream_completion", _fake_stream,
        )
        # Avoid the real tokenizer. Patch the rollover orchestrator to
        # return a no-op context.
        from server.aer.rollover import GenerationContext

        def fake_build(**kwargs):
            return GenerationContext(
                api_messages=[{"role": "system", "content": ""}],
                total_tokens=0, history_tokens=0,
                brain_tokens=0, system_tokens=0,
                new_cursor=0, new_path_ids=[], new_rolled_over=False,
            )
        monkeypatch.setattr(
            "server.routers.generate.build_messages_for_generation",
            fake_build,
        )
        monkeypatch.setattr(
            "server.routers.generate.render", lambda *a, **kw: "PROMPT",
        )

        # Drain the SSE response and collect events.
        events = []
        with client.stream(
            "GET", f"/api/chats/{chat_id}/generate?expected_tip_id=",
        ) as r:
            assert r.status_code == 200
            for line in r.iter_lines():
                if line.startswith("event: "):
                    events.append(line[7:])

    assert "done" in events, f"expected 'done' in {events}"

    # Verify the message is persisted on disk.
    msgs = storage.load_chat_messages(chat_id).messages
    contact_msgs = [m for m in msgs if m.sender == "contact"]
    # Greeting + the new generation = at least 2 contact messages.
    assert len(contact_msgs) >= 2
    # The latest one should contain "hello there".
    latest = contact_msgs[-1]
    assert any("hello there" in b.text for b in latest.body)


def test_aer_override_model_is_stamped(tmp_storage, monkeypatch):
    """An AER chat with a per-provider model override stamps the override model
    (resolved from ``model_overrides['aetherroom']``) onto the message."""
    contact = storage.save_contact(Contact(name="Alice", greeting="hi"))
    user = storage.save_user(User(name="Me"))

    with TestClient(app) as client:
        chat = client.post("/api/chats", json={
            "contact_id": contact.id, "user_id": user.id,
            "model_overrides": {"aetherroom": "custom-aer-model"},
        }).json()
        chat_id = chat["id"]

        monkeypatch.setattr(
            "server.routers.generate.stream_completion", _fake_stream,
        )
        from server.aer.rollover import GenerationContext

        def fake_build(**kwargs):
            return GenerationContext(
                api_messages=[{"role": "system", "content": ""}],
                total_tokens=0, history_tokens=0,
                brain_tokens=0, system_tokens=0,
                new_cursor=0, new_path_ids=[], new_rolled_over=False,
            )
        monkeypatch.setattr(
            "server.routers.generate.build_messages_for_generation", fake_build,
        )
        monkeypatch.setattr(
            "server.routers.generate.render", lambda *a, **kw: "PROMPT",
        )

        with client.stream(
            "GET", f"/api/chats/{chat_id}/generate?expected_tip_id=",
        ) as r:
            assert r.status_code == 200
            for _line in r.iter_lines():
                pass

    latest = [m for m in storage.load_chat_messages(chat_id).messages
              if m.sender == "contact"][-1]
    assert latest.provider == "aetherroom"
    assert latest.model == "custom-aer-model"
    assert latest.context_preset_id is None

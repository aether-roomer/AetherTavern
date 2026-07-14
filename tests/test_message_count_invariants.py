"""``Chat.message_count`` must always equal ``len(get_active_path(...))``.

The list endpoint reads ``message_count`` directly off the cached field
instead of walking ``messages.yaml`` per chat (Fix A.2 — see
``load_speed.md``). For that to be accurate, every tree-mutation route
needs to call ``storage.recount_active_path`` before persisting the chat.

These tests exercise each tree-mutation path and assert the invariant
holds end-to-end (PUT through the FastAPI route, GET the chat back,
compare ``message_count`` against a fresh active-path walk).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from server import storage
from server.aer.rollover import get_active_path
from server.main import app
from server.models import (
    Chat, ChatMessage, ChatMessages, Contact, ROOT_PARENT_KEY, SubMessage, User,
)


def _setup(tmp_storage):
    contact = storage.save_contact(Contact(name="Alpha"))
    user = storage.save_user(User(name="Me"))
    chat = storage.save_chat(Chat(
        title="Test", contact_id=contact.id, user_id=user.id,
    ))
    return chat, contact, user


def _expected(chat_id: str) -> int:
    chat = storage.get_chat(chat_id)
    messages = storage.load_chat_messages(chat_id)
    return len(get_active_path(chat, messages.messages))


def _msg(sender: str, text: str, parent_id: str | None = None) -> ChatMessage:
    return ChatMessage(
        sender=sender, sender_name=sender,
        parent_id=parent_id,
        body=[SubMessage(text=text)],
    )


def test_message_count_after_create_message(tmp_storage):
    chat, _, _ = _setup(tmp_storage)
    with TestClient(app) as client:
        # Three messages, each chained to the previous one — extends
        # the active branch. Messages created at the same parent become
        # siblings, of which only one is on the active path.
        parent_id = None
        for i in range(3):
            r = client.post(
                f"/api/chats/{chat.id}/messages",
                json={
                    "sender": "user",
                    "body": [{"text": f"hi {i}", "emotion": "neutral"}],
                    "parent_id": parent_id,
                },
            )
            assert r.status_code == 200
            parent_id = r.json()["id"]
        live = client.get(f"/api/chats/{chat.id}").json()
        assert live["message_count"] == 3 == _expected(chat.id)


def test_message_count_after_soft_delete_and_restore(tmp_storage):
    chat, _, _ = _setup(tmp_storage)
    with TestClient(app) as client:
        ids = []
        parent_id = None
        for i in range(3):
            r = client.post(
                f"/api/chats/{chat.id}/messages",
                json={
                    "sender": "user",
                    "body": [{"text": f"hi {i}", "emotion": "neutral"}],
                    "parent_id": parent_id,
                },
            )
            ids.append(r.json()["id"])
            parent_id = ids[-1]
        # Soft-delete the third (tail) — sets EMPTY_SENTINEL on its parent.
        r = client.delete(f"/api/chats/{chat.id}/messages/{ids[2]}")
        assert r.status_code == 200
        assert _expected(chat.id) == 2
        live = client.get(f"/api/chats/{chat.id}").json()
        assert live["message_count"] == 2
        # Restore — count goes back up.
        r = client.post(f"/api/chats/{chat.id}/messages/{ids[2]}/restore")
        assert r.status_code == 200
        live = client.get(f"/api/chats/{chat.id}").json()
        assert live["message_count"] == 3 == _expected(chat.id)


def test_message_count_after_select_child_branch_swap(tmp_storage):
    """Manually seed a forked tree, then swap the chosen branch via
    ``/select``. The two branches have different lengths; ``message_count``
    must track the active branch."""
    chat, _, _ = _setup(tmp_storage)
    # Build: root → A → B, with root → A' as an alternative branch.
    a = _msg("user", "A")
    b = _msg("user", "B", parent_id=a.id)
    aprime = _msg("user", "A'")
    msgs = ChatMessages(messages=[a, b, aprime])
    storage.save_chat_messages(chat.id, msgs)
    # Pick A → B path explicitly.
    chat_obj = storage.get_chat(chat.id)
    chat_obj.selected_child_id = {ROOT_PARENT_KEY: a.id, a.id: b.id}
    storage.recount_active_path(chat_obj, msgs)
    storage.save_chat(chat_obj, bump_version=False)
    assert storage.get_chat(chat.id).message_count == 2

    with TestClient(app) as client:
        # Swap root's selection to A' (length-1 branch).
        r = client.post(
            f"/api/chats/{chat.id}/select",
            json={"parent_id": None, "child_id": aprime.id},
        )
        assert r.status_code == 200
        live = client.get(f"/api/chats/{chat.id}").json()
        assert live["message_count"] == 1 == _expected(chat.id)


def test_message_count_after_restore_path(tmp_storage):
    """``/restore-path`` swaps the selection map wholesale (bookmark jump
    semantics). ``message_count`` reflects the new active path."""
    chat, _, _ = _setup(tmp_storage)
    a = _msg("user", "A")
    b = _msg("user", "B", parent_id=a.id)
    aprime = _msg("user", "A'")
    bprime = _msg("user", "B'", parent_id=aprime.id)
    cprime = _msg("user", "C'", parent_id=bprime.id)
    msgs = ChatMessages(messages=[a, b, aprime, bprime, cprime])
    storage.save_chat_messages(chat.id, msgs)
    chat_obj = storage.get_chat(chat.id)
    chat_obj.selected_child_id = {ROOT_PARENT_KEY: a.id, a.id: b.id}
    storage.recount_active_path(chat_obj, msgs)
    storage.save_chat(chat_obj, bump_version=False)
    assert storage.get_chat(chat.id).message_count == 2

    with TestClient(app) as client:
        r = client.post(
            f"/api/chats/{chat.id}/restore-path",
            json={"selected_child_id": {
                ROOT_PARENT_KEY: aprime.id,
                aprime.id: bprime.id,
                bprime.id: cprime.id,
            }},
        )
        assert r.status_code == 200
        live = client.get(f"/api/chats/{chat.id}").json()
        assert live["message_count"] == 3 == _expected(chat.id)

"""Outbound message-serialization rewrite — translates ``image_refs``
remote URLs to the proxy URL in the body text just before the client
sees it. On-disk YAML keeps the original URL (so reload-via-list still
rewrites correctly, and a missing proxy file can be re-resolved)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from server import storage
from server.main import app


def _create_chat(client: TestClient) -> str:
    r = client.post("/api/contacts", json={"id": "", "name": "C"})
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
    })
    return r.json()["id"]


def test_list_messages_rewrites_remote_url_to_proxy(tmp_storage):
    """A persisted message with ``image_refs`` should have its body text
    rewritten on the wire — the response body contains the proxy URL,
    but the on-disk YAML keeps the original URL."""
    client = TestClient(app)
    chat_id = _create_chat(client)
    chat = storage.get_chat(chat_id)
    contact = storage.get_contact(chat.contact_id)
    msgs_container = storage.load_chat_messages(chat_id)

    from server.routers.generate import _persist_assistant_message
    from server.models import SubMessage, new_id

    new_msg_id = new_id()
    body_text = "look ![cat](https://example.com/cat.jpg)"
    _persist_assistant_message(
        chat_id=chat_id, chat=chat, msgs_container=msgs_container,
        new_msg_id=new_msg_id, chosen_parent_id=None,
        bubbles=[SubMessage(text=body_text, emotion=None)],
        contact_name=contact.name,
        new_cursor=0, new_path_ids=[], new_rolled_over=False,
        context_tokens=0, active_brains_payload=[],
        origin="generic",
        image_refs={"https://example.com/cat.jpg": "myuuid"},
    )

    # GET response: text is rewritten to the proxy URL.
    msgs = client.get(f"/api/chats/{chat_id}/messages").json()
    persisted = next(m for m in msgs if m["id"] == new_msg_id)
    text = persisted["body"][0]["text"]
    assert "/api/chats/" in text and "/images/myuuid/" in text
    assert "https://example.com/cat.jpg" not in text
    # On-disk YAML still carries the original URL.
    chat_path = storage.chat_dir(chat_id)
    import msgspec.yaml
    raw = msgspec.yaml.decode((chat_path / "messages.yaml").read_bytes())
    disk_msg = next(m for m in raw["messages"] if m["id"] == new_msg_id)
    assert "https://example.com/cat.jpg" in disk_msg["body"][0]["text"]


def test_list_messages_without_image_refs_unchanged(tmp_storage):
    """Messages without ``image_refs`` pass through untouched."""
    client = TestClient(app)
    chat_id = _create_chat(client)
    r = client.post(f"/api/chats/{chat_id}/messages", json={
        "parent_id": None,
        "sender": "user",
        "sender_name": "Anon",
        "body": [{"text": "no image refs here", "emotion": "neutral"}],
    })
    assert r.status_code == 200
    msgs = client.get(f"/api/chats/{chat_id}/messages").json()
    user_msg = next(m for m in msgs if m["sender"] == "user")
    assert user_msg["body"][0]["text"] == "no image refs here"
    assert user_msg["image_refs"] == {}

"""One-shot migration that stamps ``origin: "manual"`` on user-side messages
whose YAML on disk predates the field.

Contact-side messages keep the Pydantic default ``"aer"`` — they're not
rewritten. ``updated_at`` doesn't bump (migrations use ``atomic_write_yaml``
directly). Running the migration a second time is a no-op.
"""
from __future__ import annotations

import msgspec.yaml

from server import storage


def _write_legacy_messages_yaml(path, raw_messages):
    """Write a messages.yaml without origin keys."""
    payload = {"messages": raw_messages}
    path.write_bytes(msgspec.yaml.encode(payload))


def test_user_side_legacy_message_gets_origin_manual(tmp_storage):
    chats = list(storage.index.chat_paths.items())
    # Seed a chat through the API so the index entry exists.
    from fastapi.testclient import TestClient
    from server.main import app
    client = TestClient(app)
    r = client.post("/api/contacts", json={"id": "", "name": "C"})
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
    })
    chat_id = r.json()["id"]
    chat_path = storage.chat_dir(chat_id)
    assert chat_path is not None

    # Overwrite messages.yaml with legacy shape: one user msg + one contact
    # msg, neither carrying ``origin``.
    msg_path = chat_path / "messages.yaml"
    _write_legacy_messages_yaml(msg_path, [
        {
            "id": "abc123",
            "parent_id": None,
            "sender": "user",
            "sender_name": "Anon",
            "body": [{"text": "hi", "emotion": "neutral"}],
        },
        {
            "id": "def456",
            "parent_id": "abc123",
            "sender": "contact",
            "sender_name": "C",
            "body": [{"text": "hello", "emotion": "neutral"}],
        },
    ])

    storage._migrate_legacy_message_origin()

    raw = msgspec.yaml.decode(msg_path.read_bytes())
    msgs = raw["messages"]
    assert msgs[0]["origin"] == "manual"
    # Contact-side: Pydantic defaults to "aer" on load, and the migration
    # writes the whole model back. The semantically-correct state for
    # contact messages is "aer" (Pydantic's default matches the field's
    # historical meaning), so either an absent key or an explicit "aer"
    # is acceptable. Assert the contact message's origin resolves to
    # "aer" rather than getting accidentally upgraded to "manual".
    contact_origin = msgs[1].get("origin", "aer")
    assert contact_origin == "aer"


def test_migration_is_idempotent(tmp_storage):
    from fastapi.testclient import TestClient
    from server.main import app
    client = TestClient(app)
    r = client.post("/api/contacts", json={"id": "", "name": "C"})
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
    })
    chat_id = r.json()["id"]
    chat_path = storage.chat_dir(chat_id)
    msg_path = chat_path / "messages.yaml"
    _write_legacy_messages_yaml(msg_path, [
        {
            "id": "abc",
            "parent_id": None,
            "sender": "user",
            "sender_name": "U",
            "body": [{"text": "hi", "emotion": "neutral"}],
        },
    ])

    storage._migrate_legacy_message_origin()
    mtime1 = msg_path.stat().st_mtime_ns
    # Second run should be a no-op (no missing keys → no rewrite).
    storage._migrate_legacy_message_origin()
    mtime2 = msg_path.stat().st_mtime_ns
    assert mtime1 == mtime2, "second migration shouldn't touch the file"


def test_gate_skips_already_migrated_chats(tmp_storage):
    """A chat with ``user_origin_migrated_at`` set is skipped on a future
    migration run, so impersonate-stamped user origins survive even if the
    migration logic is later changed to also touch user-side messages
    with stored origins."""
    from fastapi.testclient import TestClient
    from server.main import app
    client = TestClient(app)
    r = client.post("/api/contacts", json={"id": "", "name": "C"})
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
    })
    chat_id = r.json()["id"]
    chat_path = storage.chat_dir(chat_id)
    msg_path = chat_path / "messages.yaml"

    # User-side message with a NON-manual origin (mimics an
    # impersonate-generated user message).
    _write_legacy_messages_yaml(msg_path, [
        {
            "id": "imp1",
            "parent_id": None,
            "sender": "user",
            "sender_name": "U",
            "body": [{"text": "hi", "emotion": "neutral"}],
            "origin": "aer",
        },
    ])

    # First run: gate gets set; the impersonate message's origin stays "aer".
    storage._migrate_legacy_message_origin()
    raw = msgspec.yaml.decode(msg_path.read_bytes())
    assert raw["messages"][0]["origin"] == "aer"
    chat = storage.get_chat(chat_id)
    assert chat.user_origin_migrated_at is not None

    # Manually inject a legacy missing-origin user message AFTER the gate is
    # set, and re-run the migration. The gate must skip the chat so the
    # legacy entry stays without an origin (proving the gate is honored).
    _write_legacy_messages_yaml(msg_path, [
        {
            "id": "legacy",
            "parent_id": None,
            "sender": "user",
            "sender_name": "U",
            "body": [{"text": "later", "emotion": "neutral"}],
        },
    ])
    storage._migrate_legacy_message_origin()
    raw_after = msgspec.yaml.decode(msg_path.read_bytes())
    # No ``origin`` key got injected: the gate skipped this chat.
    assert "origin" not in raw_after["messages"][0]


def test_migration_does_not_bump_updated_at(tmp_storage):
    """The migration uses ``atomic_write_yaml`` directly (no ``save_chat``),
    so ``chat.updated_at`` should NOT change. Detection: snapshot chat.yaml's
    ``updated_at`` before, run migration, check after."""
    from fastapi.testclient import TestClient
    from server.main import app
    client = TestClient(app)
    r = client.post("/api/contacts", json={"id": "", "name": "C"})
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
    })
    chat_id = r.json()["id"]
    chat_path = storage.chat_dir(chat_id)
    msg_path = chat_path / "messages.yaml"
    chat_yaml = chat_path / "chat.yaml"
    _write_legacy_messages_yaml(msg_path, [
        {
            "id": "abc",
            "parent_id": None,
            "sender": "user",
            "sender_name": "U",
            "body": [{"text": "hi", "emotion": "neutral"}],
        },
    ])

    chat_before = msgspec.yaml.decode(chat_yaml.read_bytes())
    storage._migrate_legacy_message_origin()
    chat_after = msgspec.yaml.decode(chat_yaml.read_bytes())
    assert chat_before["updated_at"] == chat_after["updated_at"]

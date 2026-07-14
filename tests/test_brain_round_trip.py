"""Round-trip tests for brain export/import and the brain-id migration."""
from __future__ import annotations

import yaml
from pathlib import Path

import pytest

from server import storage
from server.exporters import _brains_to_aer
from server.importers import _import_brains
from server.models import (
    Brain,
    BrainKey,
    CondAnd,
    CondKeyword,
    CondNumericCompare,
    CondOr,
    CondRandomChance,
    NumericValue,
)


def test_round_trip_simple_brain():
    brains = [Brain(id="b1", name="A", content="hello")]
    exported = _brains_to_aer(brains)
    assert exported == [{"id": "b1", "name": "A", "content": "hello"}]
    imported = _import_brains(exported)
    assert len(imported) == 1
    assert imported[0].id == "b1"
    assert imported[0].name == "A"
    assert imported[0].keys == []
    assert imported[0].advanced is None


def test_round_trip_complex_brain():
    brains = [Brain(
        id="b2", name="Complex", content="Complex content",
        keys=[
            BrainKey(pattern="banana", is_regex=False, case_sensitive=True, search_range=200),
            BrainKey(pattern=r"(?i)apple", is_regex=True),
        ],
        cascades=True,
        blocks_recursion=False,
        advanced=CondAnd(children=[
            CondNumericCompare(
                lhs=NumericValue(kind="variable", variable="message_count"),
                op=">", rhs=NumericValue(kind="literal", literal=3),
            ),
            CondOr(children=[
                CondKeyword(keys=[BrainKey(pattern="trigger")]),
                CondRandomChance(percent=25.0),
            ]),
        ]),
    )]
    exported = _brains_to_aer(brains)
    imported = _import_brains(exported)
    re_exported = _brains_to_aer(imported)
    assert re_exported == exported, "Round-trip should be exact"


def test_import_skips_brain_without_name_or_content():
    raw = [
        {"name": "Good", "content": "yes"},
        {"name": "", "content": "missing name"},
        {"name": "No content", "content": ""},
        {"foo": "bar"},  # not a dict at all from the validator's POV
    ]
    out = _import_brains(raw)
    assert [b.name for b in out] == ["Good"]


def test_import_assigns_new_id_when_missing():
    raw = [{"name": "A", "content": "x"}]
    [b] = _import_brains(raw)
    assert b.id and len(b.id) == 32  # default_factory new_id format


def test_migration_backfills_brain_ids_for_contact(tmp_storage):
    # Write a contact with brains that lack ids — using atomic_write to bypass
    # save_contact (which would already fill in ids via Pydantic).
    contact_dir = storage.CONTACTS_DIR / "alice-deadbeef"
    contact_dir.mkdir(parents=True)
    info_path = contact_dir / "info.yaml"
    info_path.write_text(yaml.safe_dump({
        "id": "deadbeefdeadbeefdeadbeefdeadbeef",
        "version_id": "cafef00d" * 4,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "name": "Alice",
        "brains": [
            {"name": "Brain1", "content": "X"},
            {"name": "Brain2", "content": "Y"},
        ],
    }))
    storage.index.contacts = {}
    storage.initialize()

    after = yaml.safe_load(info_path.read_text())
    ids = [b["id"] for b in after["brains"]]
    assert all(len(i) == 32 for i in ids)
    assert ids[0] != ids[1]
    # updated_at preserved — migration uses atomic_write, not save_contact.
    assert after["updated_at"] == 1700000000.0


def test_migration_backfills_brain_ids_for_messages(tmp_storage):
    chat_dir = storage.CHATS_DIR / "chat-1"
    chat_dir.mkdir(parents=True)
    (chat_dir / "chat.yaml").write_text(yaml.safe_dump({
        "id": "chatchatchatchatchatchatchatchat",
        "version_id": "cafef00d" * 4,
        "contact_id": "x" * 32,
        "user_id": "y" * 32,
    }))
    msg_path = chat_dir / "messages.yaml"
    msg_path.write_text(yaml.safe_dump({
        "messages": [
            {
                "id": "mid1", "parent_id": None, "sender": "user",
                "sender_name": "Bob",
                "body": [{"text": "hi", "emotion": "neutral"}],
                "brains": [{"name": "MB", "content": "M-content"}],
            },
        ],
    }))
    storage.index.chats = {}
    storage.initialize()

    after = yaml.safe_load(msg_path.read_text())
    bids = [b["id"] for b in after["messages"][0]["brains"]]
    assert all(len(i) == 32 for i in bids)

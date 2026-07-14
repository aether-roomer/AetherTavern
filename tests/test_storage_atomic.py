"""Storage atomicity / safety: fsync, controlled-delete, collision refusal.

Every test here uses ``tmp_storage`` from conftest so writes hit a fresh
temp dir and ``storage.index`` is reset between tests.
"""
from __future__ import annotations

import os

import pytest

from server import storage
from server.models import Chat, ChatBookmarks, ChatMessages, Contact, User


# ---------------------------------------------------------------------------
# atomic_write_bytes: fsync makes the most recent write durable.
# ---------------------------------------------------------------------------


def test_atomic_write_bytes_writes_data(tmp_storage):
    """Sanity: the fsync path writes the bytes correctly."""
    target = tmp_storage / "scratch.bin"
    storage.atomic_write_bytes(target, b"hello world")
    assert target.read_bytes() == b"hello world"


def test_atomic_write_bytes_overwrites_existing(tmp_storage):
    target = tmp_storage / "scratch.bin"
    storage.atomic_write_bytes(target, b"first")
    storage.atomic_write_bytes(target, b"second")
    assert target.read_bytes() == b"second"


def test_atomic_write_bytes_cleans_up_tmp_on_success(tmp_storage):
    """The temp file should be ``os.replace``-d onto the target — no .tmp
    leftover after a successful write."""
    target = tmp_storage / "scratch.bin"
    storage.atomic_write_bytes(target, b"x")
    leftover = list(tmp_storage.glob("scratch.bin.tmp"))
    assert leftover == []


# ---------------------------------------------------------------------------
# Controlled-delete helpers: known files removed, user files survive.
# ---------------------------------------------------------------------------


def test_remove_known_files_unlinks_existing(tmp_storage):
    a = tmp_storage / "a.txt"
    b = tmp_storage / "b.txt"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    storage.remove_known_files(a, b)
    assert not a.exists()
    assert not b.exists()


def test_remove_known_files_ignores_missing(tmp_storage):
    """Best-effort: missing paths shouldn't raise."""
    storage.remove_known_files(tmp_storage / "nope.txt")  # no exception


def test_remove_dir_if_empty_succeeds_when_empty(tmp_storage):
    d = tmp_storage / "empty"
    d.mkdir()
    storage.remove_dir_if_empty(d)
    assert not d.exists()


def test_remove_dir_if_empty_leaves_dir_with_user_files(tmp_storage):
    """The whole point of the user's request: stray files in an entity dir
    must survive the controlled-delete. ``rmdir`` fails with ENOTEMPTY and
    we silently leave the directory for the user to inspect."""
    d = tmp_storage / "user-stuff"
    d.mkdir()
    (d / "user-notes.txt").write_text("important!")
    storage.remove_dir_if_empty(d)
    assert d.exists()
    assert (d / "user-notes.txt").read_text() == "important!"


# ---------------------------------------------------------------------------
# Per-kind dir removers: marker file gone, user file survives, dir survives.
# ---------------------------------------------------------------------------


def test_remove_contact_dir_preserves_user_files(tmp_storage):
    contact = storage.save_contact(Contact(name="Alice"))
    cdir = storage.contact_dir(contact.id)
    assert cdir is not None
    # User stashed a note in the contact directory.
    (cdir / "personal-notes.md").write_text("draft revisions")

    storage.remove_contact_dir(cdir)

    # info.yaml gone — entity is no longer indexable.
    assert not (cdir / "info.yaml").exists()
    # User file survives in the now-marker-less directory.
    assert (cdir / "personal-notes.md").read_text() == "draft revisions"
    # The dir itself survives because it isn't empty.
    assert cdir.exists()


def test_remove_chat_dir_preserves_user_files(tmp_storage):
    contact = storage.save_contact(Contact(name="Bob"))
    user = storage.save_user(User(name="Me"))
    chat = storage.save_chat(Chat(
        title="Test", contact_id=contact.id, user_id=user.id,
    ))
    cdir = storage.chat_dir(chat.id)
    storage.save_chat_messages(chat.id, ChatMessages())
    storage.save_chat_bookmarks(chat.id, ChatBookmarks())
    (cdir / "manual-export.json").write_text("[]")

    storage.remove_chat_dir(cdir)

    assert not (cdir / "chat.yaml").exists()
    assert not (cdir / "messages.yaml").exists()
    assert not (cdir / "bookmarks.yaml").exists()
    assert (cdir / "manual-export.json").read_text() == "[]"
    assert cdir.exists()  # ENOTEMPTY → leave dir behind


def test_delete_contact_drops_from_index_and_preserves_user_files(tmp_storage):
    """High-level delete API uses the controlled remover."""
    contact = storage.save_contact(Contact(name="Charlie"))
    cdir = storage.contact_dir(contact.id)
    (cdir / "stray.txt").write_text("keep me")

    assert storage.delete_contact(contact.id) is True
    assert storage.get_contact(contact.id) is None
    assert contact.id not in storage.index.contacts
    assert (cdir / "stray.txt").exists()


# ---------------------------------------------------------------------------
# bump_version flag: the entity PUT path should reroll, sibling routes shouldn't.
# ---------------------------------------------------------------------------


def test_save_contact_bumps_version_id_by_default(tmp_storage):
    contact = storage.save_contact(Contact(name="Dana"))
    first_vid = contact.version_id
    storage.save_contact(contact)
    assert contact.version_id != first_vid


def test_save_contact_keeps_version_id_when_bump_false(tmp_storage):
    contact = storage.save_contact(Contact(name="Edith"))
    first_vid = contact.version_id
    storage.save_contact(contact, bump_version=False)
    assert contact.version_id == first_vid


def test_save_chat_bump_version_false_preserves_id(tmp_storage):
    contact = storage.save_contact(Contact(name="F"))
    user = storage.save_user(User(name="Me"))
    chat = storage.save_chat(Chat(title="T", contact_id=contact.id, user_id=user.id))
    first_vid = chat.version_id
    storage.save_chat(chat, bump_version=False)
    assert chat.version_id == first_vid


# ---------------------------------------------------------------------------
# _save_entity rename collision: refuse to clobber a real different entity.
# ---------------------------------------------------------------------------


def test_save_entity_refuses_to_clobber_different_entity_at_target_dir(tmp_storage):
    """Two entities collide on the {slug}-{id8} destination during a rename.

    The collision-handler in storage._save_entity must refuse to overwrite
    the existing real entity (raises RuntimeError) rather than rmtree-ing
    it. Catastrophic data loss prevention.
    """
    # First create the "victim" — name "rivals" maps to slug "rivals".
    victim = storage.save_contact(Contact(name="rivals"))

    # Now create another contact whose initial slug differs, then rename
    # it to "rivals" — the destination dir is occupied by ``victim``.
    perp = storage.save_contact(Contact(name="other"))
    perp.name = "rivals"

    # The slug-collision destination would only hit if id[:8] also matches.
    # Force the issue by giving ``perp`` a UUID that shares its first 8
    # chars with ``victim``. Easier: just check the helper directly.
    desired = storage.CONTACTS_DIR / storage.entity_dir_name("rivals", perp.id)
    if not desired.exists():
        # Manufacture the collision by manually creating a dir at perp's
        # would-be destination that contains victim's info.yaml. This
        # simulates the rare {slug}-{id8} collision.
        desired.mkdir()
        (desired / "info.yaml").write_text(
            (storage.contact_dir(victim.id) / "info.yaml").read_text()
        )
    with pytest.raises(RuntimeError, match="Refusing to overwrite"):
        storage._save_entity(
            perp, storage.index.contacts, storage.index.contact_paths,
            storage.CONTACTS_DIR, "info.yaml", Contact, "contact",
        )


def test_save_entity_collision_with_stale_dir_proceeds(tmp_storage):
    """A stale destination dir (no parseable info.yaml) is fine to remove
    via controlled-delete. The save proceeds without error."""
    contact = storage.save_contact(Contact(name="grace"))

    # Plant a stale empty dir at the would-be destination of a rename.
    new_label = "grace-renamed"
    stale = storage.CONTACTS_DIR / storage.entity_dir_name(new_label, contact.id)
    stale.mkdir()
    # Empty dir — no marker file. Should be removed by controlled-delete.

    contact.name = new_label
    storage.save_contact(contact)

    # The save should have moved the original dir to the new slug's location.
    new_dir = storage.contact_dir(contact.id)
    assert new_dir is not None
    assert new_dir.name.startswith("grace-renamed-")
    assert (new_dir / "info.yaml").exists()


# ---------------------------------------------------------------------------
# save_chat_with_all: writes in safe order so dangling refs can't appear.
# ---------------------------------------------------------------------------


def test_save_chat_with_all_writes_all_three_files(tmp_storage):
    contact = storage.save_contact(Contact(name="H"))
    user = storage.save_user(User(name="Me"))
    chat = Chat(title="T", contact_id=contact.id, user_id=user.id)
    storage.save_chat_with_all(chat, ChatMessages(), ChatBookmarks())
    cdir = storage.chat_dir(chat.id)
    assert (cdir / "chat.yaml").exists()
    assert (cdir / "messages.yaml").exists()
    assert (cdir / "bookmarks.yaml").exists()


# ---------------------------------------------------------------------------
# .bak directory warnings on initialize.
# ---------------------------------------------------------------------------


def test_initialize_warns_about_leftover_bak_dirs(tmp_storage, caplog):
    import logging
    bak_dir = storage.CONTACTS_DIR / "abandoned-import.bak"
    bak_dir.mkdir()
    (bak_dir / "info.yaml").write_text("name: leftover\nid: x\n")
    with caplog.at_level(logging.WARNING, logger="aether.storage"):
        storage.initialize()
    assert any(".bak" in rec.message and "abandoned-import" in rec.message
               for rec in caplog.records)


# ---------------------------------------------------------------------------
# Migration: existing entities without version_id get one persisted.
# ---------------------------------------------------------------------------


def test_initialize_migrates_missing_version_ids(tmp_storage):
    """Contacts on disk without ``version_id`` get a stable id stamped by
    the boot migration, so subsequent loads return the same id (pydantic's
    default_factory would otherwise mint a fresh one on every read and
    break optimistic concurrency — every PUT would 409 because the
    server's "current id" kept changing).

    Creates a YAML file with no version_id, runs initialize, and verifies
    the file now has a version_id and that two consecutive loads return
    the same id.
    """
    # Plant a contact directory with a yaml that has no version_id.
    cdir = storage.CONTACTS_DIR / "legacy-12345678"
    cdir.mkdir(parents=True)
    yaml_text = (
        "id: 12345678abcdef0000000000000000ab\n"
        "name: Legacy\n"
        "created_at: 1700000000.0\n"
        "updated_at: 1700000000.0\n"
    )
    (cdir / "info.yaml").write_text(yaml_text)

    storage.initialize()

    # version_id is now persisted in the file.
    assert "version_id:" in (cdir / "info.yaml").read_text()

    # Two loads return the same version_id (no more drift on every read).
    a = storage.get_contact("12345678abcdef0000000000000000ab")
    b = storage.get_contact("12345678abcdef0000000000000000ab")
    assert a is not None
    assert a.version_id == b.version_id


def test_initialize_migration_preserves_updated_at(tmp_storage):
    """Migration must not bump updated_at — that would shift list-sort
    ordering for unchanged entities. ``atomic_write_yaml`` writes the
    model as-is; ``save_*`` is intentionally NOT used."""
    cdir = storage.CONTACTS_DIR / "unchanged-87654321"
    cdir.mkdir(parents=True)
    yaml_text = (
        "id: 87654321abcdef0000000000000000ab\n"
        "name: Stable\n"
        "created_at: 1700000000.0\n"
        "updated_at: 1700000123.45\n"
    )
    (cdir / "info.yaml").write_text(yaml_text)

    storage.initialize()

    c = storage.get_contact("87654321abcdef0000000000000000ab")
    assert c.updated_at == 1700000123.45


# ---------------------------------------------------------------------------
# check_version: stale version_id with identical payload is a no-op, not 409.
# ---------------------------------------------------------------------------


def test_check_version_returns_true_when_ids_match(tmp_storage):
    from server.conflicts import check_version
    contact = storage.save_contact(Contact(name="A"))
    same = contact.model_copy()
    assert check_version(same, contact) is True


def test_check_version_returns_false_when_payload_matches_despite_stale_id(
    tmp_storage,
):
    """Stale version_id is fine when the user-meaningful content is
    identical — no real conflict, nothing to save. The route returns
    the existing entity (with its fresh version_id) instead of 409'ing.
    Avoids spurious conflict modals after out-of-band server writes
    (e.g. avatar upload, .display.webp rebuild) that bump version_id
    without changing user-meaningful content."""
    from server.conflicts import check_version
    existing = storage.save_contact(Contact(name="A", description="hi"))
    incoming = existing.model_copy(update={"version_id": "stale"})
    # Same content, just a stale version_id from a draft loaded earlier.
    assert check_version(incoming, existing) is False


def test_check_version_raises_409_on_real_conflict(tmp_storage):
    from fastapi import HTTPException
    from server.conflicts import check_version
    existing = storage.save_contact(Contact(name="A", description="original"))
    # Different name → real conflict.
    incoming = existing.model_copy(update={
        "version_id": "stale", "name": "B",
    })
    with pytest.raises(HTTPException) as exc:
        check_version(incoming, existing)
    assert exc.value.status_code == 409
    assert exc.value.detail["current_version_id"] == existing.version_id


def test_update_contact_no_op_when_payload_unchanged(tmp_storage):
    """Integration: PUT /contacts with the SAME payload but a stale
    version_id returns 200 + the live entity, not 409."""
    from fastapi.testclient import TestClient
    from server.main import app

    with TestClient(app) as client:
        c = client.post("/api/contacts", json={"name": "Alice"}).json()
        # Simulate an out-of-band write that bumped version_id.
        contact = storage.get_contact(c["id"])
        storage.save_contact(contact)  # rerolls version_id
        live_vid = storage.get_contact(c["id"]).version_id
        assert live_vid != c["version_id"]

        # Client PUTs the original (stale) payload — no actual content change.
        r = client.put(f"/api/contacts/{c['id']}", json=c)
        assert r.status_code == 200, r.text
        # Response carries the live version_id so the client's draft
        # picks up the fresh id naturally.
        assert r.json()["version_id"] == live_vid

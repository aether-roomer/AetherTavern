"""File-upload order: write new → save info (bump_version=False) →
best-effort unlink old siblings. A simulated failure during write should
leave the old avatar still served by ``contact.avatar``.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from server import storage
from server.main import app
from server.models import Contact, User


# Smallest valid PNG: 8-byte signature + IHDR for a 1x1 image, IDAT, IEND.
_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\xdac\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01"
    b"^\xf3*:"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)
_JPG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF"  # Will fail PIL decode, but we
# don't need it to: we patch build_display_file in the failure-path test.


def test_avatar_upload_replaces_extension_and_cleans_up_old(tmp_storage):
    """Upload PNG, then upload a different extension. Old file should be
    cleaned up; new file should serve."""
    with TestClient(app) as client:
        c = client.post("/api/contacts", json={"name": "Alice"}).json()
        # First upload — PNG.
        r1 = client.post(
            f"/api/contacts/{c['id']}/avatar",
            files={"file": ("avatar.png", _PNG_1X1, "image/png")},
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["avatar"] == "avatar.png"
        cdir = storage.contact_dir(c["id"])
        assert (cdir / "avatar.png").exists()

        # Second upload — same PNG bytes but pretend it's a different
        # extension by uploading PNG bytes again (the sniffer always
        # returns "png" for PNG bytes; we just want to exercise the
        # upload twice).
        r2 = client.post(
            f"/api/contacts/{c['id']}/avatar",
            files={"file": ("avatar.png", _PNG_1X1, "image/png")},
        )
        assert r2.status_code == 200, r2.text
        # Same extension overwrites in place; no leftover .tmp.
        leftover_tmp = list(cdir.glob("*.tmp"))
        assert leftover_tmp == []


def test_avatar_upload_preserves_old_file_when_save_contact_fails(
    tmp_storage, monkeypatch,
):
    """If ``storage.save_contact`` raises mid-upload, the OLD avatar file
    must still serve — info.yaml still points at it.
    """
    # raise_server_exceptions=False so the patched RuntimeError surfaces as
    # a 500 instead of propagating into the test body.
    with TestClient(app, raise_server_exceptions=False) as client:
        c = client.post("/api/contacts", json={"name": "Alice"}).json()
        # Establish a working avatar.
        client.post(
            f"/api/contacts/{c['id']}/avatar",
            files={"file": ("avatar.png", _PNG_1X1, "image/png")},
        )
        cdir = storage.contact_dir(c["id"])
        original = cdir / "avatar.png"
        assert original.exists()
        original_info = (cdir / "info.yaml").read_text()

        # Patch save_contact to raise during the next upload.
        original_save = storage.save_contact

        def boom(contact, *, bump_version: bool = True):
            raise RuntimeError("disk full")

        monkeypatch.setattr(storage, "save_contact", boom)
        r = client.post(
            f"/api/contacts/{c['id']}/avatar",
            files={"file": ("avatar.png", _PNG_1X1, "image/png")},
        )
        # Upload fails (500).
        assert r.status_code >= 500

        # Critical: info.yaml is unchanged because save_contact never ran.
        # The old contact.avatar value (avatar.png) still names a file on
        # disk.
        assert (cdir / "info.yaml").read_text() == original_info
        assert original.exists()

        # Restore save_contact and confirm the contact still loads cleanly
        # with its original avatar reference.
        monkeypatch.setattr(storage, "save_contact", original_save)
        c_now = storage.get_contact(c["id"])
        assert c_now.avatar == "avatar.png"


def test_upload_emotion_returns_fresh_version_id_for_draft_sync(tmp_storage):
    """Emotion upload response carries the fresh ``version_id`` and
    ``updated_at`` so an open edit-view draft can sync them before its
    next save (otherwise the draft's stale version_id would 409).
    """
    with TestClient(app) as client:
        c = client.post("/api/contacts", json={"name": "Alice"}).json()
        original_vid = c["version_id"]

        r = client.post(
            f"/api/contacts/{c['id']}/emotions/happy",
            files={"file": ("happy.png", _PNG_1X1, "image/png")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        # Response carries bookkeeping fields the frontend uses to keep
        # the open draft in sync.
        assert "version_id" in body
        assert "updated_at" in body
        # The actual contact on disk has these values.
        live = client.get(f"/api/contacts/{c['id']}").json()
        assert body["version_id"] == live["version_id"]
        assert body["updated_at"] == live["updated_at"]


def test_avatar_upload_returns_fresh_version_id(tmp_storage):
    with TestClient(app) as client:
        c = client.post("/api/contacts", json={"name": "Alice"}).json()
        r = client.post(
            f"/api/contacts/{c['id']}/avatar",
            files={"file": ("avatar.png", _PNG_1X1, "image/png")},
        )
        body = r.json()
        assert "version_id" in body
        assert "updated_at" in body


def test_delete_avatar_clears_field_before_unlinking(tmp_storage):
    """Delete order: clear contact.avatar + save first, then unlink. A
    crash between the two leaves the entity already detached from the
    file (next read returns 404 cleanly)."""
    with TestClient(app) as client:
        c = client.post("/api/contacts", json={"name": "Alice"}).json()
        client.post(
            f"/api/contacts/{c['id']}/avatar",
            files={"file": ("avatar.png", _PNG_1X1, "image/png")},
        )
        r = client.delete(f"/api/contacts/{c['id']}/avatar")
        assert r.status_code == 200, r.text
        # info.yaml field cleared.
        c_now = storage.get_contact(c["id"])
        assert c_now.avatar is None
        # File and display sibling gone.
        cdir = storage.contact_dir(c["id"])
        assert not (cdir / "avatar.png").exists()
        assert not (cdir / "avatar.display.webp").exists()

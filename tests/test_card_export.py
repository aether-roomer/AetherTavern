"""Card-export route: PNG bytes with embedded aertavern_data + JSON
round-trip back through the import path."""
from __future__ import annotations

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server import external_formats as ef, storage
from server.main import app
from server.models import BrainLibrary, Contact, ReminderBrain, Scenario, User


def _png_bytes(size=(8, 8), color=(120, 30, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, "PNG")
    return buf.getvalue()


def _jpg_bytes(size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(50, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_storage):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers: build an entity with a card image already set on disk.
# ---------------------------------------------------------------------------


def _make_with_card(kind: str, blob: bytes, ext: str = "png") -> str:
    if kind == "contact":
        c = Contact(name="Alice", card_image=f"card.{ext}")
        storage.save_contact(c)
        cdir = storage.contact_dir(c.id)
        storage.atomic_write_bytes(cdir / f"card.{ext}", blob)
        return c.id
    if kind == "user":
        u = User(name="Bob", card_image=f"card.{ext}")
        storage.save_user(u)
        udir = storage.user_dir(u.id)
        storage.atomic_write_bytes(udir / f"card.{ext}", blob)
        return u.id
    if kind == "scenario":
        s = Scenario(name="Lobby", card_image=f"card.{ext}")
        storage.save_scenario(s)
        sdir = storage.scenario_dir(s.id)
        storage.atomic_write_bytes(sdir / f"card.{ext}", blob)
        return s.id
    if kind == "library":
        lib = BrainLibrary(name="Lore", card_image=f"card.{ext}")
        storage.save_brain_library(lib)
        ldir = storage.brain_library_dir(lib.id)
        storage.atomic_write_bytes(ldir / f"card.{ext}", blob)
        return lib.id
    raise AssertionError(kind)


def _export_url(kind: str, eid: str) -> str:
    return {
        "contact":  f"/api/export/contact/{eid}/card",
        "user":     f"/api/export/user/{eid}/card",
        "scenario": f"/api/export/scenario/{eid}/card",
        "library":  f"/api/export/library/{eid}/card",
    }[kind]


ALL_KINDS = ["contact", "user", "scenario", "library"]


@pytest.mark.parametrize("kind", ALL_KINDS)
class TestCardExport:
    def test_export_returns_png_with_embedded_json(self, client, kind):
        eid = _make_with_card(kind, _png_bytes())
        r = client.get(_export_url(kind, eid))
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

        chunks = ef.read_png_text_chunks(r.content)
        assert "aertavern_data" in chunks
        decoded = json.loads(
            base64.b64decode(chunks["aertavern_data"]).decode("utf-8")
        )
        # The embedded payload is the entity's full JSON export.
        assert "name" in decoded
        if kind == "library":
            assert decoded.get("kind") == "brain_library"

    def test_export_non_png_source_reencodes_as_png(self, client, kind):
        eid = _make_with_card(kind, _jpg_bytes(), ext="jpg")
        r = client.get(_export_url(kind, eid))
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_export_no_card_returns_404(self, client, kind):
        if kind == "contact":
            ent = Contact(name="NoCard"); storage.save_contact(ent)
        elif kind == "user":
            ent = User(name="NoCard"); storage.save_user(ent)
        elif kind == "scenario":
            ent = Scenario(name="NoCard"); storage.save_scenario(ent)
        else:
            ent = BrainLibrary(name="NoCard"); storage.save_brain_library(ent)
        r = client.get(_export_url(kind, ent.id))
        assert r.status_code == 404

    def test_filename_in_disposition(self, client, kind):
        eid = _make_with_card(kind, _png_bytes())
        r = client.get(_export_url(kind, eid))
        cd = r.headers.get("content-disposition", "")
        assert ".png" in cd
        assert "attachment" in cd


# ---------------------------------------------------------------------------
# Full round-trip: export contact card, re-import the PNG, fields preserved.
# ---------------------------------------------------------------------------


def test_export_card_round_trip_contact(client, tmp_storage):
    # Build a contact with a couple of brains, an emotion, a reminder.
    contact = Contact(
        name="RoundTrip",
        description="A test character.",
        persona="Quirky.",
        tags="test,roundtrip",
        card_image="card.png",
        reminder_brain=ReminderBrain(name="Notes", content="Stay terse.", depth=0),
    )
    storage.save_contact(contact)
    cdir = storage.contact_dir(contact.id)
    storage.atomic_write_bytes(cdir / "card.png", _png_bytes())

    # Export card.
    r = client.get(f"/api/export/contact/{contact.id}/card")
    assert r.status_code == 200
    exported_png = r.content

    # Re-import via /api/import (multipart PNG upload).
    r2 = client.post(
        "/api/import",
        files={"file": ("alice.png", exported_png, "image/png")},
        data={"mode": "copy"},  # avoid id collision with the original
    )
    assert r2.status_code == 200, r2.text

    # The contact list now has the original AND a copy.
    all_contacts = storage.list_contacts()
    assert len(all_contacts) == 2
    copies = [c for c in all_contacts if c.id != contact.id]
    assert len(copies) == 1
    copy = copies[0]
    assert copy.name == "RoundTrip"
    assert copy.description == "A test character."
    assert copy.persona == "Quirky."
    # Reminder roundtrip.
    assert copy.reminder_brain is not None
    assert copy.reminder_brain.name == "Notes"
    assert copy.reminder_brain.content == "Stay terse."
    # Card image preserved.
    assert copy.card_image is not None

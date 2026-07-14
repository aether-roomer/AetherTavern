"""Card-image slot routes: upload, display, delete on every entity kind."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server import storage
from server.main import app
from server.models import BrainLibrary, Contact, Scenario, User


def _png_bytes(size=(8, 8), color=(120, 30, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, "PNG")
    return buf.getvalue()


def _jpg_bytes(size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(50, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


def _webp_bytes(size=(8, 8)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 20, 30)).save(buf, "WEBP")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Parametrised across the four entity kinds.
# ---------------------------------------------------------------------------


def _make_entity(kind: str) -> str:
    if kind == "contact":
        c = Contact(name="Alice")
        storage.save_contact(c)
        return c.id
    if kind == "user":
        u = User(name="Bob")
        storage.save_user(u)
        return u.id
    if kind == "scenario":
        s = Scenario(name="Lobby")
        storage.save_scenario(s)
        return s.id
    if kind == "library":
        lib = BrainLibrary(name="Lore")
        storage.save_brain_library(lib)
        return lib.id
    raise AssertionError(kind)


def _get_entity(kind: str, eid: str):
    return {
        "contact":  storage.get_contact,
        "user":     storage.get_user,
        "scenario": storage.get_scenario,
        "library":  storage.get_brain_library,
    }[kind](eid)


def _entity_dir(kind: str, eid: str):
    return {
        "contact":  storage.contact_dir,
        "user":     storage.user_dir,
        "scenario": storage.scenario_dir,
        "library":  storage.brain_library_dir,
    }[kind](eid)


def _url_root(kind: str) -> str:
    return {
        "contact":  "/api/contacts",
        "user":     "/api/users",
        "scenario": "/api/scenarios",
        "library":  "/api/libraries",
    }[kind]


def _files_root(kind: str) -> str:
    return {
        "contact":  "/api/files/contacts",
        "user":     "/api/files/users",
        "scenario": "/api/files/scenarios",
        "library":  "/api/files/libraries",
    }[kind]


ALL_KINDS = ["contact", "user", "scenario", "library"]


@pytest.fixture
def client(tmp_storage):
    return TestClient(app)


@pytest.mark.parametrize("kind", ALL_KINDS)
class TestCardImageUpload:
    def test_upload_png_then_get(self, client, kind):
        eid = _make_entity(kind)
        r = client.post(
            f"{_url_root(kind)}/{eid}/card-image",
            files={"file": ("card.png", _png_bytes(), "image/png")},
        )
        assert r.status_code == 200, r.text
        assert r.json()["card_image"] == "card.png"
        ent = _get_entity(kind, eid)
        assert ent.card_image == "card.png"

        r2 = client.get(f"{_files_root(kind)}/{eid}/card-image")
        assert r2.status_code == 200
        assert r2.headers["content-type"] == "image/png"
        assert r2.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_upload_jpg_preserves_format(self, client, kind):
        eid = _make_entity(kind)
        r = client.post(
            f"{_url_root(kind)}/{eid}/card-image",
            files={"file": ("card.jpg", _jpg_bytes(), "image/jpeg")},
        )
        assert r.status_code == 200
        assert r.json()["card_image"] == "card.jpg"
        ent = _get_entity(kind, eid)
        assert ent.card_image == "card.jpg"
        r2 = client.get(f"{_files_root(kind)}/{eid}/card-image")
        assert r2.headers["content-type"] == "image/jpeg"

    def test_upload_webp_preserves_format(self, client, kind):
        eid = _make_entity(kind)
        r = client.post(
            f"{_url_root(kind)}/{eid}/card-image",
            files={"file": ("card.webp", _webp_bytes(), "image/webp")},
        )
        assert r.status_code == 200
        assert r.json()["card_image"] == "card.webp"

    def test_upload_garbage_rejected(self, client, kind):
        eid = _make_entity(kind)
        r = client.post(
            f"{_url_root(kind)}/{eid}/card-image",
            files={"file": ("card.png", b"not an image at all", "image/png")},
        )
        assert r.status_code == 400

    def test_replace_with_different_extension_cleans_up_old(self, client, kind):
        eid = _make_entity(kind)
        client.post(
            f"{_url_root(kind)}/{eid}/card-image",
            files={"file": ("card.jpg", _jpg_bytes(), "image/jpeg")},
        )
        edir = _entity_dir(kind, eid)
        assert (edir / "card.jpg").exists()
        # Replace with PNG.
        client.post(
            f"{_url_root(kind)}/{eid}/card-image",
            files={"file": ("card.png", _png_bytes(), "image/png")},
        )
        assert (edir / "card.png").exists()
        # The old JPG should be gone; the new PNG remains.
        assert not (edir / "card.jpg").exists()

    def test_display_lazy_build(self, client, kind):
        eid = _make_entity(kind)
        client.post(
            f"{_url_root(kind)}/{eid}/card-image",
            files={"file": ("card.png", _png_bytes(size=(400, 600)), "image/png")},
        )
        edir = _entity_dir(kind, eid)
        display = edir / "card.display.webp"
        # Display sibling built during upload.
        assert display.exists()
        # The /display route serves it.
        r = client.get(f"{_files_root(kind)}/{eid}/card-image/display")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/webp"

    def test_delete_clears_field_and_files(self, client, kind):
        eid = _make_entity(kind)
        client.post(
            f"{_url_root(kind)}/{eid}/card-image",
            files={"file": ("card.png", _png_bytes(), "image/png")},
        )
        edir = _entity_dir(kind, eid)
        assert (edir / "card.png").exists()
        r = client.delete(f"{_url_root(kind)}/{eid}/card-image")
        assert r.status_code == 200
        assert r.json()["deleted"] == "card_image"
        ent = _get_entity(kind, eid)
        assert ent.card_image is None
        assert not (edir / "card.png").exists()
        assert not (edir / "card.display.webp").exists()

    def test_get_nonexistent_card(self, client, kind):
        eid = _make_entity(kind)
        r = client.get(f"{_files_root(kind)}/{eid}/card-image")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Card files do not survive an entity delete (but user files inside the
# entity dir do, per the controlled-removal pattern).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_entity_delete_removes_card_files(client, kind):
    eid = _make_entity(kind)
    client.post(
        f"{_url_root(kind)}/{eid}/card-image",
        files={"file": ("card.png", _png_bytes(), "image/png")},
    )
    edir = _entity_dir(kind, eid)
    assert (edir / "card.png").exists()
    assert (edir / "card.display.webp").exists()
    # Stash a user file that should survive the delete.
    user_file = edir / "user-notes.txt"
    user_file.write_bytes(b"keep me")

    r = client.delete(f"{_url_root(kind)}/{eid}")
    assert r.status_code == 200

    assert not (edir / "card.png").exists()
    assert not (edir / "card.display.webp").exists()
    # The user file survives — the dir wasn't rmtree'd.
    assert user_file.exists()

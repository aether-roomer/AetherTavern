"""Tests for the Duplicate action on contacts / users / scenarios /
brain libraries.

Covers the ``next_copy_name`` / ``copy_entity_files`` helpers, then the
4 router endpoints — name-suffix discipline, fresh id / version_id /
created_at, cleared AER bookkeeping, file copy fidelity, and 404 on
missing source.
"""
from __future__ import annotations

import time
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from server import storage
from server.duplication import copy_entity_files, next_copy_name
from server.main import app
from server.models import (
    Brain,
    BrainLibrary,
    Contact,
    ContactScenario,
    Scenario,
)


def _png_bytes(color=(200, 100, 50)) -> bytes:
    """Produce a small but PIL-decodable PNG for upload tests."""
    buf = BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, "PNG")
    return buf.getvalue()


_PNG_1X1 = _png_bytes()


# ---------------------------------------------------------------------------
# next_copy_name
# ---------------------------------------------------------------------------


def test_next_copy_name_base_case():
    assert next_copy_name("Foo") == "Foo (1)"


def test_next_copy_name_increments_existing_suffix():
    assert next_copy_name("Foo (1)") == "Foo (2)"
    assert next_copy_name("Foo (5)") == "Foo (6)"
    assert next_copy_name("Foo (42)") == "Foo (43)"


def test_next_copy_name_handles_extra_whitespace():
    assert next_copy_name("Foo  (1)  ") == "Foo (2)"


def test_next_copy_name_walks_until_unique():
    # If "Foo (1)" already exists, walk forward.
    assert next_copy_name("Foo", existing=["Foo (1)"]) == "Foo (2)"
    assert next_copy_name("Foo", existing=["Foo (1)", "Foo (2)"]) == "Foo (3)"
    # Increment-from-source path also walks forward when the bumped name
    # collides.
    assert next_copy_name("Foo (1)", existing=["Foo (2)"]) == "Foo (3)"


def test_next_copy_name_collision_check_is_case_insensitive():
    assert next_copy_name("Foo", existing=["foo (1)"]) == "Foo (2)"


def test_next_copy_name_empty_name():
    assert next_copy_name("") == "(1)"


# ---------------------------------------------------------------------------
# copy_entity_files
# ---------------------------------------------------------------------------


def test_copy_entity_files_recurses_and_skips_marker(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "info.yaml").write_text("id: srcid\n")
    (src / "avatar.png").write_bytes(_PNG_1X1)
    (src / "avatar.display.webp").write_bytes(b"webp-stub")
    em = src / "emotions"
    em.mkdir()
    (em / "happy.png").write_bytes(_PNG_1X1)
    (em / "happy.display.webp").write_bytes(b"webp-stub")

    copy_entity_files(src, dst)

    # Marker NOT copied — the new entity gets its own info.yaml from save_*.
    assert not (dst / "info.yaml").exists()
    # Avatar + display sibling copied.
    assert (dst / "avatar.png").read_bytes() == _PNG_1X1
    assert (dst / "avatar.display.webp").read_bytes() == b"webp-stub"
    # Emotions subdir recursed.
    assert (dst / "emotions" / "happy.png").read_bytes() == _PNG_1X1
    assert (dst / "emotions" / "happy.display.webp").read_bytes() == b"webp-stub"


def test_copy_entity_files_skips_tmp_files(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "info.yaml").write_text("id: srcid\n")
    (src / "avatar.png.tmp").write_bytes(b"half-written")
    (src / "avatar.png").write_bytes(_PNG_1X1)

    copy_entity_files(src, dst)

    assert (dst / "avatar.png").exists()
    # Crash leftover from an interrupted atomic_write_bytes — not copied.
    assert not (dst / "avatar.png.tmp").exists()


def test_copy_entity_files_missing_src_is_noop(tmp_path):
    src = tmp_path / "doesnotexist"
    dst = tmp_path / "dst"
    copy_entity_files(src, dst)
    assert not dst.exists()


# ---------------------------------------------------------------------------
# Contact duplicate
# ---------------------------------------------------------------------------


def test_duplicate_contact_basic(tmp_storage):
    """Name gets " (1)", id is fresh, original is unchanged."""
    with TestClient(app) as client:
        c = client.post("/api/contacts", json={
            "name": "Alice",
            "description": "She codes.",
            "persona": "Calm.",
        }).json()
        # Pause so created_at can be compared meaningfully.
        time.sleep(0.01)

        r = client.post(f"/api/contacts/{c['id']}/duplicate")
        assert r.status_code == 200, r.text
        copy = r.json()

        assert copy["id"] != c["id"]
        assert copy["name"] == "Alice (1)"
        assert copy["description"] == "She codes."
        assert copy["persona"] == "Calm."
        assert copy["version_id"] != c["version_id"]
        assert copy["created_at"] > c["created_at"]

        # Original is unchanged.
        original = client.get(f"/api/contacts/{c['id']}").json()
        assert original["name"] == "Alice"
        assert original["version_id"] == c["version_id"]


def test_duplicate_contact_increments_suffix(tmp_storage):
    with TestClient(app) as client:
        c = client.post("/api/contacts", json={"name": "Bob (3)"}).json()
        r = client.post(f"/api/contacts/{c['id']}/duplicate")
        assert r.json()["name"] == "Bob (4)"


def test_duplicate_contact_walks_past_existing_collision(tmp_storage):
    """Duplicating "Bob" twice produces "Bob (1)" then "Bob (2)"."""
    with TestClient(app) as client:
        c = client.post("/api/contacts", json={"name": "Bob"}).json()
        first = client.post(f"/api/contacts/{c['id']}/duplicate").json()
        assert first["name"] == "Bob (1)"
        # Duplicating the original again must bump past the existing
        # "Bob (1)" copy.
        second = client.post(f"/api/contacts/{c['id']}/duplicate").json()
        assert second["name"] == "Bob (2)"


def test_duplicate_contact_copies_avatar_and_emotion_sprites(tmp_storage):
    with TestClient(app) as client:
        c = client.post("/api/contacts", json={"name": "Alice"}).json()
        # Upload an avatar and an emotion sprite.
        client.post(
            f"/api/contacts/{c['id']}/avatar",
            files={"file": ("avatar.png", _PNG_1X1, "image/png")},
        )
        client.post(
            f"/api/contacts/{c['id']}/emotions/happy",
            files={"file": ("happy.png", _PNG_1X1, "image/png")},
        )

        copy = client.post(f"/api/contacts/{c['id']}/duplicate").json()
        # Field references survive — copy points at the same filenames.
        assert copy["avatar"] == "avatar.png"
        assert copy["emotions"] == {"happy": "happy.png"}

        # And the actual files made it into the new directory.
        new_dir = storage.contact_dir(copy["id"])
        assert (new_dir / "avatar.png").read_bytes() == _PNG_1X1
        assert (new_dir / "emotions" / "happy.png").read_bytes() == _PNG_1X1
        # Display siblings copied too (avoid rebuild on the copy).
        assert (new_dir / "avatar.display.webp").exists()
        assert (new_dir / "emotions" / "happy.display.webp").exists()


def test_duplicate_contact_clears_aer_bookkeeping(tmp_storage):
    """AER-import provenance fields are zeroed on the copy."""
    c = Contact(
        name="Imported",
        aer_source_id="src-abc",
        aer_revision="rev-1",
        aer_imported_at=12345.0,
        scenarios=[ContactScenario(
            name="Scene",
            aer_source_id="src-xyz",
            aer_revision="rev-2",
            aer_imported_at=12346.0,
        )],
    )
    storage.save_contact(c)
    with TestClient(app) as client:
        copy = client.post(f"/api/contacts/{c.id}/duplicate").json()
    assert copy["aer_source_id"] is None
    assert copy["aer_revision"] is None
    assert copy["aer_imported_at"] is None
    assert copy["scenarios"][0]["aer_source_id"] is None
    assert copy["scenarios"][0]["aer_revision"] is None
    assert copy["scenarios"][0]["aer_imported_at"] is None


def test_duplicate_contact_preserves_brains(tmp_storage):
    c = Contact(
        name="Lorekeeper",
        brains=[Brain(name="Castle", content="Old stones.")],
    )
    storage.save_contact(c)
    with TestClient(app) as client:
        copy = client.post(f"/api/contacts/{c.id}/duplicate").json()
    assert len(copy["brains"]) == 1
    assert copy["brains"][0]["name"] == "Castle"
    assert copy["brains"][0]["content"] == "Old stones."


def test_duplicate_contact_404(tmp_storage):
    with TestClient(app) as client:
        r = client.post("/api/contacts/does-not-exist/duplicate")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# User duplicate
# ---------------------------------------------------------------------------


def test_duplicate_user_basic(tmp_storage):
    with TestClient(app) as client:
        u = client.post("/api/users", json={"name": "Anon", "persona": "Quiet."}).json()
        r = client.post(f"/api/users/{u['id']}/duplicate")
        assert r.status_code == 200, r.text
        copy = r.json()
        assert copy["id"] != u["id"]
        assert copy["name"] == "Anon (1)"
        assert copy["persona"] == "Quiet."


def test_duplicate_user_copies_avatar(tmp_storage):
    with TestClient(app) as client:
        u = client.post("/api/users", json={"name": "Anon"}).json()
        client.post(
            f"/api/users/{u['id']}/avatar",
            files={"file": ("avatar.png", _PNG_1X1, "image/png")},
        )
        copy = client.post(f"/api/users/{u['id']}/duplicate").json()
        new_dir = storage.user_dir(copy["id"])
        assert (new_dir / "avatar.png").read_bytes() == _PNG_1X1


def test_duplicate_user_404(tmp_storage):
    with TestClient(app) as client:
        assert client.post("/api/users/does-not-exist/duplicate").status_code == 404


# ---------------------------------------------------------------------------
# Scenario duplicate
# ---------------------------------------------------------------------------


def test_duplicate_scenario_basic(tmp_storage):
    s = Scenario(name="Forest", environment="Pines.", scene="Sunrise.")
    storage.save_scenario(s)
    with TestClient(app) as client:
        copy = client.post(f"/api/scenarios/{s.id}/duplicate").json()
    assert copy["id"] != s.id
    assert copy["name"] == "Forest (1)"
    assert copy["environment"] == "Pines."
    assert copy["scene"] == "Sunrise."


def test_duplicate_scenario_copies_background(tmp_storage):
    s = Scenario(name="Tavern")
    storage.save_scenario(s)
    with TestClient(app) as client:
        client.post(
            f"/api/scenarios/{s.id}/background",
            files={"file": ("background.png", _PNG_1X1, "image/png")},
        )
        copy = client.post(f"/api/scenarios/{s.id}/duplicate").json()
    new_dir = storage.scenario_dir(copy["id"])
    assert (new_dir / "background.png").read_bytes() == _PNG_1X1


def test_duplicate_scenario_404(tmp_storage):
    with TestClient(app) as client:
        assert client.post("/api/scenarios/does-not-exist/duplicate").status_code == 404


# ---------------------------------------------------------------------------
# Brain library duplicate
# ---------------------------------------------------------------------------


def test_duplicate_brain_library_basic(tmp_storage):
    lib = BrainLibrary(
        name="Setting bible",
        description="Recurring lore",
        brains=[Brain(name="Magic", content="Mana flows.")],
    )
    storage.save_brain_library(lib)
    with TestClient(app) as client:
        copy = client.post(f"/api/libraries/{lib.id}/duplicate").json()
    assert copy["id"] != lib.id
    assert copy["name"] == "Setting bible (1)"
    assert copy["description"] == "Recurring lore"
    assert len(copy["brains"]) == 1
    assert copy["brains"][0]["name"] == "Magic"


def test_duplicate_brain_library_copies_avatar(tmp_storage):
    lib = BrainLibrary(name="Bestiary")
    storage.save_brain_library(lib)
    with TestClient(app) as client:
        client.post(
            f"/api/libraries/{lib.id}/avatar",
            files={"file": ("avatar.png", _PNG_1X1, "image/png")},
        )
        copy = client.post(f"/api/libraries/{lib.id}/duplicate").json()
    new_dir = storage.brain_library_dir(copy["id"])
    assert (new_dir / "avatar.png").read_bytes() == _PNG_1X1


def test_duplicate_brain_library_404(tmp_storage):
    with TestClient(app) as client:
        assert client.post("/api/libraries/does-not-exist/duplicate").status_code == 404

"""Brain libraries — comprehensive server-side coverage.

Spans:
- Storage CRUD + migrations (version_id, brain.id backfill).
- Avatar upload + display-sibling lazy build.
- Router endpoints (CRUD, 409 on stale version_id, 404 on missing entities).
- Chat integration: ``brain_library_ids`` round-trips through POST/PUT and
  is NOT scrubbed by DELETE library (missing-library marker pattern).
- AER prompt assembly: unconditional library brains appear AFTER the
  contact / user / scenario unconditionals, in attach order. Conditional
  library brains feed through the same activation engine. Reordering
  reorders the rendered text.
- Brain budget overflow attributes offending brains to their owning
  library.
- Single-JSON export / import round-trips bit-for-bit, including the
  avatar as a data URI. Chat composite export includes ``brainLibraries``
  sidecars and the importer rehydrates them.
"""
from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image

from server import storage
from server.aer.format import (
    build_system_prompt,
    collect_conditional_global_brains,
    collect_global_brains,
    collect_unconditional_global_brains,
)
from server.aer.rollover import BrainBudgetExceeded
from server.exporters import export_brain_library, export_chat
from server.importers import import_streaming
from server.main import app
from server.models import (
    Brain,
    BrainKey,
    BrainLibrary,
    Chat,
    ChatBookmarks,
    ChatMessages,
    Contact,
    Intimacy,
    Scenario,
    User,
)
from server.routers.generate import _build_brain_owner_index


# ---------------------------------------------------------------------------
# Storage CRUD
# ---------------------------------------------------------------------------


def test_storage_save_load_round_trip(tmp_storage):
    lib = BrainLibrary(
        name="Setting bible",
        description="Recurring lore",
        tags="fantasy, magic",
        brains=[Brain(name="Magic system", content="Mana flows…")],
    )
    storage.save_brain_library(lib)
    reloaded = storage.get_brain_library(lib.id)
    assert reloaded is not None
    assert reloaded.name == "Setting bible"
    assert reloaded.description == "Recurring lore"
    assert reloaded.tags == "fantasy, magic"
    assert len(reloaded.brains) == 1
    assert reloaded.brains[0].name == "Magic system"


def test_storage_directory_naming(tmp_storage):
    lib = BrainLibrary(name="Sci-fi rulebook")
    storage.save_brain_library(lib)
    ldir = storage.brain_library_dir(lib.id)
    assert ldir is not None
    # ``{slug}-{id[:8]}`` is the layout discipline shared across entities.
    assert ldir.name == f"sci-fi-rulebook-{lib.id[:8]}"


def test_storage_rename_renames_directory(tmp_storage):
    lib = BrainLibrary(name="Old name")
    storage.save_brain_library(lib)
    old_dir = storage.brain_library_dir(lib.id)
    assert old_dir is not None and old_dir.exists()

    lib.name = "New name"
    storage.save_brain_library(lib)
    new_dir = storage.brain_library_dir(lib.id)
    assert new_dir is not None and new_dir.exists()
    assert new_dir != old_dir
    assert not old_dir.exists()
    # Lookup by id still works.
    assert storage.get_brain_library(lib.id).name == "New name"


def test_storage_delete_preserves_user_stashed_files(tmp_storage):
    lib = BrainLibrary(name="Has extras")
    storage.save_brain_library(lib)
    ldir = storage.brain_library_dir(lib.id)
    assert ldir is not None
    stash = ldir / "notes.txt"
    stash.write_text("user-stashed notes", encoding="utf-8")

    assert storage.delete_brain_library(lib.id) is True
    # info.yaml is gone (so the indexer skips this dir on next boot)…
    assert not (ldir / "info.yaml").exists()
    # …but the stashed file survives.
    assert stash.exists()
    # And the lib is no longer indexed.
    assert storage.get_brain_library(lib.id) is None


def test_storage_save_preserves_created_at_bumps_updated_at(tmp_storage):
    import time
    lib = BrainLibrary(name="Time-test")
    original_created = lib.created_at
    storage.save_brain_library(lib)
    first_updated = lib.updated_at

    time.sleep(0.01)
    lib.description = "edited"
    storage.save_brain_library(lib)

    assert lib.created_at == original_created
    assert lib.updated_at > first_updated


def test_storage_save_default_rerolls_version_id(tmp_storage):
    lib = BrainLibrary(name="Versioned")
    storage.save_brain_library(lib)
    first_vid = lib.version_id

    storage.save_brain_library(lib)
    assert lib.version_id != first_vid


def test_storage_save_bump_version_false_preserves_version_id(tmp_storage):
    lib = BrainLibrary(name="Sibling-write")
    storage.save_brain_library(lib)
    pinned = lib.version_id

    storage.save_brain_library(lib, bump_version=False)
    assert lib.version_id == pinned


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------


def test_migration_backfills_missing_version_id(tmp_storage):
    """A library YAML without ``version_id`` (pre-migration era) gets a
    stable id on next boot, without bumping ``updated_at``."""
    ldir = storage.LIBRARIES_DIR / "legacy-deadbeef"
    ldir.mkdir(parents=True)
    info = ldir / "info.yaml"
    legacy_id = "deadbeefdeadbeefdeadbeefdeadbeef"
    info.write_text(yaml.safe_dump({
        "id": legacy_id,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "name": "Legacy",
        "description": "",
        "tags": "",
        "brains": [],
        "favorite": False,
    }), encoding="utf-8")
    # Reinitialize storage so the migration runs against this file.
    storage.index.brain_libraries = {}
    storage.initialize()

    reloaded = storage.get_brain_library(legacy_id)
    assert reloaded is not None
    assert reloaded.version_id  # populated
    assert reloaded.updated_at == 1700000000.0  # NOT bumped


def test_migration_backfills_missing_brain_ids(tmp_storage):
    """A library YAML whose brains lack ``id`` gets ids filled in on boot,
    without bumping ``updated_at``."""
    ldir = storage.LIBRARIES_DIR / "legacy-cafef00d"
    ldir.mkdir(parents=True)
    info = ldir / "info.yaml"
    lib_id = "cafef00dcafef00dcafef00dcafef00d"
    info.write_text(yaml.safe_dump({
        "id": lib_id,
        "version_id": "abc" * 11,
        "created_at": 1700000000.0,
        "updated_at": 1700000000.0,
        "name": "Legacy with brains",
        "brains": [
            {"name": "B1", "content": "content one"},   # no id
            {"name": "B2", "content": "content two"},   # no id
        ],
    }), encoding="utf-8")
    storage.index.brain_libraries = {}
    storage.initialize()

    reloaded = storage.get_brain_library(lib_id)
    assert reloaded is not None
    assert len(reloaded.brains) == 2
    assert all(b.id and len(b.id) == 32 for b in reloaded.brains)
    assert reloaded.updated_at == 1700000000.0  # NOT bumped


# ---------------------------------------------------------------------------
# Router endpoints (HTTP)
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_storage, monkeypatch):
    """TestClient with the FastAPI lifespan (storage init) — tokenizer
    skipped so we don't download GLM-4.6 every test run."""
    monkeypatch.setenv("AETHER_SKIP_TOKENIZER", "1")
    with TestClient(app) as c:
        yield c


def test_router_crud(client):
    # POST
    r = client.post("/api/libraries", json={"name": "From API"})
    assert r.status_code == 200
    lib = r.json()
    assert lib["id"]
    assert lib["name"] == "From API"

    # GET list
    r = client.get("/api/libraries")
    assert r.status_code == 200
    assert any(x["id"] == lib["id"] for x in r.json())

    # GET single
    r = client.get(f"/api/libraries/{lib['id']}")
    assert r.status_code == 200
    assert r.json()["name"] == "From API"

    # PUT
    lib["description"] = "edited via API"
    r = client.put(f"/api/libraries/{lib['id']}", json=lib)
    assert r.status_code == 200
    assert r.json()["description"] == "edited via API"

    # DELETE
    r = client.delete(f"/api/libraries/{lib['id']}")
    assert r.status_code == 200
    assert client.get(f"/api/libraries/{lib['id']}").status_code == 404


def test_router_put_stale_version_id_returns_409(client):
    r = client.post("/api/libraries", json={"name": "Conflict me"})
    lib = r.json()

    # Mutate behind the back so a stale-version PUT collides on real content.
    server_copy = storage.get_brain_library(lib["id"])
    server_copy.description = "remote change"
    storage.save_brain_library(server_copy)

    # Now PUT with the original (stale) version_id and a different payload.
    lib["description"] = "local change"
    r = client.put(f"/api/libraries/{lib['id']}", json=lib)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "version_conflict"
    assert detail["current_version_id"]


def test_router_delete_missing_returns_404(client):
    r = client.delete("/api/libraries/no-such-id-32characters00000000")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Avatar upload (HTTP)
# ---------------------------------------------------------------------------


def _png_bytes(size=(8, 8), color=(255, 0, 0, 255)) -> bytes:
    """Build a tiny in-memory PNG so the upload path has real magic bytes."""
    buf = BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_avatar_upload_and_display_sibling(client):
    lib = client.post("/api/libraries", json={"name": "Has avatar"}).json()
    png = _png_bytes()

    r = client.post(
        f"/api/libraries/{lib['id']}/avatar",
        files={"file": ("avatar.png", png, "application/octet-stream")},
    )
    assert r.status_code == 200
    body = r.json()
    # Sniffed from magic bytes — content-type header is ignored.
    assert body["avatar"] == "avatar.png"
    # Bookkeeping comes back so the client can refresh its draft.
    assert body["version_id"]
    assert body["updated_at"]

    # Display sibling is auto-built on upload.
    ldir = storage.brain_library_dir(lib["id"])
    assert ldir is not None
    assert (ldir / "avatar.png").exists()
    assert (ldir / "avatar.display.webp").exists()

    # Display endpoint serves WebP.
    r = client.get(f"/api/files/libraries/{lib['id']}/avatar/display")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/webp")


def test_avatar_display_lazy_rebuild(client):
    lib = client.post("/api/libraries", json={"name": "Rebuilder"}).json()
    png = _png_bytes()
    client.post(
        f"/api/libraries/{lib['id']}/avatar",
        files={"file": ("avatar.png", png, "application/octet-stream")},
    )
    ldir = storage.brain_library_dir(lib["id"])
    display_path = ldir / "avatar.display.webp"
    assert display_path.exists()
    display_path.unlink()
    assert not display_path.exists()

    r = client.get(f"/api/files/libraries/{lib['id']}/avatar/display")
    assert r.status_code == 200
    assert display_path.exists()


def test_avatar_upload_cleans_up_old_extension_sibling(client):
    lib = client.post("/api/libraries", json={"name": "Re-upload"}).json()
    # First upload PNG.
    client.post(
        f"/api/libraries/{lib['id']}/avatar",
        files={"file": ("a.png", _png_bytes(), "application/octet-stream")},
    )
    ldir = storage.brain_library_dir(lib["id"])
    assert (ldir / "avatar.png").exists()

    # Second upload as JPEG.
    buf = BytesIO()
    Image.new("RGB", (8, 8), (0, 255, 0)).save(buf, "JPEG")
    client.post(
        f"/api/libraries/{lib['id']}/avatar",
        files={"file": ("a.jpg", buf.getvalue(), "application/octet-stream")},
    )
    # New file exists, old PNG was unlinked (after the new save committed).
    assert (ldir / "avatar.jpg").exists()
    assert not (ldir / "avatar.png").exists()


# ---------------------------------------------------------------------------
# Chat integration
# ---------------------------------------------------------------------------


def test_create_chat_carries_brain_library_ids(client):
    contact = client.post("/api/contacts", json={"name": "Aria"}).json()
    user = client.post("/api/users", json={"name": "Me"}).json()
    lib_a = client.post("/api/libraries", json={"name": "Lib A"}).json()
    lib_b = client.post("/api/libraries", json={"name": "Lib B"}).json()

    r = client.post("/api/chats", json={
        "contact_id": contact["id"],
        "user_id": user["id"],
        "brain_library_ids": [lib_a["id"], lib_b["id"]],
    })
    assert r.status_code == 200
    chat = r.json()
    assert chat["brain_library_ids"] == [lib_a["id"], lib_b["id"]]


def test_create_chat_dedups_library_ids(client):
    contact = client.post("/api/contacts", json={"name": "Aria"}).json()
    user = client.post("/api/users", json={"name": "Me"}).json()
    lib = client.post("/api/libraries", json={"name": "Dup"}).json()

    r = client.post("/api/chats", json={
        "contact_id": contact["id"],
        "user_id": user["id"],
        "brain_library_ids": [lib["id"], lib["id"], lib["id"]],
    })
    chat = r.json()
    assert chat["brain_library_ids"] == [lib["id"]]


def test_put_chat_round_trips_brain_library_ids(client):
    contact = client.post("/api/contacts", json={"name": "Aria"}).json()
    user = client.post("/api/users", json={"name": "Me"}).json()
    lib_a = client.post("/api/libraries", json={"name": "Lib A"}).json()
    lib_b = client.post("/api/libraries", json={"name": "Lib B"}).json()

    chat = client.post("/api/chats", json={
        "contact_id": contact["id"],
        "user_id": user["id"],
        "brain_library_ids": [lib_a["id"]],
    }).json()

    chat["brain_library_ids"] = [lib_b["id"], lib_a["id"]]
    r = client.put(f"/api/chats/{chat['id']}", json=chat)
    assert r.status_code == 200
    assert r.json()["brain_library_ids"] == [lib_b["id"], lib_a["id"]]


def test_delete_library_does_not_strip_chat_references(client):
    """The plan: DELETE leaves dangling references so a future re-import
    of the library rehydrates the attachment automatically. The chat UI
    surfaces the missing entry as a "missing library" marker."""
    contact = client.post("/api/contacts", json={"name": "Aria"}).json()
    user = client.post("/api/users", json={"name": "Me"}).json()
    lib = client.post("/api/libraries", json={"name": "Will be deleted"}).json()
    chat = client.post("/api/chats", json={
        "contact_id": contact["id"],
        "user_id": user["id"],
        "brain_library_ids": [lib["id"]],
    }).json()

    assert client.delete(f"/api/libraries/{lib['id']}").status_code == 200

    refreshed = client.get(f"/api/chats/{chat['id']}").json()
    assert refreshed["brain_library_ids"] == [lib["id"]]  # NOT scrubbed


# ---------------------------------------------------------------------------
# AER prompt assembly
# ---------------------------------------------------------------------------


def _setup_alice_bob():
    contact = Contact(
        id="char1", name="Alice",
        persona="Cheerful.",
        brains=[Brain(name="ContactGlobal", content="Always-on contact lore.")],
    )
    user = User(
        id="user1", name="Bob",
        persona="Laid-back.",
        brains=[Brain(name="UserGlobal", content="Always-on user note.")],
    )
    scenario = Scenario(
        id="scen1", name="Apartment",
        environment="Alice's place.",
        brains=[Brain(name="ScenarioGlobal", content="Always-on scenario fact.")],
    )
    return contact, user, scenario


def test_collect_unconditional_appends_library_brains_after_scenario():
    contact, user, scenario = _setup_alice_bob()
    libs = [
        BrainLibrary(name="L1", brains=[
            Brain(name="LibA1", content="library A brain 1"),
            Brain(name="LibA2", content="library A brain 2"),
        ]),
        BrainLibrary(name="L2", brains=[
            Brain(name="LibB1", content="library B brain 1"),
        ]),
    ]
    out = collect_unconditional_global_brains(contact, user, scenario, libs)
    names = [b.name for b in out]
    # contact → user → scenario → libs (in attach order, within library too).
    assert names == [
        "ContactGlobal", "UserGlobal", "ScenarioGlobal",
        "LibA1", "LibA2", "LibB1",
    ]


def test_collect_conditional_routes_through_libraries():
    contact, user, scenario = _setup_alice_bob()
    libs = [
        BrainLibrary(name="L1", brains=[
            Brain(
                name="LibConditional",
                content="conditional library brain",
                keys=[BrainKey(pattern="trigger")],
            ),
        ]),
    ]
    cond = collect_conditional_global_brains(contact, user, scenario, libs)
    assert [b.name for b in cond] == ["LibConditional"]
    # The unconditional view should NOT include it.
    uncond = collect_unconditional_global_brains(contact, user, scenario, libs)
    assert "LibConditional" not in [b.name for b in uncond]


def test_system_prompt_renders_library_brains_after_scenario():
    contact, user, scenario = _setup_alice_bob()
    libs = [
        BrainLibrary(name="L1", brains=[
            Brain(name="LibA1", content="library A entry 1"),
        ]),
    ]
    out = build_system_prompt(contact, user, scenario, Intimacy.CLOSE, libraries=libs)
    # The block order is contact / user / scenario sections, then ALL
    # unconditional brains in order. ``find`` returns the byte offset;
    # later block must come AFTER earlier block.
    pos_contact = out.find("ContactGlobal")
    pos_user = out.find("UserGlobal")
    pos_scen = out.find("ScenarioGlobal")
    pos_lib = out.find("LibA1")
    assert -1 < pos_contact < pos_user < pos_scen < pos_lib


def test_reorder_libraries_reorders_rendered_brains():
    contact, user, scenario = _setup_alice_bob()
    a = BrainLibrary(name="A", brains=[Brain(name="LibA", content="a")])
    b = BrainLibrary(name="B", brains=[Brain(name="LibB", content="b")])

    out_ab = build_system_prompt(contact, user, scenario, Intimacy.CLOSE, libraries=[a, b])
    out_ba = build_system_prompt(contact, user, scenario, Intimacy.CLOSE, libraries=[b, a])

    assert out_ab.find("LibA") < out_ab.find("LibB")
    assert out_ba.find("LibB") < out_ba.find("LibA")


# ---------------------------------------------------------------------------
# Brain budget + owner attribution
# ---------------------------------------------------------------------------


def test_brain_owner_index_attributes_library_brains():
    """When a library's brain is identified as an offender, the owner-index
    helper used by ``_attribute_brain_offenders`` (see routers/generate.py)
    tags it with ``kind="brain_library"`` + the library id so the frontend
    toast can deep-link to the library's edit page."""
    contact, user, scenario = _setup_alice_bob()
    lib = BrainLibrary(name="Lib", brains=[
        Brain(id="lib-brain-id-1234567890abcdef12345678ab", name="LibBrain", content="x"),
    ])
    chat = Chat(contact_id=contact.id, user_id=user.id)
    idx = _build_brain_owner_index(chat, contact, user, scenario, [lib], messages=[])
    assert idx["lib-brain-id-1234567890abcdef12345678ab"] == {
        "kind": "brain_library",
        "owner_id": lib.id,
        "owner_name": lib.name,
    }


def test_brain_budget_offender_carries_brain_id():
    """The exception's offender list keeps ``brain.id`` so the SSE
    handler in ``routers/generate.py`` can map each offender back to its
    owning entity via the owner index."""
    offenders = [("LibBrain", 100, "lib-brain-id-1234567890abcdef12345678ab")]
    e = BrainBudgetExceeded(100, 50, offenders)
    assert e.offenders[0][2] == "lib-brain-id-1234567890abcdef12345678ab"


# ---------------------------------------------------------------------------
# Single-JSON export / import round-trip
# ---------------------------------------------------------------------------


def test_export_brain_library_embeds_avatar(tmp_storage):
    lib = BrainLibrary(name="Iconic")
    storage.save_brain_library(lib)
    ldir = storage.brain_library_dir(lib.id)
    # Drop a tiny PNG into the dir.
    avatar_path = ldir / "avatar.png"
    avatar_path.write_bytes(_png_bytes())
    lib.avatar = "avatar.png"
    storage.save_brain_library(lib)

    payload = export_brain_library(storage.get_brain_library(lib.id))
    assert payload["kind"] == "brain_library"
    assert payload["name"] == "Iconic"
    assert payload["avatarUri"].startswith("data:image/png;base64,")
    # Round-trip the data URI back to bytes — must match the original file.
    b64 = payload["avatarUri"].split(",", 1)[1]
    assert base64.b64decode(b64) == _png_bytes()


def test_import_streaming_round_trips_brain_library(tmp_storage):
    """End-to-end: export a populated library, delete it, re-import, and
    confirm the new id (mode='ask' on no collision preserves the source
    uuid), name, brains, and avatar all return verbatim."""
    src = BrainLibrary(
        name="Round trip",
        description="…",
        tags="a, b",
        brains=[Brain(name="B1", content="content one")],
    )
    storage.save_brain_library(src)
    ldir = storage.brain_library_dir(src.id)
    (ldir / "avatar.png").write_bytes(_png_bytes())
    src.avatar = "avatar.png"
    storage.save_brain_library(src)

    exported = export_brain_library(storage.get_brain_library(src.id))
    # Wipe local state — keep id; mode='ask' on no collision = preserve identity.
    assert storage.delete_brain_library(src.id)

    async def drive():
        out = []
        async for ev in import_streaming(exported, mode="ask"):
            out.append(ev)
        return out

    events = asyncio.run(drive())
    done = next(e for e in events if e["type"] == "done")
    assert done["kind"] == "brain_library"
    assert done["id"] == src.id  # source uuid preserved

    reimported = storage.get_brain_library(src.id)
    assert reimported is not None
    assert reimported.name == "Round trip"
    assert reimported.tags == "a, b"
    assert len(reimported.brains) == 1
    assert reimported.brains[0].content == "content one"
    # Avatar file recreated under the same name.
    new_ldir = storage.brain_library_dir(src.id)
    assert (new_ldir / "avatar.png").read_bytes() == _png_bytes()


def test_chat_export_includes_brain_libraries_sidecar(tmp_storage):
    """Chats with attached libraries export ``brainLibraries`` as a
    sidecar list (full library payloads) plus ``brainLibraryIds``
    (the attach order)."""
    contact = storage.save_contact(Contact(name="C"))
    user = storage.save_user(User(name="U"))
    lib_a = storage.save_brain_library(BrainLibrary(name="A"))
    lib_b = storage.save_brain_library(BrainLibrary(name="B"))
    chat = Chat(
        contact_id=contact.id,
        user_id=user.id,
        brain_library_ids=[lib_a.id, lib_b.id],
    )
    storage.save_chat_with_all(chat, ChatMessages(), ChatBookmarks())

    payload = export_chat(chat.id)
    assert payload["chat"]["brainLibraryIds"] == [lib_a.id, lib_b.id]
    sidecars = payload["brainLibraries"]
    assert [s["id"] for s in sidecars] == [lib_a.id, lib_b.id]
    assert all(s["kind"] == "brain_library" for s in sidecars)


def test_chat_export_drops_missing_library_from_sidecar(tmp_storage):
    """Dangling references in ``brain_library_ids`` (library deleted, but
    chat still attaches it) don't crash the export — the sidecar list
    skips them. ``brainLibraryIds`` retains the dangling id so a future
    re-import can rehydrate."""
    contact = storage.save_contact(Contact(name="C"))
    user = storage.save_user(User(name="U"))
    lib = storage.save_brain_library(BrainLibrary(name="Will vanish"))
    chat = Chat(
        contact_id=contact.id,
        user_id=user.id,
        brain_library_ids=[lib.id],
    )
    storage.save_chat_with_all(chat, ChatMessages(), ChatBookmarks())

    storage.delete_brain_library(lib.id)

    payload = export_chat(chat.id)
    # Dangling id preserved on the chat…
    assert payload["chat"]["brainLibraryIds"] == [lib.id]
    # …but no sidecar for it (library data is gone).
    assert payload["brainLibraries"] == []


# ---------------------------------------------------------------------------
# Generation: silently skip unknown library ids
# ---------------------------------------------------------------------------


def test_context_tokens_returns_provenance_from_last_assistant_message(client):
    """When the active path ends in an assistant message, the active-brains
    breakdown is sourced from that message's ``active_brains`` provenance
    field (set at gen time). Pinning random conditional brains to what
    was actually shipped, instead of re-rolling on every fetch."""
    from server import storage
    from server.models import ChatMessage as _CM, ChatMessages, SubMessage

    contact = client.post("/api/contacts", json={"name": "Aria"}).json()
    user = client.post("/api/users", json={"name": "Me"}).json()
    chat = client.post("/api/chats", json={
        "contact_id": contact["id"], "user_id": user["id"],
    }).json()

    # Synthesize an assistant message carrying a baked-in active_brains
    # provenance — mimics what ``_persist_assistant_message`` would write
    # after a real generation.
    fake_provenance = [
        {"brain_id": "synthetic-1", "name": "WasUsed", "tokens": 42,
         "conditional": True, "kind": "brain_library",
         "owner_id": "synthetic-lib", "owner_name": "Test Library"},
    ]
    msgs = storage.load_chat_messages(chat["id"])
    msg = _CM(
        parent_id=None,
        sender="contact",
        sender_name="Aria",
        body=[SubMessage(text="hi", emotion="neutral")],
        active_brains=fake_provenance,
    )
    msgs.messages.append(msg)
    storage.save_chat_messages(chat["id"], msgs)
    # Wire the new message as the active tail.
    fresh = storage.get_chat(chat["id"])
    fresh.selected_child_id[""] = msg.id
    storage.save_chat(fresh, bump_version=False)

    r = client.get(f"/api/chats/{chat['id']}/context-tokens")
    body = r.json()
    assert body["active_brains_from_last_gen"] is True
    # Brain id doesn't resolve anywhere → flagged as deleted, otherwise
    # the stored at-gen labels are returned verbatim (the modal renders
    # a trash marker next to deleted entries).
    [only] = body["active_brains"]
    assert only["brain_id"] == "synthetic-1"
    assert only["name"] == "WasUsed"
    assert only["owner_name"] == "Test Library"
    assert only["deleted"] is True


def test_context_tokens_refreshes_provenance_names_from_live_state(client):
    """The stored provenance is honest at gen time, but as the user
    renames entities and edits brains, the modal should show the CURRENT
    names. Only brain ids whose owners are gone fall back to the stored
    historical labels."""
    from server import storage
    from server.models import ChatMessage as _CM, SubMessage

    contact = client.post("/api/contacts", json={
        "name": "Aria",
        "brains": [{"id": "live-brain-id-1234567890abcdef12345678", "name": "Mochi", "content": "x"}],
    }).json()
    user = client.post("/api/users", json={"name": "Me"}).json()
    chat = client.post("/api/chats", json={
        "contact_id": contact["id"], "user_id": user["id"],
    }).json()

    # Build a synthetic prior gen with stale labels.
    stale_provenance = [
        {"brain_id": "live-brain-id-1234567890abcdef12345678",
         "name": "OldBrainName", "tokens": 12, "conditional": False,
         "kind": "contact", "owner_id": contact["id"], "owner_name": "OldContactName"},
        # Second entry has a dangling id — entity since deleted. Stays at stored labels.
        {"brain_id": "dangling-id-no-such-thing",
         "name": "GhostBrain", "tokens": 9, "conditional": True,
         "kind": "brain_library", "owner_id": "deleted", "owner_name": "Lost Library"},
    ]
    msgs = storage.load_chat_messages(chat["id"])
    msg = _CM(
        parent_id=None, sender="contact", sender_name="Aria",
        body=[SubMessage(text="hi", emotion="neutral")],
        active_brains=stale_provenance,
    )
    msgs.messages.append(msg)
    storage.save_chat_messages(chat["id"], msgs)
    fresh = storage.get_chat(chat["id"])
    fresh.selected_child_id[""] = msg.id
    storage.save_chat(fresh, bump_version=False)

    # Rename contact + the live brain.
    live_contact = storage.get_contact(contact["id"])
    live_contact.name = "Aria the Renamed"
    live_contact.brains[0].name = "Mochi-Refreshed"
    storage.save_contact(live_contact)

    body = client.get(f"/api/chats/{chat['id']}/context-tokens").json()
    assert body["active_brains_from_last_gen"] is True
    [refreshed, dangling] = body["active_brains"]
    # Live brain: name + owner_name refreshed from current state.
    assert refreshed["brain_id"] == "live-brain-id-1234567890abcdef12345678"
    assert refreshed["name"] == "Mochi-Refreshed"
    assert refreshed["owner_name"] == "Aria the Renamed"
    # Dangling id: stays at the at-gen labels (provenance fallback) and
    # gets flagged as deleted so the modal renders a trash marker.
    assert dangling["brain_id"] == "dangling-id-no-such-thing"
    assert dangling["name"] == "GhostBrain"
    assert dangling["owner_name"] == "Lost Library"
    assert dangling["deleted"] is True
    # Live brain doesn't carry the flag.
    assert refreshed.get("deleted", False) is False


def test_context_tokens_returns_active_brains_breakdown(client):
    """The context-tokens endpoint surfaces per-brain breakdown (powers
    the chat-stat ``brains: N (i)`` indicator + modal). Library brains
    appear with ``kind="brain_library"`` + the library's name."""
    contact = client.post("/api/contacts", json={
        "name": "Aria",
        "brains": [{"name": "ContactLore", "content": "always on"}],
    }).json()
    user = client.post("/api/users", json={
        "name": "Me",
        "brains": [{"name": "UserNote", "content": "always on"}],
    }).json()
    lib = client.post("/api/libraries", json={
        "name": "Shared rules",
        "brains": [
            {"name": "LibAlways", "content": "library always on"},
            # Conditional — won't activate without matching content.
            {"name": "LibCond", "content": "fires when ``trigger`` appears",
             "keys": [{"pattern": "trigger"}]},
        ],
    }).json()
    chat = client.post("/api/chats", json={
        "contact_id": contact["id"],
        "user_id": user["id"],
        "brain_library_ids": [lib["id"]],
    }).json()

    r = client.get(f"/api/chats/{chat['id']}/context-tokens")
    assert r.status_code == 200
    body = r.json()
    # Empty chat (no assistant message in path) → fresh preview, not pinned.
    assert body["active_brains_from_last_gen"] is False
    active = body["active_brains"]
    names = [b["name"] for b in active]
    # Three unconditional brains shipped (contact / user / library).
    # The conditional ``LibCond`` does NOT appear — no message body
    # contains "trigger" yet.
    assert "ContactLore" in names
    assert "UserNote" in names
    assert "LibAlways" in names
    assert "LibCond" not in names

    # The library brain carries owner attribution so the modal can
    # deep-link to the library detail page.
    lib_entry = next(b for b in active if b["name"] == "LibAlways")
    assert lib_entry["kind"] == "brain_library"
    assert lib_entry["owner_id"] == lib["id"]
    assert lib_entry["owner_name"] == "Shared rules"
    assert lib_entry["conditional"] is False
    assert lib_entry["tokens"] > 0


def test_resolve_chat_libraries_skips_unknown_ids(tmp_storage):
    """A chat that attaches a library which was later deleted must still
    generate (just without that library's brains). The resolver returns
    only the known libraries; the generate route then passes those to
    ``build_messages_for_generation`` without raising."""
    from server.routers.generate import _resolve_chat_libraries
    lib = storage.save_brain_library(BrainLibrary(name="alive"))
    chat = Chat(
        contact_id="x",
        user_id="y",
        brain_library_ids=["unknown-id", lib.id, "another-unknown"],
    )
    resolved = _resolve_chat_libraries(chat)
    assert [l.id for l in resolved] == [lib.id]

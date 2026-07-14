"""Tests for the import pipeline: crop coercion, image-dim sniff, UUID
conflict resolution, and chat-composite sidecar plans (id-match, name-match,
import-new)."""
from __future__ import annotations

import struct

import pytest

from server import storage
from server.importers import (
    _free_id,
    _preresolve_sidecar,
    _resolve_target_id,
    _square_in_pixels,
    import_contact_streaming,
    import_streaming,
)
from server.models import Contact, ContactScenario, CropRect, Intimacy, ResponseLength, Scenario, Style, User, new_id
from server.routers.files import sniff_dimensions


# ---------------------------------------------------------------------------
# _square_in_pixels — coerce imported crops into the codebase's convention.
# ---------------------------------------------------------------------------


def test_square_in_pixels_portrait_w_eq_h_centres_vertically():
    # ``facePosition`` from a real older export on a 441x640 portrait. Source
    # is square in pixels (w*imgW == h*imgW); we rewrite to square-in-pixels
    # via imgH.
    out = _square_in_pixels(
        CropRect(x=0.217, y=0.013, w=0.55, h=0.55), (441, 640),
    )
    assert out.w == pytest.approx(0.55)
    assert out.h == pytest.approx(0.55 * 441 / 640)
    # Vertically centred on the original rect's centre, not anchored to y=0.013.
    cy_original = 0.013 + 0.55 / 2
    assert out.y == pytest.approx(cy_original - out.h / 2)
    assert out.x == pytest.approx(0.217)


def test_square_in_pixels_already_pixel_square_passthrough():
    crop = CropRect(x=0.1, y=0.1, w=0.5, h=0.5 * 441 / 640)
    out = _square_in_pixels(crop, (441, 640))
    assert out is crop  # exact passthrough — no allocation


def test_square_in_pixels_square_image_passthrough():
    crop = CropRect(x=0.1, y=0.2, w=0.5, h=0.5)
    out = _square_in_pixels(crop, (200, 200))
    # No reshaping needed when image is square.
    assert out is crop


def test_square_in_pixels_rectangular_crop_centred_shrink():
    # Explicitly non-square crop on a 441x640 image: pixel rect is 176×384.
    # Side = min = 176px → new_h = 176/640. Centre stays put.
    out = _square_in_pixels(
        CropRect(x=0.1, y=0.2, w=0.4, h=0.6), (441, 640),
    )
    assert out.w == pytest.approx(0.4)
    assert out.h == pytest.approx(0.4 * 441 / 640)
    # Centre matches original.
    assert (out.x + out.w / 2) == pytest.approx(0.3)
    assert (out.y + out.h / 2) == pytest.approx(0.5)


def test_square_in_pixels_none_inputs_passthrough():
    assert _square_in_pixels(None, (100, 100)) is None
    crop = CropRect(x=0, y=0, w=1, h=1)
    assert _square_in_pixels(crop, None) is crop


# ---------------------------------------------------------------------------
# sniff_dimensions — minimal-header parsers for PNG / GIF / WebP / JPEG.
# ---------------------------------------------------------------------------


def test_sniff_dimensions_png_minimal_header():
    # 8-byte sig + 4-byte length + 4-byte "IHDR" + 4-byte width + 4-byte height
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = b"\x00\x00\x00\rIHDR" + struct.pack(">II", 441, 640)
    assert sniff_dimensions(sig + ihdr + b"\x00" * 50) == (441, 640)


def test_sniff_dimensions_gif():
    # GIF87a + 2-byte width LE + 2-byte height LE
    blob = b"GIF87a" + struct.pack("<HH", 320, 200) + b"\x00" * 50
    assert sniff_dimensions(blob) == (320, 200)


def test_sniff_dimensions_webp_vp8x():
    # RIFF + 4-byte length + WEBP + VP8X chunk: 4-byte tag + 4-byte chunk len
    # + 4-byte flags + 3-byte (w-1) LE + 3-byte (h-1) LE
    head = b"RIFF" + struct.pack("<I", 0) + b"WEBP"
    vp8x = b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
    w_minus_1 = (199).to_bytes(3, "little")  # → 200
    h_minus_1 = (149).to_bytes(3, "little")  # → 150
    blob = head + vp8x + w_minus_1 + h_minus_1 + b"\x00" * 50
    assert sniff_dimensions(blob) == (200, 150)


def test_sniff_dimensions_unknown_format():
    assert sniff_dimensions(b"not an image at all") is None


# ---------------------------------------------------------------------------
# _resolve_target_id — UUID conflict-resolution policy used by every
# top-level import.
# ---------------------------------------------------------------------------


def test_resolve_target_id_no_incoming_id_assigns_fresh():
    target, conflict, should_stage = _resolve_target_id(
        None, "ask", "contact", "Alice",
        get_existing=lambda _id: None,
    )
    assert conflict is None
    assert target  # something was assigned
    assert should_stage is False


def test_resolve_target_id_incoming_id_no_conflict_preserved():
    target, conflict, should_stage = _resolve_target_id(
        "abcd1234", "ask", "contact", "Alice",
        get_existing=lambda _id: None,
    )
    assert conflict is None
    assert target == "abcd1234"
    assert should_stage is False


def test_resolve_target_id_ask_with_conflict_yields_event():
    existing = Contact(id="abc", name="OldAlice")
    target, conflict, should_stage = _resolve_target_id(
        "abc", "ask", "contact", "NewAlice",
        get_existing=lambda _id: existing,
    )
    assert target is None
    assert conflict["type"] == "conflict"
    assert conflict["kind"] == "contact"
    assert conflict["existing_name"] == "OldAlice"
    assert conflict["incoming_name"] == "NewAlice"
    assert should_stage is False


def test_resolve_target_id_replace_signals_staging():
    """Replace mode asks the caller to stage the existing entity to .bak
    before importing into the now-empty slot. The actual rename happens
    in the caller, inside the per-id lock."""
    target, conflict, should_stage = _resolve_target_id(
        "abc", "replace", "contact", "Alice",
        get_existing=lambda _id: Contact(id="abc", name="Old"),
    )
    assert conflict is None
    assert target == "abc"
    assert should_stage is True


def test_resolve_target_id_copy_assigns_new_id_on_conflict():
    target, conflict, should_stage = _resolve_target_id(
        "abc", "copy", "contact", "Alice",
        get_existing=lambda _id: Contact(id="abc", name="Old"),
    )
    assert conflict is None
    assert target != "abc"
    assert should_stage is False


# ---------------------------------------------------------------------------
# Composite sidecar resolution: id-match silently reuses, name-match yields
# an "ask" plan, otherwise we import (preserving the source UUID when free).
# ---------------------------------------------------------------------------


def test_preresolve_sidecar_id_match_silent_reuse():
    existing = User(id="user-id-1", name="Bob")
    plan = _preresolve_sidecar(
        {"id": "user-id-1", "name": "Bob"}, "user", None,
        get_existing=lambda i: existing if i == "user-id-1" else None,
        list_all=lambda: [existing],
    )
    assert plan == {"action": "existing", "entity": existing}


def test_preresolve_sidecar_name_match_asks():
    existing = User(id="other-id", name="Bob")
    plan = _preresolve_sidecar(
        {"id": "fresh-id", "name": "Bob"}, "user", None,
        get_existing=lambda _i: None,
        list_all=lambda: [existing],
    )
    assert plan["action"] == "ask"
    assert plan["candidates"] == [existing]


def test_preresolve_sidecar_no_match_imports_with_source_id():
    plan = _preresolve_sidecar(
        {"id": "fresh-id", "name": "Bob"}, "user", None,
        get_existing=lambda _i: None,
        list_all=lambda: [],
    )
    assert plan == {"action": "import", "target_id": "fresh-id"}


def test_preresolve_sidecar_resolution_picks_existing_by_id():
    picked = User(id="picked-id", name="Renamed")
    plan = _preresolve_sidecar(
        {"id": "fresh-id", "name": "Bob"}, "user",
        {"user": "picked-id"},
        get_existing=lambda i: picked if i == "picked-id" else None,
        list_all=lambda: [User(id="other", name="Bob")],
    )
    assert plan == {"action": "existing", "entity": picked}


def test_preresolve_sidecar_resolution_new_overrides_name_match():
    plan = _preresolve_sidecar(
        {"id": "fresh-id", "name": "Bob"}, "user",
        {"user": "new"},
        get_existing=lambda _i: None,
        list_all=lambda: [User(id="other", name="Bob")],
    )
    assert plan["action"] == "import"
    assert plan["target_id"] == "fresh-id"  # source UUID is free


def test_preresolve_sidecar_missing_data_skips_for_scenario():
    plan = _preresolve_sidecar(
        None, "scenario", None,
        get_existing=lambda _i: None, list_all=lambda: [],
    )
    assert plan == {"action": "missing"}


def test_free_id_fresh_passthrough_else_new():
    assert _free_id("fresh", lambda _i: None) == "fresh"
    out = _free_id("taken", lambda _i: object())
    assert out and out != "taken"


# ---------------------------------------------------------------------------
# End-to-end-ish: composite import drains import_streaming and respects the
# new resolution flow. Exercises real storage via the ``tmp_storage`` fixture.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_level_contact_import_preserves_source_uuid(tmp_storage):
    data = {"name": "Alice", "id": "alice-id-001", "greeting": "Hi"}
    events = []
    async for ev in import_streaming(data):
        events.append(ev)
    assert any(e["type"] == "done" for e in events)
    assert storage.get_contact("alice-id-001") is not None


@pytest.mark.asyncio
async def test_top_level_contact_conflict_yields_ask(tmp_storage):
    storage.save_contact(Contact(id="dup-id", name="Original"))
    events = []
    async for ev in import_streaming({"name": "Other", "id": "dup-id", "greeting": ""}):
        events.append(ev)
    assert events[-1]["type"] == "conflict"
    assert events[-1]["existing_name"] == "Original"


@pytest.mark.asyncio
async def test_top_level_contact_copy_mode_assigns_new_id(tmp_storage):
    storage.save_contact(Contact(id="dup-id", name="Original"))
    out_id: str | None = None
    async for ev in import_streaming({"name": "Other", "id": "dup-id", "greeting": ""}, mode="copy"):
        if ev["type"] == "done":
            out_id = ev["id"]
    assert out_id and out_id != "dup-id"
    assert storage.get_contact("dup-id").name == "Original"  # unchanged
    assert storage.get_contact(out_id).name == "Other"


@pytest.mark.asyncio
async def test_composite_import_idmatch_reuses_sidecar(tmp_storage):
    storage.save_contact(Contact(id="char-1", name="Alice"))
    storage.save_user(User(id="user-1", name="Bob"))
    composite = {
        "chat": {"id": "chat-1", "title": "T", "messages": [],
                 "selectedChildId": {}, "intimacy": "stranger", "style": "chat"},
        "character": {"id": "char-1", "name": "Alice"},
        "user": {"id": "user-1", "name": "Bob"},
    }
    out_id = None
    async for ev in import_streaming(composite):
        if ev["type"] == "done":
            out_id = ev["id"]
    assert out_id == "chat-1"
    chat = storage.get_chat("chat-1")
    assert chat.contact_id == "char-1"
    assert chat.user_id == "user-1"
    # Counts unchanged — sidecars reused, not duplicated. (storage.initialize
    # seeds a default "Anon" persona, so list_users includes it.)
    assert len(storage.list_contacts()) == 1
    user_ids = {u.id for u in storage.list_users()}
    assert "user-1" in user_ids
    assert not any(u.name == "Bob" and u.id != "user-1" for u in storage.list_users())


@pytest.mark.asyncio
async def test_composite_import_name_match_yields_event(tmp_storage):
    # Pre-existing contact with the same name but a different UUID.
    storage.save_contact(Contact(id="existing-char", name="Alice"))
    storage.save_user(User(id="existing-user", name="Bob"))
    composite = {
        "chat": {"id": "chat-x", "title": "T", "messages": [],
                 "selectedChildId": {}, "intimacy": "stranger", "style": "chat"},
        "character": {"id": "fresh-char", "name": "Alice"},
        "user": {"id": "fresh-user", "name": "Bob"},
    }
    events = []
    async for ev in import_streaming(composite):
        events.append(ev)
    [last] = [e for e in events if e["type"] == "name_matches"]
    roles = {item["role"] for item in last["items"]}
    assert roles == {"character", "user"}
    # Stream aborts before importing.
    assert storage.get_chat("chat-x") is None


@pytest.mark.asyncio
async def test_composite_import_resolutions_apply(tmp_storage):
    storage.save_contact(Contact(id="existing-char", name="Alice"))
    storage.save_user(User(id="existing-user", name="Bob"))
    composite = {
        "chat": {"id": "chat-y", "title": "T", "messages": [],
                 "selectedChildId": {}, "intimacy": "stranger", "style": "chat"},
        "character": {"id": "fresh-char", "name": "Alice"},
        "user": {"id": "fresh-user", "name": "Bob"},
    }
    # User picks the existing contact, fresh import for the user persona.
    resolutions = {"character": "existing-char", "user": "new"}
    out_id = None
    async for ev in import_streaming(composite, resolutions=resolutions):
        if ev["type"] == "done":
            out_id = ev["id"]
    assert out_id == "chat-y"
    chat = storage.get_chat("chat-y")
    assert chat.contact_id == "existing-char"
    assert chat.user_id == "fresh-user"  # imported with source UUID


@pytest.mark.asyncio
async def test_contact_streaming_target_id_override(tmp_storage):
    """Caller-provided target_id bypasses _resolve_target_id (used by the
    composite plan executor to import sidecars at a pre-resolved UUID)."""
    out_id = None
    async for ev in import_contact_streaming(
        {"name": "Override", "id": "ignored"}, target_id="forced-id",
    ):
        if ev["type"] == "done":
            out_id = ev["id"]
    assert out_id == "forced-id"
    assert storage.get_contact("forced-id").name == "Override"


# ---------------------------------------------------------------------------
# Coverage for legacy / no-UUID inputs and the corner cases the UI tests
# don't reach: composite chat-id conflict, scenario sidecar, and sidecars
# whose UUIDs don't match anything (must still be preserved).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_contact_no_id_assigns_fresh(tmp_storage):
    """Older exports (pre-UUID format) omit the ``id`` field — import should
    assign a fresh UUID and never raise a conflict."""
    events = []
    async for ev in import_streaming({"name": "LegacyAlice", "greeting": "hi"}):
        events.append(ev)
    [done] = [e for e in events if e["type"] == "done"]
    assert not any(e["type"] == "conflict" for e in events)
    assert storage.get_contact(done["id"]).name == "LegacyAlice"


@pytest.mark.asyncio
async def test_legacy_composite_no_ids_anywhere(tmp_storage):
    """Composite with no UUIDs on chat / character / user — every entity
    gets a fresh UUID; the chat references the new sidecar ids."""
    composite = {
        "chat": {"title": "L", "messages": [], "selectedChildId": {},
                 "intimacy": "stranger", "style": "chat"},
        "character": {"name": "LegacyChar", "greeting": ""},
        "user": {"name": "LegacyUser", "tags": ""},
    }
    out_id = None
    async for ev in import_streaming(composite):
        if ev["type"] == "done":
            out_id = ev["id"]
    chat = storage.get_chat(out_id)
    assert chat is not None
    contact = storage.get_contact(chat.contact_id)
    user = storage.get_user(chat.user_id)
    assert contact.name == "LegacyChar"
    assert user.name == "LegacyUser"


@pytest.mark.asyncio
async def test_composite_chat_title_does_not_name_match(tmp_storage):
    """Chats are deliberately exempt from name-matching — titles like
    "Chat with Waifu" collide constantly. A new composite with a duplicate
    title must import silently as a separate chat, never yielding a
    conflict or name_matches event for the chat itself."""
    storage.save_contact(Contact(id="cc1", name="C"))
    storage.save_user(User(id="uu1", name="U"))
    from server.models import Chat
    storage.save_chat(Chat(
        id="existing-chat", contact_id="cc1", user_id="uu1",
        title="Chat with namesake",
    ))

    composite = {
        "chat": {
            # Distinct UUID, identical title.
            "id": "fresh-chat", "title": "Chat with namesake",
            "messages": [], "selectedChildId": {},
            "intimacy": "stranger", "style": "chat",
        },
        "character": {"id": "cc1", "name": "C"},
        "user": {"id": "uu1", "name": "U"},
    }
    events = []
    async for ev in import_streaming(composite):
        events.append(ev)
    # No prompt at any layer for the chat — only the import succeeds.
    assert not any(e["type"] in ("conflict", "name_matches") for e in events)
    [done] = [e for e in events if e["type"] == "done"]
    assert done["id"] == "fresh-chat"
    # Both chats live side by side.
    assert storage.get_chat("existing-chat") is not None
    assert storage.get_chat("fresh-chat") is not None


@pytest.mark.asyncio
async def test_composite_chat_id_conflict_yields_event(tmp_storage):
    """Top-level conflict-resolution applies to the chat itself, not just
    standalone-imported entities."""
    storage.save_contact(Contact(id="c1", name="C"))
    storage.save_user(User(id="u1", name="U"))
    storage.save_chat_messages = storage.save_chat_messages  # silence linters
    from server.models import Chat
    storage.save_chat(Chat(id="dup-chat", contact_id="c1", user_id="u1"))

    composite = {
        "chat": {"id": "dup-chat", "title": "X", "messages": [],
                 "selectedChildId": {}, "intimacy": "stranger", "style": "chat"},
        "character": {"id": "c1", "name": "C"},
        "user": {"id": "u1", "name": "U"},
    }
    events = []
    async for ev in import_streaming(composite):
        events.append(ev)
    assert events[-1]["type"] == "conflict"
    assert events[-1]["kind"] == "chat"
    assert events[-1]["id"] == "dup-chat"


@pytest.mark.asyncio
async def test_composite_chat_id_conflict_replace_overwrites(tmp_storage):
    storage.save_contact(Contact(id="c1", name="C"))
    storage.save_user(User(id="u1", name="U"))
    from server.models import Chat
    storage.save_chat(Chat(id="dup-chat", contact_id="c1", user_id="u1", title="Original"))

    composite = {
        "chat": {"id": "dup-chat", "title": "Replaced", "messages": [],
                 "selectedChildId": {}, "intimacy": "close", "style": "chat"},
        "character": {"id": "c1", "name": "C"},
        "user": {"id": "u1", "name": "U"},
    }
    out_id = None
    async for ev in import_streaming(composite, mode="replace"):
        if ev["type"] == "done":
            out_id = ev["id"]
    assert out_id == "dup-chat"
    chat = storage.get_chat("dup-chat")
    assert chat.title == "Replaced"


@pytest.mark.asyncio
async def test_composite_with_scenario_sidecar(tmp_storage):
    """Scenario sidecar exercises the third branch of ``_preresolve_sidecar``
    (which the other composite tests skip via ``scenario: None``)."""
    composite = {
        "chat": {"id": "s-chat", "title": "S", "messages": [],
                 "selectedChildId": {}, "intimacy": "stranger", "style": "chat"},
        "character": {"id": "s-char", "name": "ScChar", "greeting": ""},
        "user": {"id": "s-user", "name": "ScUser", "tags": ""},
        "scenario": {"id": "s-scen", "name": "ParkBench",
                     "environment": "A small park bench, golden hour."},
    }
    async for ev in import_streaming(composite):
        pass
    chat = storage.get_chat("s-chat")
    assert chat.scenario_id == "s-scen"
    assert storage.get_scenario("s-scen").name == "ParkBench"


@pytest.mark.asyncio
async def test_composite_sidecar_uuid_preserved_when_no_match(tmp_storage):
    """No id-match, no name-match → sidecars import with their source UUIDs
    intact. Verifies the ``_free_id`` path inside ``_preresolve_sidecar``."""
    composite = {
        "chat": {"id": "novel-chat", "title": "N", "messages": [],
                 "selectedChildId": {}, "intimacy": "stranger", "style": "chat"},
        "character": {"id": "novel-char", "name": "NovelChar", "greeting": ""},
        "user": {"id": "novel-user", "name": "NovelUser", "tags": ""},
    }
    async for ev in import_streaming(composite):
        pass
    chat = storage.get_chat("novel-chat")
    assert chat.contact_id == "novel-char"
    assert chat.user_id == "novel-user"
    assert storage.get_contact("novel-char") is not None
    assert storage.get_user("novel-user") is not None


@pytest.mark.asyncio
async def test_top_level_user_import_round_trip(tmp_storage):
    """Standalone user-persona import — exercises the kind='user' branch of
    the streaming dispatcher (only contact has direct coverage otherwise)."""
    data = {"id": "uimp-1", "name": "ImpUser", "persona": "x", "tags": ""}
    out_id = None
    async for ev in import_streaming(data):
        if ev["type"] == "done":
            out_id = ev["id"]
    assert out_id == "uimp-1"
    assert storage.get_user("uimp-1").name == "ImpUser"


@pytest.mark.asyncio
async def test_top_level_scenario_import_round_trip(tmp_storage):
    data = {
        "id": "simp-1", "name": "ImpScenario",
        "environment": "A scene.", "scene": "Once upon a time.",
    }
    out_id = None
    async for ev in import_streaming(data):
        if ev["type"] == "done":
            out_id = ev["id"]
    assert out_id == "simp-1"
    assert storage.get_scenario("simp-1").environment == "A scene."


# A 1×1 transparent PNG — minimum valid bytes for the importer's image
# sniffer to accept the download.
_PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cb\x00"
    b"\x00\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.mark.asyncio
async def test_user_import_with_avatar_data_uri(tmp_storage):
    """User imports honour an embedded ``avatarUri`` and translate
    ``avatarCrop`` through ``_square_in_pixels`` (matches the contact path)."""
    import base64
    from server.models import CropRect

    png_b64 = base64.b64encode(_PNG_1x1).decode("ascii")
    data = {
        "id": "user-with-avatar-1",
        "name": "AvatarUser",
        "persona": "Likes long walks.",
        "avatarUri": f"data:image/png;base64,{png_b64}",
        "avatarCrop": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
    }
    out_id = None
    async for ev in import_streaming(data):
        if ev["type"] == "done":
            out_id = ev["id"]
    user = storage.get_user(out_id)
    assert user.avatar and user.avatar.startswith("avatar.")
    # Crop survived round-trip (1x1 image, identity crop).
    assert user.avatar_crop is not None
    # File actually landed on disk.
    udir = storage.user_dir(user.id)
    assert (udir / user.avatar).exists()


@pytest.mark.asyncio
async def test_user_import_no_avatar_uri_leaves_avatar_unset(tmp_storage):
    """When the source carries no ``avatarUri``, the user persona imports
    cleanly with ``avatar`` and ``avatar_crop`` both ``None``."""
    data = {"id": "no-avatar-user", "name": "NoAvatar", "persona": "Plain"}
    async for ev in import_streaming(data):
        pass
    user = storage.get_user("no-avatar-user")
    assert user is not None
    assert user.avatar is None
    assert user.avatar_crop is None


@pytest.mark.asyncio
async def test_user_export_round_trip(tmp_storage):
    """Round-trip: export a user with an avatar, import the exported JSON
    on a clean state, the avatar is back."""
    from server import exporters
    import base64

    user = storage.save_user(User(id="rt-user-1", name="RoundTrip"))
    udir = storage.user_dir(user.id)
    (udir / "avatar.png").write_bytes(_PNG_1x1)
    user.avatar = "avatar.png"
    storage.save_user(user)

    payload = exporters.export_user(storage.get_user(user.id))
    assert payload["avatarUri"].startswith("data:image/png;base64,")

    # Wipe and re-import.
    storage.delete_user(user.id)
    assert storage.get_user(user.id) is None

    async for ev in import_streaming(payload):
        pass
    restored = storage.get_user(user.id)
    assert restored is not None
    assert restored.avatar and restored.avatar.startswith("avatar.")


# ---------------------------------------------------------------------------
# Contact-scoped scenarios — accept both ``presetSpaces`` (foreign format
# in user-supplied JSON) and ``scenarios`` (our own export key).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_import_preset_spaces_round_trip(tmp_storage):
    data = {
        "id": "presetspaces-contact",
        "name": "Alice",
        "greeting": "default greeting",
        "presetSpaces": [
            {
                "is_default": True,
                "name": "Scenario A",
                "greeting": "scenario greeting",
                "greeting_emotion": "happy",
                "style": "roleplay",
                "scene": "Generic scene.",
                "environment": "Generic environment.",
                "relationship": "acquaintance",
                "response_length": "short",
                "chat_tags": ["alpha", "bravo"],
                "cjk": True,
            },
            {
                "name": "Scenario B",
                "scene": "Another generic scene.",
                "chat_tags": ["delta"],
            },
        ],
    }
    async for _ in import_streaming(data):
        pass
    contact = storage.get_contact("presetspaces-contact")
    assert contact is not None
    assert len(contact.scenarios) == 2
    first, second = contact.scenarios
    assert first.name == "Scenario A"
    assert first.greeting == "scenario greeting"
    assert first.style == Style.ROLEPLAY
    assert first.intimacy == Intimacy.ACQUAINTANCE
    assert first.response_length == ResponseLength.SHORT
    assert first.tags == "alpha, bravo"
    assert first.cjk is True
    # is_default flagged the first entry → its id is the default.
    assert contact.default_scenario_id == first.id
    # Second entry has no overrides set; chat_tags becomes a comma string.
    assert second.style is None
    assert second.intimacy is None
    assert second.response_length is None
    assert second.tags == "delta"


@pytest.mark.asyncio
async def test_contact_import_scenarios_key_works_too(tmp_storage):
    """Our export key (``scenarios``) is accepted alongside ``presetSpaces``."""
    data = {
        "id": "scenarios-contact",
        "name": "Alice",
        "greeting": "hi",
        "scenarios": [
            {"name": "Scenario A", "scene": "Generic scene.", "tags": "alpha"},
        ],
    }
    async for _ in import_streaming(data):
        pass
    contact = storage.get_contact("scenarios-contact")
    assert len(contact.scenarios) == 1
    assert contact.scenarios[0].name == "Scenario A"
    assert contact.scenarios[0].tags == "alpha"


@pytest.mark.asyncio
async def test_contact_export_emits_scenarios(tmp_storage):
    from server.exporters import export_contact

    cs = ContactScenario(
        name="Scenario A", scene="Generic scene.", tags="alpha",
        greeting="hi", style=Style.ROLEPLAY, intimacy=Intimacy.ACQUAINTANCE,
        response_length=ResponseLength.SHORT, cjk=True,
    )
    cs2 = ContactScenario(name="Scenario B", scene="Another generic scene.")
    contact = storage.save_contact(Contact(
        id="export-c", name="Alice", scenarios=[cs, cs2], default_scenario_id=cs.id,
    ))

    payload = export_contact(contact)
    assert "scenarios" in payload
    assert len(payload["scenarios"]) == 2
    first, second = payload["scenarios"]
    assert first["name"] == "Scenario A"
    assert first["is_default"] is True
    assert first["style"] == "roleplay"
    assert first["intimacy"] == "acquaintance"
    assert first["response_length"] == "short"
    assert first["cjk"] is True
    assert second["is_default"] is False


@pytest.mark.asyncio
async def test_contact_scenarios_round_trip_via_export_import(tmp_storage):
    """Export → wipe → import preserves scenarios + default."""
    from server.exporters import export_contact

    cs = ContactScenario(
        name="Scenario A", greeting="hi {{user}}", scene="generic",
        environment="generic env", tags="alpha", cjk=True,
        style=Style.ROLEPLAY, intimacy=Intimacy.ACQUAINTANCE,
        response_length=ResponseLength.SHORT,
    )
    contact = storage.save_contact(Contact(
        id="rt-c", name="Alice", scenarios=[cs], default_scenario_id=cs.id,
    ))
    payload = export_contact(contact)
    storage.delete_contact(contact.id)

    async for _ in import_streaming(payload):
        pass
    restored = storage.get_contact("rt-c")
    assert restored is not None
    assert len(restored.scenarios) == 1
    [r] = restored.scenarios
    assert r.name == "Scenario A"
    assert r.greeting == "hi {{user}}"
    assert r.tags == "alpha"
    assert r.cjk is True
    assert r.style == Style.ROLEPLAY
    assert r.intimacy == Intimacy.ACQUAINTANCE
    assert r.response_length == ResponseLength.SHORT
    # Default flag round-trips through ``is_default``.
    assert restored.default_scenario_id == r.id


# ---------------------------------------------------------------------------
# Favourite field round-trips through import / export for all three of the
# top-level entity kinds (Contact, User, Scenario).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_favorite_round_trips_through_export_import(tmp_storage):
    from server.exporters import export_chat, export_contact, export_scenario, export_user
    from server.models import Chat, ChatBookmarks, ChatMessages

    contact = storage.save_contact(Contact(id="rt-fav-c", name="Alice", greeting="hi", favorite=True))
    user = storage.save_user(User(id="rt-fav-u", name="Bob", favorite=True))
    scenario = storage.save_scenario(Scenario(
        id="rt-fav-s", name="ParkBench", environment="A small park bench.", favorite=True,
    ))
    chat = storage.save_chat(Chat(
        id="rt-fav-chat", contact_id=contact.id, user_id=user.id, favorite=True,
    ))
    storage.save_chat_messages(chat.id, ChatMessages())
    storage.save_chat_bookmarks(chat.id, ChatBookmarks())

    cpayload = export_contact(contact)
    upayload = export_user(user)
    spayload = export_scenario(scenario)
    chpayload = export_chat(chat.id)
    assert cpayload["favorite"] is True
    assert upayload["favorite"] is True
    assert spayload["favorite"] is True
    assert chpayload["chat"]["favorite"] is True

    # Wipe and re-import each.
    storage.delete_chat(chat.id)
    storage.delete_contact(contact.id)
    storage.delete_user(user.id)
    storage.delete_scenario(scenario.id)
    async for _ in import_streaming(cpayload):
        pass
    async for _ in import_streaming(upayload):
        pass
    async for _ in import_streaming(spayload):
        pass
    async for _ in import_streaming(chpayload):
        pass

    assert storage.get_contact("rt-fav-c").favorite is True
    assert storage.get_user("rt-fav-u").favorite is True
    assert storage.get_scenario("rt-fav-s").favorite is True
    assert storage.get_chat("rt-fav-chat").favorite is True


# ---------------------------------------------------------------------------
# ``kind`` marker + ``author`` field — explicit per-resource type and the
# author/creator slot that holds ST card ``creator``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kind_and_author_round_trip(tmp_storage):
    from server import exporters
    from server.importers import detect_format
    from server.models import BrainLibrary, Chat, ChatBookmarks, ChatMessages

    contact = storage.save_contact(Contact(
        id="rt-kind-c", name="Alice", greeting="hi", author="Anna",
    ))
    user = storage.save_user(User(id="rt-kind-u", name="Bob", author="Bea"))
    scenario = storage.save_scenario(Scenario(
        id="rt-kind-s", name="ParkBench",
        environment="A small park bench.", author="Sam",
    ))
    library = storage.save_brain_library(BrainLibrary(
        id="rt-kind-l", name="Worldlore", author="Liam",
    ))
    chat = storage.save_chat(Chat(
        id="rt-kind-chat", contact_id=contact.id, user_id=user.id,
    ))
    storage.save_chat_messages(chat.id, ChatMessages())
    storage.save_chat_bookmarks(chat.id, ChatBookmarks())

    cpayload = exporters.export_contact(contact)
    upayload = exporters.export_user(user)
    spayload = exporters.export_scenario(scenario)
    lpayload = exporters.export_brain_library(library)
    chpayload = exporters.export_chat(chat.id)

    # Every per-resource export stamps an explicit kind marker.
    assert cpayload["kind"] == "contact"
    assert upayload["kind"] == "user"
    assert spayload["kind"] == "scenario"
    assert lpayload["kind"] == "brain_library"
    assert chpayload["kind"] == "chat"

    # detect_format honours the marker directly without falling through to
    # the heuristic.
    assert detect_format(cpayload) == "contact"
    assert detect_format(upayload) == "user"
    assert detect_format(spayload) == "scenario"
    assert detect_format(lpayload) == "brain_library"
    assert detect_format(chpayload) == "chat"

    # Author survives export.
    assert cpayload["author"] == "Anna"
    assert upayload["author"] == "Bea"
    assert spayload["author"] == "Sam"
    assert lpayload["author"] == "Liam"

    # Wipe + re-import via the streaming dispatcher; the explicit kind
    # tells it which builder to call.
    storage.delete_chat(chat.id)
    storage.delete_contact(contact.id)
    storage.delete_user(user.id)
    storage.delete_scenario(scenario.id)
    storage.delete_brain_library(library.id)
    for payload in (cpayload, upayload, spayload, lpayload, chpayload):
        async for _ in import_streaming(payload):
            pass

    assert storage.get_contact("rt-kind-c").author == "Anna"
    assert storage.get_user("rt-kind-u").author == "Bea"
    assert storage.get_scenario("rt-kind-s").author == "Sam"
    assert storage.get_brain_library("rt-kind-l").author == "Liam"


def test_detect_format_legacy_heuristic_still_works():
    """Old exports lack the ``kind`` marker; the heuristic continues to
    classify them."""
    from server.importers import detect_format

    assert detect_format({"name": "X", "greeting": "hi"}) == "contact"
    assert detect_format({"name": "X", "persona": "p"}) == "user"
    assert detect_format({"name": "X", "environment": "e"}) == "scenario"
    assert detect_format({"chat": {}, "character": {}}) == "chat"


@pytest.mark.asyncio
async def test_contact_import_with_http_image_urls(tmp_storage, monkeypatch):
    """Contacts in the wild carry HTTP image URLs (catbox.moe etc.). We
    monkeypatch the resolver to return canned bytes so the test stays
    offline yet still exercises the avatar-from-http and emotion-from-http
    branches end-to-end."""
    from server import importers

    async def fake_resolve(value, _session):
        if isinstance(value, str) and value.startswith("http"):
            return _PNG_1x1
        return None

    monkeypatch.setattr(importers, "_resolve_image", fake_resolve)

    data = {
        "id": "http-contact-1",
        "name": "HttpContact",
        "greeting": "hi",
        "avatarUri": "https://example.invalid/avatar.png",
        "emotions": {
            "neutral": "https://example.invalid/neutral.png",
            "happy": "https://example.invalid/happy.png",
        },
    }
    out_id = None
    async for ev in import_streaming(data):
        if ev["type"] == "done":
            out_id = ev["id"]
    contact = storage.get_contact(out_id)
    assert contact is not None
    # Avatar landed on disk and is referenced.
    assert contact.avatar and contact.avatar.startswith("avatar.")
    # Emotions both downloaded and registered.
    assert set(contact.emotions.keys()) == {"neutral", "happy"}

"""Tests for the AER zip-import format.

Coverage:
- Deterministic UUID mapping + per-kind separation.
- Snake → camel translation for ``meta-contact.json``.
- Linear-bubble → multi-bubble coalescing.
- ``has_overrides`` threshold for promoting a space to a ContactScenario.
- ``_entity_state`` for all five enum values.
- ``read_zip_toc`` against an in-memory zip.
- End-to-end import + re-import (no change / upstream / local / both).
- ``replace`` and ``copy`` actions on contacts and chats.

All fixtures are synthetic — no content from any real archive.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
import zipfile
from io import BytesIO

import pytest

from server import storage
from server.importers import (
    _aer_to_uuid,
    _AER_ZIP_NAMESPACE,
    _aer_search_haystack,
    _chat_signature,
    _decorate_manifest,
    _entity_state,
    _flat_search_haystack,
    _maybe_proxy_avatar,
    _meta_contact_signature,
    _meta_space_has_overrides,
    _meta_space_signature,
    _translate_meta_contact,
    _translate_messages_to_chat_messages,
    _zip_format,
    import_zip_streaming,
    read_zip_toc,
)
from server.models import Chat, ChatMessage, Contact, ContactScenario, SubMessage


# ---------------------------------------------------------------------------
# Fixture helpers — synthetic data only
# ---------------------------------------------------------------------------


def _synthetic_id() -> str:
    """22-char URL-safe-base64 string, mimicking the upstream src_id shape
    without using any real archive's IDs."""
    return base64.urlsafe_b64encode(secrets.token_bytes(16))[:22].decode()


def _build_meta_contact(
    *,
    src_id: str | None = None,
    name: str = "Test Character",
    revision_id: str | None = None,
    persona: str = "A test persona",
    avatar_uri: str = "data:image/png;base64,iVBORw0KGgo=",
    emotions: dict | None = None,
) -> dict:
    return {
        "contact_id": src_id or _synthetic_id(),
        "name": name,
        "description": "Test description",
        "tagline": "",
        "avatar_uri": avatar_uri,
        "search_tags": ["alpha", "beta"],
        "gender": "any",
        "pronouns": "they/them",
        "species": "test",
        "relationship": "stranger",
        "emotions": emotions or {},
        "revision_data": {
            "revision_id": revision_id or _synthetic_id(),
            "revision_timestamp": "2026-05-09T12:00:00Z",
            "is_rollback": False,
        },
        "ai_data": {
            "persona": persona,
            "appearance": "Test appearance",
            "greeting": "Hi there.",
            "greeting_emotion": "neutral",
            "example_messages": [],
        },
    }


def _build_meta_space(
    *,
    src_id: str | None = None,
    name: str = "",
    scene: str = "",
    environment: str = "",
    last_update: str = "2026-05-09T12:00:00Z",
    relationship: str = "",
    style: str = "",
) -> dict:
    return {
        "stream_id": src_id or _synthetic_id(),
        "stream_name": name,
        "scene": scene,
        "environment": environment,
        "chat_tags": [],
        "greeting": "",
        "greeting_emotion": "",
        "style": style,
        "background_uri": "",
        "relationship": relationship,
        "response_length": 0,
        "last_update_timestamp": last_update,
    }


def _build_messages(*pairs: tuple[str | None, str, str | None]) -> list[dict]:
    """Each pair is ``(speaker_or_None, message, emotion_or_None)``.

    Returns a list of zip-format message entries with sequential timestamps
    in ms-since-epoch, mimicking the upstream layout.
    """
    out = []
    base_ts = 1_700_000_000_000
    for i, (speaker, msg, emo) in enumerate(pairs):
        entry = {
            "id": f"ar-test-{i}",
            "timestamp": base_ts + i * 1000,
            "sequence_number": i + 1,
            "event_type": "create",
            "speaker": speaker,
            "message": msg,
        }
        if emo is not None:
            entry["emotion"] = emo
        out.append(entry)
    return out


def _make_zip(contacts: list[dict]) -> bytes:
    """Build an in-memory zip from a list of contact dicts shaped like:

        {
          "meta": <meta-contact.json dict>,
          "spaces": [
            {"meta": <meta-space.json>, "messages": <messages.json list>},
            ...
          ],
        }
    """
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in contacts:
            cmeta = c["meta"]
            cdir = f"contacts/test-{cmeta['contact_id']}"
            zf.writestr(f"{cdir}/meta-contact.json", json.dumps(cmeta))
            for s in c.get("spaces", []):
                smeta = s["meta"]
                sdir = f"{cdir}/spaces/space-{smeta['stream_id']}"
                zf.writestr(f"{sdir}/meta-space.json", json.dumps(smeta))
                zf.writestr(f"{sdir}/messages.json", json.dumps(s["messages"]))
    return buf.getvalue()


def _toc_manifest(zf: zipfile.ZipFile) -> dict:
    """Drain the ``read_zip_toc`` generator and return the manifest dict
    from the terminal ``manifest`` event."""
    manifest: dict | None = None
    for ev in read_zip_toc(zf):
        if ev.get("type") == "manifest":
            manifest = ev["manifest"]
    assert manifest is not None, "read_zip_toc never yielded a manifest event"
    return manifest


def _write_zip(tmp_dir, raw: bytes, name: str = "archive.zip"):
    """Write ``raw`` to ``tmp_dir / name`` and return the ``Path``."""
    p = tmp_dir / name
    p.write_bytes(raw)
    return p


# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_aer_to_uuid_is_deterministic():
    sid = _synthetic_id()
    assert _aer_to_uuid("contact", sid) == _aer_to_uuid("contact", sid)
    # Output is 32 hex chars (uuid.hex).
    out = _aer_to_uuid("contact", sid)
    assert len(out) == 32
    assert all(c in "0123456789abcdef" for c in out)


def test_aer_to_uuid_per_kind_separation():
    sid = _synthetic_id()
    contact_uuid = _aer_to_uuid("contact", sid)
    scenario_uuid = _aer_to_uuid("scenario", sid)
    chat_uuid = _aer_to_uuid("chat", sid)
    assert len({contact_uuid, scenario_uuid, chat_uuid}) == 3


def test_aer_zip_namespace_pinned():
    # If this changes, every existing user's "already imported" detection
    # breaks on next re-import. The constant is documented as immutable.
    assert str(_AER_ZIP_NAMESPACE) == "f5a2c8e1-3d4b-4a7e-9c8d-2b1e3f4a5b6c"


def test_translate_meta_contact_field_mapping():
    src = _build_meta_contact(name="Mapped", persona="Hello world")
    out = _translate_meta_contact(src, target_uuid="forced-uuid")
    assert out["id"] == "forced-uuid"
    assert out["name"] == "Mapped"
    assert out["persona"] == "Hello world"
    assert out["appearance"] == "Test appearance"
    assert out["greeting"] == "Hi there."
    assert out["greetingEmotion"] == "neutral"
    # search_tags list → comma-joined string.
    assert out["tags"] == "alpha, beta"
    # avatar_uri → camelCase avatarUri.
    assert out["avatarUri"].startswith("data:image/png")


def test_translate_meta_contact_falls_back_to_tagline():
    src = _build_meta_contact()
    src["description"] = ""
    src["tagline"] = "Tagline only"
    assert _translate_meta_contact(src, "id")["description"] == "Tagline only"


def test_meta_space_has_overrides_empty():
    assert _meta_space_has_overrides(_build_meta_space()) is False


def test_meta_space_has_overrides_each_field():
    for kw in ("scene", "environment"):
        meta = _build_meta_space(**{kw: "non-empty"})
        assert _meta_space_has_overrides(meta), f"{kw} should trigger overrides"
    # Greeting / description / background_uri also count.
    for kw in ("greeting", "description", "background_uri"):
        meta = _build_meta_space()
        meta[kw] = "non-empty"
        assert _meta_space_has_overrides(meta), f"{kw} should trigger overrides"


def test_meta_space_has_overrides_chat_level_fields_dont_trigger():
    # The chat-level fields (relationship/style/response_length, plus the
    # space's name and tags which fold onto the chat itself) should not
    # promote a space to a scenario.
    meta = _build_meta_space(
        name="A name",  # → chat.title, not a scenario
        relationship="close",
        style="roleplay",
    )
    meta["response_length"] = 5
    meta["chat_tags"] = ["t1", "t2"]
    assert _meta_space_has_overrides(meta) is False


def test_chat_signature_basic():
    msgs = _build_messages(
        ("Char", "hi", "neutral"),
        (None, "hello", None),
    )
    sig = _chat_signature(msgs)
    assert ":" in sig
    count, last = sig.split(":")
    assert int(count) == 2
    assert int(last) > 0


def test_chat_signature_ignores_empty_messages():
    msgs = _build_messages(
        ("Char", "hi", "neutral"),
        (None, "   ", None),
    )
    count = int(_chat_signature(msgs).split(":")[0])
    assert count == 1


def test_chat_signature_empty():
    assert _chat_signature([]) == "0:0"
    assert _chat_signature(None) == "0:0"


def test_translate_messages_coalesces_consecutive():
    # 3 contact bubbles + 2 user bubbles → 2 ChatMessages.
    msgs = _build_messages(
        ("Char", "first", "neutral"),
        ("Char", "second", "happy"),
        ("Char", "third", "smug"),
        (None, "reply", None),
        (None, "another", None),
    )
    messages, selected = _translate_messages_to_chat_messages(msgs)
    assert len(messages) == 2
    assert messages[0].sender == "contact"
    assert len(messages[0].body) == 3
    assert messages[1].sender == "user"
    assert len(messages[1].body) == 2
    # parent_id chained.
    assert messages[0].parent_id is None
    assert messages[1].parent_id == messages[0].id
    # selected_child_id threads the active path from root.
    assert selected[""] == messages[0].id
    assert selected[messages[0].id] == messages[1].id


def test_translate_messages_emotion_defaults_neutral():
    msgs = _build_messages(("Char", "no emo", None))
    messages, _ = _translate_messages_to_chat_messages(msgs)
    assert messages[0].body[0].emotion.value == "neutral"


def test_translate_messages_drops_empty_bodies():
    msgs = _build_messages(
        ("Char", "", "neutral"),
        (None, "", None),
        ("Char", "real", "happy"),
    )
    messages, _ = _translate_messages_to_chat_messages(msgs)
    assert len(messages) == 1
    assert messages[0].body[0].text == "real"


def test_entity_state_not_imported_for_missing_local():
    assert _entity_state(None, "anything") == "not_imported"


def test_entity_state_not_imported_when_aer_imported_at_is_none(tmp_storage):
    # User-created contact (no aer_imported_at) shouldn't masquerade as
    # "already imported" just because its UUID happens to match.
    c = Contact(name="Manual")
    storage.save_contact(c)
    assert _entity_state(c, "anything") == "not_imported"


def test_entity_state_up_to_date(tmp_storage):
    c = Contact(name="X")
    storage.save_contact(c)
    c.aer_revision = "rev1"
    c.aer_imported_at = c.updated_at + 5  # imported_at AHEAD of updated_at
    assert _entity_state(c, "rev1") == "up_to_date"


def test_entity_state_updated_upstream(tmp_storage):
    c = Contact(name="X")
    storage.save_contact(c)
    c.aer_revision = "rev1"
    c.aer_imported_at = c.updated_at + 5
    assert _entity_state(c, "rev2") == "updated_upstream"


def test_entity_state_modified_locally(tmp_storage):
    c = Contact(name="X")
    storage.save_contact(c)
    c.aer_revision = "rev1"
    # imported_at is BEHIND updated_at by more than the 1s tolerance.
    c.aer_imported_at = c.updated_at - 5
    assert _entity_state(c, "rev1") == "modified_locally"


def test_entity_state_conflict(tmp_storage):
    c = Contact(name="X")
    storage.save_contact(c)
    c.aer_revision = "rev1"
    c.aer_imported_at = c.updated_at - 5
    assert _entity_state(c, "rev2") == "conflict"


# ---------------------------------------------------------------------------
# Manifest endpoint
# ---------------------------------------------------------------------------


def test_read_zip_toc_basic_shape(tmp_storage):
    cmeta = _build_meta_contact()
    smeta = _build_meta_space(name="Some space")
    msgs = _build_messages(
        ("Char", "Hi.", "neutral"),
        (None, "Hello.", None),
    )
    raw = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])

    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    assert len(toc["contacts"]) == 1
    c = toc["contacts"][0]
    assert c["src_id"] == cmeta["contact_id"]
    assert c["name"] == "Test Character"
    assert c["state"] == "not_imported"
    assert len(c["spaces"]) == 1
    s = c["spaces"][0]
    assert s["chat_state"] == "not_imported"
    assert s["message_count"] == 2
    # No overrides on the synthetic minimal space → scenario_uuid is None.
    assert s["scenario_uuid"] is None


def test_read_zip_toc_promotes_space_with_overrides(tmp_storage):
    cmeta = _build_meta_contact()
    smeta = _build_meta_space(scene="A bright room.")  # has_overrides
    msgs = _build_messages(("Char", "hi", "neutral"))
    raw = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    s = toc["contacts"][0]["spaces"][0]
    assert s["has_overrides"] is True
    assert s["scenario_uuid"] is not None
    assert s["scenario_state"] == "not_imported"


def test_read_zip_toc_name_collision_flagged(tmp_storage):
    # Existing contact with same name but a different UUID → name_collision.
    storage.save_contact(Contact(name="Test Character"))
    cmeta = _build_meta_contact(name="Test Character")
    raw = _make_zip([{"meta": cmeta, "spaces": []}])
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    c = toc["contacts"][0]
    assert c["state"] == "not_imported"
    assert c["name_collision"] is not None
    assert c["name_collision"]["name"] == "Test Character"


# ---------------------------------------------------------------------------
# End-to-end bulk import
# ---------------------------------------------------------------------------


async def _drain(gen):
    events = []
    async for ev in gen:
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_bulk_import_first_pass(tmp_storage):
    cmeta = _build_meta_contact()
    smeta = _build_meta_space(name="My space", scene="A scene")
    msgs = _build_messages(
        ("Char", "first", "neutral"),
        ("Char", "second", "happy"),
        (None, "reply", None),
    )
    raw = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])
    selection = {
        "contacts": [{
            "src_id": cmeta["contact_id"],
            "action": "import",
            "spaces": [{"src_id": smeta["stream_id"], "chat_action": "import"}],
        }]
    }
    events = await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), selection))
    [done] = [e for e in events if e.get("type") == "done"]
    assert done["imported"] == {"contacts": 1, "scenarios": 1, "chats": 1}

    # Contact persisted at the deterministic UUID with aer_* stamped.
    contact_uuid = _aer_to_uuid("contact", cmeta["contact_id"])
    contact = storage.get_contact(contact_uuid)
    assert contact is not None
    assert contact.aer_source_id == cmeta["contact_id"]
    assert contact.aer_revision == _meta_contact_signature(cmeta)
    assert contact.aer_imported_at is not None

    # Scenario nested under contact, with the deterministic UUID.
    scenario_uuid = _aer_to_uuid("scenario", smeta["stream_id"])
    [scen] = [cs for cs in contact.scenarios if cs.id == scenario_uuid]
    assert scen.aer_source_id == smeta["stream_id"]

    # Chat persisted, with messages coalesced (2 + 1 → 2 ChatMessages).
    chat_uuid = _aer_to_uuid("chat", smeta["stream_id"])
    chat = storage.get_chat(chat_uuid)
    assert chat is not None
    assert chat.contact_scenario_id == scenario_uuid
    assert chat.aer_revision == _chat_signature(msgs)
    persisted = storage.load_chat_messages(chat.id).messages
    assert len(persisted) == 2
    assert len(persisted[0].body) == 2
    assert len(persisted[1].body) == 1


@pytest.mark.asyncio
async def test_reimport_no_change_yields_up_to_date(tmp_storage):
    cmeta = _build_meta_contact()
    smeta = _build_meta_space(scene="S")
    msgs = _build_messages(("Char", "hi", "neutral"))
    raw = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])
    sel = {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "import",
            "spaces": [{"src_id": smeta["stream_id"], "chat_action": "import"}],
        }]
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), sel))

    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    c = toc["contacts"][0]
    assert c["state"] == "up_to_date"
    assert c["spaces"][0]["chat_state"] == "up_to_date"
    assert c["spaces"][0]["scenario_state"] == "up_to_date"


@pytest.mark.asyncio
async def test_reimport_upstream_change_yields_updated_upstream(tmp_storage):
    cmeta = _build_meta_contact(revision_id="rev-A")
    smeta = _build_meta_space(scene="S")
    msgs = _build_messages(("Char", "hi", "neutral"))
    raw1 = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])
    sel = {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "import",
            "spaces": [{"src_id": smeta["stream_id"], "chat_action": "import"}],
        }]
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw1), sel))

    # Bump the contact's revision in a fresh zip — same src_ids.
    cmeta2 = dict(cmeta)
    cmeta2["revision_data"] = dict(cmeta["revision_data"], revision_id="rev-B")
    raw2 = _make_zip([{"meta": cmeta2, "spaces": [{"meta": smeta, "messages": msgs}]}])
    with zipfile.ZipFile(BytesIO(raw2)) as zf:
        toc = _toc_manifest(zf)
    assert toc["contacts"][0]["state"] == "updated_upstream"


@pytest.mark.asyncio
async def test_reimport_local_edit_yields_modified_locally(tmp_storage):
    cmeta = _build_meta_contact()
    smeta = _build_meta_space(scene="S")
    msgs = _build_messages(("Char", "hi", "neutral"))
    raw = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])
    sel = {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "import",
            "spaces": [{"src_id": smeta["stream_id"], "chat_action": "import"}],
        }]
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), sel))

    contact_uuid = _aer_to_uuid("contact", cmeta["contact_id"])
    contact = storage.get_contact(contact_uuid)
    assert contact is not None
    # Sleep past the 1 s tolerance, then bump updated_at via a save.
    time.sleep(1.2)
    contact.description = "User-edited"
    storage.save_contact(contact)

    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    assert toc["contacts"][0]["state"] == "modified_locally"


@pytest.mark.asyncio
async def test_reimport_conflict_when_both_changed(tmp_storage):
    cmeta = _build_meta_contact(revision_id="rev-A")
    smeta = _build_meta_space(scene="S")
    msgs = _build_messages(("Char", "hi", "neutral"))
    raw1 = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])
    sel = {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "import",
            "spaces": [{"src_id": smeta["stream_id"], "chat_action": "import"}],
        }]
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw1), sel))

    contact_uuid = _aer_to_uuid("contact", cmeta["contact_id"])
    contact = storage.get_contact(contact_uuid)
    assert contact is not None
    time.sleep(1.2)
    contact.description = "Local edit"
    storage.save_contact(contact)

    cmeta2 = dict(cmeta)
    cmeta2["revision_data"] = dict(cmeta["revision_data"], revision_id="rev-B")
    raw2 = _make_zip([{"meta": cmeta2, "spaces": [{"meta": smeta, "messages": msgs}]}])
    with zipfile.ZipFile(BytesIO(raw2)) as zf:
        toc = _toc_manifest(zf)
    assert toc["contacts"][0]["state"] == "conflict"


@pytest.mark.asyncio
async def test_replace_action_overwrites_local_edits(tmp_storage):
    cmeta = _build_meta_contact()
    smeta = _build_meta_space(scene="S")
    msgs = _build_messages(("Char", "hi", "neutral"))
    raw = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])
    sel_initial = {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "import",
            "spaces": [{"src_id": smeta["stream_id"], "chat_action": "import"}],
        }]
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), sel_initial))

    contact_uuid = _aer_to_uuid("contact", cmeta["contact_id"])
    contact = storage.get_contact(contact_uuid)
    contact.description = "Local change"
    storage.save_contact(contact)

    sel_replace = {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "replace",
            "spaces": [{"src_id": smeta["stream_id"], "chat_action": "replace"}],
        }]
    }
    events = await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), sel_replace))
    [done] = [e for e in events if e.get("type") == "done"]
    assert done["imported"]["contacts"] == 1

    # Description back to the source's, aer_imported_at re-stamped.
    refreshed = storage.get_contact(contact_uuid)
    assert refreshed.description == "Test description"
    assert refreshed.aer_imported_at >= contact.aer_imported_at


@pytest.mark.asyncio
async def test_copy_action_keeps_original(tmp_storage):
    cmeta = _build_meta_contact()
    smeta = _build_meta_space(scene="S")
    msgs = _build_messages(("Char", "hi", "neutral"))
    raw = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])
    sel_initial = {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "import",
            "spaces": [{"src_id": smeta["stream_id"], "chat_action": "import"}],
        }]
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), sel_initial))
    pre_count = len(storage.list_contacts())
    chat_uuid = _aer_to_uuid("chat", smeta["stream_id"])
    pre_chat_count = len(storage.list_chats())

    sel_copy = {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "copy",
            "spaces": [{"src_id": smeta["stream_id"], "chat_action": "import"}],
        }]
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), sel_copy))

    # Original entity untouched, fresh-UUID copy alongside.
    assert storage.get_contact(_aer_to_uuid("contact", cmeta["contact_id"])) is not None
    assert len(storage.list_contacts()) == pre_count + 1
    assert len(storage.list_chats()) == pre_chat_count + 1
    # Original chat still exists at its deterministic UUID.
    assert storage.get_chat(chat_uuid) is not None


@pytest.mark.asyncio
async def test_reuse_action_appends_new_scenarios_only(tmp_storage):
    cmeta = _build_meta_contact()
    smeta_a = _build_meta_space(name="A", scene="Scene A")
    smeta_b = _build_meta_space(name="B", scene="Scene B")
    msgs_a = _build_messages(("Char", "hi A", "neutral"))
    msgs_b = _build_messages(("Char", "hi B", "neutral"))

    # First import: contact + space A only.
    raw1 = _make_zip([{
        "meta": cmeta,
        "spaces": [{"meta": smeta_a, "messages": msgs_a}],
    }])
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw1, "raw1.zip"), {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "import",
            "spaces": [{"src_id": smeta_a["stream_id"], "chat_action": "import"}],
        }]
    }))

    contact_uuid = _aer_to_uuid("contact", cmeta["contact_id"])
    pre_scenarios = len(storage.get_contact(contact_uuid).scenarios)
    assert pre_scenarios == 1

    # Second import: same contact reused, space B added.
    raw2 = _make_zip([{
        "meta": cmeta,
        "spaces": [
            {"meta": smeta_a, "messages": msgs_a},
            {"meta": smeta_b, "messages": msgs_b},
        ],
    }])
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw2, "raw2.zip"), {
        "contacts": [{
            "src_id": cmeta["contact_id"], "action": "reuse",
            "spaces": [
                {"src_id": smeta_a["stream_id"], "chat_action": "skip"},
                {"src_id": smeta_b["stream_id"], "chat_action": "import"},
            ],
        }]
    }))
    refreshed = storage.get_contact(contact_uuid)
    # A still there + B appended → 2 scenarios.
    assert len(refreshed.scenarios) == 2
    scen_a_uuid = _aer_to_uuid("scenario", smeta_a["stream_id"])
    scen_b_uuid = _aer_to_uuid("scenario", smeta_b["stream_id"])
    ids = {cs.id for cs in refreshed.scenarios}
    assert ids == {scen_a_uuid, scen_b_uuid}


# ---------------------------------------------------------------------------
# Flat-JSON zip variant
# ---------------------------------------------------------------------------


def _flat_contact_json(*, name: str = "FlatContact", id_: str | None = None) -> dict:
    return {
        "id": id_ or "test-flat-contact-001",
        "name": name,
        "description": "Imported from flat zip",
        "greeting": "Hello.",
        "greetingEmotion": "neutral",
        "relationship": "stranger",
        "style": "chat",
        "tags": "",
        "emotions": {},
        "exampleMessages": [],
    }


def _flat_user_json(*, name: str = "FlatUser", id_: str | None = None) -> dict:
    # detect_format requires either persona/appearance/tags so it doesn't
    # fall through to "scenario" or fail.
    return {
        "id": id_ or "test-flat-user-001",
        "name": name,
        "description": "User persona",
        "persona": "Test persona",
        "tags": "",
    }


def _flat_scenario_json(*, name: str = "FlatScenario", id_: str | None = None) -> dict:
    return {
        "id": id_ or "test-flat-scenario-001",
        "name": name,
        "description": "Test scenario",
        "environment": "A test room",
        "scene": "",
    }


def _flat_chat_composite(
    *, chat_id: str = "test-flat-chat-001",
    contact_name: str = "ChatChar", contact_id: str = "test-chat-char-001",
    user_name: str = "ChatUser", user_id: str = "test-chat-user-001",
) -> dict:
    return {
        "chat": {
            "id": chat_id,
            "title": "A test chat",
            "tags": "",
            "intimacy": "stranger",
            "style": "chat",
            "messages": [],
            "selectedChildId": {},
        },
        "character": _flat_contact_json(name=contact_name, id_=contact_id),
        "user": _flat_user_json(name=user_name, id_=user_id),
    }


def _make_flat_zip(files: dict[str, dict]) -> bytes:
    """``files`` is ``{path_in_zip: json_dict}``."""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, json.dumps(data))
    return buf.getvalue()


def test_zip_format_aer_bulk():
    cmeta = _build_meta_contact()
    raw = _make_zip([{"meta": cmeta, "spaces": []}])
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        assert _zip_format(zf) == "aer_bulk"


def test_zip_format_flat_json():
    raw = _make_flat_zip({"contact.json": _flat_contact_json()})
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        assert _zip_format(zf) == "flat_json"


def test_zip_format_empty():
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("README.md", "no JSONs here")
    with zipfile.ZipFile(BytesIO(buf.getvalue())) as zf:
        assert _zip_format(zf) == "empty"


def test_zip_format_mixed_prefers_aer_bulk():
    # An AER tree alongside a loose JSON: AER wins, loose file ignored.
    cmeta = _build_meta_contact()
    aer_raw = _make_zip([{"meta": cmeta, "spaces": []}])
    # Now repack with an extra loose JSON in the same zip.
    out = BytesIO()
    with zipfile.ZipFile(BytesIO(aer_raw)) as src, \
         zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info.filename))
        dst.writestr("loose.json", json.dumps(_flat_contact_json()))
    with zipfile.ZipFile(BytesIO(out.getvalue())) as zf:
        assert _zip_format(zf) == "aer_bulk"


def test_read_zip_toc_flat_shape(tmp_storage):
    raw = _make_flat_zip({
        "contact.json": _flat_contact_json(name="Alice"),
        "user.json": _flat_user_json(name="Bob"),
        "scenario.json": _flat_scenario_json(name="Lobby"),
        "chat.json": _flat_chat_composite(),
    })
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    assert toc["format"] == "flat_json"
    by_kind = {item["kind"]: item for item in toc["items"]}
    assert set(by_kind) == {"contact", "user", "scenario", "chat"}
    assert by_kind["contact"]["name"] == "Alice"
    assert by_kind["chat"]["name"] == "A test chat"
    # Chat description shows the embedded sidecar names for context.
    assert "ChatChar" in by_kind["chat"]["description"]
    # All not_imported on a clean install.
    assert all(item["state"] == "not_imported" for item in toc["items"])


def test_read_zip_toc_aer_bulk_shape_carries_format(tmp_storage):
    cmeta = _build_meta_contact()
    raw = _make_zip([{"meta": cmeta, "spaces": []}])
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    assert toc["format"] == "aer_bulk"
    assert "contacts" in toc


def test_read_zip_toc_flat_skips_unrecognised(tmp_storage):
    raw = _make_flat_zip({
        "good.json": _flat_contact_json(),
        "junk.json": {"random": "object"},
    })
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    assert toc["format"] == "flat_json"
    assert len(toc["items"]) == 1


def test_read_zip_toc_flat_state_up_to_date_when_id_matches(tmp_storage):
    # Pre-populate a contact with the same UUID the flat JSON carries.
    storage.save_contact(Contact(id="test-flat-contact-001", name="Existing"))
    raw = _make_flat_zip({"alice.json": _flat_contact_json(id_="test-flat-contact-001")})
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    [item] = toc["items"]
    assert item["state"] == "up_to_date"


@pytest.mark.asyncio
async def test_flat_import_one_contact(tmp_storage):
    raw = _make_flat_zip({
        "alice.json": _flat_contact_json(name="Alice", id_="alice-001"),
    })
    selection = {
        "format": "flat_json",
        "items": [{"path": "alice.json", "action": "import"}],
    }
    events = await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), selection))
    [done] = [e for e in events if e.get("type") == "done"]
    assert done["imported"] == {
        "contacts": 1, "users": 0, "scenarios": 0, "chats": 0, "libraries": 0,
    }
    assert storage.get_contact("alice-001") is not None
    assert storage.get_contact("alice-001").name == "Alice"


@pytest.mark.asyncio
async def test_flat_import_mixed_kinds(tmp_storage):
    raw = _make_flat_zip({
        "alice.json": _flat_contact_json(name="Alice", id_="alice-001"),
        "bob.json": _flat_user_json(name="Bob", id_="bob-001"),
        "lobby.json": _flat_scenario_json(name="Lobby", id_="lobby-001"),
    })
    selection = {
        "format": "flat_json",
        "items": [
            {"path": "alice.json", "action": "import"},
            {"path": "bob.json", "action": "import"},
            {"path": "lobby.json", "action": "import"},
        ],
    }
    events = await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), selection))
    [done] = [e for e in events if e.get("type") == "done"]
    assert done["imported"] == {
        "contacts": 1, "users": 1, "scenarios": 1, "chats": 0, "libraries": 0,
    }


@pytest.mark.asyncio
async def test_flat_import_chat_composite_creates_sidecars(tmp_storage):
    raw = _make_flat_zip({
        "chat.json": _flat_chat_composite(
            chat_id="chat-flat-1",
            contact_id="char-flat-1",
            user_id="user-flat-1",
        ),
    })
    selection = {
        "format": "flat_json",
        "items": [{"path": "chat.json", "action": "import"}],
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), selection))
    assert storage.get_chat("chat-flat-1") is not None
    assert storage.get_contact("char-flat-1") is not None
    assert storage.get_user("user-flat-1") is not None


@pytest.mark.asyncio
async def test_flat_import_chat_with_existing_sidecar_reuses_silently(tmp_storage):
    # Pre-create a contact with the same id the chat composite references.
    storage.save_contact(Contact(id="char-shared", name="PreExisting"))
    raw = _make_flat_zip({
        "chat.json": _flat_chat_composite(
            chat_id="chat-shared-1",
            contact_id="char-shared",
            user_id="user-shared-1",
        ),
    })
    pre_contacts = len(storage.list_contacts())
    selection = {
        "format": "flat_json",
        "items": [{"path": "chat.json", "action": "import"}],
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), selection))
    # Sidecar id-match → silent reuse, not duplicated.
    assert len(storage.list_contacts()) == pre_contacts
    chat = storage.get_chat("chat-shared-1")
    assert chat is not None
    assert chat.contact_id == "char-shared"


@pytest.mark.asyncio
async def test_flat_import_replace_overwrites(tmp_storage):
    storage.save_contact(Contact(id="alice-002", name="OldName"))
    raw = _make_flat_zip({
        "alice.json": _flat_contact_json(name="NewName", id_="alice-002"),
    })
    selection = {
        "format": "flat_json",
        "items": [{"path": "alice.json", "action": "replace"}],
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), selection))
    assert storage.get_contact("alice-002").name == "NewName"


@pytest.mark.asyncio
async def test_flat_import_copy_keeps_original(tmp_storage):
    storage.save_contact(Contact(id="alice-003", name="OriginalName"))
    raw = _make_flat_zip({
        "alice.json": _flat_contact_json(name="ImportName", id_="alice-003"),
    })
    selection = {
        "format": "flat_json",
        "items": [{"path": "alice.json", "action": "copy"}],
    }
    await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), selection))
    # Original alice-003 untouched; a fresh-UUID contact named "ImportName"
    # exists alongside.
    assert storage.get_contact("alice-003").name == "OriginalName"
    assert any(
        c.name == "ImportName" and c.id != "alice-003"
        for c in storage.list_contacts()
    )


@pytest.mark.asyncio
async def test_flat_import_skipped_action_is_no_op(tmp_storage):
    raw = _make_flat_zip({
        "alice.json": _flat_contact_json(id_="alice-skip"),
    })
    selection = {
        "format": "flat_json",
        "items": [{"path": "alice.json", "action": "skip"}],
    }
    events = await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), selection))
    [done] = [e for e in events if e.get("type") == "done"]
    assert done["imported"] == {
        "contacts": 0, "users": 0, "scenarios": 0, "chats": 0, "libraries": 0,
    }
    assert storage.get_contact("alice-skip") is None


@pytest.mark.asyncio
async def test_flat_import_stale_picker_downgrades_import_to_skip(tmp_storage):
    # Pre-populate a contact with the id the picker thought was free.
    storage.save_contact(Contact(id="alice-stale", name="Existing"))
    raw = _make_flat_zip({
        "alice.json": _flat_contact_json(name="WouldBeNew", id_="alice-stale"),
    })
    selection = {
        "format": "flat_json",
        "items": [{"path": "alice.json", "action": "import"}],
    }
    events = await _drain(import_zip_streaming(_write_zip(tmp_storage, raw), selection))
    [done] = [e for e in events if e.get("type") == "done"]
    # Defensive downgrade: nothing imported, original untouched.
    assert done["imported"]["contacts"] == 0
    assert storage.get_contact("alice-stale").name == "Existing"


# ---------------------------------------------------------------------------
# Streaming / staging / preview proxy
# ---------------------------------------------------------------------------


def test_clean_temp_dir_wipes_existing(tmp_storage):
    # Seed the temp dir with leftover files from a previous run.
    leftover = storage.TEMP_DIR / "zip-imports" / "abc"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "archive.zip").write_bytes(b"junk")
    assert (leftover / "archive.zip").exists()

    storage._clean_temp_dir()

    assert not (leftover / "archive.zip").exists()
    assert storage.TEMP_DIR.exists()


def test_clean_temp_dir_runs_on_initialize(tmp_storage):
    leftover = storage.TEMP_DIR / "zip-imports" / "abc"
    leftover.mkdir(parents=True, exist_ok=True)
    (leftover / "archive.zip").write_bytes(b"junk")

    # Re-running initialize wipes /.tmp without disturbing entity dirs.
    storage.initialize()
    assert not (leftover / "archive.zip").exists()
    assert storage.TEMP_DIR.exists()


def test_read_zip_toc_emits_progress_events_aer(tmp_storage):
    # 1 contact + 1 space → 1 meta-contact parse + 1 meta-space parse +
    # 1 messages.json summary parse = 3 progress ticks plus the final
    # "Done" tick.
    cmeta = _build_meta_contact()
    smeta = _build_meta_space(scene="S")
    msgs = _build_messages(("Char", "hi", "neutral"))
    raw = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])

    events = []
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        for ev in read_zip_toc(zf):
            events.append(ev)

    progresses = [e for e in events if e.get("type") == "progress"]
    manifests = [e for e in events if e.get("type") == "manifest"]
    assert len(manifests) == 1
    # Total advertised matches what we count from the archive shape.
    assert progresses[0]["total"] == 3
    # Last progress event reports current == total (the "Done" tick).
    assert progresses[-1]["current"] == progresses[-1]["total"] == 3
    # Progress comes before the manifest event.
    assert events[-1]["type"] == "manifest"


def test_read_zip_toc_emits_progress_events_flat(tmp_storage):
    raw = _make_flat_zip({
        "alice.json": _flat_contact_json(name="A"),
        "bob.json": _flat_user_json(name="B"),
    })
    events = []
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        for ev in read_zip_toc(zf):
            events.append(ev)

    progresses = [e for e in events if e.get("type") == "progress"]
    assert progresses[0]["total"] == 2
    assert events[-1]["type"] == "manifest"


def test_aer_search_haystack_includes_child_text():
    cmeta = _build_meta_contact(name="Alice")
    cmeta["description"] = "A friendly tester"
    cmeta["search_tags"] = ["happy", "tag"]
    space_metas = [
        _build_meta_space(name="HiddenSpaceName", scene="A bright park"),
    ]
    h = _aer_search_haystack(cmeta, space_metas)
    assert "alice" in h
    assert "friendly" in h
    assert "happy" in h            # tag content
    assert "hiddenspacename" in h  # bubbles up from child stream_name
    assert "bright park" in h       # bubbles up from child scene


def test_flat_search_haystack_per_kind():
    contact = {"name": "Alice", "description": "A nice character",
               "tags": "kind, gentle"}
    h = _flat_search_haystack(contact, "contact")
    assert "alice" in h and "nice character" in h and "kind" in h

    user = {"name": "Bob", "persona": "A supportive friend"}
    h = _flat_search_haystack(user, "user")
    assert "bob" in h and "supportive friend" in h

    scenario = {"name": "Park", "environment": "A wooded clearing", "scene": "Sunny"}
    h = _flat_search_haystack(scenario, "scenario")
    assert "park" in h and "wooded clearing" in h and "sunny" in h

    chat = {
        "chat": {"title": "Catchup"},
        "character": {"name": "Alice"},
        "user": {"name": "Bob"},
    }
    h = _flat_search_haystack(chat, "chat")
    assert "catchup" in h and "alice" in h and "bob" in h


def test_maybe_proxy_avatar_inline_small_data_uri_passes_through():
    item = {"path": "alice.json", "avatar_uri": "data:image/png;base64,iVBORw0KGgo="}
    _maybe_proxy_avatar(item, "abcd1234")
    # Below the 4 KB inline threshold — no proxy needed.
    assert item["avatar_uri"].startswith("data:image/png")


def test_maybe_proxy_avatar_large_data_uri_proxied():
    big = "data:image/png;base64," + "A" * 5000
    item = {"path": "alice.json", "avatar_uri": big}
    _maybe_proxy_avatar(item, "deadbeefcafe1234deadbeefcafe1234")
    assert item["avatar_uri"].startswith("/api/import-zip-preview/")
    assert "path=alice.json" in item["avatar_uri"]


def test_maybe_proxy_avatar_http_url_always_proxied():
    item = {"path": "x.json", "avatar_uri": "https://example.com/big.png"}
    _maybe_proxy_avatar(item, "deadbeefcafe1234deadbeefcafe1234")
    assert item["avatar_uri"].startswith("/api/import-zip-preview/")


def test_maybe_proxy_avatar_no_path_no_op():
    item = {"avatar_uri": "https://example.com/big.png"}
    _maybe_proxy_avatar(item, "abcd")
    # Without ``path`` the proxy can't look the source up — leave alone.
    assert item["avatar_uri"] == "https://example.com/big.png"


def test_decorate_manifest_aer_swaps_contact_avatar(tmp_storage):
    # Contact with a >4KB data-URI avatar should get its avatar_uri
    # rewritten to a proxy URL.
    big = "data:image/png;base64," + "A" * 5000
    cmeta = _build_meta_contact(avatar_uri=big)
    raw = _make_zip([{"meta": cmeta, "spaces": []}])
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        manifest = _toc_manifest(zf)
    token = "deadbeefcafe1234deadbeefcafe1234"
    _decorate_manifest(token, manifest)
    [c] = manifest["contacts"]
    assert c["avatar_uri"].startswith(f"/api/import-zip-preview/{token}")


# ---- Route-level tests via TestClient ----


def _client():
    from fastapi.testclient import TestClient
    from server.main import app

    return TestClient(app)


def test_upload_endpoint_streams_to_disk(tmp_storage):
    cmeta = _build_meta_contact()
    raw = _make_zip([{"meta": cmeta, "spaces": []}])
    client = _client()
    r = client.post("/api/import-zip-upload", files={"file": ("a.zip", raw, "application/zip")})
    assert r.status_code == 200
    token = r.json()["token"]
    assert len(token) == 32
    archive = storage.TEMP_DIR / "zip-imports" / token / "archive.zip"
    assert archive.exists()
    assert archive.read_bytes() == raw


def test_toc_endpoint_streams_progress_then_manifest(tmp_storage):
    cmeta = _build_meta_contact()
    smeta = _build_meta_space(scene="S")
    msgs = _build_messages(("Char", "hi", "neutral"))
    raw = _make_zip([{"meta": cmeta, "spaces": [{"meta": smeta, "messages": msgs}]}])
    client = _client()
    token = client.post(
        "/api/import-zip-upload",
        files={"file": ("a.zip", raw, "application/zip")},
    ).json()["token"]

    # Stream the SSE response. ``stream`` keeps the response open for the
    # duration of the with-block; we accumulate the body and parse events.
    with client.stream("GET", f"/api/import-zip-toc/{token}") as r:
        body = b"".join(r.iter_bytes())
    text = body.decode("utf-8")
    assert "event: progress" in text
    assert "event: manifest" in text
    # The manifest event carries the final manifest object.
    block = text.split("event: manifest", 1)[1]
    data_line = next(l for l in block.splitlines() if l.startswith("data: "))
    manifest = json.loads(data_line[len("data: "):])["manifest"]
    assert manifest["format"] == "aer_bulk"
    assert len(manifest["contacts"]) == 1


def test_cancel_endpoint_wipes_staging(tmp_storage):
    cmeta = _build_meta_contact()
    raw = _make_zip([{"meta": cmeta, "spaces": []}])
    client = _client()
    token = client.post(
        "/api/import-zip-upload",
        files={"file": ("a.zip", raw, "application/zip")},
    ).json()["token"]
    target_dir = storage.TEMP_DIR / "zip-imports" / token
    assert target_dir.exists()

    r = client.delete(f"/api/import-zip-token/{token}")
    assert r.status_code == 204
    assert not target_dir.exists()


def test_cancel_endpoint_rejects_bad_token(tmp_storage):
    client = _client()
    # Out-of-shape token (uppercase / non-hex) is a silent no-op — refused
    # at the regex check inside ``_wipe_token``. Verifies the endpoint
    # doesn't fall through to a destructive default.
    r = client.delete("/api/import-zip-token/" + "Z" * 32)
    assert r.status_code == 204
    assert storage.CONTACTS_DIR.exists()


def test_import_endpoint_consumes_token_and_wipes_staging(tmp_storage):
    cmeta = _build_meta_contact()
    raw = _make_zip([{"meta": cmeta, "spaces": []}])
    client = _client()
    token = client.post(
        "/api/import-zip-upload",
        files={"file": ("a.zip", raw, "application/zip")},
    ).json()["token"]
    target_dir = storage.TEMP_DIR / "zip-imports" / token

    selection = {
        "format": "aer_bulk",
        "contacts": [{"src_id": cmeta["contact_id"], "action": "import", "spaces": []}],
    }
    with client.stream(
        "POST", "/api/import-zip",
        json={"token": token, "selection": selection},
    ) as r:
        body = b"".join(r.iter_bytes()).decode("utf-8")
    assert "event: done" in body
    # Staging dir deleted on completion.
    assert not target_dir.exists()


def test_preview_endpoint_serves_data_uri_thumbnail(tmp_storage):
    # Build a real PNG so the preview encoder has something to work on.
    from PIL import Image
    im = Image.new("RGB", (200, 150), (200, 100, 50))
    buf_png = BytesIO()
    im.save(buf_png, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf_png.getvalue()).decode()

    cmeta = _build_meta_contact(avatar_uri=data_uri)
    raw = _make_zip([{"meta": cmeta, "spaces": []}])
    client = _client()
    token = client.post(
        "/api/import-zip-upload",
        files={"file": ("a.zip", raw, "application/zip")},
    ).json()["token"]
    cmeta_path = next(
        n for n in zipfile.ZipFile(BytesIO(raw)).namelist()
        if n.endswith("/meta-contact.json")
    )

    r = client.get(f"/api/import-zip-preview/{token}", params={"path": cmeta_path})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert len(r.content) > 0
    assert len(r.content) < 5000  # tiny thumbnail

    # Cached file lands on disk; second hit serves from cache.
    cache_dir = storage.TEMP_DIR / "zip-imports" / token / "previews"
    assert cache_dir.exists()
    assert any(cache_dir.iterdir())
    r2 = client.get(f"/api/import-zip-preview/{token}", params={"path": cmeta_path})
    assert r2.status_code == 200
    assert r2.content == r.content


def test_preview_endpoint_rejects_unknown_token(tmp_storage):
    client = _client()
    r = client.get(
        "/api/import-zip-preview/00000000000000000000000000000000",
        params={"path": "anything"},
    )
    assert r.status_code == 404


def test_preview_endpoint_rejects_path_traversal(tmp_storage):
    cmeta = _build_meta_contact()
    raw = _make_zip([{"meta": cmeta, "spaces": []}])
    client = _client()
    token = client.post(
        "/api/import-zip-upload",
        files={"file": ("a.zip", raw, "application/zip")},
    ).json()["token"]

    # Paths not in the archive 404 — including any traversal attempt.
    r = client.get(
        f"/api/import-zip-preview/{token}",
        params={"path": "../../../etc/passwd"},
    )
    assert r.status_code == 404

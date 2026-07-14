"""Per-chat generation overrides round-trip through single-chat JSON
export/import. Chat export is field-explicit, so the three override fields must
be carried by ``export_chat`` and read back by the importer."""
from __future__ import annotations

import asyncio

from server import storage
from server.exporters import export_chat
from server.importers import import_streaming
from server.models import (
    Chat, ChatBookmarks, ChatMessages, Contact, ContextPreset, Preset, User,
)


def _drain(agen):
    async def run():
        out = []
        async for ev in agen:
            out.append(ev)
        return out
    return asyncio.run(run())


def _make_chat() -> str:
    contact = storage.save_contact(Contact(name="Alice"))
    user = storage.save_user(User(name="Me"))
    chat = Chat(
        contact_id=contact.id,
        user_id=user.id,
        title="Override chat",
        provider_override="openai_compatible:abc",
        model_overrides={"openai_compatible:abc": "gpt-x", "novelai": "nai-y"},
        context_preset_override="cp-123",
    )
    storage.save_chat_with_all(chat, ChatMessages(), ChatBookmarks())
    return chat.id


def test_export_chat_carries_overrides(tmp_storage):
    chat_id = _make_chat()
    payload = export_chat(chat_id)["chat"]
    assert payload["providerOverride"] == "openai_compatible:abc"
    assert payload["modelOverrides"] == {
        "openai_compatible:abc": "gpt-x", "novelai": "nai-y",
    }
    assert payload["contextPresetOverride"] == "cp-123"


def test_import_chat_restores_overrides(tmp_storage):
    chat_id = _make_chat()
    payload = export_chat(chat_id)

    events = _drain(import_streaming(
        payload, mode="copy", resolutions={"character": "new", "user": "new"},
    ))
    done = next(e for e in events if e.get("type") == "done")
    new_chat = storage.get_chat(done["id"])
    assert new_chat is not None
    assert new_chat.provider_override == "openai_compatible:abc"
    assert new_chat.model_overrides == {
        "openai_compatible:abc": "gpt-x", "novelai": "nai-y",
    }
    assert new_chat.context_preset_override == "cp-123"


def _make_chat_with_presets() -> tuple[str, str, str]:
    """Chat pinning a real generation Preset + a real ContextPreset override."""
    contact = storage.save_contact(Contact(name="Alice"))
    user = storage.save_user(User(name="Me"))
    preset = Preset(name="MyPreset", temperature=0.7, top_k=40, max_new_tokens=999)
    lib = storage.load_presets()
    lib.presets.append(preset)
    storage.save_presets(lib)
    cp = ContextPreset(name="MyCtx")
    storage.save_context_preset(cp)
    chat = Chat(
        contact_id=contact.id, user_id=user.id, title="Bundled",
        preset_id=preset.id, context_preset_override=cp.id,
    )
    storage.save_chat_with_all(chat, ChatMessages(), ChatBookmarks())
    return chat.id, preset.id, cp.id


def test_export_bundles_preset_and_context_preset(tmp_storage):
    chat_id, preset_id, cp_id = _make_chat_with_presets()
    payload = export_chat(chat_id)
    assert payload["chat"]["presetId"] == preset_id
    assert payload["preset"]["id"] == preset_id
    assert payload["preset"]["name"] == "MyPreset"
    assert payload["preset"]["maxNewTokens"] == 999
    assert payload["contextPreset"]["id"] == cp_id
    assert payload["contextPreset"]["kind"] == "context_preset"


def test_import_recreates_bundled_presets(tmp_storage):
    """On a machine missing the bundled presets, import recreates them (by their
    original ids) and the new chat points at them."""
    chat_id, preset_id, cp_id = _make_chat_with_presets()
    payload = export_chat(chat_id)
    # Simulate a target machine lacking both presets.
    lib = storage.load_presets()
    lib.presets = [p for p in lib.presets if p.id != preset_id]
    storage.save_presets(lib)
    storage.delete_context_preset(cp_id)
    assert not any(p.id == preset_id for p in storage.load_presets().presets)
    assert storage.get_context_preset(cp_id) is None

    events = _drain(import_streaming(
        payload, mode="copy", resolutions={"character": "new", "user": "new"},
    ))
    new_chat = storage.get_chat(next(e for e in events if e.get("type") == "done")["id"])
    # Recreated with their original ids...
    recreated = next(p for p in storage.load_presets().presets if p.id == preset_id)
    assert recreated.max_new_tokens == 999
    assert storage.get_context_preset(cp_id) is not None
    # ...and the new chat references them.
    assert new_chat.preset_id == preset_id
    assert new_chat.context_preset_override == cp_id


def test_import_reuses_existing_bundled_preset(tmp_storage):
    """When the bundled preset id already exists locally, import reuses it
    (idempotent) rather than duplicating or clobbering the local copy."""
    chat_id, preset_id, cp_id = _make_chat_with_presets()
    payload = export_chat(chat_id)
    # Locally edit the existing preset; a re-import must NOT overwrite it.
    lib = storage.load_presets()
    next(p for p in lib.presets if p.id == preset_id).name = "Locally Renamed"
    storage.save_presets(lib)
    before = len(storage.load_presets().presets)

    _drain(import_streaming(
        payload, mode="copy", resolutions={"character": "new", "user": "new"},
    ))
    after = storage.load_presets().presets
    assert len(after) == before  # no duplicate appended
    assert next(p for p in after if p.id == preset_id).name == "Locally Renamed"

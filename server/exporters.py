"""Produce JSON exports compatible with the original on-disk export
format, so files round-trip cleanly between this server and external tools
that consume the same shape."""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

from server import external_formats, storage
from server.models import (
    Bookmark,
    BrainLibrary,
    Chat,
    ChatMessage,
    Contact,
    ContactScenario,
    ContextPreset,
    Emotion,
    Preset,
    Scenario,
    User,
)
from server.routers.files import _MIME_BY_EXT


log = logging.getLogger("aether.exporters")


def _file_to_data_uri(path: Path) -> str | None:
    if not path.exists():
        return None
    ext = path.suffix.lower().lstrip(".")
    mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _emotion_value(e: Emotion | None) -> str:
    return e.value if e is not None else ""


def _example_chats_to_aer(example_chats: list) -> list:
    out = []
    for ec in example_chats:
        msgs = []
        for m in ec.messages:
            entry: dict[str, Any] = {
                "isContact": m.is_contact,
                "text": m.text,
            }
            if m.emotion is not None:
                entry["emotion"] = m.emotion.value
            msgs.append(entry)
        out.append({
            "name": ec.name,
            "messages": msgs,
            "userName": ec.user_name,
            "style": ec.style.value,
        })
    return out


def _brain_key_to_aer(k) -> dict:
    """Serialise a BrainKey, omitting default-valued fields so older
    importers (and human readers) see a clean shape."""
    out: dict[str, Any] = {"pattern": k.pattern, "is_regex": k.is_regex}
    if k.case_sensitive:
        out["case_sensitive"] = True
    if k.match_whole_words:
        out["match_whole_words"] = True
    if k.search_range is not None:
        out["search_range"] = k.search_range
    if k.search_messages is not None:
        out["search_messages"] = k.search_messages
    return out


def _brains_to_aer(brains: list) -> list[dict]:
    out: list[dict] = []
    for b in brains:
        d: dict = {"id": b.id, "name": b.name, "content": b.content}
        if b.keys:
            d["keys"] = [_brain_key_to_aer(k) for k in b.keys]
        if b.cascades:
            d["cascades"] = True
        if b.blocks_recursion:
            d["blocks_recursion"] = True
        if b.disabled:
            d["disabled"] = True
        if b.advanced is not None:
            d["advanced"] = b.advanced.model_dump()
        out.append(d)
    return out


def _reminder_brain_to_aer(rb) -> dict | None:
    if rb is None:
        return None
    out: dict[str, Any] = {
        "id": rb.id,
        "name": rb.name,
        "content": rb.content,
        "depth": rb.depth,
    }
    if rb.disabled:
        out["disabled"] = True
    return out


def _contact_scenario_to_aer(cs: ContactScenario, *, is_default: bool) -> dict:
    return {
        "name": cs.name,
        "description": cs.description,
        "environment": cs.environment,
        "scene": cs.scene,
        "tags": cs.tags,
        "cjk": cs.cjk,
        "greeting": cs.greeting,
        "greeting_emotion": _emotion_value(cs.greeting_emotion),
        "style": cs.style.value if cs.style is not None else None,
        "intimacy": cs.intimacy.value if cs.intimacy is not None else None,
        "response_length": cs.response_length.value if cs.response_length is not None else None,
        "is_default": is_default,
        "brains": _brains_to_aer(cs.brains),
    }


# ---------------------------------------------------------------------------
# Contact
# ---------------------------------------------------------------------------


def export_contact(contact: Contact) -> dict:
    """Produce a character JSON in the on-disk export shape, with images
    embedded as data URIs."""
    contact_dir = storage.contact_dir(contact.id)
    avatar_uri = ""
    if contact_dir is not None and contact.avatar:
        avatar_uri = _file_to_data_uri(contact_dir / contact.avatar) or ""
    card_uri = ""
    if contact_dir is not None and contact.card_image:
        card_uri = _file_to_data_uri(contact_dir / contact.card_image) or ""

    emotions: dict[str, str] = {}
    if contact_dir is not None:
        em_dir = contact_dir / "emotions"
        for emotion_name, fname in contact.emotions.items():
            uri = _file_to_data_uri(em_dir / fname)
            if uri is not None:
                emotions[emotion_name] = uri

    payload: dict[str, Any] = {
        "kind": "contact",
        "id": contact.id,
        "name": contact.name,
        "description": contact.description,
        "author": contact.author,
        "gndr": contact.gender,
        "pronouns": contact.pronouns,
        "species": contact.species,
        "tags": contact.tags,
        "persona": contact.persona,
        "appearance": contact.appearance,
        "greeting": contact.greeting,
        "greetingEmotion": _emotion_value(contact.greeting_emotion),
        "exampleMessages": _example_chats_to_aer(contact.example_chats),
        "emotions": emotions,
        "avatarUri": avatar_uri,
        "relationship": contact.default_intimacy.value,
        "responseLength": (
            contact.default_response_length.value
            if contact.default_response_length is not None
            else "unspecified"
        ),
        "style": contact.default_style.value,
        "cjk": contact.cjk,
        "favorite": contact.favorite,
        "brains": _brains_to_aer(contact.brains),
    }
    if card_uri:
        payload["cardImageUri"] = card_uri
    reminder = _reminder_brain_to_aer(contact.reminder_brain)
    if reminder is not None:
        payload["reminderBrain"] = reminder
    if contact.scenarios:
        payload["scenarios"] = [
            _contact_scenario_to_aer(cs, is_default=(cs.id == contact.default_scenario_id))
            for cs in contact.scenarios
        ]
    # Crop rectangles. We emit our own ``avatarCrop`` / ``emotionsCrop`` keys
    # plus the legacy ``facePosition`` field (which only knows about the
    # emotions crop) so files round-trip with consumers of either shape.
    if contact.avatar_crop is not None:
        c = contact.avatar_crop
        payload["avatarCrop"] = {"x": c.x, "y": c.y, "w": c.w, "h": c.h}
    if contact.emotions_crop is not None:
        c = contact.emotions_crop
        payload["emotionsCrop"] = {"x": c.x, "y": c.y, "w": c.w, "h": c.h}
        payload["facePosition"] = {"x": c.x, "y": c.y, "width": c.w, "height": c.h}
    return payload


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


def export_user(user: User) -> dict:
    user_root = storage.user_dir(user.id)
    avatar_uri = ""
    if user_root is not None and user.avatar:
        avatar_uri = _file_to_data_uri(user_root / user.avatar) or ""
    card_uri = ""
    if user_root is not None and user.card_image:
        card_uri = _file_to_data_uri(user_root / user.card_image) or ""
    payload: dict[str, Any] = {
        "kind": "user",
        "id": user.id,
        "name": user.name,
        "description": user.description,
        "author": user.author,
        "species": user.species,
        "gender": user.gender,
        "pronouns": user.pronouns,
        "persona": user.persona,
        "appearance": user.appearance,
        "tags": user.tags,
        "cjk": user.cjk,
        "favorite": user.favorite,
        "avatarUri": avatar_uri,
        "brains": _brains_to_aer(user.brains),
    }
    if card_uri:
        payload["cardImageUri"] = card_uri
    if user.avatar_crop is not None:
        c = user.avatar_crop
        payload["avatarCrop"] = {"x": c.x, "y": c.y, "w": c.w, "h": c.h}
    return payload


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


def export_scenario(scenario: Scenario) -> dict:
    sdir = storage.scenario_dir(scenario.id)
    background_uri = ""
    if sdir is not None and scenario.background_image:
        background_uri = _file_to_data_uri(sdir / scenario.background_image) or ""
    card_uri = ""
    if sdir is not None and scenario.card_image:
        card_uri = _file_to_data_uri(sdir / scenario.card_image) or ""

    payload: dict[str, Any] = {
        "kind": "scenario",
        "id": scenario.id,
        "name": scenario.name,
        "description": scenario.description,
        "author": scenario.author,
        "environment": scenario.environment,
        "scene": scenario.scene,
        "tags": scenario.tags,
        "cjk": scenario.cjk,
        "favorite": scenario.favorite,
        "brains": _brains_to_aer(scenario.brains),
        "backgroundImageUri": background_uri,
        "backgroundMode": scenario.background_mode,
        "backgroundBlur": scenario.background_blur,
        "backgroundDim": scenario.background_dim,
        "backgroundBrighten": scenario.background_brighten,
        "backgroundTintStrength": scenario.background_tint_strength,
    }
    if card_uri:
        payload["cardImageUri"] = card_uri
    if scenario.avatar_crop is not None:
        c = scenario.avatar_crop
        payload["avatarCrop"] = {"x": c.x, "y": c.y, "w": c.w, "h": c.h}
    if scenario.background_focal is not None:
        fx, fy = scenario.background_focal
        payload["backgroundFocal"] = {"x": fx, "y": fy}
    return payload


# ---------------------------------------------------------------------------
# Brain library
# ---------------------------------------------------------------------------


def export_brain_library(library: BrainLibrary) -> dict:
    """Per-resource library export. Avatar embedded as a data URI.

    Carries a ``"kind": "brain_library"`` marker so :func:`importers.detect_format`
    can match unambiguously — library shape would otherwise collide with the
    user-persona heuristic.
    """
    ldir = storage.brain_library_dir(library.id)
    avatar_uri = ""
    if ldir is not None and library.avatar:
        avatar_uri = _file_to_data_uri(ldir / library.avatar) or ""
    card_uri = ""
    if ldir is not None and library.card_image:
        card_uri = _file_to_data_uri(ldir / library.card_image) or ""
    payload: dict[str, Any] = {
        "kind": "brain_library",
        "id": library.id,
        "name": library.name,
        "description": library.description,
        "author": library.author,
        "tags": library.tags,
        "favorite": library.favorite,
        "avatarUri": avatar_uri,
        "brains": _brains_to_aer(library.brains),
    }
    if card_uri:
        payload["cardImageUri"] = card_uri
    if library.avatar_crop is not None:
        c = library.avatar_crop
        payload["avatarCrop"] = {"x": c.x, "y": c.y, "w": c.w, "h": c.h}
    return payload


# ---------------------------------------------------------------------------
# Card-image export (PNG with embedded AER JSON)
# ---------------------------------------------------------------------------


def export_contact_card(contact: Contact) -> bytes | None:
    """Re-encode the contact's stored card image as PNG with the contact's
    JSON export baked into an ``aertavern_data`` tEXt chunk.

    Returns ``None`` when the contact has no card image set. Any image
    format Pillow can decode is accepted as input; the output is always
    PNG (we're rewriting the image anyway to embed the chunk).
    """
    if not contact.card_image:
        return None
    contact_dir = storage.contact_dir(contact.id)
    if contact_dir is None:
        return None
    src = contact_dir / contact.card_image
    if not src.exists():
        return None
    return external_formats.write_png_with_data(src.read_bytes(), export_contact(contact))


def export_user_card(user: User) -> bytes | None:
    if not user.card_image:
        return None
    udir = storage.user_dir(user.id)
    if udir is None:
        return None
    src = udir / user.card_image
    if not src.exists():
        return None
    return external_formats.write_png_with_data(src.read_bytes(), export_user(user))


def export_scenario_card(scenario: Scenario) -> bytes | None:
    if not scenario.card_image:
        return None
    sdir = storage.scenario_dir(scenario.id)
    if sdir is None:
        return None
    src = sdir / scenario.card_image
    if not src.exists():
        return None
    return external_formats.write_png_with_data(src.read_bytes(), export_scenario(scenario))


def export_preset(preset: Preset) -> dict:
    """Generation-preset (sampling params) export. Bundled with a chat export so
    the chat's pinned preset travels with it. Flat — presets carry no images."""
    return {
        "kind": "preset",
        "id": preset.id,
        "name": preset.name,
        "temperature": preset.temperature,
        "topP": preset.top_p,
        "topK": preset.top_k,
        "minP": preset.min_p,
        "maxContextTokens": preset.max_context_tokens,
        "rolloverWindowTokens": preset.rollover_window_tokens,
        "maxNewTokens": preset.max_new_tokens,
        "presencePenalty": preset.presence_penalty,
        "frequencyPenalty": preset.frequency_penalty,
    }


def export_context_preset(preset: ContextPreset) -> dict:
    """Per-resource context-preset export. Avatar + card embedded as data URIs.

    Carries a ``"kind": "context_preset"`` marker so the importer's format
    detection is unambiguous (the preset's shape would otherwise be
    indistinguishable from arbitrary JSON without heuristics)."""
    pdir = storage.context_preset_dir(preset.id)
    avatar_uri = ""
    if pdir is not None and preset.avatar:
        avatar_uri = _file_to_data_uri(pdir / preset.avatar) or ""
    card_uri = ""
    if pdir is not None and preset.card_image:
        card_uri = _file_to_data_uri(pdir / preset.card_image) or ""
    payload: dict[str, Any] = {
        "kind": "context_preset",
        "id": preset.id,
        "name": preset.name,
        "description": preset.description,
        "author": preset.author,
        "favorite": preset.favorite,
        "prefixNames": preset.prefix_names,
        "avatarUri": avatar_uri,
        "systemPromptBlocks": [
            {
                "id": b.id,
                "name": b.name,
                "enabled": b.enabled,
                "content": b.content,
            }
            for b in preset.system_prompt_blocks
        ],
        "additionalMessages": [
            {
                "id": m.id,
                "name": m.name,
                "enabled": m.enabled,
                "role": m.role,
                "mode": m.mode,
                "simpleContent": m.simple_content,
                "blocks": [
                    {
                        "id": b.id,
                        "name": b.name,
                        "enabled": b.enabled,
                        "content": b.content,
                    }
                    for b in m.blocks
                ],
                "floatEnabled": m.float_enabled,
                "floatDepth": m.float_depth,
            }
            for m in preset.additional_messages
        ],
    }
    if card_uri:
        payload["cardImageUri"] = card_uri
    if preset.avatar_crop is not None:
        c = preset.avatar_crop
        payload["avatarCrop"] = {"x": c.x, "y": c.y, "w": c.w, "h": c.h}
    return payload


def export_context_preset_card(preset: ContextPreset) -> bytes | None:
    if not preset.card_image:
        return None
    pdir = storage.context_preset_dir(preset.id)
    if pdir is None:
        return None
    src = pdir / preset.card_image
    if not src.exists():
        return None
    return external_formats.write_png_with_data(
        src.read_bytes(), export_context_preset(preset),
    )


def export_brain_library_card(library: BrainLibrary) -> bytes | None:
    if not library.card_image:
        return None
    ldir = storage.brain_library_dir(library.id)
    if ldir is None:
        return None
    src = ldir / library.card_image
    if not src.exists():
        return None
    return external_formats.write_png_with_data(src.read_bytes(), export_brain_library(library))


# ---------------------------------------------------------------------------
# Chat composite
# ---------------------------------------------------------------------------


def _message_to_aer(msg: ChatMessage, chat_id: str | None = None) -> dict:
    out: dict = {
        "id": msg.id,
        "parentId": msg.parent_id,
        "sender": msg.sender,
        "senderName": msg.sender_name,
        "body": [
            {
                "text": b.text,
                # Generic-mode bubbles can carry ``emotion=None``; export
                # the sentinel string so importers can round-trip it.
                "emotion": b.emotion.value if b.emotion is not None else None,
            }
            for b in msg.body
        ],
        "brains": _brains_to_aer(msg.brains),
        "timestamp": int(msg.timestamp * 1000),  # source format uses ms
        # Preserve every field that survives a Pydantic round-trip so an
        # exported chat re-imports byte-identical. Pre-exporter behavior
        # dropped these and re-imports silently coerced (origin → manual
        # for user msgs, generation metadata → None, etc.); that was
        # genuine data loss for impersonate-stamped messages, generic
        # reasoning blocks, image_refs proxy maps, and brain provenance.
        "origin": msg.origin,
        "reasoning": msg.reasoning,
        "imageRefs": dict(msg.image_refs) if msg.image_refs else {},
        "activeBrains": list(msg.active_brains) if msg.active_brains else [],
        "generationStartedAt":
            int(msg.generation_started_at * 1000)
            if msg.generation_started_at is not None else None,
        "generationDurationSeconds": msg.generation_duration_seconds,
        "provider": msg.provider,
        "model": msg.model,
        "generationPresetId": msg.generation_preset_id,
        "contextPresetId": msg.context_preset_id,
    }
    # Per-message attachments: embed each file as a base64 data URI so the
    # JSON is self-contained. Importers decode back to disk under the new
    # chat's ``attachments/`` subdir. Missing files (unlikely) are skipped
    # silently — a half-deleted attachment shouldn't break the whole export.
    if msg.attachments and chat_id is not None:
        import base64
        att_dir = storage.chat_attachments_dir(chat_id)
        atts_out: list[dict] = []
        if att_dir is not None and att_dir.exists():
            for att in msg.attachments:
                match = next(
                    (p for p in att_dir.glob(f"{att.id}.*")
                     if not p.name.endswith(".tmp")),
                    None,
                )
                if match is None:
                    continue
                try:
                    payload = match.read_bytes()
                except OSError:
                    continue
                atts_out.append({
                    "id": att.id,
                    "mime": att.mime,
                    "filename": att.filename,
                    "byteSize": att.byte_size,
                    "source": att.source,
                    "prompt": att.prompt,
                    "seed": att.seed,
                    "dataUri": f"data:{att.mime};base64,"
                               + base64.b64encode(payload).decode("ascii"),
                })
        if atts_out:
            out["attachments"] = atts_out
    return out


def _bookmark_to_aer(bm: Bookmark) -> dict:
    return {
        "id": bm.id,
        "title": bm.title,
        "snippet": bm.snippet,
        "selectedChildId": dict(bm.selected_child_id),
        "favorite": bm.favorite,
        "createdAt": int(bm.created_at * 1000),
    }


def export_chat(chat_id: str) -> dict:
    chat = storage.get_chat(chat_id)
    if chat is None:
        raise ValueError(f"chat {chat_id!r} not found")
    contact = storage.get_contact(chat.contact_id)
    user = storage.get_user(chat.user_id)
    scenario = storage.get_scenario(chat.scenario_id) if chat.scenario_id else None
    messages = storage.load_chat_messages(chat_id).messages
    bookmarks = storage.load_chat_bookmarks(chat_id).bookmarks

    # Resolve attached libraries — silently skip unknown ids so an export
    # never errors on a dangling reference (DELETE library leaves these in
    # place by design).
    libraries: list[BrainLibrary] = []
    for lid in (chat.brain_library_ids or []):
        lib = storage.get_brain_library(lid)
        if lib is not None:
            libraries.append(lib)

    # Bundle the chat's pinned generation preset + context-preset override so
    # the export is self-contained (re-import recreates them if missing). Both
    # are None when the chat inherits the global/provider default — nothing
    # chat-specific to carry.
    gen_preset = None
    if chat.preset_id:
        gen_preset = next(
            (p for p in storage.load_presets().presets if p.id == chat.preset_id),
            None,
        )
    ctx_preset = (
        storage.get_context_preset(chat.context_preset_override)
        if chat.context_preset_override else None
    )

    chat_payload = {
        "id": chat.id,
        "title": chat.title,
        "tags": chat.tags,
        "characterId": chat.contact_id,
        "userId": chat.user_id,
        "scenarioId": chat.scenario_id,
        "brainLibraryIds": list(chat.brain_library_ids or []),
        "intimacy": chat.intimacy.value,
        "style": chat.style.value,
        "responseLength": chat.response_length.value if chat.response_length else None,
        "cjk": chat.cjk,
        "favorite": chat.favorite,
        "presetId": chat.preset_id,
        "providerOverride": chat.provider_override,
        "modelOverrides": dict(chat.model_overrides),
        "contextPresetOverride": chat.context_preset_override,
        "selectedChildId": dict(chat.selected_child_id),
        "messages": [_message_to_aer(m, chat_id) for m in messages],
        "createdAt": int(chat.created_at * 1000),
        "updatedAt": int(chat.updated_at * 1000),
    }
    return {
        "kind": "chat",
        "chat": chat_payload,
        "character": export_contact(contact) if contact is not None else None,
        "user": export_user(user) if user is not None else None,
        "scenario": export_scenario(scenario) if scenario is not None else None,
        "preset": export_preset(gen_preset) if gen_preset is not None else None,
        "contextPreset": (
            export_context_preset(ctx_preset) if ctx_preset is not None else None
        ),
        "brainLibraries": [export_brain_library(lib) for lib in libraries],
        "bookmarks": [_bookmark_to_aer(b) for b in bookmarks],
    }

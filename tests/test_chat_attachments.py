"""Chat attachments: upload → bind to user message → serve → multimodal context.

Generic mode only (phase 1: images). Uploads land in
``data/chats/{slug}-{id8}/attachments/`` as opaque blobs. The client
references them by id on the message-create POST; orphans (no
matching file) are dropped. Generic context builder emits an
OpenAI-style multimodal content array for user messages with
image attachments.
"""
from __future__ import annotations

import base64
from pathlib import Path

from fastapi.testclient import TestClient

from server import storage
from server.main import app
from server.models import Contact, User


# Smallest valid PNG: 8-byte signature + IHDR + IDAT + IEND.
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


def _make_chat(client):
    contact = storage.save_contact(Contact(name="Alice"))
    user = storage.save_user(User(name="Me"))
    chat = client.post(
        "/api/chats",
        json={"contact_id": contact.id, "user_id": user.id},
    ).json()
    return chat["id"], contact, user


def test_missing_attachments_field_is_backfilled(tmp_storage):
    """Legacy messages.yaml (predating the attachments field) gets the
    field materialized on next boot's migration."""
    import msgspec.yaml
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        chat_id, _, _ = _make_chat(client)
        chat_path = storage.chat_dir(chat_id)
        msg_path = chat_path / "messages.yaml"
        # Overwrite with a single message that lacks ``attachments``.
        msg_path.write_bytes(msgspec.yaml.encode({
            "messages": [{
                "id": "x", "parent_id": None,
                "sender": "user", "sender_name": "U",
                "body": [{"text": "hi", "emotion": "neutral"}],
                # NB: no ``attachments`` key.
            }],
        }))
        storage._migrate_missing_message_attachments()
        raw = msgspec.yaml.decode(msg_path.read_bytes())
        assert "attachments" in raw["messages"][0]
        assert raw["messages"][0]["attachments"] == []


def test_pending_attachments_persist_on_chat(tmp_storage):
    """Uploads append to chat.pending_attachments; deletes remove from it;
    sending a message with attachments atomically consumes the entries
    from the pending list. The pending list is what the chip row reads
    from on chat reopen, so attachments survive a chat switch / reload."""
    with TestClient(app) as client:
        chat_id, _, _ = _make_chat(client)

        # Upload two — both land in pending.
        a1 = client.post(
            f"/api/chats/{chat_id}/attachments",
            files={"file": ("one.png", _PNG_1X1, "image/png")},
        ).json()
        a2 = client.post(
            f"/api/chats/{chat_id}/attachments",
            files={"file": ("two.png", _PNG_1X1, "image/png")},
        ).json()
        chat = storage.get_chat(chat_id)
        pending_ids = [a.id for a in chat.pending_attachments]
        assert pending_ids == [a1["id"], a2["id"]]

        # Delete one → drops from pending.
        client.delete(f"/api/chats/{chat_id}/attachments/{a1['id']}")
        chat = storage.get_chat(chat_id)
        assert [a.id for a in chat.pending_attachments] == [a2["id"]]

        # Send a message with the remaining one → server binds it to the
        # message and atomically clears it from pending.
        client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "sender": "user",
                "body": [{"text": "look", "emotion": "neutral"}],
                "attachments": [a2],
            },
        )
        chat = storage.get_chat(chat_id)
        assert chat.pending_attachments == []
        # The bound message carries the attachment.
        msgs = storage.load_chat_messages(chat_id).messages
        user_msgs = [m for m in msgs if m.sender == "user"]
        assert user_msgs[-1].attachments[0].id == a2["id"]


def test_attachment_upload_and_serve(tmp_storage):
    with TestClient(app) as client:
        chat_id, _, _ = _make_chat(client)

        up = client.post(
            f"/api/chats/{chat_id}/attachments",
            files={"file": ("cat.png", _PNG_1X1, "image/png")},
        )
        assert up.status_code == 200, up.text
        att = up.json()
        assert att["mime"] == "image/png"
        assert att["filename"] == "cat.png"
        assert att["byte_size"] == len(_PNG_1X1)

        # File served via GET.
        got = client.get(f"/api/files/chats/{chat_id}/attachments/{att['id']}")
        assert got.status_code == 200
        assert got.headers["content-type"] == "image/png"
        assert got.content == _PNG_1X1


def test_attachment_bound_to_message(tmp_storage):
    with TestClient(app) as client:
        chat_id, _, _ = _make_chat(client)
        att = client.post(
            f"/api/chats/{chat_id}/attachments",
            files={"file": ("cat.png", _PNG_1X1, "image/png")},
        ).json()

        # Create a user message carrying the attachment id.
        msg = client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "sender": "user",
                "body": [{"text": "look", "emotion": "neutral"}],
                "attachments": [att],
            },
        ).json()
        assert len(msg["attachments"]) == 1
        assert msg["attachments"][0]["id"] == att["id"]

        # Persists on disk.
        msgs = storage.load_chat_messages(chat_id).messages
        new = [m for m in msgs if m.id == msg["id"]][0]
        assert len(new.attachments) == 1
        assert new.attachments[0].mime == "image/png"


def test_attachment_orphan_id_dropped(tmp_storage):
    """An ``attachment`` id that doesn't resolve to a file on disk is
    silently dropped from the persisted message."""
    with TestClient(app) as client:
        chat_id, _, _ = _make_chat(client)
        msg = client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "sender": "user",
                "body": [{"text": "look", "emotion": "neutral"}],
                "attachments": [
                    {"id": "nonexistent", "mime": "image/png",
                     "filename": "x", "byte_size": 1},
                ],
            },
        ).json()
        assert msg["attachments"] == []


def test_attachment_delete_removes_file(tmp_storage):
    with TestClient(app) as client:
        chat_id, _, _ = _make_chat(client)
        att = client.post(
            f"/api/chats/{chat_id}/attachments",
            files={"file": ("cat.png", _PNG_1X1, "image/png")},
        ).json()
        # File exists.
        d = storage.chat_attachments_dir(chat_id)
        assert any(d.glob(f"{att['id']}.*"))

        r = client.delete(f"/api/chats/{chat_id}/attachments/{att['id']}")
        assert r.status_code == 200
        assert not any(d.glob(f"{att['id']}.*"))


def test_message_attachment_delete_failure_is_not_false_success(
    tmp_storage, monkeypatch,
):
    """A failed unlink must leave the message reference available to retry."""
    with TestClient(app) as client:
        chat_id, _, _ = _make_chat(client)
        att = client.post(
            f"/api/chats/{chat_id}/attachments",
            files={"file": ("cat.png", _PNG_1X1, "image/png")},
        ).json()
        message = client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "sender": "user",
                "body": [{"text": "look", "emotion": "neutral"}],
                "attachments": [att],
            },
        ).json()
        original = next(storage.chat_attachments_dir(chat_id).glob(f"{att['id']}.*"))
        real_unlink = Path.unlink

        def deny_original(path, *args, **kwargs):
            if path == original:
                raise PermissionError("read only")
            return real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", deny_original)
        response = client.delete(
            f"/api/chats/{chat_id}/messages/{message['id']}/attachments/{att['id']}"
        )
        assert response.status_code == 500
        stored = storage.load_chat_messages(chat_id)
        target = next(m for m in stored.messages if m.id == message["id"])
        assert [a.id for a in target.attachments] == [att["id"]]
        assert original.exists()


def test_attachment_export_import_round_trip(tmp_storage):
    """Exporting a chat with attachments embeds each as a data URI;
    re-importing decodes it back to a file on disk under the new chat's
    attachments dir."""
    import asyncio
    from server.exporters import export_chat
    from server.importers import import_chat

    with TestClient(app) as client:
        chat_id, _, _ = _make_chat(client)
        att = client.post(
            f"/api/chats/{chat_id}/attachments",
            files={"file": ("cat.png", _PNG_1X1, "image/png")},
        ).json()
        client.post(
            f"/api/chats/{chat_id}/messages",
            json={
                "sender": "user",
                "body": [{"text": "look", "emotion": "neutral"}],
                "attachments": [att],
            },
        )

        # Export → JSON carries the dataUri.
        payload = export_chat(chat_id)
        embedded = [
            a for m in payload["chat"]["messages"]
            for a in (m.get("attachments") or [])
        ]
        assert len(embedded) == 1
        assert embedded[0]["dataUri"].startswith("data:image/png;base64,")

        # Re-import → file written to the new chat's attachments dir, message
        # references the freshly-minted attachment id.
        # Strip the original id so import_chat allocates a fresh chat id.
        payload["chat"].pop("id", None)
        new_chat = asyncio.run(import_chat(payload))
        msgs = storage.load_chat_messages(new_chat.id).messages
        user_msgs = [m for m in msgs if m.sender == "user"]
        assert user_msgs
        new_att_list = user_msgs[-1].attachments
        assert len(new_att_list) == 1
        new_att = new_att_list[0]
        # Fresh id (not the original).
        assert new_att.id != att["id"]
        # File on disk under the new chat dir.
        new_dir = storage.chat_attachments_dir(new_chat.id)
        files = list(new_dir.glob(f"{new_att.id}.*"))
        assert files
        assert files[0].read_bytes() == _PNG_1X1


def test_native_export_preserves_all_message_fields(tmp_storage):
    """A round-trip through export_chat → import_chat preserves every
    field that lives on a ChatMessage: origin, reasoning, image_refs,
    active_brains, and generation metadata. Pre-fix these were silently
    dropped and re-imports coerced them to defaults (origin → "manual"
    for user msgs, metadata → None), which was actual data loss for
    impersonate-generated user messages and Generic-mode reasoning."""
    import asyncio
    from server.exporters import export_chat
    from server.importers import import_chat
    from server.models import ChatMessage, SubMessage

    with TestClient(app) as client:
        chat_id, _, _ = _make_chat(client)
        # Inject a richly-populated user-side message that mimics what
        # impersonate would produce — non-manual origin + generation
        # metadata + non-empty image_refs / active_brains.
        msgs = storage.load_chat_messages(chat_id)
        msg = ChatMessage(
            sender="user",
            sender_name="Me",
            body=[SubMessage(text="hi", emotion=None)],
            origin="generic",
            reasoning="thinking out loud",
            image_refs={"https://example.com/x.png": "uuid-1"},
            active_brains=[{"brain_id": "b1", "name": "Lore", "owner_name": "Alice"}],
            generation_started_at=1234567890.0,
            generation_duration_seconds=1.5,
            provider="openai_compatible",
            model="gpt-4o",
            generation_preset_id="gp1",
            context_preset_id="cp1",
        )
        msgs.messages.append(msg)
        chat_obj = storage.get_chat(chat_id)
        chat_obj.selected_child_id[""] = msg.id
        storage.save_chat_messages(chat_id, msgs)
        storage.save_chat(chat_obj, bump_version=False)

        # Export → re-import as a fresh chat → verify the user-side
        # message landed with all fields intact.
        payload = export_chat(chat_id)
        payload["chat"].pop("id", None)
        new_chat = asyncio.run(import_chat(payload))
        new_msgs = storage.load_chat_messages(new_chat.id).messages
        impersonated = [m for m in new_msgs if m.sender == "user"]
        assert len(impersonated) == 1
        rt = impersonated[0]
        assert rt.origin == "generic"
        assert rt.reasoning == "thinking out loud"
        assert rt.image_refs == {"https://example.com/x.png": "uuid-1"}
        assert rt.active_brains == [
            {"brain_id": "b1", "name": "Lore", "owner_name": "Alice"}
        ]
        assert rt.generation_started_at == 1234567890.0
        assert rt.generation_duration_seconds == 1.5
        assert rt.provider == "openai_compatible"
        assert rt.model == "gpt-4o"
        assert rt.generation_preset_id == "gp1"
        assert rt.context_preset_id == "cp1"


def test_generic_context_emits_multimodal_content(tmp_storage):
    """Generic mode's history builder emits an OpenAI-style multimodal
    content array (``{type:"image_url",...}, {type:"text",...}``) for user
    messages with image attachments — image first, then the text."""
    from server.generic.context import build_messages_for_generic
    from server.models import (
        Attachment, Chat, ChatMessage, ContextPreset, Preset, Settings,
        SubMessage,
    )

    contact = storage.save_contact(Contact(name="Alice"))
    user = storage.save_user(User(name="Me"))
    # Create the chat dir via the storage API so chat_attachments_dir() works.
    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
    chat_id = chat["id"]
    chat_obj = storage.get_chat(chat_id)

    # Drop a PNG in place as if uploaded via the route.
    att_dir = storage.chat_attachments_dir(chat_id)
    att_dir.mkdir(parents=True, exist_ok=True)
    att = Attachment(mime="image/png", filename="cat.png",
                     byte_size=len(_PNG_1X1))
    (att_dir / f"{att.id}.png").write_bytes(_PNG_1X1)

    user_msg = ChatMessage(
        sender="user", sender_name=user.name,
        body=[SubMessage(text="look", emotion=None)],
        attachments=[att],
    )

    settings = Settings()
    preset = ContextPreset(
        name="probe", prefix_names=False,
        system_prompt_blocks=[],
        additional_messages=[],
    )
    gen_preset = Preset(
        name="g", max_context_tokens=4096, rollover_window_tokens=512,
        max_new_tokens=256,
    )
    ctx = build_messages_for_generic(
        chat=chat_obj, contact=contact, user=user, scenario=None,
        preset=preset, generation_preset=gen_preset, history=[user_msg],
        settings=settings,
    )

    # The user message should land as a multimodal content array with
    # the image first, then the text.
    user_msgs = [m for m in ctx.api_messages if m.get("role") == "user"]
    assert user_msgs, ctx.api_messages
    content = user_msgs[-1]["content"]
    assert isinstance(content, list), content
    assert content[0]["type"] == "image_url", content
    assert content[-1]["type"] == "text", content
    assert content[-1]["text"] == "look"
    image_part = content[0]
    assert image_part["image_url"]["url"].startswith("data:image/png;base64,")
    # Round-trip the base64 back and confirm it's the PNG bytes.
    payload = image_part["image_url"]["url"].split("base64,", 1)[1]
    assert base64.b64decode(payload) == _PNG_1X1


# --- Image compression (Settings.compress_images) -------------------------


_PHOTO_PNG_CACHE: dict[int, bytes] = {}


def _photo_png(size: int = 256) -> bytes:
    """A noisy-gradient image: lossless PNG must store the per-pixel noise
    (so it stays large), while JPEG's lossy DCT discards it (so it shrinks).
    That makes the compressor's "smaller wins" choice land on the JPEG — the
    1x1 ``_PNG_1X1`` would instead lose to JPEG framing overhead and keep the
    original. A smooth/periodic pattern won't do: PNG row filters crush it
    below the JPEG. Seeded for reproducibility; memoised (the fill is slow)."""
    if size not in _PHOTO_PNG_CACHE:
        import io
        import random
        from PIL import Image
        rng = random.Random(20260530)
        im = Image.new("RGB", (size, size))
        px = im.load()
        for y in range(size):
            base = int(y * 255 / size)
            for x in range(size):
                px[x, y] = (
                    max(0, min(255, base + rng.randint(-20, 20))),
                    max(0, min(255, base + 30 + rng.randint(-20, 20))),
                    max(0, min(255, base + 70 + rng.randint(-20, 20))),
                )
        out = io.BytesIO()
        im.save(out, format="PNG")
        _PHOTO_PNG_CACHE[size] = out.getvalue()
    return _PHOTO_PNG_CACHE[size]


def _build_generic_ctx(chat_obj, contact, user, history, settings):
    from server.generic.context import build_messages_for_generic
    from server.models import ContextPreset, Preset
    preset = ContextPreset(
        name="probe", prefix_names=False,
        system_prompt_blocks=[], additional_messages=[],
    )
    gen_preset = Preset(
        name="g", max_context_tokens=4096, rollover_window_tokens=512,
        max_new_tokens=256,
    )
    return build_messages_for_generic(
        chat=chat_obj, contact=contact, user=user, scenario=None,
        preset=preset, generation_preset=gen_preset, history=history,
        settings=settings,
    )


def _drop_attachment(chat_id, raw_bytes, ext, mime):
    from server.models import Attachment
    att_dir = storage.chat_attachments_dir(chat_id)
    att_dir.mkdir(parents=True, exist_ok=True)
    att = Attachment(mime=mime, filename=f"img.{ext}", byte_size=len(raw_bytes))
    (att_dir / f"{att.id}.{ext}").write_bytes(raw_bytes)
    return att


def _user_image_url(ctx):
    user_msgs = [m for m in ctx.api_messages if m.get("role") == "user"]
    assert user_msgs, ctx.api_messages
    content = user_msgs[-1]["content"]
    assert isinstance(content, list), content
    return content[0]["image_url"]["url"]


def test_build_compressed_jpeg_shrinks_continuous_tone(tmp_storage):
    from server import imaging
    png = _photo_png()
    jpeg = imaging.build_compressed_jpeg_bytes(png, 85)
    assert jpeg[:2] == b"\xff\xd8"            # JPEG SOI marker
    assert len(jpeg) < len(png)


def test_build_compressed_jpeg_flattens_alpha_to_white(tmp_storage):
    import io
    from PIL import Image
    from server import imaging
    src = io.BytesIO()
    Image.new("RGBA", (32, 32), (10, 200, 30, 0)).save(src, format="PNG")
    out = Image.open(io.BytesIO(imaging.build_compressed_jpeg_bytes(src.getvalue(), 85)))
    assert out.mode == "RGB"
    r, g, b = out.getpixel((0, 0))
    assert r > 248 and g > 248 and b > 248   # transparent → white (JPEG-fuzzed)


def test_build_compressed_jpeg_area_cap(tmp_storage):
    """Above the area cap: downscaled aspect-ratio-preserving. Below it:
    left at native size (never upscaled)."""
    import io
    from PIL import Image
    from server import imaging

    big = io.BytesIO()
    Image.new("RGB", (3000, 2000), (120, 130, 140)).save(big, format="PNG")
    out = Image.open(io.BytesIO(imaging.build_compressed_jpeg_bytes(big.getvalue(), 85)))
    w, h = out.size
    assert w < 3000 and h < 2000                       # actually shrank
    assert w * h <= imaging.COMPRESS_MAX_AREA           # area capped
    assert abs((w / h) - (3000 / 2000)) < 0.01          # aspect preserved

    small = io.BytesIO()
    Image.new("RGB", (100, 80), (10, 20, 30)).save(small, format="PNG")
    out2 = Image.open(io.BytesIO(imaging.build_compressed_jpeg_bytes(small.getvalue(), 85)))
    assert out2.size == (100, 80)                       # never upscaled


def test_compress_images_emits_jpeg_and_caches(tmp_storage):
    """With compression on, a compressible PNG attachment is sent as JPEG,
    a single cache file lands under ``attachments/.cached/``, and the
    cache never shadows the original under the ``{id}.*`` lookup glob."""
    from server.models import ChatMessage, Settings, SubMessage
    with TestClient(app) as client:
        chat_id, contact, user = _make_chat(client)
    chat_obj = storage.get_chat(chat_id)
    att = _drop_attachment(chat_id, _photo_png(), "png", "image/png")
    msg = ChatMessage(sender="user", sender_name=user.name,
                      body=[SubMessage(text="look", emotion=None)],
                      attachments=[att])
    settings = Settings(compress_images=True, image_compression_quality=85)

    ctx = _build_generic_ctx(chat_obj, contact, user, [msg], settings)
    assert _user_image_url(ctx).startswith("data:image/jpeg;base64,")

    # Cache file under the dot-dir; the originals glob still sees ONLY the PNG.
    cache_dir = storage.chat_attachment_cache_dir(chat_id)
    assert (cache_dir / f"{att.id}.q85.jpg").exists()
    att_dir = storage.chat_attachments_dir(chat_id)
    assert sorted(p.name for p in att_dir.glob(f"{att.id}.*")) == [f"{att.id}.png"]

    # Second build hits the cache and still emits JPEG.
    ctx2 = _build_generic_ctx(chat_obj, contact, user, [msg], settings)
    assert _user_image_url(ctx2).startswith("data:image/jpeg;base64,")


def test_compress_quality_change_replaces_cache_file(tmp_storage):
    """Switching quality leaves at most one cached copy per attachment."""
    from server.models import ChatMessage, Settings, SubMessage
    with TestClient(app) as client:
        chat_id, contact, user = _make_chat(client)
    chat_obj = storage.get_chat(chat_id)
    att = _drop_attachment(chat_id, _photo_png(), "png", "image/png")
    msg = ChatMessage(sender="user", sender_name=user.name,
                      body=[SubMessage(text="look", emotion=None)],
                      attachments=[att])
    cache_dir = storage.chat_attachment_cache_dir(chat_id)

    _build_generic_ctx(chat_obj, contact, user, [msg],
                       Settings(compress_images=True, image_compression_quality=85))
    _build_generic_ctx(chat_obj, contact, user, [msg],
                       Settings(compress_images=True, image_compression_quality=60))
    cached = sorted(p.name for p in cache_dir.glob(f"{att.id}.q*.jpg"))
    assert cached == [f"{att.id}.q60.jpg"]


def test_compress_images_keeps_smaller_original(tmp_storage):
    """A 1x1 PNG is smaller than any JPEG re-encode, so compression mode
    forwards the original PNG untouched."""
    from server.models import ChatMessage, Settings, SubMessage
    with TestClient(app) as client:
        chat_id, contact, user = _make_chat(client)
    chat_obj = storage.get_chat(chat_id)
    att = _drop_attachment(chat_id, _PNG_1X1, "png", "image/png")
    msg = ChatMessage(sender="user", sender_name=user.name,
                      body=[SubMessage(text="hi", emotion=None)],
                      attachments=[att])
    ctx = _build_generic_ctx(chat_obj, contact, user, [msg],
                             Settings(compress_images=True))
    url = _user_image_url(ctx)
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split("base64,", 1)[1]) == _PNG_1X1


def test_compress_images_defaults_on(tmp_storage):
    """Compression defaults to on — off tripped size limits on some providers."""
    from server.models import Settings
    assert Settings().compress_images is True


def test_compress_images_settings_round_trip(tmp_storage):
    with TestClient(app) as client:
        r = client.put("/api/settings", json={
            "compress_images": True, "image_compression_quality": 70,
        })
        assert r.status_code == 200, r.text
        view = r.json()
        assert view["compress_images"] is True
        assert view["image_compression_quality"] == 70
        got = client.get("/api/settings").json()
        assert got["compress_images"] is True
        assert got["image_compression_quality"] == 70
        # Out-of-range quality is rejected before it can reach disk.
        assert client.put(
            "/api/settings", json={"image_compression_quality": 0},
        ).status_code == 422


def test_chat_delete_sweeps_compressed_cache(tmp_storage):
    from server.models import ChatMessage, Settings, SubMessage
    with TestClient(app) as client:
        chat_id, contact, user = _make_chat(client)
    chat_obj = storage.get_chat(chat_id)
    att = _drop_attachment(chat_id, _photo_png(), "png", "image/png")
    msg = ChatMessage(sender="user", sender_name=user.name,
                      body=[SubMessage(text="look", emotion=None)],
                      attachments=[att])
    _build_generic_ctx(chat_obj, contact, user, [msg],
                       Settings(compress_images=True))
    cache_dir = storage.chat_attachment_cache_dir(chat_id)
    chat_path = storage.chat_dir(chat_id)
    assert cache_dir.exists() and any(cache_dir.iterdir())

    assert storage.delete_chat(chat_id) is True
    assert not cache_dir.exists()
    assert not chat_path.exists()

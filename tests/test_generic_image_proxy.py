"""End-to-end image proxy route tests.

``GET /api/chats/{chat_id}/images/{uuid}/{filename}``:
- Disk lookup serves byte-identical bytes when present.
- Reverse-lookup via ``ChatMessage.image_refs`` triggers the lazy
  download path; we stub ``_ImageSession.fetch`` to skip the real
  upstream.
- Decode-verify rejects HTML upstream; ``sniff_image`` rejects
  non-image content (returned as 502).
- The same uuid resolves identically across reloads (disk cache).
- ``Content-Disposition: inline; filename="..."`` preserves the
  original filename from the URL path.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server import storage
from server.main import app


# A tiny synthetic PNG so we don't depend on any fixture file. PIL
# generates 1x1 RGB → ~70 bytes. Use the same bytes in two places
# (cached disk + lazy download) so byte-identical comparison works.
def _tiny_png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _setup_chat(client: TestClient) -> tuple[str, str]:
    r = client.post("/api/contacts", json={"id": "", "name": "C"})
    contact_id = r.json()["id"]
    user_id = client.get("/api/users").json()[0]["id"]
    r = client.post("/api/chats", json={
        "id": "", "contact_id": contact_id, "user_id": user_id, "title": "T",
    })
    return contact_id, r.json()["id"]


def test_disk_lookup_serves_cached_bytes_identically(tmp_storage):
    """A file already on disk under ``images/`` is served byte-identical
    — no download, no transcoding."""
    client = TestClient(app)
    _contact_id, chat_id = _setup_chat(client)
    chat_path = storage.chat_dir(chat_id)
    images_dir = chat_path / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    png_bytes = _tiny_png_bytes()
    (images_dir / "abc.png").write_bytes(png_bytes)

    r = client.get(f"/api/chats/{chat_id}/images/abc/cat.png")
    assert r.status_code == 200, r.text
    assert r.content == png_bytes
    assert r.headers["content-type"] == "image/png"
    assert 'filename="cat.png"' in r.headers["content-disposition"]


def test_404_when_uuid_neither_on_disk_nor_in_image_refs(tmp_storage):
    client = TestClient(app)
    _contact_id, chat_id = _setup_chat(client)
    r = client.get(f"/api/chats/{chat_id}/images/missing-uuid/anything.png")
    assert r.status_code == 404


def test_lazy_download_writes_to_disk_then_serves(tmp_storage, monkeypatch):
    """First hit downloads via ``_ImageSession.fetch``, writes to disk,
    returns bytes. Second hit serves from disk without re-fetching."""
    client = TestClient(app)
    _contact_id, chat_id = _setup_chat(client)

    # Persist a Generic-origin message with ``image_refs`` so the route
    # can reverse-lookup the uuid to a remote URL.
    chat = storage.get_chat(chat_id)
    contact = storage.get_contact(chat.contact_id)
    msgs_container = storage.load_chat_messages(chat_id)
    from server.routers.generate import _persist_assistant_message
    from server.models import SubMessage, new_id

    msg_id = new_id()
    _persist_assistant_message(
        chat_id=chat_id, chat=chat, msgs_container=msgs_container,
        new_msg_id=msg_id, chosen_parent_id=None,
        bubbles=[SubMessage(text="![x](https://example.com/foo.png)", emotion=None)],
        contact_name=contact.name,
        new_cursor=0, new_path_ids=[], new_rolled_over=False,
        context_tokens=0, active_brains_payload=[],
        origin="generic",
        image_refs={"https://example.com/foo.png": "myuuid"},
    )

    png_bytes = _tiny_png_bytes()
    fetch_call_count = {"n": 0}

    class FakeSession:
        def __init__(self, *args, **kwargs): pass
        async def fetch(self, url):
            fetch_call_count["n"] += 1
            return png_bytes

    from server import importers
    monkeypatch.setattr(importers, "_ImageSession", FakeSession)

    # First hit: download + cache + serve.
    r = client.get(f"/api/chats/{chat_id}/images/myuuid/foo.png")
    assert r.status_code == 200
    assert r.content == png_bytes
    assert fetch_call_count["n"] == 1
    chat_path = storage.chat_dir(chat_id)
    on_disk = list((chat_path / "images").glob("myuuid.*"))
    assert len(on_disk) == 1
    assert on_disk[0].read_bytes() == png_bytes

    # Second hit: served from disk. No second fetch call.
    r2 = client.get(f"/api/chats/{chat_id}/images/myuuid/foo.png")
    assert r2.status_code == 200
    assert r2.content == png_bytes
    assert fetch_call_count["n"] == 1, "second hit should not re-download"


def test_decode_verify_rejects_html_upstream(tmp_storage, monkeypatch):
    """Some hosts (imgur with bot filter) return an HTML error page
    with image content-type. ``sniff_image`` catches that — the route
    returns 502 and doesn't write anything to disk."""
    client = TestClient(app)
    _contact_id, chat_id = _setup_chat(client)

    chat = storage.get_chat(chat_id)
    contact = storage.get_contact(chat.contact_id)
    msgs_container = storage.load_chat_messages(chat_id)
    from server.routers.generate import _persist_assistant_message
    from server.models import SubMessage, new_id

    msg_id = new_id()
    _persist_assistant_message(
        chat_id=chat_id, chat=chat, msgs_container=msgs_container,
        new_msg_id=msg_id, chosen_parent_id=None,
        bubbles=[SubMessage(text="![x](https://bad.example.com/foo.png)", emotion=None)],
        contact_name=contact.name,
        new_cursor=0, new_path_ids=[], new_rolled_over=False,
        context_tokens=0, active_brains_payload=[],
        origin="generic",
        image_refs={"https://bad.example.com/foo.png": "htmluuid"},
    )

    class HtmlFakeSession:
        def __init__(self, *args, **kwargs): pass
        async def fetch(self, url):
            return b"<!doctype html><html><body>Not an image</body></html>"

    from server import importers
    monkeypatch.setattr(importers, "_ImageSession", HtmlFakeSession)

    r = client.get(f"/api/chats/{chat_id}/images/htmluuid/foo.png")
    assert r.status_code == 502
    # Nothing written to disk for the rejected URL.
    chat_path = storage.chat_dir(chat_id)
    images_dir = chat_path / "images"
    assert not (images_dir.exists() and list(images_dir.glob("htmluuid.*")))


def test_remove_chat_dir_sweeps_images_subdir(tmp_storage):
    """Deleting a chat must also clean up its ``images/`` subdir so
    deleted chats don't leak files."""
    client = TestClient(app)
    _contact_id, chat_id = _setup_chat(client)
    chat_path = storage.chat_dir(chat_id)
    images_dir = chat_path / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "a.png").write_bytes(_tiny_png_bytes())
    (images_dir / "b.webp").write_bytes(_tiny_png_bytes())

    # Trigger deletion via the storage helper used by routes.
    storage.remove_chat_dir(chat_path)

    # Both the images subdir and the chat dir should be gone.
    assert not images_dir.exists()
    assert not chat_path.exists()

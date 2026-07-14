"""Avatar + card_image round-trip for context presets — JSON export +
PNG card export + re-import."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server import external_formats as ef, storage
from server.importers import import_streaming
from server.main import app
from server.models import ContextPreset


def _png_bytes(size=(8, 8), color=(180, 50, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_storage):
    return TestClient(app)


def _make_with_card(blob: bytes) -> str:
    p = ContextPreset(
        name="Carded",
        avatar="avatar.png",
        card_image="card.png",
    )
    storage.save_context_preset(p)
    pdir = storage.context_preset_dir(p.id)
    storage.atomic_write_bytes(pdir / "avatar.png", blob)
    storage.atomic_write_bytes(pdir / "card.png", blob)
    return p.id


# ---------------------------------------------------------------------------
# JSON export embeds avatar + card as data-URIs
# ---------------------------------------------------------------------------


def test_json_export_embeds_avatar_and_card(client):
    pid = _make_with_card(_png_bytes())
    r = client.get(f"/api/export/context-preset/{pid}")
    assert r.status_code == 200
    payload = r.json()
    assert payload["kind"] == "context_preset"
    assert payload["avatarUri"].startswith("data:image/png;base64,")
    assert payload["cardImageUri"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------------------
# PNG card export carries the JSON in an aertavern_data tEXt chunk
# ---------------------------------------------------------------------------


def test_card_export_returns_png_with_embedded_json(client):
    pid = _make_with_card(_png_bytes())
    r = client.get(f"/api/export/context-preset/{pid}/card")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    chunks = ef.read_png_text_chunks(r.content)
    assert "aertavern_data" in chunks
    decoded = json.loads(base64.b64decode(chunks["aertavern_data"]).decode("utf-8"))
    assert decoded["kind"] == "context_preset"
    assert decoded["name"] == "Carded"


def test_card_export_404_when_no_card_image(client):
    p = ContextPreset(name="No card")
    storage.save_context_preset(p)
    r = client.get(f"/api/export/context-preset/{p.id}/card")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Re-import round-trip (JSON form)
# ---------------------------------------------------------------------------


def test_json_export_reimports_with_avatar(client, tmp_storage):
    pid = _make_with_card(_png_bytes())
    r = client.get(f"/api/export/context-preset/{pid}")
    payload = r.json()
    # Re-import as a copy.
    payload["id"] = ""
    payload["name"] = payload["name"] + " (copy)"

    async def go():
        done = None
        async for ev in import_streaming(payload, mode="copy"):
            if ev.get("type") == "done":
                done = ev
        return done
    done = asyncio.run(go())
    assert done is not None
    assert done["kind"] == "context_preset"
    # New preset on disk; avatar + card present.
    new_id = done["id"]
    preset = storage.get_context_preset(new_id)
    assert preset is not None
    assert preset.name.endswith("(copy)")
    pdir = storage.context_preset_dir(new_id)
    assert pdir is not None
    avatar_files = list(pdir.glob("avatar.*"))
    card_files = list(pdir.glob("card.*"))
    # At least one avatar (HTTP/HTTPS image-import sniffed as PNG) and
    # one card file landed on disk.
    assert avatar_files
    assert card_files

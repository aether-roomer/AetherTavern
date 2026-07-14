"""Tests for the display-image pipeline.

Covers the imaging helpers (round-trip + crop baking) and the file
routes that wire them together — uploads must produce a sibling, the
display GET endpoint must lazy-build, and a crop-changing PUT must
regenerate.
"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server import storage
from server.imaging import (
    DISPLAY_MAX_EDGE,
    build_display_bytes,
    build_display_file,
    display_path_for,
)
from server.main import app
from server.models import Contact, CropRect, User


def _png(width: int, height: int, fill: tuple[int, int, int] = (200, 50, 50)) -> bytes:
    im = Image.new("RGB", (width, height), fill)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _is_webp(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _decode(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_build_display_bytes_caps_long_edge():
    src = _png(2000, 1000)
    out = build_display_bytes(src, None)
    assert _is_webp(out)
    im = _decode(out)
    assert max(im.size) == DISPLAY_MAX_EDGE
    # Aspect ratio preserved.
    assert im.size[0] / im.size[1] == pytest.approx(2.0, rel=1e-2)


def test_build_display_bytes_skips_upscale_for_small_input():
    src = _png(64, 64)
    out = build_display_bytes(src, None)
    assert _decode(out).size == (64, 64)


def test_build_display_bytes_bakes_crop():
    # Half-width vertical strip starting at x=0.5 → 500 px tall, 500 wide.
    src = _png(1000, 1000)
    crop = CropRect(x=0.5, y=0.0, w=0.5, h=0.5)
    out = build_display_bytes(src, crop)
    im = _decode(out)
    assert max(im.size) <= DISPLAY_MAX_EDGE
    # Square crop → square output.
    assert im.size[0] == im.size[1]


def test_build_display_file_writes_sibling(tmp_path):
    original = tmp_path / "avatar.png"
    original.write_bytes(_png(800, 800))
    out = build_display_file(original, None)
    assert out is not None
    assert out == display_path_for(original)
    assert out.exists()
    assert out.suffix == ".webp"
    assert _is_webp(out.read_bytes())


def test_build_display_file_handles_corrupt_source(tmp_path):
    original = tmp_path / "broken.png"
    original.write_bytes(b"not actually a png")
    assert build_display_file(original, None) is None
    assert not display_path_for(original).exists()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_storage):
    return TestClient(app)


def test_upload_avatar_writes_display_sibling(client, tmp_storage):
    c = storage.save_contact(Contact(name="Alice"))
    r = client.post(
        f"/api/contacts/{c.id}/avatar",
        files={"file": ("a.png", _png(800, 800), "image/png")},
    )
    assert r.status_code == 200
    cdir = storage.contact_dir(c.id)
    assert cdir is not None
    assert (cdir / "avatar.png").exists()
    assert (cdir / "avatar.display.webp").exists()


def test_avatar_display_endpoint_serves_webp(client, tmp_storage):
    c = storage.save_contact(Contact(name="Bob"))
    client.post(
        f"/api/contacts/{c.id}/avatar",
        files={"file": ("a.png", _png(1024, 768), "image/png")},
    )
    r = client.get(f"/api/files/contacts/{c.id}/avatar/display")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert _is_webp(r.content)
    assert max(_decode(r.content).size) <= DISPLAY_MAX_EDGE


def test_avatar_display_lazy_builds_when_missing(client, tmp_storage):
    c = storage.save_contact(Contact(name="Carol"))
    client.post(
        f"/api/contacts/{c.id}/avatar",
        files={"file": ("a.png", _png(800, 800), "image/png")},
    )
    cdir = storage.contact_dir(c.id)
    sibling = cdir / "avatar.display.webp"
    sibling.unlink()
    r = client.get(f"/api/files/contacts/{c.id}/avatar/display")
    assert r.status_code == 200
    assert sibling.exists()


def test_changing_avatar_crop_regenerates_display(client, tmp_storage):
    c = storage.save_contact(Contact(name="Dave"))
    client.post(
        f"/api/contacts/{c.id}/avatar",
        files={"file": ("a.png", _png(1000, 500), "image/png")},
    )
    cdir = storage.contact_dir(c.id)
    sibling = cdir / "avatar.display.webp"
    before = sibling.read_bytes()

    fresh = storage.get_contact(c.id)
    payload = fresh.model_dump(mode="json")
    payload["avatar_crop"] = {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}
    r = client.put(f"/api/contacts/{c.id}", json=payload)
    assert r.status_code == 200

    after = sibling.read_bytes()
    assert before != after
    # New crop is square (w=0.5 of width=1000 → 500 px wide; h=1.0 of
    # height=500 → 500 px tall) so the display image is square.
    im = _decode(after)
    assert im.size[0] == im.size[1]


def test_emotion_upload_and_display(client, tmp_storage):
    c = storage.save_contact(Contact(name="Eve"))
    r = client.post(
        f"/api/contacts/{c.id}/emotions/happy",
        files={"file": ("h.png", _png(600, 600), "image/png")},
    )
    assert r.status_code == 200
    cdir = storage.contact_dir(c.id)
    assert (cdir / "emotions" / "happy.png").exists()
    assert (cdir / "emotions" / "happy.display.webp").exists()
    r = client.get(f"/api/files/contacts/{c.id}/emotions/happy/display")
    assert r.status_code == 200
    assert _is_webp(r.content)


def test_changing_emotions_crop_regenerates_all_sprites(client, tmp_storage):
    c = storage.save_contact(Contact(name="Faye"))
    for em in ("happy", "sad"):
        client.post(
            f"/api/contacts/{c.id}/emotions/{em}",
            files={"file": (f"{em}.png", _png(400, 400), "image/png")},
        )
    cdir = storage.contact_dir(c.id)
    happy = cdir / "emotions" / "happy.display.webp"
    sad = cdir / "emotions" / "sad.display.webp"
    before_h, before_s = happy.read_bytes(), sad.read_bytes()

    fresh = storage.get_contact(c.id)
    payload = fresh.model_dump(mode="json")
    payload["emotions_crop"] = {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}
    r = client.put(f"/api/contacts/{c.id}", json=payload)
    assert r.status_code == 200

    assert happy.read_bytes() != before_h
    assert sad.read_bytes() != before_s


def test_user_avatar_display(client, tmp_storage):
    u = storage.save_user(User(name="Persona"))
    client.post(
        f"/api/users/{u.id}/avatar",
        files={"file": ("a.png", _png(800, 800), "image/png")},
    )
    udir = storage.user_dir(u.id)
    assert (udir / "avatar.display.webp").exists()
    r = client.get(f"/api/files/users/{u.id}/avatar/display")
    assert r.status_code == 200
    assert _is_webp(r.content)

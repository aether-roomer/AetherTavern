"""Tests for the per-scenario background image feature.

Covers the model field shape, the upload / delete / serve routes, the
crop-change rebuild on PUT, and the exporter/importer round-trip.
"""
from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from server import storage
from server.exporters import export_scenario
from server.imaging import display_path_for
from server.importers import import_streaming
from server.main import app
from server.models import CropRect, Scenario


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _png(width: int, height: int, fill: tuple[int, int, int] = (90, 130, 200)) -> bytes:
    im = Image.new("RGB", (width, height), fill)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _is_webp(blob: bytes) -> bool:
    return len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"


@pytest.fixture
def client(tmp_storage):
    return TestClient(app)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


def test_scenario_defaults_for_new_fields():
    s = Scenario(name="Empty")
    assert s.background_image is None
    assert s.avatar_crop is None
    assert s.background_focal is None
    assert s.background_mode == "cover"
    assert s.background_blur == 0
    assert s.background_dim == 0
    assert s.background_tint_strength == 0


def test_scenario_round_trips_new_fields():
    s = Scenario(
        name="Beach",
        background_image="background.jpg",
        avatar_crop=CropRect(x=0.1, y=0.2, w=0.5, h=0.5),
        background_focal=(0.7, 0.3),
        background_mode="tile",
        background_blur=8,
        background_dim=20,
        background_tint_strength=45,
    )
    payload = s.model_dump(mode="json")
    assert payload["background_focal"] == [0.7, 0.3]
    rebuilt = Scenario.model_validate(payload)
    assert rebuilt.background_image == "background.jpg"
    assert rebuilt.background_focal == (0.7, 0.3)
    assert rebuilt.background_mode == "tile"
    assert rebuilt.background_blur == 8


def test_scenario_clamps_out_of_range():
    with pytest.raises(Exception):
        Scenario(name="Bad", background_blur=-1)
    with pytest.raises(Exception):
        Scenario(name="Bad", background_blur=21)
    with pytest.raises(Exception):
        Scenario(name="Bad", background_dim=120)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_upload_background_writes_original_and_display(client, tmp_storage):
    s = storage.save_scenario(Scenario(name="Cabin"))
    r = client.post(
        f"/api/scenarios/{s.id}/background",
        files={"file": ("bg.png", _png(1600, 900), "image/png")},
    )
    assert r.status_code == 200
    sdir = storage.scenario_dir(s.id)
    assert sdir is not None
    assert (sdir / "background.png").exists()
    assert (sdir / "background.display.webp").exists()
    fresh = storage.get_scenario(s.id)
    assert fresh.background_image == "background.png"


def test_get_background_serves_original_bytes(client, tmp_storage):
    s = storage.save_scenario(Scenario(name="Field"))
    src = _png(800, 600, (10, 20, 30))
    client.post(
        f"/api/scenarios/{s.id}/background",
        files={"file": ("bg.png", src, "image/png")},
    )
    r = client.get(f"/api/files/scenarios/{s.id}/background")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == src


def test_get_background_display_serves_webp(client, tmp_storage):
    s = storage.save_scenario(Scenario(name="Loft"))
    client.post(
        f"/api/scenarios/{s.id}/background",
        files={"file": ("bg.png", _png(1024, 768), "image/png")},
    )
    r = client.get(f"/api/files/scenarios/{s.id}/background/display")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/webp"
    assert _is_webp(r.content)


def test_put_with_avatar_crop_change_rebuilds_display(client, tmp_storage):
    s = storage.save_scenario(Scenario(name="Garden"))
    client.post(
        f"/api/scenarios/{s.id}/background",
        files={"file": ("bg.png", _png(1200, 600), "image/png")},
    )
    sdir = storage.scenario_dir(s.id)
    sibling = display_path_for(sdir / "background.png")
    before = sibling.read_bytes()

    fresh = storage.get_scenario(s.id)
    payload = fresh.model_dump(mode="json")
    payload["avatar_crop"] = {"x": 0.1, "y": 0.0, "w": 0.4, "h": 1.0}
    r = client.put(f"/api/scenarios/{s.id}", json=payload)
    assert r.status_code == 200
    assert sibling.read_bytes() != before


def test_delete_background_clears_files_and_fields(client, tmp_storage):
    s = storage.save_scenario(Scenario(name="Studio"))
    client.post(
        f"/api/scenarios/{s.id}/background",
        files={"file": ("bg.png", _png(400, 400), "image/png")},
    )
    sdir = storage.scenario_dir(s.id)
    r = client.delete(f"/api/scenarios/{s.id}/background")
    assert r.status_code == 200
    assert not (sdir / "background.png").exists()
    assert not (sdir / "background.display.webp").exists()
    fresh = storage.get_scenario(s.id)
    assert fresh.background_image is None
    assert fresh.avatar_crop is None
    assert fresh.background_focal is None


def test_get_background_404_when_unset(client, tmp_storage):
    s = storage.save_scenario(Scenario(name="Empty"))
    r = client.get(f"/api/files/scenarios/{s.id}/background")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Export / import round-trip
# ---------------------------------------------------------------------------


def test_export_scenario_embeds_background_and_fields(client, tmp_storage):
    s = storage.save_scenario(Scenario(name="Beach"))
    src = _png(640, 360)
    client.post(
        f"/api/scenarios/{s.id}/background",
        files={"file": ("bg.png", src, "image/png")},
    )
    # Apply the user-tunable settings AFTER upload — the upload route resets
    # focal/crop, so the live UI sets them via subsequent PUT requests.
    fresh = storage.get_scenario(s.id)
    fresh.background_focal = (0.4, 0.6)
    fresh.background_mode = "tile"
    fresh.background_blur = 5
    fresh.background_dim = 30
    fresh.background_brighten = 12
    fresh.background_tint_strength = 50
    storage.save_scenario(fresh)
    fresh = storage.get_scenario(s.id)
    payload = export_scenario(fresh)
    uri = payload["backgroundImageUri"]
    assert uri.startswith("data:image/png;base64,")
    decoded = base64.b64decode(uri.split(",", 1)[1])
    assert decoded == src
    assert payload["backgroundMode"] == "tile"
    assert payload["backgroundBlur"] == 5
    assert payload["backgroundDim"] == 30
    assert payload["backgroundBrighten"] == 12
    assert payload["backgroundTintStrength"] == 50
    assert payload["backgroundFocal"] == {"x": 0.4, "y": 0.6}


@pytest.mark.asyncio
async def test_import_streaming_round_trips_background(client, tmp_storage):
    src = _png(720, 540, (200, 50, 50))
    uri = "data:image/png;base64," + base64.b64encode(src).decode("ascii")
    incoming = {
        "name": "Forest",
        "environment": "deep woods",
        "scene": "midday",
        "backgroundImageUri": uri,
        "backgroundMode": "cover",
        "backgroundBlur": 7,
        "backgroundDim": 25,
        "backgroundBrighten": 10,
        "backgroundTintStrength": 60,
        "backgroundFocal": {"x": 0.3, "y": 0.7},
        "avatarCrop": {"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.4},
    }

    events = []
    async for ev in import_streaming(incoming, mode="copy"):
        events.append(ev)
    done = [e for e in events if e.get("type") == "done"]
    assert done, events
    sid = done[-1]["id"]
    fresh = storage.get_scenario(sid)
    assert fresh is not None
    assert fresh.background_image == "background.png"
    assert fresh.background_mode == "cover"
    assert fresh.background_blur == 7
    assert fresh.background_dim == 25
    assert fresh.background_brighten == 10
    assert fresh.background_tint_strength == 60
    assert fresh.background_focal == (0.3, 0.7)
    assert fresh.avatar_crop is not None
    sdir = storage.scenario_dir(sid)
    assert (sdir / "background.png").exists()
    assert (sdir / "background.display.webp").exists()

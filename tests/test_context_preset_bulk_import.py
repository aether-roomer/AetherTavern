"""flat_json bulk-zip picker — context-preset rows classify with the right
kind + state badge, and the import action lands the preset on disk."""
from __future__ import annotations

import json
import zipfile
from io import BytesIO

import pytest

from server import storage
from server.importers import (
    import_zip_streaming,
    read_zip_toc,
)
from server.models import ContextPreset


def _flat_preset_json(*, name: str = "Bulk preset", id_: str | None = None) -> dict:
    return {
        "kind": "context_preset",
        "id": id_ or "",
        "name": name,
        "description": "",
        "author": "",
        "prefixNames": True,
        "systemPromptBlocks": [
            {"name": "Intro", "enabled": True, "content": "Hello world."},
        ],
        "additionalMessages": [],
        "avatarUri": "",
    }


def _make_flat_zip(files: dict[str, dict]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in files.items():
            zf.writestr(path, json.dumps(data))
    return buf.getvalue()


def _toc_manifest(zf: zipfile.ZipFile) -> dict:
    manifest = None
    for ev in read_zip_toc(zf):
        if ev["type"] == "manifest":
            manifest = ev["manifest"]
    assert manifest is not None
    return manifest


# ---------------------------------------------------------------------------
# Picker manifest
# ---------------------------------------------------------------------------


def test_picker_classifies_context_preset(tmp_storage):
    raw = _make_flat_zip({"p.json": _flat_preset_json(name="From zip", id_="p-001")})
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    assert toc["format"] == "flat_json"
    [item] = toc["items"]
    assert item["kind"] == "context_preset"
    assert item["name"] == "From zip"
    assert item["state"] == "not_imported"  # nothing on disk with this id


def test_picker_state_up_to_date_when_id_matches(tmp_storage):
    # Pre-create a preset with the same id the zip carries.
    storage.save_context_preset(ContextPreset(id="p-existing", name="Already here"))
    raw = _make_flat_zip({"p.json": _flat_preset_json(id_="p-existing")})
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        toc = _toc_manifest(zf)
    [item] = toc["items"]
    assert item["state"] == "up_to_date"


# ---------------------------------------------------------------------------
# Importing the selection lands the preset on disk
# ---------------------------------------------------------------------------


def _write_zip(tmp_path, raw):
    z = tmp_path / "bulk.zip"
    z.write_bytes(raw)
    return z


async def _drain(agen):
    return [e async for e in agen]


@pytest.mark.asyncio
async def test_flat_import_context_preset(tmp_storage):
    raw = _make_flat_zip({"p.json": _flat_preset_json(name="Imported", id_="p-imp")})
    selection = {
        "format": "flat_json",
        "items": [{"path": "p.json", "action": "import"}],
    }
    events = await _drain(
        import_zip_streaming(_write_zip(tmp_storage, raw), selection),
    )
    [done] = [e for e in events if e.get("type") == "done"]
    p = storage.get_context_preset("p-imp")
    assert p is not None
    assert p.name == "Imported"
    assert len(p.system_prompt_blocks) == 1
    assert p.system_prompt_blocks[0].content == "Hello world."

"""Generation preset library routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server import storage
from server.models import Preset, PresetLibrary, new_id


router = APIRouter(prefix="/api/presets", tags=["presets"])


@router.get("")
async def list_presets() -> list[Preset]:
    return storage.load_presets().presets


@router.post("")
async def create_preset(preset: Preset) -> Preset:
    async with storage.lock("presets"):
        if not preset.id or any(p.id == preset.id for p in storage.load_presets().presets):
            preset = preset.model_copy(update={"id": new_id()})
        lib = storage.load_presets()
        lib.presets.append(preset)
        storage.save_presets(lib)
        return preset


@router.put("/{preset_id}")
async def update_preset(preset_id: str, preset: Preset) -> Preset:
    if preset.id != preset_id:
        raise HTTPException(400, "preset id in body does not match URL")
    async with storage.lock("presets"):
        lib = storage.load_presets()
        for i, p in enumerate(lib.presets):
            if p.id == preset_id:
                lib.presets[i] = preset
                storage.save_presets(lib)
                return preset
        raise HTTPException(404, f"preset {preset_id!r} not found")


@router.delete("/{preset_id}")
async def delete_preset(preset_id: str) -> dict:
    async with storage.lock("presets"):
        lib = storage.load_presets()
        if not any(p.id == preset_id for p in lib.presets):
            raise HTTPException(404, f"preset {preset_id!r} not found")
        if len(lib.presets) <= 1:
            raise HTTPException(400, "cannot delete the last remaining preset")
        settings = storage.load_settings()
        if settings.default_preset_id == preset_id:
            # Re-point default to whatever's left.
            remaining = [p for p in lib.presets if p.id != preset_id]
            settings.default_preset_id = remaining[0].id
            # Acquire settings lock too — settings is a separate file. Brief
            # nested lock; safe because no other settings writer holds it.
            async with storage.lock("settings"):
                storage.save_settings(settings)
        lib.presets = [p for p in lib.presets if p.id != preset_id]
        storage.save_presets(lib)
        return {"deleted": preset_id}

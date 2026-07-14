"""Scenario routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server import storage
from server.conflicts import check_version
from server.duplication import copy_entity_files, next_copy_name
from server.imaging import build_display_file
from server.list_models import FavoriteUpdate, ScenarioSummary
from server.models import Scenario, new_id, now_seconds


router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


@router.get("")
async def list_scenarios() -> list[ScenarioSummary]:
    return [
        ScenarioSummary.from_scenario(
            s, last_used_at=storage.index.last_used_scenario.get(s.id),
        )
        for s in storage.list_scenarios()
    ]


@router.get("/{scenario_id}")
async def get_scenario(scenario_id: str) -> Scenario:
    s = storage.get_scenario(scenario_id)
    if s is None:
        raise HTTPException(404, f"scenario {scenario_id!r} not found")
    return s


@router.post("")
async def create_scenario(scenario: Scenario) -> Scenario:
    if not scenario.id or storage.get_scenario(scenario.id) is not None:
        scenario = scenario.model_copy(update={"id": new_id()})
    async with storage.lock(f"scenario:{scenario.id}"):
        return storage.save_scenario(scenario)


@router.put("/{scenario_id}")
async def update_scenario(scenario_id: str, scenario: Scenario) -> Scenario:
    if scenario.id != scenario_id:
        raise HTTPException(400, "scenario id in body does not match URL")
    async with storage.lock(f"scenario:{scenario_id}"):
        prev = storage.get_scenario(scenario_id)
        if prev is None:
            raise HTTPException(404, f"scenario {scenario_id!r} not found")
        if not check_version(scenario, prev):
            return prev
        saved = storage.save_scenario(scenario)
        # Rebuild the avatar display sibling when the crop changes — bakes
        # the new rect into the WebP so the list-row preview stays correct
        # without CSS-side cropping. Mirrors the contact-avatar flow.
        sdir = storage.scenario_dir(scenario_id)
        if sdir is not None and saved.background_image and saved.avatar_crop != prev.avatar_crop:
            build_display_file(sdir / saved.background_image, saved.avatar_crop)
        return saved


@router.patch("/{scenario_id}/favorite")
async def set_scenario_favorite(scenario_id: str, body: FavoriteUpdate) -> ScenarioSummary:
    """See ``contacts.py:set_contact_favorite``."""
    async with storage.lock(f"scenario:{scenario_id}"):
        s = storage.get_scenario(scenario_id)
        if s is None:
            raise HTTPException(404, f"scenario {scenario_id!r} not found")
        s.favorite = body.favorite
        storage.save_scenario(s, bump_version=False)
        return ScenarioSummary.from_scenario(
            s, last_used_at=storage.index.last_used_scenario.get(s.id),
        )


@router.delete("/{scenario_id}")
async def delete_scenario(scenario_id: str) -> dict:
    async with storage.lock(f"scenario:{scenario_id}"):
        if not storage.delete_scenario(scenario_id):
            raise HTTPException(404, f"scenario {scenario_id!r} not found")
        return {"deleted": scenario_id}


@router.post("/{scenario_id}/duplicate")
async def duplicate_scenario(scenario_id: str) -> Scenario:
    src = storage.get_scenario(scenario_id)
    if src is None:
        raise HTTPException(404, f"scenario {scenario_id!r} not found")
    src_dir = storage.scenario_dir(scenario_id)
    existing_names = [s.name for s in storage.list_scenarios()]
    now = now_seconds()
    new_scenario = src.model_copy(update={
        "id": new_id(),
        "version_id": new_id(),
        "created_at": now,
        "updated_at": now,
        "name": next_copy_name(src.name, existing_names),
    })
    async with storage.lock(f"scenario:{new_scenario.id}"):
        # Files first, info.yaml second — see contacts duplicate.
        if src_dir is not None:
            dst_dir = storage.SCENARIOS_DIR / storage.entity_dir_name(
                new_scenario.name, new_scenario.id,
            )
            copy_entity_files(src_dir, dst_dir)
        return storage.save_scenario(new_scenario)

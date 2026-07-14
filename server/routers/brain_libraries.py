"""Brain-library routes.

Brain libraries are reusable bundles of brains that any chat can attach
0..N of, in display + prompt order. Library brains land in the AER system
prompt after the contact / user / scenario unconditionals. See
``server/aer/format.py``.

Deleting a library does NOT strip its id from any ``Chat.brain_library_ids``
list. Chats keep dangling references so a later re-import of the library
brings the attachment back automatically; the chat UI surfaces a "missing
library" marker for unresolved ids so the user can manually detach if
they prefer.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server import storage
from server.conflicts import check_version
from server.duplication import copy_entity_files, next_copy_name
from server.imaging import build_display_file
from server.list_models import BrainLibrarySummary, FavoriteUpdate
from server.models import BrainLibrary, new_id, now_seconds


router = APIRouter(prefix="/api/libraries", tags=["libraries"])


@router.get("")
async def list_brain_libraries() -> list[BrainLibrarySummary]:
    return [
        BrainLibrarySummary.from_library(
            lib, last_used_at=storage.index.last_used_library.get(lib.id),
        )
        for lib in storage.list_brain_libraries()
    ]


@router.get("/{library_id}")
async def get_brain_library(library_id: str) -> BrainLibrary:
    lib = storage.get_brain_library(library_id)
    if lib is None:
        raise HTTPException(404, f"library {library_id!r} not found")
    return lib


@router.post("")
async def create_brain_library(library: BrainLibrary) -> BrainLibrary:
    if not library.id or storage.get_brain_library(library.id) is not None:
        library = library.model_copy(update={"id": new_id()})
    async with storage.lock(f"library:{library.id}"):
        return storage.save_brain_library(library)


@router.put("/{library_id}")
async def update_brain_library(library_id: str, library: BrainLibrary) -> BrainLibrary:
    if library.id != library_id:
        raise HTTPException(400, "library id in body does not match URL")
    async with storage.lock(f"library:{library_id}"):
        prev = storage.get_brain_library(library_id)
        if prev is None:
            raise HTTPException(404, f"library {library_id!r} not found")
        if not check_version(library, prev):
            return prev
        saved = storage.save_brain_library(library)
        # Rebuild the avatar display sibling when the crop changes — bakes
        # the new rect into the WebP. Mirrors the contact / scenario flow.
        ldir = storage.brain_library_dir(library_id)
        if ldir is not None and saved.avatar and saved.avatar_crop != prev.avatar_crop:
            build_display_file(ldir / saved.avatar, saved.avatar_crop)
        return saved


@router.patch("/{library_id}/favorite")
async def set_library_favorite(library_id: str, body: FavoriteUpdate) -> BrainLibrarySummary:
    """See ``contacts.py:set_contact_favorite``."""
    async with storage.lock(f"brain_library:{library_id}"):
        lib = storage.get_brain_library(library_id)
        if lib is None:
            raise HTTPException(404, f"brain library {library_id!r} not found")
        lib.favorite = body.favorite
        storage.save_brain_library(lib, bump_version=False)
        return BrainLibrarySummary.from_library(
            lib, last_used_at=storage.index.last_used_library.get(lib.id),
        )


@router.delete("/{library_id}")
async def delete_brain_library(library_id: str) -> dict:
    async with storage.lock(f"library:{library_id}"):
        if not storage.delete_brain_library(library_id):
            raise HTTPException(404, f"library {library_id!r} not found")
        return {"deleted": library_id}


@router.post("/{library_id}/duplicate")
async def duplicate_brain_library(library_id: str) -> BrainLibrary:
    src = storage.get_brain_library(library_id)
    if src is None:
        raise HTTPException(404, f"library {library_id!r} not found")
    src_dir = storage.brain_library_dir(library_id)
    existing_names = [lib.name for lib in storage.list_brain_libraries()]
    now = now_seconds()
    new_library = src.model_copy(update={
        "id": new_id(),
        "version_id": new_id(),
        "created_at": now,
        "updated_at": now,
        "name": next_copy_name(src.name, existing_names),
    })
    async with storage.lock(f"library:{new_library.id}"):
        # Files first, info.yaml second — see contacts duplicate.
        if src_dir is not None:
            dst_dir = storage.LIBRARIES_DIR / storage.entity_dir_name(
                new_library.name, new_library.id,
            )
            copy_entity_files(src_dir, dst_dir)
        return storage.save_brain_library(new_library)

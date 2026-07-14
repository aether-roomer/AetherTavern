"""User persona routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server import storage
from server.conflicts import check_version
from server.duplication import copy_entity_files, next_copy_name
from server.imaging import build_display_file
from server.list_models import FavoriteUpdate, UserSummary
from server.models import User, new_id, now_seconds


router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
async def list_users() -> list[UserSummary]:
    return [
        UserSummary.from_user(
            u, last_used_at=storage.index.last_used_user.get(u.id),
        )
        for u in storage.list_users()
    ]


@router.get("/{user_id}")
async def get_user(user_id: str) -> User:
    u = storage.get_user(user_id)
    if u is None:
        raise HTTPException(404, f"user {user_id!r} not found")
    return u


@router.post("")
async def create_user(user: User) -> User:
    if not user.id or storage.get_user(user.id) is not None:
        user = user.model_copy(update={"id": new_id()})
    async with storage.lock(f"user:{user.id}"):
        return storage.save_user(user)


@router.put("/{user_id}")
async def update_user(user_id: str, user: User) -> User:
    if user.id != user_id:
        raise HTTPException(400, "user id in body does not match URL")
    async with storage.lock(f"user:{user_id}"):
        prev = storage.get_user(user_id)
        if prev is None:
            raise HTTPException(404, f"user {user_id!r} not found")
        if not check_version(user, prev):
            return prev
        saved = storage.save_user(user)
        udir = storage.user_dir(user_id)
        if udir is not None and saved.avatar and saved.avatar_crop != prev.avatar_crop:
            build_display_file(udir / saved.avatar, saved.avatar_crop)
        return saved


@router.patch("/{user_id}/favorite")
async def set_user_favorite(user_id: str, body: FavoriteUpdate) -> UserSummary:
    """See ``contacts.py:set_contact_favorite``."""
    async with storage.lock(f"user:{user_id}"):
        u = storage.get_user(user_id)
        if u is None:
            raise HTTPException(404, f"user {user_id!r} not found")
        u.favorite = body.favorite
        storage.save_user(u, bump_version=False)
        return UserSummary.from_user(
            u, last_used_at=storage.index.last_used_user.get(u.id),
        )


@router.delete("/{user_id}")
async def delete_user(user_id: str) -> dict:
    async with storage.lock(f"user:{user_id}"):
        if not storage.delete_user(user_id):
            raise HTTPException(404, f"user {user_id!r} not found")
        return {"deleted": user_id}


@router.post("/{user_id}/duplicate")
async def duplicate_user(user_id: str) -> User:
    src = storage.get_user(user_id)
    if src is None:
        raise HTTPException(404, f"user {user_id!r} not found")
    src_dir = storage.user_dir(user_id)
    existing_names = [u.name for u in storage.list_users()]
    now = now_seconds()
    new_user = src.model_copy(update={
        "id": new_id(),
        "version_id": new_id(),
        "created_at": now,
        "updated_at": now,
        "name": next_copy_name(src.name, existing_names),
    })
    async with storage.lock(f"user:{new_user.id}"):
        # Files first, info.yaml second — see contacts duplicate.
        if src_dir is not None:
            dst_dir = storage.USERS_DIR / storage.entity_dir_name(
                new_user.name, new_user.id,
            )
            copy_entity_files(src_dir, dst_dir)
        return storage.save_user(new_user)

"""Contacts (characters) routes."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server import storage
from server.conflicts import check_version
from server.duplication import copy_entity_files, next_copy_name
from server.imaging import build_display_file
from server.list_models import ContactSummary, FavoriteUpdate
from server.models import Contact, new_id, now_seconds


router = APIRouter(prefix="/api/contacts", tags=["contacts"])


@router.get("")
async def list_contacts() -> list[ContactSummary]:
    return [
        ContactSummary.from_contact(
            c, last_used_at=storage.index.last_used_contact.get(c.id),
        )
        for c in storage.list_contacts()
    ]


@router.get("/{contact_id}")
async def get_contact(contact_id: str) -> Contact:
    c = storage.get_contact(contact_id)
    if c is None:
        raise HTTPException(404, f"contact {contact_id!r} not found")
    return c


@router.post("")
async def create_contact(contact: Contact) -> Contact:
    if not contact.id or storage.get_contact(contact.id) is not None:
        contact = contact.model_copy(update={"id": new_id()})
    async with storage.lock(f"contact:{contact.id}"):
        return storage.save_contact(contact)


@router.put("/{contact_id}")
async def update_contact(contact_id: str, contact: Contact) -> Contact:
    if contact.id != contact_id:
        raise HTTPException(400, "contact id in body does not match URL")
    async with storage.lock(f"contact:{contact_id}"):
        prev = storage.get_contact(contact_id)
        if prev is None:
            raise HTTPException(404, f"contact {contact_id!r} not found")
        if not check_version(contact, prev):
            # Stale version_id but payload matches — nothing to save.
            # Return the live entity so the client's draft picks up the
            # fresh version_id without firing a Save dot or conflict modal.
            return prev
        saved = storage.save_contact(contact)
        # Regenerate display siblings when the crop changes — bakes the new
        # rect into the WebP so list rows / chat sprites stay correct without
        # CSS-side cropping.
        cdir = storage.contact_dir(contact_id)
        if cdir is not None:
            if saved.avatar and saved.avatar_crop != prev.avatar_crop:
                build_display_file(cdir / saved.avatar, saved.avatar_crop)
            if saved.emotions and saved.emotions_crop != prev.emotions_crop:
                em_dir = cdir / "emotions"
                for em_name, fname in saved.emotions.items():
                    p = em_dir / fname
                    if p.exists():
                        build_display_file(p, saved.emotions_crop)
        return saved


@router.patch("/{contact_id}/favorite")
async def set_contact_favorite(contact_id: str, body: FavoriteUpdate) -> ContactSummary:
    """Toggle ``favorite`` without bumping ``version_id`` — the chat info
    modal / edit-view draft elsewhere stays valid. Returns the updated
    Summary so the client can patch list state without a full re-fetch."""
    async with storage.lock(f"contact:{contact_id}"):
        c = storage.get_contact(contact_id)
        if c is None:
            raise HTTPException(404, f"contact {contact_id!r} not found")
        c.favorite = body.favorite
        storage.save_contact(c, bump_version=False)
        return ContactSummary.from_contact(
            c, last_used_at=storage.index.last_used_contact.get(c.id),
        )


@router.delete("/{contact_id}")
async def delete_contact(contact_id: str) -> dict:
    async with storage.lock(f"contact:{contact_id}"):
        if not storage.delete_contact(contact_id):
            raise HTTPException(404, f"contact {contact_id!r} not found")
        return {"deleted": contact_id}


@router.post("/{contact_id}/duplicate")
async def duplicate_contact(contact_id: str) -> Contact:
    src = storage.get_contact(contact_id)
    if src is None:
        raise HTTPException(404, f"contact {contact_id!r} not found")
    src_dir = storage.contact_dir(contact_id)
    existing_names = [c.name for c in storage.list_contacts()]
    now = now_seconds()
    new_contact = src.model_copy(update={
        "id": new_id(),
        "version_id": new_id(),
        "created_at": now,
        "updated_at": now,
        "name": next_copy_name(src.name, existing_names),
        # The copy is its own entity, not a (potentially re-importable)
        # mirror of the source archive.
        "aer_source_id": None,
        "aer_revision": None,
        "aer_imported_at": None,
        # Clear per-scenario AER bookkeeping too.
        "scenarios": [
            cs.model_copy(update={
                "aer_source_id": None,
                "aer_revision": None,
                "aer_imported_at": None,
            }) for cs in src.scenarios
        ],
    })
    async with storage.lock(f"contact:{new_contact.id}"):
        # Copy files BEFORE info.yaml, so a crash mid-write leaves either
        # nothing (indexer skips dirs without a marker) or a complete
        # entity — never an info.yaml referencing missing files.
        if src_dir is not None:
            dst_dir = storage.CONTACTS_DIR / storage.entity_dir_name(
                new_contact.name, new_contact.id,
            )
            copy_entity_files(src_dir, dst_dir)
        return storage.save_contact(new_contact)

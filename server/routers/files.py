"""Image upload + serving for contact avatars and emotion sprites.

Layout on disk (created by ``server.storage``)::

    data/contacts/{slug}-{id8}/avatar.{ext}
    data/contacts/{slug}-{id8}/emotions/{emotion}.{ext}

Uploads accept multipart/form-data; format is sniffed from magic bytes (we
don't rely on the client-supplied content-type).

Upload ordering — this is load-bearing for crash safety:

  1. Write the new file under its full extension via ``atomic_write_bytes``.
  2. Build the display sibling.
  3. Save info.yaml with ``bump_version=False`` (so the open edit-view's
     draft doesn't 409 on its next autosave).
  4. *Then* unlink old siblings of a different extension (best-effort).

The reverse of the obvious "wipe then write" order — if step 1, 2, or 3
fails, info.yaml still names the previously-working file. If step 4 fails,
info.yaml is correct and a stale file lingers on disk (cosmetic, recovered
on next upload).

Deletion mirrors this: clear the field on the entity and save first
(``bump_version=False``), THEN unlink. Same failure-mode discipline.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response

from server import storage
from server.imaging import build_display_file, display_path_for, DISPLAY_SUFFIX
from server.models import EMOTIONS


# ---------------------------------------------------------------------------
# Sibling cleanup — best-effort glob+unlink, preserves .tmp + .display.webp.
# ---------------------------------------------------------------------------

def _remove_old_ext_siblings(parent: Path, prefix: str, keep: Path) -> None:
    """Unlink files matching ``{prefix}.*`` that aren't the kept target,
    a ``.tmp`` leftover, or the ``.display.webp`` sibling. Used after a
    successful upload to clean up the old extension's file when the new
    upload landed under a different extension."""
    if not parent.exists():
        return
    display_name = f"{prefix}{DISPLAY_SUFFIX}"
    for p in parent.glob(f"{prefix}.*"):
        if p == keep or p.name.endswith(".tmp") or p.name == display_name:
            continue
        try:
            p.unlink()
        except OSError:
            pass


def _remove_all_with_prefix(parent: Path, prefix: str) -> None:
    """Unlink every file matching ``{prefix}.*`` (incl. display sibling),
    skipping ``.tmp`` leftovers. Used by the delete handlers."""
    if not parent.exists():
        return
    for p in parent.glob(f"{prefix}.*"):
        if p.name.endswith(".tmp"):
            continue
        try:
            p.unlink()
        except OSError:
            pass


router = APIRouter(tags=["files"])


# ---------------------------------------------------------------------------
# Format sniffing
# ---------------------------------------------------------------------------

_IMAGE_MAX_BYTES = 20 * 1024 * 1024  # 20 MB


def sniff_image(data: bytes) -> tuple[str, str]:
    """Return ``(ext, mime)`` for a supported image format, or raise 400."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data[:4] == b"\x00\x00\x01\x00" or data[:4] == b"\x00\x00\x02\x00":
        return "ico", "image/x-icon"
    raise HTTPException(400, "Unsupported image format (PNG/JPEG/WEBP/GIF only).")


def sniff_dimensions(data: bytes) -> tuple[int, int] | None:
    """Best-effort ``(width, height)`` parse for PNG / JPEG / GIF / WebP bytes.

    Returns ``None`` if the format is unknown or the header is malformed.
    Used at import time so we can normalise ``facePosition``-derived crops
    (which assume "square in pixels") into this codebase's convention
    (h normalised to image height, so h ≠ w for non-square images).
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"\xff\xd8\xff"):
        i, n = 2, len(data)
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0x00, 0xFF):
                i += 1
                continue
            if marker in (0xD8, 0xD9):
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            # SOF markers: C0..CF except C4 (DHT), C8 (JPG-extension), CC (DAC).
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return w, h
            i += 2 + seg_len
        return None
    if (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")) and len(data) >= 10:
        return (
            int.from_bytes(data[6:8], "little"),
            int.from_bytes(data[8:10], "little"),
        )
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8 ":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
        if chunk == b"VP8L":
            b1, b2, b3, b4 = data[21], data[22], data[23], data[24]
            return (
                (((b2 & 0x3F) << 8) | b1) + 1,
                (((b4 & 0x0F) << 10) | (b3 << 2) | (b2 >> 6)) + 1,
            )
        if chunk == b"VP8X":
            return (
                (data[24] | (data[25] << 8) | (data[26] << 16)) + 1,
                (data[27] | (data[28] << 8) | (data[29] << 16)) + 1,
            )
    return None


_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "ico": "image/x-icon",
}


def _read_image_or_400(file: UploadFile) -> bytes:
    if file.size and file.size > _IMAGE_MAX_BYTES:
        raise HTTPException(413, f"Image too large (max {_IMAGE_MAX_BYTES // (1024*1024)} MB).")
    data = file.file.read(_IMAGE_MAX_BYTES + 1)
    if len(data) > _IMAGE_MAX_BYTES:
        raise HTTPException(413, f"Image too large (max {_IMAGE_MAX_BYTES // (1024*1024)} MB).")
    return data


# ---------------------------------------------------------------------------
# Avatar
# ---------------------------------------------------------------------------


def _bookkeeping(entity) -> dict:
    """Pluck the version_id + updated_at off a saved entity so the upload/
    delete responses can carry them. The frontend merges these into the
    open edit-view's draft so the next autosave doesn't 409 against the
    fresh server-side state."""
    return {
        "version_id": entity.version_id,
        "updated_at": entity.updated_at,
    }


@router.post("/api/contacts/{contact_id}/avatar")
async def upload_avatar(contact_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"contact:{contact_id}"):
        contact = storage.get_contact(contact_id)
        if contact is None:
            raise HTTPException(404, f"contact {contact_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        cdir = storage.contact_dir(contact_id)
        if cdir is None:
            raise HTTPException(500, "contact dir not indexed")
        # Write new file FIRST. If anything from here through save_contact
        # fails, info.yaml still names the previously-working avatar.
        fname = f"avatar.{ext}"
        target = cdir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        contact.avatar = fname
        contact.avatar_crop = None
        # bump_version=False: an avatar upload shouldn't invalidate the open
        # edit-view's draft version_id (would 409 the next autosave).
        # Even so, ``updated_at`` always bumps and the frontend's draft
        # needs both fields refreshed via the response below.
        storage.save_contact(contact, bump_version=False)
        # Now safe to clean up old siblings of a different extension.
        _remove_old_ext_siblings(cdir, "avatar", target)
        return {"avatar": fname, **_bookkeeping(contact)}


@router.delete("/api/contacts/{contact_id}/avatar")
async def delete_avatar(contact_id: str) -> dict:
    async with storage.lock(f"contact:{contact_id}"):
        contact = storage.get_contact(contact_id)
        if contact is None:
            raise HTTPException(404, f"contact {contact_id!r} not found")
        cdir = storage.contact_dir(contact_id)
        if cdir is None:
            raise HTTPException(500, "contact dir not indexed")
        # Clear the field FIRST so a crash before unlink leaves the
        # entity already detached from the file (next read returns 404
        # cleanly instead of pointing at half-deleted bytes).
        contact.avatar = None
        contact.avatar_crop = None
        storage.save_contact(contact, bump_version=False)
        _remove_all_with_prefix(cdir, "avatar")
        return {"deleted": "avatar", **_bookkeeping(contact)}


@router.get("/api/files/contacts/{contact_id}/avatar")
async def get_avatar(contact_id: str) -> Response:
    contact = storage.get_contact(contact_id)
    contact_dir = storage.contact_dir(contact_id)
    if contact is None or contact_dir is None or not contact.avatar:
        raise HTTPException(404, "avatar not set")
    path = contact_dir / contact.avatar
    if not path.exists():
        raise HTTPException(404, "avatar file missing")
    return _serve_image(path)


@router.get("/api/files/contacts/{contact_id}/avatar/display")
async def get_avatar_display(contact_id: str) -> Response:
    contact = storage.get_contact(contact_id)
    contact_dir = storage.contact_dir(contact_id)
    if contact is None or contact_dir is None or not contact.avatar:
        raise HTTPException(404, "avatar not set")
    original = contact_dir / contact.avatar
    return _serve_display(original, contact.avatar_crop)


# ---------------------------------------------------------------------------
# Emotion sprites
# ---------------------------------------------------------------------------


def _validate_emotion_name(name: str) -> str:
    name = name.lower()
    if name not in {e.value for e in EMOTIONS}:
        raise HTTPException(400, f"unknown emotion {name!r}")
    return name


@router.post("/api/contacts/{contact_id}/emotions/{emotion}")
async def upload_emotion(contact_id: str, emotion: str, file: UploadFile) -> dict:
    emotion = _validate_emotion_name(emotion)
    async with storage.lock(f"contact:{contact_id}"):
        contact = storage.get_contact(contact_id)
        if contact is None:
            raise HTTPException(404, f"contact {contact_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        cdir = storage.contact_dir(contact_id)
        if cdir is None:
            raise HTTPException(500, "contact dir not indexed")
        em_dir = cdir / "emotions"
        em_dir.mkdir(parents=True, exist_ok=True)
        # Write new file FIRST; clean up old siblings only after save.
        fname = f"{emotion}.{ext}"
        target = em_dir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, contact.emotions_crop)
        contact.emotions = {**contact.emotions, emotion: fname}
        storage.save_contact(contact, bump_version=False)
        _remove_old_ext_siblings(em_dir, emotion, target)
        return {"emotion": emotion, "filename": fname, **_bookkeeping(contact)}


@router.delete("/api/contacts/{contact_id}/emotions/{emotion}")
async def delete_emotion(contact_id: str, emotion: str) -> dict:
    emotion = _validate_emotion_name(emotion)
    async with storage.lock(f"contact:{contact_id}"):
        contact = storage.get_contact(contact_id)
        if contact is None:
            raise HTTPException(404, f"contact {contact_id!r} not found")
        cdir = storage.contact_dir(contact_id)
        if cdir is None:
            raise HTTPException(500, "contact dir not indexed")
        contact.emotions = {k: v for k, v in contact.emotions.items() if k != emotion}
        storage.save_contact(contact, bump_version=False)
        em_dir = cdir / "emotions"
        if em_dir.exists():
            _remove_all_with_prefix(em_dir, emotion)
        return {"deleted": emotion, **_bookkeeping(contact)}


@router.get("/api/files/contacts/{contact_id}/emotions/{emotion}")
async def get_emotion(contact_id: str, emotion: str) -> Response:
    emotion = _validate_emotion_name(emotion)
    contact = storage.get_contact(contact_id)
    contact_dir = storage.contact_dir(contact_id)
    if contact is None or contact_dir is None:
        raise HTTPException(404, "contact not found")
    fname = contact.emotions.get(emotion)
    if not fname:
        raise HTTPException(404, "emotion sprite not set")
    path = contact_dir / "emotions" / fname
    if not path.exists():
        raise HTTPException(404, "emotion file missing")
    return _serve_image(path)


@router.get("/api/files/contacts/{contact_id}/emotions/{emotion}/display")
async def get_emotion_display(contact_id: str, emotion: str) -> Response:
    emotion = _validate_emotion_name(emotion)
    contact = storage.get_contact(contact_id)
    contact_dir = storage.contact_dir(contact_id)
    if contact is None or contact_dir is None:
        raise HTTPException(404, "contact not found")
    fname = contact.emotions.get(emotion)
    if not fname:
        raise HTTPException(404, "emotion sprite not set")
    original = contact_dir / "emotions" / fname
    return _serve_display(original, contact.emotions_crop)


# ---------------------------------------------------------------------------
# User-persona avatar (mirrors the contact avatar — separate dir tree).
# ---------------------------------------------------------------------------


@router.post("/api/users/{user_id}/avatar")
async def upload_user_avatar(user_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"user:{user_id}"):
        user = storage.get_user(user_id)
        if user is None:
            raise HTTPException(404, f"user {user_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        udir = storage.user_dir(user_id)
        if udir is None:
            raise HTTPException(500, "user dir not indexed")
        fname = f"avatar.{ext}"
        target = udir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        user.avatar = fname
        user.avatar_crop = None
        storage.save_user(user, bump_version=False)
        _remove_old_ext_siblings(udir, "avatar", target)
        return {"avatar": fname, **_bookkeeping(user)}


@router.delete("/api/users/{user_id}/avatar")
async def delete_user_avatar(user_id: str) -> dict:
    async with storage.lock(f"user:{user_id}"):
        user = storage.get_user(user_id)
        if user is None:
            raise HTTPException(404, f"user {user_id!r} not found")
        udir = storage.user_dir(user_id)
        if udir is None:
            raise HTTPException(500, "user dir not indexed")
        user.avatar = None
        user.avatar_crop = None
        storage.save_user(user, bump_version=False)
        _remove_all_with_prefix(udir, "avatar")
        return {"deleted": "avatar", **_bookkeeping(user)}


@router.get("/api/files/users/{user_id}/avatar")
async def get_user_avatar(user_id: str) -> Response:
    user = storage.get_user(user_id)
    udir = storage.user_dir(user_id)
    if user is None or udir is None or not user.avatar:
        raise HTTPException(404, "avatar not set")
    path = udir / user.avatar
    if not path.exists():
        raise HTTPException(404, "avatar file missing")
    return _serve_image(path)


@router.get("/api/files/users/{user_id}/avatar/display")
async def get_user_avatar_display(user_id: str) -> Response:
    user = storage.get_user(user_id)
    udir = storage.user_dir(user_id)
    if user is None or udir is None or not user.avatar:
        raise HTTPException(404, "avatar not set")
    original = udir / user.avatar
    return _serve_display(original, user.avatar_crop)


# ---------------------------------------------------------------------------
# Brain-library avatar (mirrors the user avatar — separate dir tree).
# ---------------------------------------------------------------------------


@router.post("/api/libraries/{library_id}/avatar")
async def upload_library_avatar(library_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"library:{library_id}"):
        library = storage.get_brain_library(library_id)
        if library is None:
            raise HTTPException(404, f"library {library_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        ldir = storage.brain_library_dir(library_id)
        if ldir is None:
            raise HTTPException(500, "library dir not indexed")
        fname = f"avatar.{ext}"
        target = ldir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        library.avatar = fname
        library.avatar_crop = None
        storage.save_brain_library(library, bump_version=False)
        _remove_old_ext_siblings(ldir, "avatar", target)
        return {"avatar": fname, **_bookkeeping(library)}


@router.delete("/api/libraries/{library_id}/avatar")
async def delete_library_avatar(library_id: str) -> dict:
    async with storage.lock(f"library:{library_id}"):
        library = storage.get_brain_library(library_id)
        if library is None:
            raise HTTPException(404, f"library {library_id!r} not found")
        ldir = storage.brain_library_dir(library_id)
        if ldir is None:
            raise HTTPException(500, "library dir not indexed")
        library.avatar = None
        library.avatar_crop = None
        storage.save_brain_library(library, bump_version=False)
        _remove_all_with_prefix(ldir, "avatar")
        return {"deleted": "avatar", **_bookkeeping(library)}


@router.get("/api/files/libraries/{library_id}/avatar")
async def get_library_avatar(library_id: str) -> Response:
    library = storage.get_brain_library(library_id)
    ldir = storage.brain_library_dir(library_id)
    if library is None or ldir is None or not library.avatar:
        raise HTTPException(404, "avatar not set")
    path = ldir / library.avatar
    if not path.exists():
        raise HTTPException(404, "avatar file missing")
    return _serve_image(path)


@router.get("/api/files/libraries/{library_id}/avatar/display")
async def get_library_avatar_display(library_id: str) -> Response:
    library = storage.get_brain_library(library_id)
    ldir = storage.brain_library_dir(library_id)
    if library is None or ldir is None or not library.avatar:
        raise HTTPException(404, "avatar not set")
    original = ldir / library.avatar
    return _serve_display(original, library.avatar_crop)


# ---------------------------------------------------------------------------
# Scenario background image (atmospheric chat-pane background; the
# small cropped avatar slot in the scenario list is the display sibling).
# ---------------------------------------------------------------------------


@router.post("/api/scenarios/{scenario_id}/background")
async def upload_scenario_background(scenario_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"scenario:{scenario_id}"):
        scenario = storage.get_scenario(scenario_id)
        if scenario is None:
            raise HTTPException(404, f"scenario {scenario_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        sdir = storage.scenario_dir(scenario_id)
        if sdir is None:
            raise HTTPException(500, "scenario dir not indexed")
        fname = f"background.{ext}"
        target = sdir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        scenario.background_image = fname
        scenario.avatar_crop = None
        scenario.background_focal = None
        storage.save_scenario(scenario, bump_version=False)
        _remove_old_ext_siblings(sdir, "background", target)
        return {"background": fname, **_bookkeeping(scenario)}


@router.delete("/api/scenarios/{scenario_id}/background")
async def delete_scenario_background(scenario_id: str) -> dict:
    async with storage.lock(f"scenario:{scenario_id}"):
        scenario = storage.get_scenario(scenario_id)
        if scenario is None:
            raise HTTPException(404, f"scenario {scenario_id!r} not found")
        sdir = storage.scenario_dir(scenario_id)
        if sdir is None:
            raise HTTPException(500, "scenario dir not indexed")
        scenario.background_image = None
        scenario.avatar_crop = None
        scenario.background_focal = None
        storage.save_scenario(scenario, bump_version=False)
        _remove_all_with_prefix(sdir, "background")
        return {"deleted": "background", **_bookkeeping(scenario)}


@router.get("/api/files/scenarios/{scenario_id}/background")
async def get_scenario_background(scenario_id: str) -> Response:
    scenario = storage.get_scenario(scenario_id)
    sdir = storage.scenario_dir(scenario_id)
    if scenario is None or sdir is None or not scenario.background_image:
        raise HTTPException(404, "background not set")
    path = sdir / scenario.background_image
    if not path.exists():
        raise HTTPException(404, "background file missing")
    return _serve_image(path)


@router.get("/api/files/scenarios/{scenario_id}/background/display")
async def get_scenario_background_display(scenario_id: str) -> Response:
    scenario = storage.get_scenario(scenario_id)
    sdir = storage.scenario_dir(scenario_id)
    if scenario is None or sdir is None or not scenario.background_image:
        raise HTTPException(404, "background not set")
    original = sdir / scenario.background_image
    return _serve_display(original, scenario.avatar_crop)


# ---------------------------------------------------------------------------
# Card image — Contact / User / Scenario / BrainLibrary
#
# A "card image" is the optional original PNG (or any decodable image) the
# entity was imported from — a SillyTavern character card, a lorebook
# card-PNG, or our own ``aertavern_data``-stamped export. The card-image
# slot is independent from ``avatar``: when set, the entity can be re-
# exported as a PNG with its JSON baked into a tEXt chunk. Any decodable
# format is accepted at upload time and the original bytes are preserved;
# re-encoding to PNG only happens at export time (when we have to rewrite
# the image anyway to embed the metadata chunk).
# ---------------------------------------------------------------------------


@router.post("/api/contacts/{contact_id}/card-image")
async def upload_contact_card(contact_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"contact:{contact_id}"):
        contact = storage.get_contact(contact_id)
        if contact is None:
            raise HTTPException(404, f"contact {contact_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        cdir = storage.contact_dir(contact_id)
        if cdir is None:
            raise HTTPException(500, "contact dir not indexed")
        fname = f"card.{ext}"
        target = cdir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        contact.card_image = fname
        storage.save_contact(contact, bump_version=False)
        _remove_old_ext_siblings(cdir, "card", target)
        return {"card_image": fname, **_bookkeeping(contact)}


@router.delete("/api/contacts/{contact_id}/card-image")
async def delete_contact_card(contact_id: str) -> dict:
    async with storage.lock(f"contact:{contact_id}"):
        contact = storage.get_contact(contact_id)
        if contact is None:
            raise HTTPException(404, f"contact {contact_id!r} not found")
        cdir = storage.contact_dir(contact_id)
        if cdir is None:
            raise HTTPException(500, "contact dir not indexed")
        contact.card_image = None
        storage.save_contact(contact, bump_version=False)
        _remove_all_with_prefix(cdir, "card")
        return {"deleted": "card_image", **_bookkeeping(contact)}


@router.get("/api/files/contacts/{contact_id}/card-image")
async def get_contact_card(contact_id: str) -> Response:
    contact = storage.get_contact(contact_id)
    cdir = storage.contact_dir(contact_id)
    if contact is None or cdir is None or not contact.card_image:
        raise HTTPException(404, "card image not set")
    path = cdir / contact.card_image
    if not path.exists():
        raise HTTPException(404, "card image file missing")
    return _serve_image(path)


@router.get("/api/files/contacts/{contact_id}/card-image/display")
async def get_contact_card_display(contact_id: str) -> Response:
    contact = storage.get_contact(contact_id)
    cdir = storage.contact_dir(contact_id)
    if contact is None or cdir is None or not contact.card_image:
        raise HTTPException(404, "card image not set")
    original = cdir / contact.card_image
    return _serve_display(original, None)


@router.post("/api/users/{user_id}/card-image")
async def upload_user_card(user_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"user:{user_id}"):
        user = storage.get_user(user_id)
        if user is None:
            raise HTTPException(404, f"user {user_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        udir = storage.user_dir(user_id)
        if udir is None:
            raise HTTPException(500, "user dir not indexed")
        fname = f"card.{ext}"
        target = udir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        user.card_image = fname
        storage.save_user(user, bump_version=False)
        _remove_old_ext_siblings(udir, "card", target)
        return {"card_image": fname, **_bookkeeping(user)}


@router.delete("/api/users/{user_id}/card-image")
async def delete_user_card(user_id: str) -> dict:
    async with storage.lock(f"user:{user_id}"):
        user = storage.get_user(user_id)
        if user is None:
            raise HTTPException(404, f"user {user_id!r} not found")
        udir = storage.user_dir(user_id)
        if udir is None:
            raise HTTPException(500, "user dir not indexed")
        user.card_image = None
        storage.save_user(user, bump_version=False)
        _remove_all_with_prefix(udir, "card")
        return {"deleted": "card_image", **_bookkeeping(user)}


@router.get("/api/files/users/{user_id}/card-image")
async def get_user_card(user_id: str) -> Response:
    user = storage.get_user(user_id)
    udir = storage.user_dir(user_id)
    if user is None or udir is None or not user.card_image:
        raise HTTPException(404, "card image not set")
    path = udir / user.card_image
    if not path.exists():
        raise HTTPException(404, "card image file missing")
    return _serve_image(path)


@router.get("/api/files/users/{user_id}/card-image/display")
async def get_user_card_display(user_id: str) -> Response:
    user = storage.get_user(user_id)
    udir = storage.user_dir(user_id)
    if user is None or udir is None or not user.card_image:
        raise HTTPException(404, "card image not set")
    original = udir / user.card_image
    return _serve_display(original, None)


@router.post("/api/scenarios/{scenario_id}/card-image")
async def upload_scenario_card(scenario_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"scenario:{scenario_id}"):
        scenario = storage.get_scenario(scenario_id)
        if scenario is None:
            raise HTTPException(404, f"scenario {scenario_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        sdir = storage.scenario_dir(scenario_id)
        if sdir is None:
            raise HTTPException(500, "scenario dir not indexed")
        fname = f"card.{ext}"
        target = sdir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        scenario.card_image = fname
        storage.save_scenario(scenario, bump_version=False)
        _remove_old_ext_siblings(sdir, "card", target)
        return {"card_image": fname, **_bookkeeping(scenario)}


@router.delete("/api/scenarios/{scenario_id}/card-image")
async def delete_scenario_card(scenario_id: str) -> dict:
    async with storage.lock(f"scenario:{scenario_id}"):
        scenario = storage.get_scenario(scenario_id)
        if scenario is None:
            raise HTTPException(404, f"scenario {scenario_id!r} not found")
        sdir = storage.scenario_dir(scenario_id)
        if sdir is None:
            raise HTTPException(500, "scenario dir not indexed")
        scenario.card_image = None
        storage.save_scenario(scenario, bump_version=False)
        _remove_all_with_prefix(sdir, "card")
        return {"deleted": "card_image", **_bookkeeping(scenario)}


@router.get("/api/files/scenarios/{scenario_id}/card-image")
async def get_scenario_card(scenario_id: str) -> Response:
    scenario = storage.get_scenario(scenario_id)
    sdir = storage.scenario_dir(scenario_id)
    if scenario is None or sdir is None or not scenario.card_image:
        raise HTTPException(404, "card image not set")
    path = sdir / scenario.card_image
    if not path.exists():
        raise HTTPException(404, "card image file missing")
    return _serve_image(path)


@router.get("/api/files/scenarios/{scenario_id}/card-image/display")
async def get_scenario_card_display(scenario_id: str) -> Response:
    scenario = storage.get_scenario(scenario_id)
    sdir = storage.scenario_dir(scenario_id)
    if scenario is None or sdir is None or not scenario.card_image:
        raise HTTPException(404, "card image not set")
    original = sdir / scenario.card_image
    return _serve_display(original, None)


@router.post("/api/libraries/{library_id}/card-image")
async def upload_library_card(library_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"library:{library_id}"):
        library = storage.get_brain_library(library_id)
        if library is None:
            raise HTTPException(404, f"library {library_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        ldir = storage.brain_library_dir(library_id)
        if ldir is None:
            raise HTTPException(500, "library dir not indexed")
        fname = f"card.{ext}"
        target = ldir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        library.card_image = fname
        storage.save_brain_library(library, bump_version=False)
        _remove_old_ext_siblings(ldir, "card", target)
        return {"card_image": fname, **_bookkeeping(library)}


@router.delete("/api/libraries/{library_id}/card-image")
async def delete_library_card(library_id: str) -> dict:
    async with storage.lock(f"library:{library_id}"):
        library = storage.get_brain_library(library_id)
        if library is None:
            raise HTTPException(404, f"library {library_id!r} not found")
        ldir = storage.brain_library_dir(library_id)
        if ldir is None:
            raise HTTPException(500, "library dir not indexed")
        library.card_image = None
        storage.save_brain_library(library, bump_version=False)
        _remove_all_with_prefix(ldir, "card")
        return {"deleted": "card_image", **_bookkeeping(library)}


@router.get("/api/files/libraries/{library_id}/card-image")
async def get_library_card(library_id: str) -> Response:
    library = storage.get_brain_library(library_id)
    ldir = storage.brain_library_dir(library_id)
    if library is None or ldir is None or not library.card_image:
        raise HTTPException(404, "card image not set")
    path = ldir / library.card_image
    if not path.exists():
        raise HTTPException(404, "card image file missing")
    return _serve_image(path)


@router.get("/api/files/libraries/{library_id}/card-image/display")
async def get_library_card_display(library_id: str) -> Response:
    library = storage.get_brain_library(library_id)
    ldir = storage.brain_library_dir(library_id)
    if library is None or ldir is None or not library.card_image:
        raise HTTPException(404, "card image not set")
    original = ldir / library.card_image
    return _serve_display(original, None)


# ---------------------------------------------------------------------------
# Context-preset avatar & card image
# ---------------------------------------------------------------------------


@router.post("/api/context-presets/{preset_id}/avatar")
async def upload_context_preset_avatar(preset_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"context_preset:{preset_id}"):
        preset = storage.get_context_preset(preset_id)
        if preset is None:
            raise HTTPException(404, f"context preset {preset_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        pdir = storage.context_preset_dir(preset_id)
        if pdir is None:
            raise HTTPException(500, "context preset dir not indexed")
        fname = f"avatar.{ext}"
        target = pdir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        preset.avatar = fname
        preset.avatar_crop = None
        storage.save_context_preset(preset, bump_version=False)
        _remove_old_ext_siblings(pdir, "avatar", target)
        return {"avatar": fname, **_bookkeeping(preset)}


@router.delete("/api/context-presets/{preset_id}/avatar")
async def delete_context_preset_avatar(preset_id: str) -> dict:
    async with storage.lock(f"context_preset:{preset_id}"):
        preset = storage.get_context_preset(preset_id)
        if preset is None:
            raise HTTPException(404, f"context preset {preset_id!r} not found")
        pdir = storage.context_preset_dir(preset_id)
        if pdir is None:
            raise HTTPException(500, "context preset dir not indexed")
        preset.avatar = None
        preset.avatar_crop = None
        storage.save_context_preset(preset, bump_version=False)
        _remove_all_with_prefix(pdir, "avatar")
        return {"deleted": "avatar", **_bookkeeping(preset)}


@router.get("/api/files/context-presets/{preset_id}/avatar")
async def get_context_preset_avatar(preset_id: str) -> Response:
    preset = storage.get_context_preset(preset_id)
    pdir = storage.context_preset_dir(preset_id)
    if preset is None or pdir is None or not preset.avatar:
        raise HTTPException(404, "avatar not set")
    path = pdir / preset.avatar
    if not path.exists():
        raise HTTPException(404, "avatar file missing")
    return _serve_image(path)


@router.get("/api/files/context-presets/{preset_id}/avatar/display")
async def get_context_preset_avatar_display(preset_id: str) -> Response:
    preset = storage.get_context_preset(preset_id)
    pdir = storage.context_preset_dir(preset_id)
    if preset is None or pdir is None or not preset.avatar:
        raise HTTPException(404, "avatar not set")
    original = pdir / preset.avatar
    return _serve_display(original, preset.avatar_crop)


@router.post("/api/context-presets/{preset_id}/card-image")
async def upload_context_preset_card(preset_id: str, file: UploadFile) -> dict:
    async with storage.lock(f"context_preset:{preset_id}"):
        preset = storage.get_context_preset(preset_id)
        if preset is None:
            raise HTTPException(404, f"context preset {preset_id!r} not found")
        data = _read_image_or_400(file)
        ext, _ = sniff_image(data)
        pdir = storage.context_preset_dir(preset_id)
        if pdir is None:
            raise HTTPException(500, "context preset dir not indexed")
        fname = f"card.{ext}"
        target = pdir / fname
        storage.atomic_write_bytes(target, data)
        build_display_file(target, None)
        preset.card_image = fname
        storage.save_context_preset(preset, bump_version=False)
        _remove_old_ext_siblings(pdir, "card", target)
        return {"card_image": fname, **_bookkeeping(preset)}


@router.delete("/api/context-presets/{preset_id}/card-image")
async def delete_context_preset_card(preset_id: str) -> dict:
    async with storage.lock(f"context_preset:{preset_id}"):
        preset = storage.get_context_preset(preset_id)
        if preset is None:
            raise HTTPException(404, f"context preset {preset_id!r} not found")
        pdir = storage.context_preset_dir(preset_id)
        if pdir is None:
            raise HTTPException(500, "context preset dir not indexed")
        preset.card_image = None
        storage.save_context_preset(preset, bump_version=False)
        _remove_all_with_prefix(pdir, "card")
        return {"deleted": "card_image", **_bookkeeping(preset)}


@router.get("/api/files/context-presets/{preset_id}/card-image")
async def get_context_preset_card(preset_id: str) -> Response:
    preset = storage.get_context_preset(preset_id)
    pdir = storage.context_preset_dir(preset_id)
    if preset is None or pdir is None or not preset.card_image:
        raise HTTPException(404, "card image not set")
    path = pdir / preset.card_image
    if not path.exists():
        raise HTTPException(404, "card image file missing")
    return _serve_image(path)


@router.get("/api/files/context-presets/{preset_id}/card-image/display")
async def get_context_preset_card_display(preset_id: str) -> Response:
    preset = storage.get_context_preset(preset_id)
    pdir = storage.context_preset_dir(preset_id)
    if preset is None or pdir is None or not preset.card_image:
        raise HTTPException(404, "card image not set")
    original = pdir / preset.card_image
    return _serve_display(original, None)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _serve_image(path: Path) -> Response:
    ext = path.suffix.lower().lstrip(".")
    mime = _MIME_BY_EXT.get(ext, "application/octet-stream")
    data = path.read_bytes()
    return Response(content=data, media_type=mime)


def _serve_display(original: Path, crop) -> Response:
    """Serve the display sibling of ``original``, lazy-building if missing.

    Falls back to the original if the build fails (corrupt source, etc.)
    so the UI still has something to show.
    """
    if not original.exists():
        raise HTTPException(404, "image file missing")
    target = display_path_for(original)
    if not target.exists():
        if build_display_file(original, crop) is None:
            return _serve_image(original)
    return Response(content=target.read_bytes(), media_type="image/webp")


# ---------------------------------------------------------------------------
# Notification sound (single global asset under data/, optional override of
# the synthetic Web Audio ping in ``static/notification.js``).
# ---------------------------------------------------------------------------

_NOTIFICATION_SOUND_PREFIX = "notification_sound"
_NOTIFICATION_SOUND_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_NOTIFICATION_SOUND_MIME_BY_EXT = {
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
}


def _sniff_audio(data: bytes) -> tuple[str, str]:
    """Return ``(ext, mime)`` for a supported audio container, or raise 400."""
    if data.startswith(b"ID3") or data[:2] == b"\xff\xfb" or data[:2] == b"\xff\xf3" \
       or data[:2] == b"\xff\xf2":
        return "mp3", "audio/mpeg"
    if data.startswith(b"OggS"):
        return "ogg", "audio/ogg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav", "audio/wav"
    # M4A / MP4-audio: ``ftyp`` box at offset 4.
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "m4a", "audio/mp4"
    raise HTTPException(400, "Unsupported audio format (MP3/OGG/WAV/M4A only).")


@router.post("/api/settings/notification-sound")
async def upload_notification_sound(file: UploadFile) -> dict:
    """Replace the global notification sound. Write-then-cleanup discipline:
    write the new file, save settings to point at it, THEN unlink any
    previous-extension siblings.
    """
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > _NOTIFICATION_SOUND_MAX_BYTES:
        raise HTTPException(413, "File too large (max 5 MB).")
    ext, _mime = _sniff_audio(data)
    target = storage.DATA_DIR / f"{_NOTIFICATION_SOUND_PREFIX}.{ext}"
    async with storage.lock("settings"):
        storage.atomic_write_bytes(target, data)
        current = storage.load_settings()
        current.notification_sound = target.name
        storage.save_settings(current)
    # Cleanup other-ext leftovers AFTER the save points at the new file.
    _remove_old_ext_siblings(storage.DATA_DIR, _NOTIFICATION_SOUND_PREFIX, target)
    return {"filename": target.name}


@router.get("/api/files/notification-sound")
async def get_notification_sound() -> Response:
    settings = storage.load_settings()
    if not settings.notification_sound:
        raise HTTPException(404, "No custom notification sound")
    path = storage.DATA_DIR / settings.notification_sound
    if not path.exists():
        raise HTTPException(404, "Notification sound file missing on disk")
    ext = path.suffix.lstrip(".").lower()
    mime = _NOTIFICATION_SOUND_MIME_BY_EXT.get(ext, "application/octet-stream")
    return Response(content=path.read_bytes(), media_type=mime)


@router.delete("/api/settings/notification-sound")
async def delete_notification_sound() -> dict:
    """Revert to the synthetic default. Save-then-unlink discipline."""
    async with storage.lock("settings"):
        current = storage.load_settings()
        current.notification_sound = None
        storage.save_settings(current)
    _remove_all_with_prefix(storage.DATA_DIR, _NOTIFICATION_SOUND_PREFIX)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat attachments (generic mode multimodal — phase 1: images only).
# Uploads are opaque blobs until the client binds them to a message via
# the POST /api/chats/{id}/messages body's ``attachments`` field. Orphaned
# uploads can be reaped via DELETE.
# ---------------------------------------------------------------------------


def _attachment_paths(chat_id: str, att_id: str) -> tuple[Path | None, Path | None]:
    """Look up the on-disk file for an attachment id under a chat dir.

    Returns ``(directory, match_path_or_None)`` so callers can either glob
    for the match or compose a new path.
    """
    d = storage.chat_attachments_dir(chat_id)
    if d is None:
        return None, None
    if not d.exists():
        return d, None
    for p in d.glob(f"{att_id}.*"):
        if p.name.endswith(".tmp"):
            continue
        return d, p
    return d, None


@router.post("/api/chats/{chat_id}/attachments")
async def upload_chat_attachment(chat_id: str, file: UploadFile) -> dict:
    """Stash an attachment file under the chat dir AND append it to
    ``chat.pending_attachments`` so the chip row survives a chat switch
    / reload / tab close. ``POST /messages`` later consumes the entry
    by binding it to the new ChatMessage.
    """
    chat = storage.get_chat(chat_id)
    if chat is None:
        raise HTTPException(404, f"chat {chat_id!r} not found")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > _IMAGE_MAX_BYTES:
        raise HTTPException(413, "File too large (max 20 MB).")
    # Phase 1: images only.
    ext, mime = sniff_image(data)
    from server.models import Attachment, new_id as _new_id
    att_id = _new_id()
    d = storage.chat_attachments_dir(chat_id)
    if d is None:
        raise HTTPException(500, "Chat directory missing")
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"{att_id}.{ext}"
    storage.atomic_write_bytes(target, data)
    att = Attachment(
        id=att_id,
        mime=mime,
        filename=file.filename or f"attachment.{ext}",
        byte_size=len(data),
    )
    # Add to the chat's pending list under the chat lock — file first,
    # then the list write, so a failure after disk-write leaves a
    # harmless orphan rather than a dangling pending entry pointing
    # at a missing file.
    async with storage.lock(f"chat:{chat_id}"):
        chat = storage.get_chat(chat_id)
        if chat is not None:
            chat.pending_attachments = list(chat.pending_attachments) + [att]
            storage.save_chat(chat, bump_version=False)
    return {
        "id": att.id,
        "mime": att.mime,
        "filename": att.filename,
        "byte_size": att.byte_size,
    }


@router.get("/api/files/chats/{chat_id}/attachments/{att_id}")
async def get_chat_attachment(chat_id: str, att_id: str) -> Response:
    _, match = _attachment_paths(chat_id, att_id)
    if match is None or not match.exists():
        raise HTTPException(404, "attachment not found")
    ext = match.suffix.lstrip(".").lower()
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "application/octet-stream")
    return Response(content=match.read_bytes(), media_type=mime)


@router.delete("/api/chats/{chat_id}/attachments/{att_id}")
async def delete_chat_attachment(chat_id: str, att_id: str) -> dict:
    """Unlink an unsent attachment + drop it from the chat's pending
    list. Used by the chip row's remove button. Lock held across both
    operations so a concurrent send doesn't bind a half-deleted ref."""
    async with storage.lock(f"chat:{chat_id}"):
        chat = storage.get_chat(chat_id)
        if chat is not None:
            new_pending = [
                a for a in chat.pending_attachments if a.id != att_id
            ]
            if len(new_pending) != len(chat.pending_attachments):
                chat.pending_attachments = new_pending
                storage.save_chat(chat, bump_version=False)
        _, match = _attachment_paths(chat_id, att_id)
        if match is not None and match.exists():
            try:
                match.unlink()
            except OSError:
                pass
    return {"ok": True}

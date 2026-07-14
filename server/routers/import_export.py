"""Import / export routes for character / user / scenario / chat JSON."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import uuid
import zipfile
from pathlib import Path
from typing import Any

import base64

import msgspec
import msgspec.json
from fastapi import APIRouter, Body, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from server import exporters, external_formats, imaging, importers, storage
from server.proxy_rules import proxy_rules_scope

# Reusable JSON decoder. SSE event encoding stays on stdlib ``json.dumps``
# below — tiny payloads, dominated by network not CPU.
_json_dec = msgspec.json.Decoder()


log = logging.getLogger("aether.import_export")

router = APIRouter(prefix="/api", tags=["import_export"])


# ---------------------------------------------------------------------------
# Import (auto-detect)
# ---------------------------------------------------------------------------


async def _read_json(file: UploadFile) -> Any:
    raw = await file.read()
    try:
        return _json_dec.decode(raw)
    except msgspec.DecodeError as e:
        raise HTTPException(400, f"Could not parse JSON: {e}") from e


async def _read_import_payload(file: UploadFile) -> tuple[dict, str]:
    """Read an import file and return ``(AER-shape JSON, filename)``.

    Accepts:
      - JSON files (native AER, ST card v1/v2, ST WI, lorebook).
      - PNG files with an embedded ``aertavern_data`` / ``chara`` /
        ``naidata`` tEXt chunk.

    For PNG input the raw image bytes are folded into the JSON as a
    ``cardImageUri`` data URI so the existing import pipeline writes
    them to the entity's card-image slot.
    """
    raw = await file.read()
    filename = file.filename or ""
    if not raw:
        raise HTTPException(400, "Empty file.")

    # PNG path.
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            parsed = external_formats.parse_external(raw, filename)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if parsed is None:
            raise HTTPException(400, "no embedded card data found")
        payload = parsed["payload"]
        if parsed["card_image_bytes"]:
            encoded = base64.b64encode(parsed["card_image_bytes"]).decode("ascii")
            payload = dict(payload)
            payload["cardImageUri"] = f"data:image/png;base64,{encoded}"
        return payload, filename

    # JSON path.
    try:
        data = _json_dec.decode(raw)
    except msgspec.DecodeError as e:
        raise HTTPException(400, f"Could not parse JSON: {e}") from e
    if not isinstance(data, dict):
        raise HTTPException(400, "Top-level JSON must be an object.")
    return data, filename


def _sse(event: str, payload: Any) -> bytes:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


@router.post("/import")
async def import_any(
    request: Request,
    file: UploadFile,
    mode: str = Form(default="ask"),
    resolutions: str = Form(default=""),
) -> StreamingResponse:
    """Auto-detect the format and dispatch to the right importer.

    Streams Server-Sent Events:
      - ``progress``      ``{ label, current, total }`` while work is in flight.
      - ``conflict``      ``{ kind, id, existing_name, incoming_name }`` if
                          the source UUID is already in storage and
                          ``mode='ask'``. The client prompts the user, then
                          re-uploads with ``mode='replace'`` (overwrite) or
                          ``mode='copy'`` (assign a new UUID).
      - ``name_matches``  ``{ items: [{role, kind, incoming, candidates}] }``
                          for chat composites whose sidecars (character /
                          user / scenario) match an existing entity *by name*.
                          The client prompts the user per role and re-uploads
                          with ``resolutions={role: id_or_"new"}`` as JSON.
      - ``done``          ``{ kind, id, name | title }`` on success.
      - ``error``         ``{ message }`` on failure.
    """
    data, filename = await _read_import_payload(file)

    parsed_resolutions: dict | None = None
    if resolutions:
        try:
            parsed_resolutions = _json_dec.decode(resolutions)
            if not isinstance(parsed_resolutions, dict):
                raise ValueError("resolutions must decode to an object")
        except (msgspec.DecodeError, ValueError) as e:
            raise HTTPException(400, f"Invalid resolutions: {e}") from e

    async def stream():
        with proxy_rules_scope(getattr(request.app.state, "proxy_rules", None)):
            try:
                async for ev in importers.import_streaming(
                    data, mode=mode, resolutions=parsed_resolutions, filename=filename,
                ):
                    yield _sse(ev["type"], ev)
            except ValueError as e:
                yield _sse("error", {"message": str(e)})
            except Exception as e:
                log.exception("import failed")
                yield _sse("error", {"message": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Bulk zip import — staged on disk under a token, picker driven by SSE.
#
# Phase 1: ``POST /api/import-zip-upload`` chunks the multipart body straight
#   into ``data/.tmp/zip-imports/{token}/archive.zip`` so memory stays
#   bounded by the chunk size regardless of upload size.
# Phase 2: ``GET /api/import-zip-toc/{token}`` is an SSE stream that emits
#   per-file progress while parsing, finishing with a ``manifest`` event.
# Phase 3: ``POST /api/import-zip`` (JSON ``{token, selection}``) runs the
#   bulk import; the staging dir is wiped on completion or error.
# Plus: ``DELETE /api/import-zip-token/{token}`` for the picker's Cancel
# path, and ``GET /api/import-zip-preview/{token}?path=...`` to lazily
# generate tiny avatar thumbnails for picker rows.
# ---------------------------------------------------------------------------


# uuid4().hex shape — 32 lowercase hex chars. Validated on every token
# input to avoid path-traversal via the staging directory.
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_UPLOAD_CHUNK = 4 * 1024 * 1024  # 4 MB
_PREVIEW_CACHE_TTL = 3600  # seconds, advertised via Cache-Control


def _zip_imports_root() -> Path:
    return storage.TEMP_DIR / "zip-imports"


def _token_dir(token: str) -> Path | None:
    """Return the staging dir for ``token`` if it exists, else ``None``.

    Refuses any token that doesn't match the strict UUID-hex shape so
    callers can't reach outside ``data/.tmp/zip-imports/`` via crafted
    path components.
    """
    if not _TOKEN_RE.match(token or ""):
        return None
    p = _zip_imports_root() / token
    return p if p.exists() else None


def _archive_path(token: str) -> Path | None:
    d = _token_dir(token)
    if d is None:
        return None
    p = d / "archive.zip"
    return p if p.exists() else None


def _preview_cache_path(token_dir: Path, zip_path: str) -> Path:
    """Per-zip-member cache path. Hash so we don't have to mkdir every
    intermediate directory the source path implies."""
    digest = hashlib.sha256(zip_path.encode("utf-8")).hexdigest()[:16]
    return token_dir / "previews" / f"{digest}.webp"


def _wipe_token(token: str) -> None:
    if not _TOKEN_RE.match(token or ""):
        return
    target = _zip_imports_root() / token
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


@router.post("/import-zip-upload")
async def import_zip_upload(file: UploadFile) -> JSONResponse:
    """Stream the multipart upload to disk under a fresh token.

    Returns ``{token}``. The caller drives `xhr.upload.onprogress` for
    real-time upload-progress UI, then opens the SSE ToC stream.
    """
    token = uuid.uuid4().hex
    target_dir = _zip_imports_root() / token
    archive_path = target_dir / "archive.zip"
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        with archive_path.open("wb") as dst:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
    except Exception as e:
        log.exception("upload failed for token %s", token)
        shutil.rmtree(target_dir, ignore_errors=True)
        raise HTTPException(500, f"Upload failed: {e}") from e
    return JSONResponse(content={"token": token})


@router.get("/import-zip-toc/{token}")
async def import_zip_toc_stream(token: str) -> StreamingResponse:
    """SSE: ``progress`` per JSON / metadata file as the manifest is built,
    terminated by a single ``manifest`` event carrying the picker payload.
    Avatar URIs in the manifest have already been swapped for proxy URLs
    pointing at :func:`import_zip_preview`."""
    archive_path = _archive_path(token)
    if archive_path is None:
        raise HTTPException(404, "Unknown or expired token")

    async def stream():
        try:
            with zipfile.ZipFile(archive_path) as zf:
                manifest: dict | None = None
                for ev in importers.read_zip_toc(zf):
                    t = ev.get("type")
                    if t == "manifest":
                        manifest = ev["manifest"]
                    elif t == "progress":
                        yield _sse("progress", ev)
                    elif t == "error":
                        yield _sse("error", ev)
                if manifest is None:
                    yield _sse("error", {"message": "Empty manifest"})
                    return
                importers._decorate_manifest(token, manifest)
                yield _sse("manifest", {"manifest": manifest})
        except zipfile.BadZipFile as e:
            yield _sse("error", {"message": f"Invalid zip: {e}"})
        except Exception as e:
            log.exception("toc build failed for token %s", token)
            yield _sse("error", {"message": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/import-zip")
async def import_zip(
    request: Request,
    payload: dict = Body(...),
) -> StreamingResponse:
    """Stream-import the entities the user selected.

    Body: ``{token, selection}``. Same SSE shape as ``/api/import``:
    ``progress``, ``done``, ``error``. The staging dir is wiped on
    completion or error — the token is one-shot.
    """
    token = str(payload.get("token") or "")
    selection = payload.get("selection") or {}
    if not isinstance(selection, dict):
        raise HTTPException(400, "selection must be an object")
    archive_path = _archive_path(token)
    if archive_path is None:
        raise HTTPException(404, "Unknown or expired token")

    async def stream():
        with proxy_rules_scope(getattr(request.app.state, "proxy_rules", None)):
            try:
                async for ev in importers.import_zip_streaming(
                    archive_path, selection,
                ):
                    yield _sse(ev["type"], ev)
            except Exception as e:
                log.exception("zip import failed for token %s", token)
                yield _sse("error", {"message": str(e)})
            finally:
                _wipe_token(token)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/import-zip-token/{token}")
async def import_zip_cancel(token: str) -> Response:
    """Wipe the staging dir for ``token``. Idempotent — used by the
    picker's Cancel path so abandoned uploads don't sit on disk until
    the next server restart."""
    _wipe_token(token)
    return Response(status_code=204)


def _extract_preview_source(data: dict) -> str:
    """Pull the avatar source (data URI or URL) from a single zip member's
    parsed JSON. Knows about both AER ``meta-contact.json`` (carries
    ``avatar_uri`` directly with no ``kind``) and flat-JSON exports
    (kind via :func:`importers.detect_format`)."""
    # AER bulk meta-contact: detect by the unique top-level field.
    if isinstance(data.get("contact_id"), str):
        return str(data.get("avatar_uri") or "")
    try:
        kind = importers.detect_format(data)
    except ValueError:
        return ""
    return importers._avatar_for_kind(data, kind) or ""


@router.get("/import-zip-preview/{token}")
async def import_zip_preview(
    request: Request,
    token: str,
    path: str = Query(..., min_length=1, max_length=1024),
) -> Response:
    """Lazy-thumbnail proxy for picker avatars.

    On first hit, parses the JSON at ``path`` inside the staged archive,
    resolves the avatar (data URI or HTTP URL), runs it through
    :func:`imaging.build_preview_bytes`, and caches the result under the
    token's preview cache. Subsequent hits serve the cached file.

    Returns 404 on any failure — the picker's ``<img onError>`` falls
    through to a monogram in that case, matching the existing UX.
    """
    target_dir = _token_dir(token)
    if target_dir is None:
        raise HTTPException(404, "Unknown or expired token")
    archive_path = target_dir / "archive.zip"
    if not archive_path.exists():
        raise HTTPException(404, "Unknown or expired token")

    cache_path = _preview_cache_path(target_dir, path)
    if cache_path.exists():
        return Response(
            content=cache_path.read_bytes(),
            media_type="image/webp",
            headers={"Cache-Control": f"private, max-age={_PREVIEW_CACHE_TTL}"},
        )

    try:
        with zipfile.ZipFile(archive_path) as zf:
            try:
                raw_json = zf.read(path)
            except KeyError as e:
                raise HTTPException(404, "Member not found") from e
        data = _json_dec.decode(raw_json)
    except (HTTPException, KeyError):
        raise
    except (msgspec.DecodeError, zipfile.BadZipFile):
        raise HTTPException(404, "Could not parse member")
    if not isinstance(data, dict):
        raise HTTPException(404, "Unexpected JSON shape")

    avatar_src = _extract_preview_source(data)
    if not avatar_src:
        raise HTTPException(404, "No resolvable avatar")

    try:
        if avatar_src.startswith("data:"):
            raw = importers._decode_data_uri(avatar_src)
        elif avatar_src.startswith(("http://", "https://")):
            with proxy_rules_scope(getattr(request.app.state, "proxy_rules", None)):
                async with importers._ImageSession() as session:
                    raw = await importers._resolve_image(avatar_src, session)
            if raw is None:
                raise HTTPException(404, "Avatar fetch failed")
        else:
            raise HTTPException(404, "Unrecognised avatar source")
    except HTTPException:
        raise
    except Exception:
        log.exception("avatar source resolution failed")
        raise HTTPException(404, "Avatar source error")

    try:
        out = imaging.build_preview_bytes(raw)
    except Exception:
        log.exception("preview encode failed")
        raise HTTPException(404, "Preview build failed")

    storage.atomic_write_bytes(cache_path, out)
    return Response(
        content=out,
        media_type="image/webp",
        headers={"Cache-Control": f"private, max-age={_PREVIEW_CACHE_TTL}"},
    )


# ---------------------------------------------------------------------------
# Export (per-resource)
# ---------------------------------------------------------------------------


def _filename_safe(s: str, fallback: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in (s or "").strip())
    return safe[:80] or fallback


def _short_id(entity_id: str) -> str:
    """First 8 chars of the UUID — enough to disambiguate exports of
    same-named entities without making the filename ugly."""
    return (entity_id or "")[:8]


@router.get("/export/contact/{contact_id}")
async def export_contact(contact_id: str) -> JSONResponse:
    contact = storage.get_contact(contact_id)
    if contact is None:
        raise HTTPException(404, "contact not found")
    payload = exporters.export_contact(contact)
    name = _filename_safe(contact.name, "contact")
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-{_short_id(contact.id)}.json"'
            )
        },
    )


@router.get("/export/user/{user_id}")
async def export_user(user_id: str) -> JSONResponse:
    user = storage.get_user(user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    payload = exporters.export_user(user)
    name = _filename_safe(user.name, "user")
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-persona-{_short_id(user.id)}.json"'
            )
        },
    )


@router.get("/export/scenario/{scenario_id}")
async def export_scenario(scenario_id: str) -> JSONResponse:
    scenario = storage.get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(404, "scenario not found")
    payload = exporters.export_scenario(scenario)
    name = _filename_safe(scenario.name, "scenario")
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-scenario-{_short_id(scenario.id)}.json"'
            )
        },
    )


@router.get("/export/library/{library_id}")
async def export_library(library_id: str) -> JSONResponse:
    library = storage.get_brain_library(library_id)
    if library is None:
        raise HTTPException(404, "library not found")
    payload = exporters.export_brain_library(library)
    name = _filename_safe(library.name, "library")
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-library-{_short_id(library.id)}.json"'
            )
        },
    )


@router.get("/export/contact/{contact_id}/card")
async def export_contact_card(contact_id: str) -> Response:
    contact = storage.get_contact(contact_id)
    if contact is None:
        raise HTTPException(404, "contact not found")
    blob = exporters.export_contact_card(contact)
    if blob is None:
        raise HTTPException(404, "contact has no card image")
    name = _filename_safe(contact.name, "contact")
    return Response(
        content=blob,
        media_type="image/png",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-{_short_id(contact.id)}.png"'
            )
        },
    )


@router.get("/export/user/{user_id}/card")
async def export_user_card(user_id: str) -> Response:
    user = storage.get_user(user_id)
    if user is None:
        raise HTTPException(404, "user not found")
    blob = exporters.export_user_card(user)
    if blob is None:
        raise HTTPException(404, "user has no card image")
    name = _filename_safe(user.name, "user")
    return Response(
        content=blob,
        media_type="image/png",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-persona-{_short_id(user.id)}.png"'
            )
        },
    )


@router.get("/export/scenario/{scenario_id}/card")
async def export_scenario_card(scenario_id: str) -> Response:
    scenario = storage.get_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(404, "scenario not found")
    blob = exporters.export_scenario_card(scenario)
    if blob is None:
        raise HTTPException(404, "scenario has no card image")
    name = _filename_safe(scenario.name, "scenario")
    return Response(
        content=blob,
        media_type="image/png",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-scenario-{_short_id(scenario.id)}.png"'
            )
        },
    )


@router.get("/export/library/{library_id}/card")
async def export_library_card(library_id: str) -> Response:
    library = storage.get_brain_library(library_id)
    if library is None:
        raise HTTPException(404, "library not found")
    blob = exporters.export_brain_library_card(library)
    if blob is None:
        raise HTTPException(404, "library has no card image")
    name = _filename_safe(library.name, "library")
    return Response(
        content=blob,
        media_type="image/png",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-library-{_short_id(library.id)}.png"'
            )
        },
    )


@router.get("/export/context-preset/{preset_id}")
async def export_context_preset(preset_id: str) -> JSONResponse:
    preset = storage.get_context_preset(preset_id)
    if preset is None:
        raise HTTPException(404, "context preset not found")
    payload = exporters.export_context_preset(preset)
    name = _filename_safe(preset.name, "context-preset")
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-preset-{_short_id(preset.id)}.json"'
            )
        },
    )


@router.get("/export/context-preset/{preset_id}/card")
async def export_context_preset_card(preset_id: str) -> Response:
    preset = storage.get_context_preset(preset_id)
    if preset is None:
        raise HTTPException(404, "context preset not found")
    blob = exporters.export_context_preset_card(preset)
    if blob is None:
        raise HTTPException(404, "context preset has no card image")
    name = _filename_safe(preset.name, "context-preset")
    return Response(
        content=blob,
        media_type="image/png",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{name}-preset-{_short_id(preset.id)}.png"'
            )
        },
    )


@router.post("/import-brains")
async def import_brains(file: UploadFile) -> JSONResponse:
    """Brain-only import: returns the brain list a file would contribute,
    without applying it to any entity. The frontend's "Import brains"
    button uses this to merge brains into the current editor.
    """
    raw = await file.read()
    filename = file.filename or ""
    try:
        result = external_formats.extract_brains(raw, filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return JSONResponse(result)


@router.get("/export/chat/{chat_id}")
async def export_chat(chat_id: str) -> JSONResponse:
    try:
        payload = exporters.export_chat(chat_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    chat = storage.get_chat(chat_id)
    safe_title = _filename_safe(chat.title if chat else "chat", "chat")
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": (
                f'attachment; filename="chat-{safe_title}-{_short_id(chat.id if chat else "")}.json"'
            )
        },
    )


# ---------------------------------------------------------------------------
# Preset import / export
# ---------------------------------------------------------------------------


@router.post("/import-preset")
async def import_preset(file: UploadFile) -> dict:
    data = await _read_json(file)
    if not isinstance(data, dict) or "name" not in data:
        raise HTTPException(400, "Not a valid preset JSON.")
    from server.models import Preset, new_id

    preset = Preset(
        id=new_id(),
        name=str(data.get("name") or "Imported"),
        temperature=float(data.get("temperature") or 0.85),
        top_p=float(data.get("top_p") or 0.95),
        top_k=int(data.get("top_k") or 250),
        min_p=float(data.get("min_p") or 0.0),
        max_tokens=int(data.get("max_tokens") or 1536),
    )
    lib = storage.load_presets()
    lib.presets.append(preset)
    storage.save_presets(lib)
    return {"kind": "preset", "id": preset.id, "name": preset.name}


@router.get("/export/preset/{preset_id}")
async def export_preset(preset_id: str) -> JSONResponse:
    lib = storage.load_presets()
    preset = next((p for p in lib.presets if p.id == preset_id), None)
    if preset is None:
        raise HTTPException(404, "preset not found")
    payload = preset.model_dump(exclude={"id"}, mode="json")
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_filename_safe(preset.name, "preset")}-preset.json"'
            )
        },
    )

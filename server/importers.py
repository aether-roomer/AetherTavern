"""Import character / user / scenario / chat JSON files.

Format detection keys off the field shape (not the filename), so older
exports that omit some fields are still recognised as the same kind of
entity as newer ones. Embedded data-URI images are decoded; HTTP URL images
are downloaded via httpx with a 20 MB cap. Failures on individual images
warn but do not abort the import.

Replace-mode staging: when an import collides with an existing entity by
UUID and the user picks 'replace', the original entity directory is
renamed to ``<dir>.bak`` BEFORE the new content is written. On success
the .bak is controlled-deleted; on failure the .bak is renamed back so
the original survives. Without this, a partial-failure import would
leave the user with neither the original nor a complete replacement.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator

import httpx
import msgspec
import msgspec.json

# Reusable JSON decoder for zip-member parsing (chat composite payloads can
# be hundreds of KB). The canonical-form hashing further down keeps
# stdlib ``json.dumps(..., sort_keys=True)`` since msgspec doesn't sort.
_json_dec = msgspec.json.Decoder()

from server import external_formats, storage
from server.models import (
    Attachment,
    Bookmark,
    BookmarkHistoryEntry,
    Brain,
    BrainLibrary,
    Chat,
    ChatBookmarks,
    ChatMessage,
    ChatMessages,
    Contact,
    ContactScenario,
    ContextPreset,
    CropRect,
    Emotion,
    EMOTION_VALUES,
    ExampleChat,
    ExampleMessage,
    Intimacy,
    Preset,
    ReminderBrain,
    ResponseLength,
    ROOT_PARENT_KEY,
    Scenario,
    Style,
    SubMessage,
    User,
    new_id,
    now_seconds,
)
from server.imaging import build_display_file
from server.routers.files import sniff_dimensions, sniff_image


log = logging.getLogger("aether.importers")

_DATA_URI_RE = re.compile(r"^data:image/[^;]+;base64,(.*)$", re.DOTALL)
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024  # 20 MB

# Conflict-resolution modes, threaded through ``import_streaming``.
#  ``ask``     — abort with a ``conflict`` event so the UI can prompt.
#  ``replace`` — overwrite the existing entity (same UUID, fresh data).
#  ``copy``    — assign a new UUID even when the source had one.
_VALID_MODES = {"ask", "replace", "copy"}


def _strip_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    return s or None


# ---------------------------------------------------------------------------
# Replace-mode staging — preserves originals until the new import succeeds.
# ---------------------------------------------------------------------------


@dataclass
class _ReplaceStaging:
    """Captures the .bak rename done at the start of a replace-mode import.

    The caller threads this through a try/finally: on success it calls
    :func:`_commit_staging` to controlled-delete the .bak; on failure it
    calls :func:`_restore_from_staging` to undo the rename.
    """
    kind: str  # "contact" | "user" | "scenario" | "chat"
    target_id: str
    bak: Path
    original_path: Path


def _stage_replace(kind: str, target_id: str) -> _ReplaceStaging | None:
    """Rename the existing entity dir to ``<dir>.bak`` and drop from index.

    Returns the staging info, or ``None`` if there's no existing entity at
    ``target_id`` (replace becomes a no-op create). Caller MUST commit or
    restore before the request ends.
    """
    if kind == "contact":
        path = storage.contact_dir(target_id)
        paths_idx = storage.index.contact_paths
        entities_idx = storage.index.contacts
    elif kind == "user":
        path = storage.user_dir(target_id)
        paths_idx = storage.index.user_paths
        entities_idx = storage.index.users
    elif kind == "scenario":
        path = storage.scenario_dir(target_id)
        paths_idx = storage.index.scenario_paths
        entities_idx = storage.index.scenarios
    elif kind == "brain_library":
        path = storage.brain_library_dir(target_id)
        paths_idx = storage.index.brain_library_paths
        entities_idx = storage.index.brain_libraries
    elif kind == "context_preset":
        path = storage.context_preset_dir(target_id)
        paths_idx = storage.index.context_preset_paths
        entities_idx = storage.index.context_presets
    elif kind == "chat":
        path = storage.chat_dir(target_id)
        paths_idx = storage.index.chat_paths
        entities_idx = storage.index.chats
    else:
        return None
    if path is None:
        return None
    bak = path.with_name(path.name + ".bak")
    if bak.exists():
        # Stale .bak from a prior failed import — controlled-delete using
        # the per-kind remover so user files survive.
        log.warning("Removing stale .bak before staging: %s", bak)
        remover = storage._REMOVERS.get(kind)
        if remover is not None:
            remover(bak)
    if bak.exists():
        # Stale .bak still has user files. Refuse to overwrite — the user
        # must clean it up manually.
        raise RuntimeError(
            f"Cannot stage replace: stale {bak} contains user files. "
            "Inspect and remove manually before retrying."
        )
    os.rename(path, bak)
    paths_idx.pop(target_id, None)
    entities_idx.pop(target_id, None)
    return _ReplaceStaging(
        kind=kind, target_id=target_id, bak=bak, original_path=path,
    )


def _restore_from_staging(staging: _ReplaceStaging) -> None:
    """Undo a :func:`_stage_replace`: controlled-delete any partial new
    content for ``target_id``, rename .bak back to its original name, and
    re-add the entity to ``storage.index``.

    Best-effort: if the original path is still occupied (user files in the
    partial dir survived controlled-delete), the .bak is left in place
    and a warning is logged — the user can recover manually.
    """
    if staging.kind == "contact":
        new_path = storage.contact_dir(staging.target_id)
        paths_idx = storage.index.contact_paths
        entities_idx = storage.index.contacts
        marker = "info.yaml"
        model_cls = Contact
    elif staging.kind == "user":
        new_path = storage.user_dir(staging.target_id)
        paths_idx = storage.index.user_paths
        entities_idx = storage.index.users
        marker = "info.yaml"
        model_cls = User
    elif staging.kind == "scenario":
        new_path = storage.scenario_dir(staging.target_id)
        paths_idx = storage.index.scenario_paths
        entities_idx = storage.index.scenarios
        marker = "info.yaml"
        model_cls = Scenario
    elif staging.kind == "brain_library":
        new_path = storage.brain_library_dir(staging.target_id)
        paths_idx = storage.index.brain_library_paths
        entities_idx = storage.index.brain_libraries
        marker = "info.yaml"
        model_cls = BrainLibrary
    elif staging.kind == "context_preset":
        new_path = storage.context_preset_dir(staging.target_id)
        paths_idx = storage.index.context_preset_paths
        entities_idx = storage.index.context_presets
        marker = "info.yaml"
        model_cls = ContextPreset
    elif staging.kind == "chat":
        new_path = storage.chat_dir(staging.target_id)
        paths_idx = storage.index.chat_paths
        entities_idx = storage.index.chats
        marker = "chat.yaml"
        model_cls = Chat
    else:
        return
    # Remove the partial new dir if any.
    if new_path is not None and new_path.exists():
        remover = storage._REMOVERS.get(staging.kind)
        if remover is not None:
            remover(new_path)
        paths_idx.pop(staging.target_id, None)
        entities_idx.pop(staging.target_id, None)
    # Rename .bak back to its original location.
    if staging.original_path.exists():
        log.warning(
            "Cannot restore %s: target path %s still exists "
            "(user files survived controlled-delete). Leaving .bak at %s "
            "for manual recovery.",
            staging.target_id, staging.original_path, staging.bak,
        )
        return
    os.rename(staging.bak, staging.original_path)
    paths_idx[staging.target_id] = staging.original_path
    # Re-load the restored entity into the parsed cache.
    restored = storage.load_yaml(staging.original_path / marker, model_cls)
    if restored is not None:
        entities_idx[staging.target_id] = restored


def _commit_staging(staging: _ReplaceStaging) -> None:
    """Controlled-delete the .bak — called when the import succeeded.

    Honours the per-kind remover so any user files the user stashed in
    the original directory survive in the leftover ``.bak`` (the remover
    fails to ``rmdir`` and leaves the dir behind).
    """
    remover = storage._REMOVERS.get(staging.kind)
    if remover is not None:
        remover(staging.bak)


def _resolve_target_id(
    incoming_id: str | None,
    mode: str,
    kind: str,
    incoming_label: str,
    *,
    get_existing,
) -> tuple[str | None, dict | None, bool]:
    """Resolve the target UUID for an import based on ``mode``.

    Returns ``(target_id, conflict_event, should_stage)``. If
    ``conflict_event`` is not None the caller must yield it and abort.
    If ``should_stage`` is True the caller — after acquiring the per-id
    lock — must call :func:`_stage_replace` to .bak-rename the existing
    entity, then commit/restore via try/finally.

    Staging itself is left to the caller so it happens inside the lock,
    which serializes against concurrent imports of the same UUID.
    """
    if not incoming_id:
        return new_id(), None, False
    existing = get_existing(incoming_id)
    if existing is None:
        # Source UUID is new on this server — preserve identity.
        return incoming_id, None, False
    if mode == "replace":
        return incoming_id, None, True
    if mode == "copy":
        return new_id(), None, False
    # ``ask``: surface the conflict and let the UI decide.
    return None, {
        "type": "conflict",
        "kind": kind,
        "id": incoming_id,
        "existing_name": getattr(existing, "name", None)
            or getattr(existing, "title", None)
            or "",
        "incoming_name": incoming_label,
    }, False


# Priority order for synthesising an avatar from an emotion sprite when the
# imported contact carried no dedicated avatar. Roughly neutral → calm →
# expressive → intense, so the auto-pick is something portrait-y when
# possible. Deterministic so re-imports converge on the same choice.
_AVATAR_EMOTION_PRIORITY: tuple[str, ...] = (
    "neutral", "happy", "thinking", "confused", "smug", "shy",
    "playful", "tired", "bored", "surprised", "nervous",
    "embarrassed", "determined", "worried", "sad", "love",
    "excited", "laughing", "scared", "hurt", "irritated",
    "angry", "disgusted", "aroused",
)


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------


def detect_format(data: Any) -> str:
    """Return one of ``"chat" | "contact" | "user" | "scenario" | "brain_library"``.

    Recognises both native AER shapes and foreign formats (ST character
    card v1/v2/v3, ST world-info standalone, lorebook). For foreign
    payloads the caller must run :func:`normalize_foreign` to convert
    the data to AER shape before feeding it to :func:`import_streaming`.
    """
    if isinstance(data, dict):
        # Foreign formats — check first so a ``kind: brain_library``
        # mistakenly added to a foreign shape doesn't shadow recognition.
        spec = data.get("spec")
        if isinstance(spec, str) and spec in ("chara_card_v2", "chara_card_v3"):
            return "contact"
        if isinstance(data.get("lorebookVersion"), (int, float)) and isinstance(
            data.get("entries"), list
        ):
            return "brain_library"
        entries = data.get("entries")
        if isinstance(entries, dict) and entries:
            if any(
                isinstance(v, dict) and ("content" in v or "key" in v)
                for v in entries.values()
            ):
                return "brain_library"
        if external_formats._looks_like_st_card_v1(data):
            return "contact"

        # Native AER shapes. Honour the explicit ``kind`` marker (emitted by
        # every per-resource exporter); fall through to the heuristic only
        # for legacy files that pre-date the marker. Context presets MUST
        # carry the marker — there's no heuristic that distinguishes their
        # shape from arbitrary JSON.
        explicit = data.get("kind")
        if explicit in (
            "contact", "user", "scenario", "brain_library", "context_preset", "chat",
        ):
            return explicit
        if "chat" in data and ("character" in data or "user" in data):
            return "chat"
        if any(
            k in data
            for k in ("emotions", "exampleMessages", "relationship", "greeting", "greetingEmotion")
        ):
            return "contact"
        if data.get("environment") or data.get("scene"):
            # Scenario only if it's NOT a character (handled above).
            return "scenario"
        if "name" in data and ("persona" in data or "appearance" in data or "tags" in data):
            return "user"
    raise ValueError("Could not detect import format from JSON content.")


def normalize_foreign(data: dict, filename: str = "") -> dict:
    """If ``data`` is a recognised foreign format, return the AER-shape
    equivalent. Otherwise return ``data`` unchanged.

    Used at the entry point of any import flow that wants to consume
    foreign payloads through the existing AER-shape pipeline.
    """
    parsed = external_formats.parse_external_json(data, filename)
    if parsed is None:
        return data
    return parsed["payload"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_data_uri(uri: str) -> bytes:
    m = _DATA_URI_RE.match(uri.strip())
    if not m:
        raise ValueError("Not a data URI")
    return base64.b64decode(m.group(1), validate=False)


async def _download_url(session: "_ImageSession", url: str) -> bytes:
    return await session.fetch(url)


class _ImageSession:
    """Tiny abstraction so we can swap the HTTP client for the rare host that
    blocks default httpx requests. Uses ``curl_cffi`` to impersonate a recent
    Chrome's TLS fingerprint, which gets past the bot filters on hosts like
    catbox.moe / imgur.com that 403 plain httpx requests.

    Proxy resolution: when a proxy-rules file is in scope (set by the
    route via :func:`server.proxy_rules.proxy_rules_scope`),
    :meth:`fetch` runs per-URL ``lookup(category="image-import")`` and
    passes a per-request ``proxies={}`` to ``curl_cffi``. With no rules
    in scope, requests go DIRECT.
    """

    def __init__(self) -> None:
        self._session = None
        self._proxy_rules = None

    async def __aenter__(self):
        from curl_cffi.requests import AsyncSession

        from server.proxy_rules import current_proxy_rules
        self._proxy_rules = current_proxy_rules()

        self._session = AsyncSession(impersonate="chrome")
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *exc):
        if self._session is not None:
            await self._session.__aexit__(*exc)
            self._session = None

    def _proxies_for(self, url: str) -> dict[str, str] | None:
        """Per-URL ``proxies=`` kwarg for ``curl_cffi``.

        Returns ``None`` when no rules file is loaded (session-level
        proxy applies, if any) or when the matching rule says DIRECT.
        Raises :class:`server.proxy_rules.NoMatchingProxyRule` when a
        rules file is loaded and no rule matches the URL.
        """
        if self._proxy_rules is None:
            return None
        p = self._proxy_rules.lookup(url=url, category="image-import")
        if not p:
            return None
        return {"http": p, "https": p}

    async def fetch(self, url: str) -> bytes:
        # Imgur's bot filter wraps direct image hits in an HTML "post page"
        # unless the request looks like it came from imgur.com itself, so we
        # send a Referer up front for any imgur host. The HTML response comes
        # back as 200 text/html — detected below.
        headers = {"Referer": "https://imgur.com/"} if "imgur.com" in url else None
        proxies = self._proxies_for(url)
        r = await self._session.get(url, allow_redirects=True, timeout=30, headers=headers, proxies=proxies)
        # Belt-and-braces: if we still get an HTML interstitial, prime the
        # session by visiting the gallery URL and retry once.
        if self._looks_like_imgur_block(url, r):
            album = self._imgur_album_url(url)
            if album:
                await self._session.get(album, allow_redirects=True, timeout=30, proxies=self._proxies_for(album))
                r = await self._session.get(url, allow_redirects=True, timeout=30, headers=headers, proxies=proxies)
        if r.status_code >= 400:
            raise ValueError(f"upstream {r.status_code}")
        if self._looks_like_imgur_block(url, r):
            raise ValueError("imgur returned HTML (bot filter)")
        cl = r.headers.get("content-length")
        if cl and int(cl) > _MAX_DOWNLOAD_BYTES:
            raise ValueError("image too large")
        data = r.content
        if len(data) > _MAX_DOWNLOAD_BYTES:
            raise ValueError("image too large")
        return data

    @staticmethod
    def _looks_like_imgur_block(url: str, response) -> bool:
        if "imgur.com" not in url:
            return False
        if response.status_code in (403, 429):
            return True
        ct = (response.headers.get("content-type") or "").lower()
        return ct.startswith("text/html")

    @staticmethod
    def _imgur_album_url(url: str) -> str | None:
        # Strip any subdomain (i. or whatever) and pull the gallery id.
        m = re.match(
            r"https?://(?:[a-z0-9.-]*\.)?imgur\.com/([A-Za-z0-9]+)",
            url,
        )
        if not m:
            return None
        return f"https://imgur.com/{m.group(1)}"


async def _resolve_image(value: Any, session: _ImageSession) -> bytes | None:
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    try:
        if _DATA_URI_RE.match(s):
            return _decode_data_uri(s)
        if _HTTP_URL_RE.match(s):
            return await _download_url(session, s)
    except Exception as e:
        log.warning("Failed to resolve image (%s): %s", s[:60], e)
    return None


def _opt_emotion(value: Any) -> Emotion | None:
    if not value:
        return None
    s = str(value).strip().lower()
    if s in EMOTION_VALUES:
        return Emotion(s)
    return None


def _opt_intimacy(value: Any) -> Intimacy:
    s = (str(value or "")).strip().lower()
    try:
        return Intimacy(s)
    except ValueError:
        return Intimacy.STRANGER


def _opt_style(value: Any) -> Style:
    s = (str(value or "")).strip().lower()
    try:
        return Style(s)
    except ValueError:
        return Style.CHAT


def _clamp_int(value: Any, lo: int, hi: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


def _opt_focal_point(raw: Any) -> tuple[float, float] | None:
    if not isinstance(raw, dict):
        return None
    try:
        x = float(raw.get("x", 0.5))
        y = float(raw.get("y", 0.5))
    except (TypeError, ValueError):
        return None
    return (max(0.0, min(1.0, x)), max(0.0, min(1.0, y)))


def _opt_response_length(value: Any) -> ResponseLength | None:
    if not value:
        return None
    s = str(value).strip().lower()
    if s in ("unspecified", "default", "any", ""):
        return None
    try:
        return ResponseLength(s)
    except ValueError:
        return None


def _opt_intimacy_or_none(value: Any) -> Intimacy | None:
    if not value:
        return None
    s = str(value).strip().lower()
    try:
        return Intimacy(s)
    except ValueError:
        return None


def _opt_style_or_none(value: Any) -> Style | None:
    if not value:
        return None
    s = str(value).strip().lower()
    try:
        return Style(s)
    except ValueError:
        return None


def _chat_tags_to_string(value: Any) -> str:
    """Map a ``chat_tags`` array (foreign format) or a ``tags`` string (our
    format) onto our comma-separated tags string."""
    if isinstance(value, list):
        return ", ".join(str(t).strip() for t in value if str(t).strip())
    if isinstance(value, str):
        return value
    return ""


def _import_contact_scenarios(raw: Any) -> tuple[list[ContactScenario], str | None]:
    """Build a list of ``ContactScenario`` from the foreign ``presetSpaces``
    array (or our own ``scenarios``). Returns ``(scenarios, default_id)`` —
    the first entry flagged ``is_default: true`` becomes the default."""
    out: list[ContactScenario] = []
    default_id: str | None = None
    if not isinstance(raw, list):
        return out, default_id
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        cs = ContactScenario(
            name=name,
            description=str(entry.get("description") or ""),
            environment=str(entry.get("environment") or ""),
            scene=str(entry.get("scene") or ""),
            tags=_chat_tags_to_string(entry.get("tags") or entry.get("chat_tags")),
            cjk=bool(entry.get("cjk")),
            greeting=str(entry.get("greeting") or ""),
            greeting_emotion=_opt_emotion(entry.get("greeting_emotion") or entry.get("greetingEmotion")),
            style=_opt_style_or_none(entry.get("style")),
            intimacy=_opt_intimacy_or_none(entry.get("intimacy") or entry.get("relationship")),
            response_length=_opt_response_length(entry.get("response_length") or entry.get("responseLength")),
            brains=_import_brains(entry.get("brains") or []),
        )
        out.append(cs)
        if default_id is None and bool(entry.get("is_default") or entry.get("isDefault")):
            default_id = cs.id
    return out, default_id


def _import_reminder_brain(raw: Any) -> ReminderBrain | None:
    """Build a ReminderBrain from the imported ``reminderBrain`` payload.

    Returns ``None`` if the input is missing, malformed, or has no
    content (an empty reminder is indistinguishable from "no reminder"
    in practice).
    """
    if not isinstance(raw, dict):
        return None
    content = str(raw.get("content") or "").strip()
    if not content:
        return None
    name = str(raw.get("name") or "").strip()
    depth_raw = raw.get("depth", 0)
    try:
        depth = int(depth_raw)
    except (TypeError, ValueError):
        depth = 0
    depth = max(0, min(10, depth))
    payload: dict = {"name": name, "content": content, "depth": depth}
    if isinstance(raw.get("id"), str) and raw["id"]:
        payload["id"] = raw["id"]
    if bool(raw.get("disabled")):
        payload["disabled"] = True
    try:
        return ReminderBrain.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("Skipping malformed reminder brain: %s", exc)
        return None


def _strip_brain_block_framing(content: str, name: str) -> str:
    """Strip AER brain-block framing from imported brain content.

    Some sources (esp. brains hand-copied from a rendered prompt) inline
    the ``----`` separator and the brain name header that the AER
    renderer would add. If we kept them, the live prompt would render
    them twice. Strip in two steps: leading ``----``-only line first,
    then the ``{name}``-only line that follows.
    """
    text = content
    if text.startswith("----"):
        # Match "----" optionally followed by spaces, then newline.
        nl = text.find("\n")
        if nl != -1 and text[:nl].rstrip() == "----":
            text = text[nl + 1 :]
    if name and text.startswith(name):
        # Match "{name}" exactly, optionally followed by trailing spaces, on its own line.
        nl = text.find("\n")
        head = text[:nl] if nl != -1 else text
        if head.rstrip() == name:
            text = text[nl + 1 :] if nl != -1 else ""
    return text


def _import_brains(raw: Any) -> list[Brain]:
    """Tolerantly import a brain list.

    Required: ``name`` and ``content`` must be non-empty. Optional new fields
    (``id``, ``keys``, ``cascades``, ``blocks_recursion``, ``advanced``)
    round-trip through Pydantic validation; on validation failure for those
    optional fields we keep the brain but drop them silently so old / partial
    archives still import cleanly.

    Content sanitisation: leading ``----`` separator and a matching
    name header are stripped — see :func:`_strip_brain_block_framing`.
    """
    out: list[Brain] = []
    for b in raw or []:
        if not isinstance(b, dict):
            continue
        name = str(b.get("name") or "").strip()
        content = str(b.get("content") or "").strip()
        if not (name and content):
            continue
        content = _strip_brain_block_framing(content, name).strip()
        if not content:
            continue
        payload: dict = {"name": name, "content": content}
        if isinstance(b.get("id"), str) and b["id"]:
            payload["id"] = b["id"]
        for opt in ("keys", "cascades", "blocks_recursion", "advanced", "disabled"):
            if opt in b and b[opt] is not None:
                payload[opt] = b[opt]
        try:
            out.append(Brain.model_validate(payload))
        except Exception as exc:
            log.warning("Skipping malformed optional brain fields for %r: %s", name, exc)
            out.append(Brain(name=name, content=content))
    return out


def _import_example_chats(raw: Any) -> list[ExampleChat]:
    out: list[ExampleChat] = []
    for ex in raw or []:
        if not isinstance(ex, dict):
            continue
        msgs: list[ExampleMessage] = []
        for m in ex.get("messages") or []:
            if not isinstance(m, dict) or not m.get("text"):
                continue
            is_contact = bool(m.get("isContact"))
            emotion = _opt_emotion(m.get("emotion"))
            if is_contact and emotion is None:
                emotion = Emotion.NEUTRAL
            msgs.append(
                ExampleMessage(
                    is_contact=is_contact,
                    text=str(m["text"]),
                    emotion=emotion,
                )
            )
        if msgs:
            out.append(
                ExampleChat(
                    name=str(ex.get("name") or ""),
                    style=_opt_style(ex.get("style")),
                    user_name=str(ex.get("userName") or ""),
                    messages=msgs,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Contact (character)
# ---------------------------------------------------------------------------


def _import_crop(raw: Any) -> CropRect | None:
    """Accept either our ``{x, y, w, h}`` shape or the v9 ``facePosition``
    ``{x, y, width, height}`` shape."""
    if not isinstance(raw, dict):
        return None
    try:
        return CropRect(
            x=float(raw.get("x", 0)),
            y=float(raw.get("y", 0)),
            w=float(raw.get("w", raw.get("width", 1))),
            h=float(raw.get("h", raw.get("height", 1))),
        )
    except (TypeError, ValueError):
        return None


def _square_in_pixels(crop: CropRect | None, dims: tuple[int, int] | None) -> CropRect | None:
    """Coerce a crop into this codebase's convention: a rectangle that's
    SQUARE in image pixels (``crop.w * imgW == crop.h * imgH``), keeping the
    original rect's centre as the focal point.

    The source ``facePosition`` rect (where ``width == height`` in normalised
    space) covers a region whose centre is the face; the original tool
    renders it with ``object-fit: cover`` so the focal point stays put while
    the long axis is trimmed equally on both sides. We mirror that here by
    shrinking the rect to the largest centred pixel-square that fits inside
    it.

    Already pixel-square crops, ``None`` inputs, and unknown image dims pass
    through unchanged.
    """
    if crop is None or dims is None:
        return crop
    img_w, img_h = dims
    if img_w <= 0 or img_h <= 0:
        return crop
    rect_w_px = crop.w * img_w
    rect_h_px = crop.h * img_h
    # Already pixel-square (within 1 px) — leave alone.
    if abs(rect_w_px - rect_h_px) < 1.0:
        return crop

    side_px = min(rect_w_px, rect_h_px)
    new_w = side_px / img_w
    new_h = side_px / img_h
    cx = crop.x + crop.w / 2
    cy = crop.y + crop.h / 2
    new_x = max(0.0, min(1.0 - new_w, cx - new_w / 2))
    new_y = max(0.0, min(1.0 - new_h, cy - new_h / 2))
    return CropRect(x=new_x, y=new_y, w=new_w, h=new_h)


async def import_contact_streaming(
    data: dict, *, mode: str = "ask", target_id: str | None = None,
):
    """Async-generator variant of :func:`import_contact` that yields progress
    events. Each event is a ``dict`` of shape ``{"type": "progress", "label":
    str, "current": int, "total": int}`` while work is in flight, ending with
    ``{"type": "done", "kind": "contact", "id": str, "name": str}``.

    ``mode`` controls UUID conflict handling — see :data:`_VALID_MODES`.
    ``target_id`` overrides ``mode`` when set: caller has already resolved
    the destination UUID (and verified it doesn't collide); staging is the
    caller's responsibility in that case.
    """
    incoming_name = str(data.get("name") or "Imported")
    should_stage = False
    if target_id is None:
        target_id, conflict, should_stage = _resolve_target_id(
            _strip_id(data.get("id")),
            mode,
            "contact",
            incoming_name,
            get_existing=storage.get_contact,
        )
        if conflict is not None:
            yield conflict
            return

    async with storage.lock(f"contact:{target_id}"):
        staging = _stage_replace("contact", target_id) if should_stage else None
        committed = False
        try:
            async for ev in _import_contact_streaming_inner(
                data, target_id, incoming_name,
            ):
                yield ev
            if staging is not None:
                _commit_staging(staging)
                committed = True
        finally:
            if staging is not None and not committed:
                _restore_from_staging(staging)


async def _import_contact_streaming_inner(
    data: dict, target_id: str, incoming_name: str,
):
    """Body of :func:`import_contact_streaming` — split out so the staging
    try/finally in the wrapper covers the whole import including the
    ``done`` yield."""
    avatar_crop_in = _import_crop(data.get("avatarCrop"))
    emotions_crop_in = _import_crop(data.get("emotionsCrop"))
    fp_crop = _import_crop(data.get("facePosition"))
    avatar_crop_in = avatar_crop_in or fp_crop
    emotions_crop_in = emotions_crop_in or fp_crop

    contact_scenarios, default_scenario_id = _import_contact_scenarios(
        data.get("scenarios") or data.get("presetSpaces"),
    )
    contact = Contact(
        id=target_id,
        name=incoming_name,
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        species=str(data.get("species") or ""),
        gender=str(data.get("gndr") or data.get("gender") or ""),
        pronouns=str(data.get("pronouns") or ""),
        persona=str(data.get("persona") or ""),
        appearance=str(data.get("appearance") or ""),
        greeting=str(data.get("greeting") or ""),
        greeting_emotion=_opt_emotion(data.get("greetingEmotion")) or Emotion.NEUTRAL,
        default_intimacy=_opt_intimacy(data.get("relationship")),
        default_style=_opt_style(data.get("style")),
        default_response_length=_opt_response_length(
            data.get("responseLength") or data.get("response_length")
        ),
        cjk=bool(data.get("cjk")),
        tags=str(data.get("tags") or ""),
        example_chats=_import_example_chats(data.get("exampleMessages") or []),
        brains=_import_brains(data.get("brains") or []),
        reminder_brain=_import_reminder_brain(data.get("reminderBrain")),
        scenarios=contact_scenarios,
        default_scenario_id=default_scenario_id,
        favorite=bool(data.get("favorite")),
    )
    # Resolve default scenario id by name when the foreign-format
    # path used ``is_default: true`` flagging rather than an explicit id.
    if contact.default_scenario_id is None and contact_scenarios:
        flagged = next(
            (cs.id for cs, src in zip(contact_scenarios, (data.get("scenarios") or [])[: len(contact_scenarios)])
             if isinstance(src, dict) and bool(src.get("is_default"))),
            None,
        )
        if flagged is not None:
            contact.default_scenario_id = flagged
    storage.save_contact(contact)
    contact_dir = storage.contact_dir(contact.id)

    avatar_ref = data.get("avatarUri")
    has_avatar_work = isinstance(avatar_ref, str) and avatar_ref.strip()
    card_ref = data.get("cardImageUri") or data.get("cardImage")
    has_card_work = isinstance(card_ref, str) and card_ref.strip()

    emotions = data.get("emotions") or {}
    emotion_jobs: list[tuple[str, str]] = []
    if isinstance(emotions, dict):
        for em_name, ref in emotions.items():
            if _opt_emotion(em_name) is None:
                continue
            if not (isinstance(ref, str) and ref.strip()):
                continue
            emotion_jobs.append((em_name, ref))

    total = (1 if has_avatar_work else 0) + (1 if has_card_work else 0) + len(emotion_jobs)
    done = 0
    failed = 0
    yield {"type": "progress", "label": f"Importing {contact.name}",
           "phase": "download",
           "current": done, "total": total, "failed": failed}

    avatar_dims: tuple[int, int] | None = None
    sprite_dims: tuple[int, int] | None = None
    card_bytes: bytes | None = None  # kept around for avatar synthesis below

    if contact_dir is not None:
        async with _ImageSession() as session:
            if has_avatar_work:
                yield {"type": "progress", "label": "Downloading avatar",
                       "phase": "download",
                       "current": done, "total": total, "failed": failed}
                blob = await _resolve_image(avatar_ref, session)
                ok = False
                if blob is not None:
                    try:
                        ext, _ = sniff_image(blob)
                        fname = f"avatar.{ext}"
                        storage.atomic_write_bytes(contact_dir / fname, blob)
                        contact.avatar = fname
                        avatar_dims = sniff_dimensions(blob)
                        ok = True
                    except Exception as e:
                        log.warning("Skipping avatar: %s", e)
                if not ok:
                    failed += 1
                done += 1
                yield {"type": "progress", "label": "Avatar downloaded",
                       "phase": "download",
                       "current": done, "total": total, "failed": failed}

            if has_card_work:
                yield {"type": "progress", "label": "Downloading card image",
                       "phase": "download",
                       "current": done, "total": total, "failed": failed}
                blob = await _resolve_image(card_ref, session)
                ok = False
                if blob is not None:
                    try:
                        ext, _ = sniff_image(blob)
                        fname = f"card.{ext}"
                        storage.atomic_write_bytes(contact_dir / fname, blob)
                        contact.card_image = fname
                        card_bytes = blob
                        ok = True
                    except Exception as e:
                        log.warning("Skipping card image: %s", e)
                if not ok:
                    failed += 1
                done += 1
                yield {"type": "progress", "label": "Card image downloaded",
                       "phase": "download",
                       "current": done, "total": total, "failed": failed}

            if emotion_jobs:
                (contact_dir / "emotions").mkdir(parents=True, exist_ok=True)
                for em_name, ref in emotion_jobs:
                    yield {"type": "progress",
                           "label": f"Downloading {em_name} sprite",
                           "phase": "download",
                           "current": done, "total": total, "failed": failed}
                    blob = await _resolve_image(ref, session)
                    ok = False
                    if blob is not None:
                        try:
                            ext, _ = sniff_image(blob)
                            fname = f"{em_name.lower()}.{ext}"
                            storage.atomic_write_bytes(contact_dir / "emotions" / fname, blob)
                            contact.emotions[em_name.lower()] = fname
                            # Prefer neutral's dims as the canonical sprite size.
                            if sprite_dims is None or em_name.lower() == "neutral":
                                d = sniff_dimensions(blob)
                                if d is not None:
                                    sprite_dims = d
                            ok = True
                        except Exception as e:
                            log.warning("Skipping emotion %s: %s", em_name, e)
                    if not ok:
                        failed += 1
                    done += 1
                    yield {"type": "progress",
                           "label": f"{em_name} downloaded",
                           "phase": "download",
                           "current": done, "total": total, "failed": failed}

    # Card-image avatar fallback. When the source carried a card image but
    # no explicit avatar (the common ST-card case — the card *is* the
    # portrait), copy the card bytes to ``avatar.<ext>`` so the entity
    # has a usable portrait without the user having to set one manually.
    if (
        contact.avatar is None
        and contact.card_image is not None
        and card_bytes is not None
        and contact_dir is not None
    ):
        try:
            ext, _ = sniff_image(card_bytes)
            avatar_fname = f"avatar.{ext}"
            storage.atomic_write_bytes(contact_dir / avatar_fname, card_bytes)
            contact.avatar = avatar_fname
            avatar_dims = sniff_dimensions(card_bytes)
        except Exception as e:
            log.warning("Could not synthesise avatar from card image: %s", e)

    # Synthesise an avatar from a fallback emotion sprite when no card
    # image or explicit avatar was provided. Gives the avatar editor a
    # real file to crop/remove, lets ``avatarEl`` show a portrait
    # without falling through to the neutral-sprite branch, and gives
    # ``renderEmotionSprite`` a concrete avatar to use when a specific
    # emotion sprite is missing.
    if (
        contact.avatar is None
        and contact.emotions
        and contact_dir is not None
    ):
        for em_name in _AVATAR_EMOTION_PRIORITY:
            sprite = contact.emotions.get(em_name)
            if not sprite:
                continue
            sprite_path = contact_dir / "emotions" / sprite
            if not sprite_path.exists():
                continue
            try:
                ext = sprite_path.suffix.lstrip(".") or "png"
                avatar_fname = f"avatar.{ext}"
                storage.atomic_write_bytes(
                    contact_dir / avatar_fname, sprite_path.read_bytes()
                )
                contact.avatar = avatar_fname
                avatar_dims = sprite_dims
            except Exception as e:
                log.warning("Could not synthesise avatar from %s: %s", em_name, e)
            break

    # Crops only make sense alongside the images they describe; clear them when
    # the corresponding download didn't land. ``_square_in_pixels`` no-ops if
    # we don't know the dimensions or the source rect is already non-square.
    contact.avatar_crop = (
        _square_in_pixels(avatar_crop_in, avatar_dims) if contact.avatar else None
    )
    contact.emotions_crop = (
        _square_in_pixels(emotions_crop_in, sprite_dims) if contact.emotions else None
    )

    if contact_dir is not None:
        # Derivative builds (small WebP siblings used by every list row +
        # bubble) run synchronously and can take noticeable time when there
        # are 24 emotion sprites — enough to make the import look stuck if
        # the progress UI stalls on the last download. Emit a separate
        # ``derive`` phase so the bar restarts and the label tracks each
        # build.
        derive_jobs: list[tuple[str, Path, CropRect | None]] = []
        if contact.avatar:
            derive_jobs.append((
                "avatar", contact_dir / contact.avatar, contact.avatar_crop,
            ))
        if contact.card_image:
            derive_jobs.append((
                "card", contact_dir / contact.card_image, None,
            ))
        if contact.emotions:
            em_dir = contact_dir / "emotions"
            for em_name, fname in contact.emotions.items():
                p = em_dir / fname
                if p.exists():
                    derive_jobs.append((em_name, p, contact.emotions_crop))

        d_total = len(derive_jobs)
        d_done = 0
        d_failed = 0
        for label_name, src, crop in derive_jobs:
            yield {"type": "progress",
                   "label": f"Saving {label_name} display image",
                   "phase": "derive",
                   "current": d_done, "total": d_total, "failed": d_failed}
            try:
                build_display_file(src, crop)
            except Exception as e:
                log.warning("Could not build display for %s: %s", src.name, e)
                d_failed += 1
            d_done += 1
            # Capitalise sentence-initial: emotion / "avatar" reads better
            # as ``Neutral display image saved`` than ``neutral display
            # image saved``.
            yield {"type": "progress",
                   "label": f"{label_name.capitalize()} display image saved",
                   "phase": "derive",
                   "current": d_done, "total": d_total, "failed": d_failed}

    storage.save_contact(contact)
    yield {"type": "done", "kind": "contact", "id": contact.id, "name": contact.name}


async def import_streaming(
    data: dict, *, mode: str = "ask", resolutions: dict | None = None,
    filename: str = "",
):
    """Top-level streaming dispatch. Yields progress + done events; raises
    ``ValueError`` if the data shape isn't recognised.

    ``mode`` is the conflict-resolution strategy applied to the *top-level*
    entity. ``resolutions`` (chat composites only) maps each sidecar role
    (``character`` / ``user`` / ``scenario``) to either an existing entity
    UUID to reuse, or the string ``"new"`` to import the source data.
    ``filename`` is used purely as a naming hint for foreign formats
    that don't carry a name field of their own (standalone WI / lorebook).
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}")
    # Foreign formats normalise to AER-shape JSON first.
    data = normalize_foreign(data, filename)
    kind = detect_format(data)
    if kind == "contact":
        async for ev in import_contact_streaming(data, mode=mode):
            yield ev
    elif kind == "user":
        target_id, conflict, should_stage = _resolve_target_id(
            _strip_id(data.get("id")), mode, "user",
            str(data.get("name") or ""),
            get_existing=storage.get_user,
        )
        if conflict is not None:
            yield conflict
            return
        u = await _import_user_at(data, target_id, should_stage=should_stage)
        yield {"type": "done", "kind": "user", "id": u.id, "name": u.name}
    elif kind == "scenario":
        target_id, conflict, should_stage = _resolve_target_id(
            _strip_id(data.get("id")), mode, "scenario",
            str(data.get("name") or ""),
            get_existing=storage.get_scenario,
        )
        if conflict is not None:
            yield conflict
            return
        s = await _import_scenario_at(data, target_id, should_stage=should_stage)
        yield {"type": "done", "kind": "scenario", "id": s.id, "name": s.name}
    elif kind == "brain_library":
        target_id, conflict, should_stage = _resolve_target_id(
            _strip_id(data.get("id")), mode, "brain_library",
            str(data.get("name") or ""),
            get_existing=storage.get_brain_library,
        )
        if conflict is not None:
            yield conflict
            return
        lib = await _import_brain_library_at(data, target_id, should_stage=should_stage)
        yield {"type": "done", "kind": "brain_library", "id": lib.id, "name": lib.name}
    elif kind == "context_preset":
        target_id, conflict, should_stage = _resolve_target_id(
            _strip_id(data.get("id")), mode, "context_preset",
            str(data.get("name") or ""),
            get_existing=storage.get_context_preset,
        )
        if conflict is not None:
            yield conflict
            return
        preset = await _import_context_preset_at(data, target_id, should_stage=should_stage)
        yield {"type": "done", "kind": "context_preset", "id": preset.id, "name": preset.name}
    elif kind == "chat":
        async for ev in _import_chat_streaming(data, mode=mode, resolutions=resolutions):
            yield ev
    else:
        raise ValueError(f"Unsupported import kind {kind!r}")


async def import_contact(data: dict) -> Contact:
    """Non-streaming convenience wrapper used by the chat-composite import.

    Drains :func:`import_contact_streaming` to share the dim-aware crop logic
    instead of duplicating download bookkeeping.
    """
    contact_id: str | None = None
    async for ev in import_contact_streaming(data):
        if ev.get("type") == "done":
            contact_id = ev.get("id")
    if contact_id is None:
        # Should not happen — streaming always emits a done event.
        return storage.save_contact(Contact(name=str(data.get("name") or "Imported")))
    contact = storage.get_contact(contact_id)
    if contact is None:
        return storage.save_contact(Contact(name=str(data.get("name") or "Imported")))
    return contact


# ---------------------------------------------------------------------------
# User persona / Scenario
# ---------------------------------------------------------------------------


def _build_user(data: dict, target_id: str) -> User:
    return User(
        id=target_id,
        name=str(data.get("name") or "Imported"),
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        species=str(data.get("species") or data.get("userSpecies") or ""),
        gender=str(data.get("gndr") or data.get("gender") or data.get("userGender") or ""),
        pronouns=str(data.get("pronouns") or data.get("userPronouns") or ""),
        persona=str(data.get("persona") or data.get("userPersonality") or ""),
        appearance=str(data.get("appearance") or data.get("userAppearance") or ""),
        tags=str(data.get("tags") or data.get("userTags") or ""),
        cjk=bool(data.get("cjk")),
        brains=_import_brains(data.get("brains") or []),
        favorite=bool(data.get("favorite")),
    )


def _build_scenario(data: dict, target_id: str) -> Scenario:
    mode_raw = (str(data.get("backgroundMode") or "cover")).strip().lower()
    mode = "tile" if mode_raw == "tile" else "cover"
    return Scenario(
        id=target_id,
        name=str(data.get("name") or "Imported"),
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        environment=str(data.get("environment") or ""),
        scene=str(data.get("scene") or ""),
        tags=str(data.get("tags") or data.get("chatTags") or ""),
        cjk=bool(data.get("cjk")),
        brains=_import_brains(data.get("brains") or []),
        favorite=bool(data.get("favorite")),
        avatar_crop=_import_crop(data.get("avatarCrop")),
        background_focal=_opt_focal_point(data.get("backgroundFocal")),
        background_mode=mode,
        background_blur=_clamp_int(data.get("backgroundBlur"), 0, 20),
        background_dim=_clamp_int(data.get("backgroundDim"), 0, 100),
        background_brighten=_clamp_int(data.get("backgroundBrighten"), 0, 100),
        background_tint_strength=_clamp_int(data.get("backgroundTintStrength"), 0, 100),
    )


async def _import_scenario_at(
    data: dict, target_id: str, *, should_stage: bool = False,
) -> Scenario:
    """Build, persist, and (if the source carries one) download the background
    image for a scenario at the given UUID. Mirrors :func:`_import_user_at`.

    ``should_stage`` (replace mode): rename the existing entity to .bak
    inside the lock, commit on success, restore on failure.
    """
    async with storage.lock(f"scenario:{target_id}"):
        staging = _stage_replace("scenario", target_id) if should_stage else None
        committed = False
        try:
            scenario = _build_scenario(data, target_id)
            storage.save_scenario(scenario)
            sdir = storage.scenario_dir(scenario.id)
            if sdir is None:
                if staging is not None:
                    _commit_staging(staging)
                    committed = True
                return scenario

            bg_ref = data.get("backgroundImageUri")
            card_ref = data.get("cardImageUri") or data.get("cardImage")
            card_bytes: bytes | None = None
            async with _ImageSession() as session:
                if isinstance(bg_ref, str) and bg_ref.strip():
                    blob = await _resolve_image(bg_ref, session)
                    if blob is not None:
                        try:
                            ext, _ = sniff_image(blob)
                            fname = f"background.{ext}"
                            storage.atomic_write_bytes(sdir / fname, blob)
                            scenario.background_image = fname
                            build_display_file(sdir / fname, scenario.avatar_crop)
                        except Exception as e:
                            log.warning("Skipping scenario background: %s", e)
                if isinstance(card_ref, str) and card_ref.strip():
                    blob = await _resolve_image(card_ref, session)
                    if blob is not None:
                        try:
                            ext, _ = sniff_image(blob)
                            fname = f"card.{ext}"
                            storage.atomic_write_bytes(sdir / fname, blob)
                            scenario.card_image = fname
                            card_bytes = blob
                            build_display_file(sdir / fname, None)
                        except Exception as e:
                            log.warning("Skipping scenario card image: %s", e)

            # If no background was supplied but a card image was,
            # synthesise the background from the card so the scenario
            # has a usable atmospheric image.
            if scenario.background_image is None and card_bytes is not None:
                try:
                    ext, _ = sniff_image(card_bytes)
                    fname = f"background.{ext}"
                    storage.atomic_write_bytes(sdir / fname, card_bytes)
                    scenario.background_image = fname
                    build_display_file(sdir / fname, scenario.avatar_crop)
                except Exception as e:
                    log.warning("Could not synthesise background from card image: %s", e)

            storage.save_scenario(scenario)
            if staging is not None:
                _commit_staging(staging)
                committed = True
            return scenario
        finally:
            if staging is not None and not committed:
                _restore_from_staging(staging)


async def _import_user_at(
    data: dict, target_id: str, *, should_stage: bool = False,
) -> User:
    """Build, persist, and (if the source carries one) download the avatar
    for a user persona at the given UUID. Mirrors the contact-importer flow
    but skips the emotion-sprite branch since users don't have those.

    ``should_stage`` (replace mode): rename the existing entity to .bak
    inside the lock, commit on success, restore on failure.
    """
    async with storage.lock(f"user:{target_id}"):
        staging = _stage_replace("user", target_id) if should_stage else None
        committed = False
        try:
            user = _build_user(data, target_id)
            storage.save_user(user)
            udir = storage.user_dir(user.id)
            if udir is None:
                if staging is not None:
                    _commit_staging(staging)
                    committed = True
                return user

            avatar_ref = data.get("avatarUri")
            card_ref = data.get("cardImageUri") or data.get("cardImage")
            avatar_crop_in = _import_crop(data.get("avatarCrop"))
            avatar_blob: bytes | None = None
            card_blob: bytes | None = None
            async with _ImageSession() as session:
                if isinstance(avatar_ref, str) and avatar_ref.strip():
                    blob = await _resolve_image(avatar_ref, session)
                    if blob is not None:
                        try:
                            ext, _ = sniff_image(blob)
                            fname = f"avatar.{ext}"
                            storage.atomic_write_bytes(udir / fname, blob)
                            user.avatar = fname
                            avatar_blob = blob
                        except Exception as e:
                            log.warning("Skipping user avatar: %s", e)
                if isinstance(card_ref, str) and card_ref.strip():
                    blob = await _resolve_image(card_ref, session)
                    if blob is not None:
                        try:
                            ext, _ = sniff_image(blob)
                            fname = f"card.{ext}"
                            storage.atomic_write_bytes(udir / fname, blob)
                            user.card_image = fname
                            card_blob = blob
                            build_display_file(udir / fname, None)
                        except Exception as e:
                            log.warning("Skipping user card image: %s", e)

            # Avatar synthesis from card image when no explicit avatar.
            if user.avatar is None and card_blob is not None:
                try:
                    ext, _ = sniff_image(card_blob)
                    fname = f"avatar.{ext}"
                    storage.atomic_write_bytes(udir / fname, card_blob)
                    user.avatar = fname
                    avatar_blob = card_blob
                except Exception as e:
                    log.warning("Could not synthesise user avatar from card: %s", e)

            if user.avatar and avatar_blob is not None:
                if avatar_crop_in is not None:
                    user.avatar_crop = _square_in_pixels(
                        avatar_crop_in, sniff_dimensions(avatar_blob),
                    )
                build_display_file(udir / user.avatar, user.avatar_crop)

            storage.save_user(user)
            if staging is not None:
                _commit_staging(staging)
                committed = True
            return user
        finally:
            if staging is not None and not committed:
                _restore_from_staging(staging)


def import_user(data: dict) -> User:
    """Standalone user import — synchronous fallback that skips avatar
    download. Use the streaming dispatcher when avatars matter."""
    return storage.save_user(_build_user(data, new_id()))


def import_scenario(data: dict) -> Scenario:
    return storage.save_scenario(_build_scenario(data, new_id()))


def _build_brain_library(data: dict, target_id: str) -> BrainLibrary:
    return BrainLibrary(
        id=target_id,
        name=str(data.get("name") or "Imported"),
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        tags=str(data.get("tags") or ""),
        favorite=bool(data.get("favorite")),
        brains=_import_brains(data.get("brains") or []),
    )


async def _import_brain_library_at(
    data: dict, target_id: str, *, should_stage: bool = False,
) -> BrainLibrary:
    """Build, persist, and (if present) download the avatar for a brain
    library at the given UUID. Mirrors :func:`_import_user_at`.
    """
    async with storage.lock(f"library:{target_id}"):
        staging = _stage_replace("brain_library", target_id) if should_stage else None
        committed = False
        try:
            library = _build_brain_library(data, target_id)
            storage.save_brain_library(library)
            ldir = storage.brain_library_dir(library.id)
            if ldir is None:
                if staging is not None:
                    _commit_staging(staging)
                    committed = True
                return library

            avatar_ref = data.get("avatarUri")
            card_ref = data.get("cardImageUri") or data.get("cardImage")
            avatar_crop_in = _import_crop(data.get("avatarCrop"))
            avatar_blob: bytes | None = None
            card_blob: bytes | None = None
            async with _ImageSession() as session:
                if isinstance(avatar_ref, str) and avatar_ref.strip():
                    blob = await _resolve_image(avatar_ref, session)
                    if blob is not None:
                        try:
                            ext, _ = sniff_image(blob)
                            fname = f"avatar.{ext}"
                            storage.atomic_write_bytes(ldir / fname, blob)
                            library.avatar = fname
                            avatar_blob = blob
                        except Exception as e:
                            log.warning("Skipping library avatar: %s", e)
                if isinstance(card_ref, str) and card_ref.strip():
                    blob = await _resolve_image(card_ref, session)
                    if blob is not None:
                        try:
                            ext, _ = sniff_image(blob)
                            fname = f"card.{ext}"
                            storage.atomic_write_bytes(ldir / fname, blob)
                            library.card_image = fname
                            card_blob = blob
                            build_display_file(ldir / fname, None)
                        except Exception as e:
                            log.warning("Skipping library card image: %s", e)

            if library.avatar is None and card_blob is not None:
                try:
                    ext, _ = sniff_image(card_blob)
                    fname = f"avatar.{ext}"
                    storage.atomic_write_bytes(ldir / fname, card_blob)
                    library.avatar = fname
                    avatar_blob = card_blob
                except Exception as e:
                    log.warning("Could not synthesise library avatar from card: %s", e)

            if library.avatar and avatar_blob is not None:
                if avatar_crop_in is not None:
                    library.avatar_crop = _square_in_pixels(
                        avatar_crop_in, sniff_dimensions(avatar_blob),
                    )
                build_display_file(ldir / library.avatar, library.avatar_crop)

            storage.save_brain_library(library)
            if staging is not None:
                _commit_staging(staging)
                committed = True
            return library
        finally:
            if staging is not None and not committed:
                _restore_from_staging(staging)


def import_brain_library(data: dict) -> BrainLibrary:
    return storage.save_brain_library(_build_brain_library(data, new_id()))


# ---------------------------------------------------------------------------
# Context preset
# ---------------------------------------------------------------------------


def _import_preset_blocks(raw: Any) -> list:
    """Coerce a raw ``systemPromptBlocks`` / ``blocks`` list from an export
    JSON into the model shape. Tolerates missing fields (defaults on each
    block kick in)."""
    from server.models import ContextPresetBlock
    out: list[ContextPresetBlock] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        out.append(ContextPresetBlock(
            id=str(entry.get("id") or "") or new_id(),
            name=str(entry.get("name") or ""),
            enabled=bool(entry.get("enabled", True)),
            content=str(entry.get("content") or ""),
        ))
    return out


def _import_preset_additional_messages(raw: Any) -> list:
    from server.models import ContextPresetAdditionalMessage
    out: list[ContextPresetAdditionalMessage] = []
    if not isinstance(raw, list):
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "system")
        if role not in ("system", "user", "assistant"):
            role = "system"
        mode = str(entry.get("mode") or "simple")
        if mode not in ("simple", "blocks"):
            mode = "simple"
        depth = int(entry.get("floatDepth") or entry.get("float_depth") or 0)
        depth = max(0, min(10, depth))
        out.append(ContextPresetAdditionalMessage(
            id=str(entry.get("id") or "") or new_id(),
            name=str(entry.get("name") or ""),
            enabled=bool(entry.get("enabled", True)),
            role=role,
            mode=mode,
            simple_content=str(
                entry.get("simpleContent")
                or entry.get("simple_content")
                or "",
            ),
            blocks=_import_preset_blocks(
                entry.get("blocks") or entry.get("Blocks"),
            ),
            float_enabled=bool(
                entry.get("floatEnabled", entry.get("float_enabled", False)),
            ),
            float_depth=depth,
        ))
    return out


def _build_context_preset(data: dict, target_id: str) -> ContextPreset:
    return ContextPreset(
        id=target_id,
        name=str(data.get("name") or "Imported"),
        description=str(data.get("description") or ""),
        author=str(data.get("author") or ""),
        prefix_names=bool(data.get("prefixNames", data.get("prefix_names", True))),
        favorite=bool(data.get("favorite")),
        avatar_crop=_import_crop(data.get("avatarCrop")),
        system_prompt_blocks=_import_preset_blocks(
            data.get("systemPromptBlocks") or data.get("system_prompt_blocks"),
        ),
        additional_messages=_import_preset_additional_messages(
            data.get("additionalMessages") or data.get("additional_messages"),
        ),
    )


async def _import_context_preset_at(
    data: dict, target_id: str, *, should_stage: bool = False,
) -> ContextPreset:
    """Build, persist, and (if present) download the avatar + card image
    for a context preset at the given UUID. Mirrors
    :func:`_import_brain_library_at`."""
    async with storage.lock(f"context_preset:{target_id}"):
        staging = _stage_replace("context_preset", target_id) if should_stage else None
        committed = False
        try:
            preset = _build_context_preset(data, target_id)
            storage.save_context_preset(preset)
            pdir = storage.context_preset_dir(preset.id)
            if pdir is None:
                if staging is not None:
                    _commit_staging(staging)
                    committed = True
                return preset

            avatar_ref = data.get("avatarUri")
            card_ref = data.get("cardImageUri") or data.get("cardImage")
            avatar_crop_in = _import_crop(data.get("avatarCrop"))
            avatar_blob: bytes | None = None
            card_blob: bytes | None = None
            async with _ImageSession() as session:
                if isinstance(avatar_ref, str) and avatar_ref.strip():
                    blob = await _resolve_image(avatar_ref, session)
                    if blob is not None:
                        try:
                            ext, _ = sniff_image(blob)
                            fname = f"avatar.{ext}"
                            storage.atomic_write_bytes(pdir / fname, blob)
                            preset.avatar = fname
                            avatar_blob = blob
                        except Exception as e:
                            log.warning("Skipping context preset avatar: %s", e)
                if isinstance(card_ref, str) and card_ref.strip():
                    blob = await _resolve_image(card_ref, session)
                    if blob is not None:
                        try:
                            ext, _ = sniff_image(blob)
                            fname = f"card.{ext}"
                            storage.atomic_write_bytes(pdir / fname, blob)
                            preset.card_image = fname
                            card_blob = blob
                            build_display_file(pdir / fname, None)
                        except Exception as e:
                            log.warning("Skipping context preset card image: %s", e)

            if preset.avatar is None and card_blob is not None:
                try:
                    ext, _ = sniff_image(card_blob)
                    fname = f"avatar.{ext}"
                    storage.atomic_write_bytes(pdir / fname, card_blob)
                    preset.avatar = fname
                    avatar_blob = card_blob
                except Exception as e:
                    log.warning(
                        "Could not synthesise context preset avatar from card: %s", e,
                    )

            if preset.avatar and avatar_blob is not None:
                if avatar_crop_in is not None:
                    preset.avatar_crop = _square_in_pixels(
                        avatar_crop_in, sniff_dimensions(avatar_blob),
                    )
                build_display_file(pdir / preset.avatar, preset.avatar_crop)

            storage.save_context_preset(preset)
            if staging is not None:
                _commit_staging(staging)
                committed = True
            return preset
        finally:
            if staging is not None and not committed:
                _restore_from_staging(staging)


# ---------------------------------------------------------------------------
# Chat composite
# ---------------------------------------------------------------------------


# Sidecar resolution plans returned by :func:`_preresolve_sidecar`.
#  {"action": "existing", "entity": <obj>}  — silent reuse (id-match or user-picked)
#  {"action": "import",   "target_id": id}  — import the source data at this id
#  {"action": "missing"}                    — source data is None; caller substitutes
#  {"action": "ask",      "candidates": []} — name matches, awaiting user decision


def _preview_existing(entity, kind: str) -> dict:
    """Server-side projection of an on-disk entity for the name-match modal.
    Avatar/sprite URLs are reconstructed by the frontend from ``id``."""
    base = {
        "id": entity.id,
        "name": entity.name,
        "description": getattr(entity, "description", "") or "",
        "tags": getattr(entity, "tags", "") or "",
    }
    if kind == "contact":
        base["has_avatar"] = bool(getattr(entity, "avatar", None))
        base["has_neutral_emotion"] = "neutral" in (getattr(entity, "emotions", {}) or {})
    elif kind == "scenario":
        env = (entity.environment or "").strip()
        base["environment"] = env[:200] + ("…" if len(env) > 200 else "")
    return base


def _preview_incoming(data: dict, kind: str) -> dict:
    """Same projection but for the JSON we're about to import. Avatars stay
    out of the payload — data URIs would balloon the SSE event."""
    base = {
        "id": _strip_id(data.get("id")),
        "name": str(data.get("name") or ""),
        "description": str(data.get("description") or ""),
        "tags": str(data.get("tags") or data.get("userTags") or data.get("chatTags") or ""),
    }
    if kind == "scenario":
        env = str(data.get("environment") or "").strip()
        base["environment"] = env[:200] + ("…" if len(env) > 200 else "")
    return base


def _preresolve_sidecar(
    data: dict | None,
    role: str,
    resolutions: dict | None,
    *,
    get_existing,
    list_all,
) -> dict:
    """Decide how to satisfy a composite sidecar. See module-level comment
    block for the plan shapes.

    Resolution priority:
      1. id-match against on-disk entities (silent reuse).
      2. ``resolutions[role]`` if the user has already picked: an entity id
         to reuse, or the literal ``"new"`` to import the source data.
      3. Name match → ``ask`` (caller yields a ``name_matches`` event).
      4. No match → import the source data (preserving the source UUID
         when free, otherwise a new one).
    """
    if data is None:
        return {"action": "missing"}

    incoming_id = _strip_id(data.get("id"))
    if incoming_id:
        existing = get_existing(incoming_id)
        if existing is not None:
            return {"action": "existing", "entity": existing}

    if resolutions and role in resolutions:
        choice = resolutions[role]
        if choice == "new":
            return {"action": "import", "target_id": _free_id(incoming_id, get_existing)}
        if isinstance(choice, str) and choice:
            existing = get_existing(choice)
            if existing is not None:
                return {"action": "existing", "entity": existing}
            # Resolution points at a now-missing entity — fall through.

    name = str(data.get("name") or "").strip().lower()
    if name:
        candidates = [
            e for e in list_all()
            if (getattr(e, "name", "") or "").strip().lower() == name
        ]
        if candidates:
            return {"action": "ask", "candidates": candidates}

    return {"action": "import", "target_id": _free_id(incoming_id, get_existing)}


def _free_id(incoming_id: str | None, get_existing) -> str:
    """Prefer the source UUID, fall back to a fresh one if it collides."""
    if incoming_id and get_existing(incoming_id) is None:
        return incoming_id
    return new_id()


async def _execute_user_plan(data: dict | None, plan: dict) -> User:
    if plan["action"] == "existing":
        return plan["entity"]
    if plan["action"] == "missing":
        return storage.save_user(User(name="Anon"))
    return await _import_user_at(data or {}, plan["target_id"])


async def _execute_scenario_plan(data: dict | None, plan: dict) -> Scenario | None:
    if plan["action"] == "missing":
        return None
    if plan["action"] == "existing":
        return plan["entity"]
    return await _import_scenario_at(data or {}, plan["target_id"])


async def _import_chat_brain_libraries(
    raw_libraries: Any, raw_ids: Any,
) -> list[str]:
    """Resolve a chat's attached libraries during composite import.

    For each ``brainLibraries`` sidecar entry, look it up by id locally:
    if present, reuse silently; if not, import it as new. Returns the
    resulting list of library ids in attach order. Falls back to the
    raw ``brainLibraryIds`` list when no sidecar data is present so the
    chat keeps its references even if the export omitted full library
    payloads (those references then surface as "missing library"
    markers in the chat UI for manual reattachment).
    """
    seen: set[str] = set()
    out: list[str] = []

    if isinstance(raw_libraries, list):
        for entry in raw_libraries:
            if not isinstance(entry, dict):
                continue
            incoming_id = _strip_id(entry.get("id"))
            if incoming_id and storage.get_brain_library(incoming_id) is not None:
                # ID match → reuse silently.
                if incoming_id not in seen:
                    seen.add(incoming_id)
                    out.append(incoming_id)
                continue
            # Import as new (prefer the source uuid if it doesn't collide).
            target_id = _free_id(incoming_id, storage.get_brain_library)
            try:
                lib = await _import_brain_library_at(entry, target_id)
                if lib.id not in seen:
                    seen.add(lib.id)
                    out.append(lib.id)
            except Exception as e:
                log.warning("Skipping library sidecar during chat import: %s", e)

    # Carry across any id from the chat payload that wasn't in the sidecar
    # list — preserves missing-library markers so a later library re-import
    # rehydrates the attachment without intervention.
    if isinstance(raw_ids, list):
        for lid in raw_ids:
            if not isinstance(lid, str):
                continue
            if lid in seen:
                continue
            seen.add(lid)
            out.append(lid)

    return out


async def _execute_contact_plan(data: dict | None, plan: dict) -> Contact:
    if plan["action"] == "missing":
        return storage.save_contact(Contact(name="Imported"))
    if plan["action"] == "existing":
        return plan["entity"]
    # Drain the streaming importer with the resolved target_id so we share
    # all the avatar/emotion/crop logic.
    contact_id: str | None = None
    async for ev in import_contact_streaming(data or {}, target_id=plan["target_id"]):
        if ev.get("type") == "done":
            contact_id = ev.get("id")
    if contact_id is None:
        return storage.save_contact(Contact(name=str((data or {}).get("name") or "Imported")))
    contact = storage.get_contact(contact_id)
    return contact or storage.save_contact(Contact(name=str((data or {}).get("name") or "Imported")))


def _import_sub_messages(raw: Any) -> list[SubMessage]:
    out: list[SubMessage] = []
    for sm in raw or []:
        if not isinstance(sm, dict) or not sm.get("text"):
            continue
        out.append(
            SubMessage(
                text=str(sm["text"]),
                emotion=_opt_emotion(sm.get("emotion")) or Emotion.NEUTRAL,
            )
        )
    return out


def _convert_aer_timestamp(value: Any) -> float | None:
    """The source format uses ms-since-epoch; we use seconds."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1e12:  # heuristic: >> 10^12 means it's in ms
        v = v / 1000.0
    return v


def _import_messages(
    raw: Any, target_chat_id: str | None = None,
) -> tuple[list[ChatMessage], dict[str, str]]:
    """Return (messages, id_remap) — old IDs stay as-is unless they collide.

    ``target_chat_id`` is the destination chat's id (used to decode any
    embedded attachment ``dataUri`` payloads back to files under
    ``chat_attachments_dir(target_chat_id)``). When omitted, attachments
    are dropped — callers that care about preservation must pass it.

    Every field that survives a Pydantic round-trip is honored when
    present on the source dict: origin / reasoning / image_refs /
    active_brains / generation metadata (started_at, duration, provider,
    model, presets). Missing fields fall through to the model's
    defaults (preserves backward compat with older exports that didn't
    carry these).
    """
    out: list[ChatMessage] = []
    id_remap: dict[str, str] = {}
    if not isinstance(raw, list):
        return out, id_remap
    for m in raw:
        if not isinstance(m, dict):
            continue
        old_id = str(m.get("id") or "")
        if old_id and old_id not in id_remap:
            id_remap[old_id] = old_id
        else:
            id_remap[old_id] = new_id()
        new_msg_id = id_remap[old_id]
        parent_old = m.get("parentId")
        parent_new = (
            None if parent_old in (None, "") else id_remap.get(str(parent_old), str(parent_old))
        )
        sender = m.get("sender")
        if sender not in ("user", "contact"):
            continue
        ts = _convert_aer_timestamp(m.get("timestamp")) or 0.0
        body = _import_sub_messages(m.get("body") or [])
        if not body:
            continue
        attachments = _decode_message_attachments(
            m.get("attachments") or [], target_chat_id,
        )
        kwargs: dict = {
            "id": new_msg_id,
            "parent_id": parent_new,
            "sender": sender,
            "sender_name": str(m.get("senderName") or ""),
            "body": body,
            "brains": _import_brains(m.get("brains") or []),
            "attachments": attachments,
            "timestamp": ts,
        }
        # Optional fields — only set when the source carries them so older
        # exports keep falling back to the model's defaults.
        if m.get("origin") in ("aer", "generic", "manual"):
            kwargs["origin"] = m["origin"]
        if isinstance(m.get("reasoning"), str):
            kwargs["reasoning"] = m["reasoning"]
        if isinstance(m.get("imageRefs"), dict):
            kwargs["image_refs"] = {
                str(k): str(v) for k, v in m["imageRefs"].items()
            }
        if isinstance(m.get("activeBrains"), list):
            kwargs["active_brains"] = [
                dict(e) for e in m["activeBrains"] if isinstance(e, dict)
            ]
        if m.get("generationStartedAt") is not None:
            ts_gen = _convert_aer_timestamp(m.get("generationStartedAt"))
            if ts_gen is not None:
                kwargs["generation_started_at"] = ts_gen
        if m.get("generationDurationSeconds") is not None:
            try:
                kwargs["generation_duration_seconds"] = float(
                    m["generationDurationSeconds"]
                )
            except (TypeError, ValueError):
                pass
        for src_key, dst_key in (
            ("provider", "provider"),
            ("model", "model"),
            ("generationPresetId", "generation_preset_id"),
            ("contextPresetId", "context_preset_id"),
        ):
            v = m.get(src_key)
            if isinstance(v, str) and v:
                kwargs[dst_key] = v
        out.append(ChatMessage(**kwargs))
    return out, id_remap


def _decode_message_attachments(
    raw: Any, target_chat_id: str | None,
) -> list[Attachment]:
    """Decode each ``{dataUri, mime, filename, byteSize}`` entry to a file
    on disk under the target chat's ``attachments/`` subdir, returning
    fresh Attachment models. Missing target_chat_id or unparseable
    entries are silently dropped — a half-broken attachment shouldn't
    block the rest of the import."""
    if not isinstance(raw, list) or not raw or target_chat_id is None:
        return []
    import base64
    out: list[Attachment] = []
    att_dir = storage.chat_attachments_dir(target_chat_id)
    if att_dir is None:
        return []
    att_dir.mkdir(parents=True, exist_ok=True)
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        data_uri = entry.get("dataUri")
        mime = str(entry.get("mime") or "")
        if not data_uri or not isinstance(data_uri, str) or "base64," not in data_uri:
            continue
        try:
            payload = base64.b64decode(data_uri.split("base64,", 1)[1])
        except Exception:
            continue
        if not payload or not mime:
            continue
        # Re-sniff to derive the canonical extension; fall back to a
        # mime-derived guess when sniffing fails (e.g. non-image phase-2
        # attachments).
        ext = _ext_from_mime(mime)
        if not ext:
            continue
        att_id = new_id()
        path = att_dir / f"{att_id}.{ext}"
        try:
            storage.atomic_write_bytes(path, payload)
        except OSError:
            continue
        out.append(Attachment(
            id=att_id,
            mime=mime,
            filename=str(entry.get("filename") or f"attachment.{ext}"),
            byte_size=int(entry.get("byteSize") or len(payload)),
        ))
    return out


def _ext_from_mime(mime: str) -> str | None:
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(mime.lower())


def _remap_selected_child_id(
    raw: Any, id_remap: dict[str, str]
) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        new_k = "" if k in (None, "") else id_remap.get(str(k), str(k))
        if isinstance(v, str) and v == "__empty__":
            out[new_k] = "__empty__"
        else:
            out[new_k] = id_remap.get(str(v), str(v))
    return out


def _build_generation_preset(raw: dict, pid: str) -> Preset:
    def num(key, default):
        v = raw.get(key)
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else default
    return Preset(
        id=pid,
        name=str(raw.get("name") or "Imported preset"),
        temperature=num("temperature", 0.85),
        top_p=num("topP", 0.95),
        top_k=int(num("topK", 250)),
        min_p=num("minP", 0.0),
        max_context_tokens=int(num("maxContextTokens", 28672)),
        rollover_window_tokens=int(num("rolloverWindowTokens", 8192)),
        max_new_tokens=int(num("maxNewTokens", 1536)),
        presence_penalty=num("presencePenalty", 0.0),
        frequency_penalty=num("frequencyPenalty", 0.0),
    )


async def _import_bundled_generation_preset(raw: Any) -> str | None:
    """Import a generation Preset bundled with a chat export into the
    PresetLibrary, returning its id (or None when nothing was bundled).
    Idempotent: an existing preset with the same id is reused, not overwritten —
    so re-importing the same chat doesn't duplicate or clobber it."""
    if not isinstance(raw, dict):
        return None
    pid = _strip_id(raw.get("id")) or new_id()
    async with storage.lock("presets"):
        lib = storage.load_presets()
        if any(p.id == pid for p in lib.presets):
            return pid
        lib.presets.append(_build_generation_preset(raw, pid))
        storage.save_presets(lib)
    return pid


async def _import_bundled_context_preset(raw: Any) -> str | None:
    """Import a ContextPreset bundled with a chat export, returning its id (or
    None when nothing was bundled). Idempotent: an existing preset with the same
    id is reused (not clobbered), so re-import doesn't wipe local edits."""
    if not isinstance(raw, dict):
        return None
    pid = _strip_id(raw.get("id")) or new_id()
    if storage.get_context_preset(pid) is not None:
        return pid
    preset = await _import_context_preset_at(raw, pid, should_stage=False)
    return preset.id


async def _import_chat_streaming(
    data: dict, *, mode: str = "ask", resolutions: dict | None = None,
):
    """Streaming variant of :func:`import_chat`. Yields ``conflict`` /
    ``name_matches`` / ``done`` events as needed.

    - ``conflict`` covers the top-level chat UUID (mode='ask' + collision).
    - ``name_matches`` covers composite sidecars (character/user/scenario)
      that match an existing entity *by name only* — id-matches reuse
      silently. ``resolutions`` carries the user's per-role decision back:
      either an existing entity id, or the literal ``"new"``.
    """
    raw_chat = data.get("chat") or {}
    target_id, conflict, should_stage = _resolve_target_id(
        _strip_id(raw_chat.get("id")), mode, "chat",
        str(raw_chat.get("title") or ""),
        get_existing=storage.get_chat,
    )
    if conflict is not None:
        yield conflict
        return

    char_plan = _preresolve_sidecar(
        data.get("character"), "character", resolutions,
        get_existing=storage.get_contact, list_all=storage.list_contacts,
    )
    user_plan = _preresolve_sidecar(
        data.get("user"), "user", resolutions,
        get_existing=storage.get_user, list_all=storage.list_users,
    )
    scen_plan = _preresolve_sidecar(
        data.get("scenario"), "scenario", resolutions,
        get_existing=storage.get_scenario, list_all=storage.list_scenarios,
    )

    pending: list[dict] = []
    if char_plan["action"] == "ask":
        pending.append({
            "role": "character", "kind": "contact",
            "incoming": _preview_incoming(data.get("character") or {}, "contact"),
            "candidates": [_preview_existing(c, "contact") for c in char_plan["candidates"]],
        })
    if user_plan["action"] == "ask":
        pending.append({
            "role": "user", "kind": "user",
            "incoming": _preview_incoming(data.get("user") or {}, "user"),
            "candidates": [_preview_existing(u, "user") for u in user_plan["candidates"]],
        })
    if scen_plan["action"] == "ask":
        pending.append({
            "role": "scenario", "kind": "scenario",
            "incoming": _preview_incoming(data.get("scenario") or {}, "scenario"),
            "candidates": [_preview_existing(s, "scenario") for s in scen_plan["candidates"]],
        })
    if pending:
        # User needs to resolve sidecar name-matches; client will re-issue.
        # Don't stage yet — the re-issued request will stage when it
        # re-enters this code path.
        yield {"type": "name_matches", "items": pending}
        return

    async with storage.lock(f"chat:{target_id}"):
        staging = _stage_replace("chat", target_id) if should_stage else None
        committed = False
        try:
            chat = await _build_chat_from_plans(
                data, target_id, char_plan, user_plan, scen_plan,
            )
            yield {"type": "done", "kind": "chat", "id": chat.id, "title": chat.title}
            if staging is not None:
                _commit_staging(staging)
                committed = True
        finally:
            if staging is not None and not committed:
                _restore_from_staging(staging)


async def import_chat(data: dict) -> Chat:
    """Backwards-compatible non-streaming entry point — assigns a fresh chat
    UUID and resolves sidecars via id-or-import (no name-match prompt)."""
    raw_chat = data.get("chat") or {}
    char_plan = _preresolve_sidecar(
        data.get("character"), "character", {"character": "new"},
        get_existing=storage.get_contact, list_all=storage.list_contacts,
    )
    user_plan = _preresolve_sidecar(
        data.get("user"), "user", {"user": "new"},
        get_existing=storage.get_user, list_all=storage.list_users,
    )
    scen_plan = _preresolve_sidecar(
        data.get("scenario"), "scenario", {"scenario": "new"},
        get_existing=storage.get_scenario, list_all=storage.list_scenarios,
    )
    return await _build_chat_from_plans(
        data, new_id(), char_plan, user_plan, scen_plan,
    )


async def _build_chat_from_plans(
    data: dict, target_id: str,
    char_plan: dict, user_plan: dict, scen_plan: dict,
) -> Chat:
    raw_chat = data.get("chat") or {}
    contact = await _execute_contact_plan(data.get("character"), char_plan)
    user = await _execute_user_plan(data.get("user"), user_plan)
    scenario = await _execute_scenario_plan(data.get("scenario"), scen_plan)
    library_ids = await _import_chat_brain_libraries(
        data.get("brainLibraries"), raw_chat.get("brainLibraryIds"),
    )

    chat_title = str(raw_chat.get("title") or f"Chat with {contact.name}")
    # Pre-register the chat dir in the index so ``_import_messages`` can
    # write any embedded attachment data URIs to ``chat_attachments_dir(...)``.
    # ``save_chat_with_all`` below will use the SAME directory (same slug +
    # id8 derivation) when it commits the chat metadata, so we're not
    # creating dead state — only filling it in earlier than the existing
    # save did.
    chat_path = storage.CHATS_DIR / storage.entity_dir_name(chat_title, target_id)
    chat_path.mkdir(parents=True, exist_ok=True)
    storage.index.chat_paths[target_id] = chat_path

    messages, id_remap = _import_messages(
        raw_chat.get("messages") or [], target_chat_id=target_id,
    )
    selected = _remap_selected_child_id(raw_chat.get("selectedChildId") or {}, id_remap)

    # Per-chat generation overrides. These reference Settings-scoped ids (the
    # custom-provider id embedded in provider_override / model_overrides keys,
    # and the context-preset id) that may not exist on the importing machine;
    # they fail safe at generation time (GenerationTargetError) and are
    # intentionally NOT remapped via id_remap (there is nothing to remap them
    # against, unlike message ids).
    raw_model_overrides = raw_chat.get("modelOverrides")
    model_overrides = (
        {
            str(k): str(v)
            for k, v in raw_model_overrides.items()
            if isinstance(k, str) and isinstance(v, str) and v
        }
        if isinstance(raw_model_overrides, dict)
        else {}
    )
    # Bundled generation preset + context-preset override travel with the chat
    # so a re-import is self-contained. Import recreates them (idempotent reuse
    # by id), then the chat points at the resulting id. Fall back to the raw
    # field id when nothing was bundled (older exports) — fail-safe like the
    # provider override.
    preset_id = await _import_bundled_generation_preset(data.get("preset"))
    if preset_id is None and raw_chat.get("presetId"):
        preset_id = str(raw_chat["presetId"])
    context_preset_override = await _import_bundled_context_preset(
        data.get("contextPreset"),
    )
    if context_preset_override is None and raw_chat.get("contextPresetOverride"):
        context_preset_override = str(raw_chat["contextPresetOverride"])
    chat = Chat(
        id=target_id,
        title=chat_title,
        tags=str(raw_chat.get("tags") or ""),
        contact_id=contact.id,
        user_id=user.id,
        scenario_id=scenario.id if scenario is not None else None,
        brain_library_ids=library_ids,
        intimacy=_opt_intimacy(raw_chat.get("intimacy")),
        style=_opt_style(raw_chat.get("style")),
        response_length=_opt_response_length(raw_chat.get("responseLength")),
        cjk=bool(raw_chat.get("cjk")),
        favorite=bool(raw_chat.get("favorite")),
        preset_id=preset_id,
        provider_override=(
            str(raw_chat["providerOverride"])
            if raw_chat.get("providerOverride") else None
        ),
        model_overrides=model_overrides,
        context_preset_override=context_preset_override,
        selected_child_id=selected,
        created_at=_convert_aer_timestamp(raw_chat.get("createdAt")) or 0.0,
        updated_at=_convert_aer_timestamp(raw_chat.get("updatedAt")) or 0.0,
    )
    # Bookmarks (optional).
    raw_bm = data.get("bookmarks") or []
    bookmarks: list[Bookmark] = []
    if isinstance(raw_bm, list):
        for b in raw_bm:
            if not isinstance(b, dict):
                continue
            bookmarks.append(
                Bookmark(
                    title=str(b.get("title") or ""),
                    snippet=str(b.get("snippet") or ""),
                    selected_child_id=_remap_selected_child_id(
                        b.get("selectedChildId") or {}, id_remap
                    ),
                    favorite=bool(b.get("favorite")),
                    created_at=_convert_aer_timestamp(b.get("createdAt")) or 0.0,
                )
            )

    # save_chat_with_all writes in safe order (messages → bookmarks → chat),
    # so chat.selected_child_id (which references the imported messages)
    # is the last write — any reference in chat.yaml is guaranteed to
    # resolve even if the process dies mid-write.
    storage.save_chat_with_all(
        chat,
        ChatMessages(messages=messages),
        ChatBookmarks(bookmarks=bookmarks),
    )
    return chat


# ---------------------------------------------------------------------------
# AER zip import (multi-entity bulk format)
#
# Layout:
#   contacts/{slug}-{src_id}/meta-contact.json
#   contacts/{slug}-{src_id}/spaces/{slug}-{src_id}/meta-space.json
#   contacts/{slug}-{src_id}/spaces/{slug}-{src_id}/messages.json
#
# Source IDs are 22-char URL-safe-base64-ish strings. We map each to one of
# our 32-hex UUIDs deterministically via UUID5 + a fixed namespace, so a
# re-import detects "already-imported" entities without any sidecar file.
# ---------------------------------------------------------------------------


# Hardcoded namespace UUID — DO NOT CHANGE. Changing this regenerates every
# user's import-tracking UUIDs and makes "already imported" detection on
# re-import fail.
_AER_ZIP_NAMESPACE = uuid.UUID("f5a2c8e1-3d4b-4a7e-9c8d-2b1e3f4a5b6c")


def _aer_to_uuid(kind: str, src_id: str) -> str:
    """Map a source ID to one of our UUIDs deterministically.

    ``kind`` ∈ {"contact", "scenario", "chat"} so the same source ID drives
    distinct UUIDs for each derived entity. Cross-installation deterministic.
    """
    return uuid.uuid5(_AER_ZIP_NAMESPACE, f"{kind}:{src_id}").hex


# Fields whose presence on a meta-space.json justifies promoting it to a
# ContactScenario. The "easy" chat fields (relationship/style/response_length,
# plus stream_name → chat title and chat_tags → chat tags) fold straight onto
# the Chat itself; only fields that mutate roleplay context count here.
_SPACE_OVERRIDE_TEXT_FIELDS: tuple[str, ...] = (
    "greeting", "environment", "scene", "background_uri", "description",
)


def _meta_space_has_overrides(meta: Any) -> bool:
    """Return True if a space carries any field meaningful enough to need a
    dedicated ``ContactScenario``."""
    if not isinstance(meta, dict):
        return False
    for key in _SPACE_OVERRIDE_TEXT_FIELDS:
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            return True
    return False


def _hash_meta(meta: Any) -> str:
    """Stable hash of a JSON-serialisable dict, used as a revision fallback."""
    canon = json.dumps(meta, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _meta_contact_signature(meta: dict) -> str:
    """Source revision for a contact: its upstream ``revision_id`` (a UUID
    upstream bumps on edit), or a SHA-256 fallback when missing."""
    rev_data = meta.get("revision_data") or {}
    rev = rev_data.get("revision_id") if isinstance(rev_data, dict) else None
    if isinstance(rev, str) and rev.strip():
        return rev.strip()
    return f"sha:{_hash_meta(meta)}"


def _meta_space_signature(meta: dict) -> str:
    """Source revision for a space's scenario: ``last_update_timestamp``,
    or a hash fallback."""
    ts = meta.get("last_update_timestamp")
    if isinstance(ts, str) and ts.strip():
        return ts.strip()
    return f"sha:{_hash_meta(meta)}"


def _chat_signature(entries: Any) -> str:
    """Source revision for a chat: ``"{count}:{tail-timestamp-ms}"``.

    Cheap and changes whenever the upstream chat grows or its tail edits.
    Empty chats sign as ``"0:0"`` so the manifest treats them consistently.
    """
    if not isinstance(entries, list):
        return "0:0"
    last_ts = 0
    count = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        msg = e.get("message")
        if not (isinstance(msg, str) and msg.strip()):
            continue
        count += 1
        t = e.get("timestamp")
        if isinstance(t, (int, float)) and t > last_ts:
            last_ts = int(t)
    return f"{count}:{last_ts}"


# Tolerance (seconds) absorbing the post-stamp ``save_*`` bump that
# re-touches ``updated_at`` after we snapshot ``aer_imported_at``.
_LOCAL_EDIT_TOLERANCE_S = 1.0


def _entity_state(local: Any, source_revision: str | None) -> str:
    """One of ``not_imported | up_to_date | updated_upstream | modified_locally | conflict``.

    A local entity that exists but has no ``aer_imported_at`` is treated as
    ``not_imported`` — it came in by another route (manual creation,
    single-JSON import). Such collisions surface separately on the manifest
    as ``name_collision``, not as ``up_to_date``.
    """
    if local is None:
        return "not_imported"
    imported_at = getattr(local, "aer_imported_at", None)
    if imported_at is None:
        return "not_imported"
    upstream_changed = getattr(local, "aer_revision", None) != source_revision
    locally_modified = (
        getattr(local, "updated_at", 0.0) > imported_at + _LOCAL_EDIT_TOLERANCE_S
    )
    if upstream_changed and locally_modified:
        return "conflict"
    if upstream_changed:
        return "updated_upstream"
    if locally_modified:
        return "modified_locally"
    return "up_to_date"


def _truncate(value: Any, n: int = 200) -> str:
    s = str(value or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _zip_read_json(zf: zipfile.ZipFile, name: str) -> Any:
    return _json_dec.decode(zf.read(name))


# Path matchers for the AER zip layout.
_CONTACT_META_RE = re.compile(r"^contacts/[^/]+/meta-contact\.json$")
_SPACE_META_RE = re.compile(r"^contacts/[^/]+/spaces/[^/]+/meta-space\.json$")


def _translate_meta_contact(meta: dict, target_uuid: str) -> dict:
    """Translate the AER snake_case contact dict to the camelCase shape that
    :func:`import_contact_streaming` already accepts. Reuses the existing
    avatar / emotion / crop / display-derivative pipeline by feeding it a
    dict in the format it already understands."""
    ai = meta.get("ai_data") or {}
    if not isinstance(ai, dict):
        ai = {}
    return {
        "id": target_uuid,
        "name": (meta.get("name") or "Imported"),
        "description": meta.get("description") or meta.get("tagline") or "",
        "species": meta.get("species") or "",
        "gender": meta.get("gender") or "",
        "pronouns": meta.get("pronouns") or "",
        "persona": ai.get("persona") or "",
        "appearance": ai.get("appearance") or "",
        "greeting": ai.get("greeting") or "",
        "greetingEmotion": ai.get("greeting_emotion") or "",
        "relationship": meta.get("relationship") or "",
        "responseLength": meta.get("response_length") or "",
        "tags": ", ".join(
            str(t).strip()
            for t in (meta.get("search_tags") or [])
            if str(t).strip()
        ),
        "avatarUri": meta.get("avatar_uri") or "",
        "emotions": meta.get("emotions") or {},
        "exampleMessages": ai.get("example_messages") or [],
    }


def _translate_meta_space_to_scenario(
    meta: dict, target_uuid: str, src_id: str | None, source_revision: str | None,
) -> ContactScenario:
    """Build a :class:`ContactScenario` from a meta-space dict.

    Caller checked :func:`_meta_space_has_overrides` first — empty spaces
    don't get promoted to scenarios. ``src_id`` / ``source_revision`` may
    be None when the caller wants a non-tracked copy (e.g. ``copy``
    contact action).
    """
    name = (
        str(meta.get("stream_name") or "").strip()
        or str(meta.get("description") or "").strip()
        or "Imported scenario"
    )
    return ContactScenario(
        id=target_uuid,
        name=name,
        description=str(meta.get("description") or ""),
        environment=str(meta.get("environment") or ""),
        scene=str(meta.get("scene") or ""),
        tags=_chat_tags_to_string(meta.get("chat_tags")),
        greeting=str(meta.get("greeting") or ""),
        greeting_emotion=_opt_emotion(meta.get("greeting_emotion")),
        style=_opt_style_or_none(meta.get("style")),
        intimacy=_opt_intimacy_or_none(meta.get("relationship")),
        response_length=_opt_response_length(meta.get("response_length")),
        aer_source_id=src_id,
        aer_revision=source_revision,
        aer_imported_at=now_seconds() if src_id is not None else None,
    )


def _translate_messages_to_chat_messages(
    entries: Any,
) -> tuple[list[ChatMessage], dict[str, str]]:
    """Coalesce the linear AER bubble stream into multi-bubble messages.

    Walks ``entries`` in order. Ignores ``event_type`` and any entry with
    an empty ``message``. Consecutive entries sharing a sender class
    (``speaker is None`` ⇒ user; otherwise contact) collapse into one
    :class:`ChatMessage` whose ``body`` is the list of :class:`SubMessage`.
    Returns ``(messages, selected_child_id)`` ready for storage; the linear
    ``parent_id`` chain is built here too.
    """
    if not isinstance(entries, list):
        return [], {}

    groups: list[dict] = []
    current: dict | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("message") or "")
        if not text.strip():
            continue
        speaker = entry.get("speaker")
        sender = "user" if speaker is None else "contact"
        emotion = _opt_emotion(entry.get("emotion")) or Emotion.NEUTRAL
        ts = _convert_aer_timestamp(entry.get("timestamp")) or 0.0
        sub = SubMessage(text=text, emotion=emotion)
        if current is None or current["sender"] != sender:
            current = {
                "sender": sender,
                "sender_name": "" if sender == "user" else str(speaker or ""),
                "body": [sub],
                "timestamp": ts,
            }
            groups.append(current)
        else:
            current["body"].append(sub)

    messages: list[ChatMessage] = []
    selected: dict[str, str] = {}
    prev_id: str | None = None
    for g in groups:
        msg = ChatMessage(
            parent_id=prev_id,
            sender=g["sender"],
            sender_name=g["sender_name"],
            body=g["body"],
            timestamp=g["timestamp"],
        )
        messages.append(msg)
        key = ROOT_PARENT_KEY if prev_id is None else prev_id
        selected[key] = msg.id
        prev_id = msg.id
    return messages, selected


def _summarise_messages_for_toc(zf: zipfile.ZipFile, msgs_path: str) -> dict:
    """Cheap summary for the manifest: ``{message_count, latest_snippet,
    last_update_ms, signature}``. Reads the entire messages.json — small
    for typical chats."""
    try:
        entries = _zip_read_json(zf, msgs_path)
    except (KeyError, msgspec.DecodeError) as e:
        log.warning("Could not read %s: %s", msgs_path, e)
        return {
            "message_count": 0, "latest_snippet": "",
            "last_update_ms": 0, "signature": "0:0",
        }
    if not isinstance(entries, list):
        return {
            "message_count": 0, "latest_snippet": "",
            "last_update_ms": 0, "signature": "0:0",
        }
    last_text = ""
    last_ts = 0
    count = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        msg = str(e.get("message") or "").strip()
        if not msg:
            continue
        count += 1
        ts = e.get("timestamp")
        if isinstance(ts, (int, float)) and ts > last_ts:
            last_ts = int(ts)
            last_text = msg
    snippet = last_text[:120] + ("…" if len(last_text) > 120 else "")
    return {
        "message_count": count,
        "latest_snippet": snippet,
        "last_update_ms": last_ts,
        "signature": _chat_signature(entries),
    }


def _aer_search_haystack(cmeta: dict, space_metas: list[dict]) -> str:
    """Lowercased haystack for AER contact rows. Spans the contact's own
    ``name`` / ``description`` / ``tagline`` / ``search_tags`` plus the
    `stream_name` / `scene` / `environment` of each child space, so a
    user typing the name of a scene matches the parent contact."""
    parts: list[str] = [
        str(cmeta.get("name") or ""),
        str(cmeta.get("description") or ""),
        str(cmeta.get("tagline") or ""),
    ]
    tags = cmeta.get("search_tags")
    if isinstance(tags, list):
        parts.append(" ".join(str(t) for t in tags if str(t).strip()))
    elif isinstance(tags, str):
        parts.append(tags)
    for smeta in space_metas:
        parts.append(str(smeta.get("stream_name") or ""))
        parts.append(str(smeta.get("scene") or ""))
        parts.append(str(smeta.get("environment") or ""))
    return " ".join(p for p in parts if p).lower()


def _read_aer_zip_toc(zf: zipfile.ZipFile) -> Iterator[dict]:
    """Iterator yielding ``progress`` events as it parses each AER metadata
    member (``meta-contact.json`` / ``meta-space.json`` /
    ``messages.json``), ending with a ``manifest`` event carrying the
    AER-bulk manifest. Image references stay as URLs / data URIs at this
    stage; :func:`_decorate_manifest` swaps them to proxy URLs later."""
    contact_meta_paths: list[str] = []
    space_metas_by_contact_dir: dict[str, list[str]] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if _CONTACT_META_RE.match(name):
            contact_meta_paths.append(name)
            continue
        if _SPACE_META_RE.match(name):
            contact_dir = name.split("/spaces/", 1)[0]
            space_metas_by_contact_dir.setdefault(contact_dir, []).append(name)

    # Total parses: one per meta-contact, plus two per space (meta + messages).
    total = len(contact_meta_paths) + 2 * sum(
        len(v) for v in space_metas_by_contact_dir.values()
    )
    done = 0

    contacts_out: list[dict] = []
    for cmeta_path in sorted(contact_meta_paths):
        yield {"type": "progress",
               "label": f"Reading {os.path.basename(cmeta_path)}",
               "current": done, "total": total}
        try:
            cmeta = _zip_read_json(zf, cmeta_path)
        except (KeyError, msgspec.DecodeError) as e:
            log.warning("Skipping malformed %s: %s", cmeta_path, e)
            done += 1
            continue
        done += 1
        if not isinstance(cmeta, dict):
            continue
        contact_dir = cmeta_path[: -len("/meta-contact.json")]
        src_id = str(cmeta.get("contact_id") or "").strip()
        if not src_id:
            log.warning("Skipping %s: no contact_id", cmeta_path)
            continue
        contact_uuid = _aer_to_uuid("contact", src_id)
        source_revision = _meta_contact_signature(cmeta)
        local = storage.get_contact(contact_uuid)
        state = _entity_state(local, source_revision)

        # Name-collision: a different entity already uses this name (only
        # interesting when the deterministic UUID slot is empty).
        name_collision: dict | None = None
        if local is None:
            target_name = str(cmeta.get("name") or "").strip().lower()
            if target_name:
                for c in storage.list_contacts():
                    if c.id == contact_uuid:
                        continue
                    if (c.name or "").strip().lower() == target_name:
                        name_collision = {"id": c.id, "name": c.name}
                        break

        spaces: list[dict] = []
        # Track the parsed space metas so we can build a richer search
        # haystack at the contact level (covers child stream/scene/env).
        parsed_smetas: list[dict] = []
        for smeta_path in sorted(space_metas_by_contact_dir.get(contact_dir, [])):
            yield {"type": "progress",
                   "label": f"Reading {os.path.basename(smeta_path)}",
                   "current": done, "total": total}
            try:
                smeta = _zip_read_json(zf, smeta_path)
            except (KeyError, msgspec.DecodeError) as e:
                log.warning("Skipping malformed %s: %s", smeta_path, e)
                done += 2  # also charge the messages.json we won't read
                continue
            done += 1
            if not isinstance(smeta, dict):
                done += 1
                continue
            space_dir = smeta_path[: -len("/meta-space.json")]
            src_space_id = str(smeta.get("stream_id") or "").strip()
            if not src_space_id:
                log.warning("Skipping %s: no stream_id", smeta_path)
                done += 1
                continue
            chat_uuid = _aer_to_uuid("chat", src_space_id)
            has_overrides = _meta_space_has_overrides(smeta)
            scenario_uuid = (
                _aer_to_uuid("scenario", src_space_id) if has_overrides else None
            )
            scenario_revision = _meta_space_signature(smeta) if has_overrides else None

            yield {"type": "progress",
                   "label": f"Reading messages for {smeta.get('stream_name') or src_space_id}",
                   "current": done, "total": total}
            chat_local = storage.get_chat(chat_uuid)
            chat_summary = _summarise_messages_for_toc(zf, f"{space_dir}/messages.json")
            done += 1
            chat_state = _entity_state(chat_local, chat_summary["signature"])

            scenario_state = "not_imported"
            if scenario_uuid is not None:
                scen_local = None
                if local is not None:
                    for cs in local.scenarios:
                        if cs.id == scenario_uuid:
                            scen_local = cs
                            break
                scenario_state = _entity_state(scen_local, scenario_revision)

            spaces.append({
                "src_id": src_space_id,
                "name": str(smeta.get("stream_name") or "").strip() or "(unnamed)",
                "scenario_uuid": scenario_uuid,
                "scenario_state": scenario_state,
                "scenario_source_revision": scenario_revision,
                "has_overrides": has_overrides,
                "chat_uuid": chat_uuid,
                "chat_state": chat_state,
                "chat_source_revision": chat_summary["signature"],
                "message_count": chat_summary["message_count"],
                "latest_snippet": chat_summary["latest_snippet"],
                "last_update": str(smeta.get("last_update_timestamp") or ""),
                "scene_excerpt": _truncate(smeta.get("scene"), 200),
                "environment_excerpt": _truncate(smeta.get("environment"), 200),
            })
            parsed_smetas.append(smeta)

        contacts_out.append({
            "src_id": src_id,
            "uuid": contact_uuid,
            "path": cmeta_path,           # used by the preview proxy lookup
            "name": str(cmeta.get("name") or ""),
            "description": str(cmeta.get("description") or ""),
            "tagline": str(cmeta.get("tagline") or ""),
            "avatar_uri": str(cmeta.get("avatar_uri") or ""),
            "tags": [
                str(t).strip()
                for t in (cmeta.get("search_tags") or [])
                if isinstance(t, (str, int, float)) and str(t).strip()
            ],
            "source_revision": source_revision,
            "state": state,
            "name_collision": name_collision,
            "spaces": spaces,
            "_search": _aer_search_haystack(cmeta, parsed_smetas),
        })

    yield {"type": "progress", "label": "Done",
           "current": total, "total": total}
    yield {"type": "manifest",
           "manifest": {"format": "aer_bulk", "contacts": contacts_out}}


# ---------------------------------------------------------------------------
# Bulk import (driven by a manifest selection from the picker UI)
# ---------------------------------------------------------------------------


def _default_user_id() -> str:
    """Imported chats need a ``user_id``. The seeded ``Anon`` persona is
    preferred; fall back to any user, then create one as a last resort."""
    users = storage.list_users()
    for u in users:
        if (u.name or "").strip().lower() == "anon":
            return u.id
    if users:
        return users[0].id
    return storage.save_user(User(name="Anon")).id


_VALID_CONTACT_ACTIONS = {"import", "reuse", "replace", "copy", "skip"}
_VALID_CHAT_ACTIONS = {"import", "replace", "copy", "skip"}


async def _process_contact_action(
    action: str, target_uuid: str, src_id: str, cmeta: dict, do_stamp: bool,
):
    """Apply ``action`` to one contact, yielding inner progress events.

    ``target_uuid`` is precomputed: deterministic for ``import|reuse|replace``,
    a fresh ``new_id()`` for ``copy``. ``do_stamp`` is False for ``copy`` (so
    the new entity isn't claimed by the source's deterministic UUID slot).
    Caller must filter ``skip`` and ``reuse`` out before invoking — both are
    no-ops here that yield nothing.
    """
    translated = _translate_meta_contact(cmeta, target_uuid)
    incoming_name = translated["name"] or "Imported"
    do_stage = (action == "replace")
    source_revision = _meta_contact_signature(cmeta)

    async with storage.lock(f"contact:{target_uuid}"):
        staging = _stage_replace("contact", target_uuid) if do_stage else None
        committed = False
        try:
            async for ev in _import_contact_streaming_inner(
                translated, target_uuid, incoming_name,
            ):
                if ev.get("type") == "progress":
                    yield ev
                # Inner ``done`` event dropped — bulk emits its own at the end.
            if do_stamp:
                contact = storage.get_contact(target_uuid)
                if contact is not None:
                    contact.aer_source_id = src_id
                    contact.aer_revision = source_revision
                    contact.aer_imported_at = contact.updated_at
                    storage.save_contact(contact)
            if staging is not None:
                _commit_staging(staging)
                committed = True
        finally:
            if staging is not None and not committed:
                _restore_from_staging(staging)


def _attach_scenarios(
    contact_id: str,
    scenarios_to_add: list[ContactScenario],
) -> None:
    """Append ContactScenarios that aren't already present (by ``id``).

    Idempotent: a scenario whose UUID is already in ``contact.scenarios``
    is left alone (existing instance, possibly with user edits, wins).
    Caller must hold the contact lock.
    """
    if not scenarios_to_add:
        return
    contact = storage.get_contact(contact_id)
    if contact is None:
        return
    existing_ids = {cs.id for cs in contact.scenarios}
    appended = False
    for cs in scenarios_to_add:
        if cs.id in existing_ids:
            continue
        contact.scenarios.append(cs)
        existing_ids.add(cs.id)
        appended = True
    if appended:
        storage.save_contact(contact)


async def _process_chat_action(
    action: str,
    chat_uuid: str,
    contact_id: str,
    scen_uuid: str | None,
    src_id: str,
    smeta: dict,
    msgs_entries: Any,
    do_stamp: bool,
) -> None:
    """Create or replace a single chat from a space's meta + messages.

    Caller filters ``skip`` out. ``scen_uuid`` is the ContactScenario's
    UUID to link via ``contact_scenario_id`` (None when the source space
    has no overrides). Whether the scenario itself is appended to the
    contact is the caller's responsibility (see :func:`_attach_scenarios`).
    """
    do_stage = (action == "replace")
    chat_revision = _chat_signature(msgs_entries)

    async with storage.lock(f"chat:{chat_uuid}"):
        staging = _stage_replace("chat", chat_uuid) if do_stage else None
        committed = False
        try:
            messages, selected = _translate_messages_to_chat_messages(msgs_entries)
            if messages:
                ts_first = min(m.timestamp for m in messages) or now_seconds()
                ts_last = max(m.timestamp for m in messages) or ts_first
            else:
                ts_first = ts_last = now_seconds()

            title = str(smeta.get("stream_name") or "").strip() or "Imported chat"
            chat = Chat(
                id=chat_uuid,
                title=title,
                contact_id=contact_id,
                user_id=_default_user_id(),
                contact_scenario_id=scen_uuid,
                tags=_chat_tags_to_string(smeta.get("chat_tags")),
                intimacy=_opt_intimacy(smeta.get("relationship")),
                style=_opt_style(smeta.get("style")),
                response_length=_opt_response_length(smeta.get("response_length")),
                selected_child_id=selected,
                created_at=ts_first,
                updated_at=ts_last,
            )
            storage.save_chat_with_all(
                chat, ChatMessages(messages=messages), ChatBookmarks(),
            )
            if do_stamp:
                stamped = storage.get_chat(chat_uuid)
                if stamped is not None:
                    stamped.aer_source_id = src_id
                    stamped.aer_revision = chat_revision
                    stamped.aer_imported_at = stamped.updated_at
                    storage.save_chat(stamped)
            if staging is not None:
                _commit_staging(staging)
                committed = True
        finally:
            if staging is not None and not committed:
                _restore_from_staging(staging)


def _index_zip_paths(zf: zipfile.ZipFile) -> dict[str, dict]:
    """Walk the zip once, return ``{contact_src_id: {"meta": path, "spaces": [...]}}``.

    Each space entry is ``{"src_id", "meta", "messages"}``. Used by the
    bulk importer to look up zip member paths by source ID.
    """
    by_dir: dict[str, str] = {}                       # contact_dir → contact_meta_path
    space_paths_by_dir: dict[str, list[str]] = {}     # contact_dir → [space_meta_path]
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if _CONTACT_META_RE.match(name):
            by_dir[name[: -len("/meta-contact.json")]] = name
        elif _SPACE_META_RE.match(name):
            contact_dir = name.split("/spaces/", 1)[0]
            space_paths_by_dir.setdefault(contact_dir, []).append(name)

    out: dict[str, dict] = {}
    for contact_dir, cmeta_path in by_dir.items():
        try:
            cmeta = _zip_read_json(zf, cmeta_path)
        except (KeyError, msgspec.DecodeError):
            continue
        if not isinstance(cmeta, dict):
            continue
        csrc = str(cmeta.get("contact_id") or "").strip()
        if not csrc:
            continue
        spaces: list[dict] = []
        for smeta_path in sorted(space_paths_by_dir.get(contact_dir, [])):
            try:
                smeta = _zip_read_json(zf, smeta_path)
            except (KeyError, msgspec.DecodeError):
                continue
            if not isinstance(smeta, dict):
                continue
            ssrc = str(smeta.get("stream_id") or "").strip()
            if not ssrc:
                continue
            space_dir = smeta_path[: -len("/meta-space.json")]
            spaces.append({
                "src_id": ssrc,
                "meta": smeta_path,
                "messages": f"{space_dir}/messages.json",
            })
        out[csrc] = {"meta": cmeta_path, "spaces": spaces}
    return out


async def _import_aer_zip_streaming(zf: zipfile.ZipFile, selection: dict):
    """Stream-import the AER-bulk-format selection. Caller has already
    opened the zip + dispatched on format."""
    zip_paths = _index_zip_paths(zf)
    contacts_in = []
    if isinstance(selection, dict):
        cs = selection.get("contacts")
        if isinstance(cs, list):
            contacts_in = cs

    # Pre-count work for the progress bar (one tick per non-skipped
    # contact + one per non-skipped chat).
    total = 0
    for csel in contacts_in:
        if not isinstance(csel, dict):
            continue
        action = str(csel.get("action") or "skip").lower()
        if action != "skip":
            total += 1
        for ssel in csel.get("spaces") or []:
            if not isinstance(ssel, dict):
                continue
            if str(ssel.get("chat_action") or "skip").lower() != "skip":
                total += 1
    done = 0
    imported = {"contacts": 0, "scenarios": 0, "chats": 0}
    yield {"type": "progress", "label": "Starting bulk import",
           "phase": "bulk", "current": done, "total": total, "failed": 0}

    for csel in contacts_in:
        if not isinstance(csel, dict):
            continue
        csrc = str(csel.get("src_id") or "").strip()
        paths = zip_paths.get(csrc)
        if paths is None:
            log.warning("Selection references unknown contact %r", csrc)
            continue
        action = str(csel.get("action") or "skip").lower()
        if action not in _VALID_CONTACT_ACTIONS:
            log.warning("Invalid contact action %r — skipping", action)
            continue
        if action == "skip":
            continue

        try:
            cmeta = _zip_read_json(zf, paths["meta"])
        except (KeyError, msgspec.DecodeError) as e:
            log.warning("Could not read %s: %s", paths["meta"], e)
            continue

        # Pick the contact UUID once.
        if action == "copy":
            contact_uuid = new_id()
        else:
            contact_uuid = _aer_to_uuid("contact", csrc)

        # Defensive: if "import" but UUID already taken, fall through to
        # reuse rather than crashing the whole bulk.
        if action == "import" and storage.get_contact(contact_uuid) is not None:
            action = "reuse"

        if action in ("import", "replace", "copy"):
            do_stamp = (action != "copy")
            async for ev in _process_contact_action(
                action, contact_uuid, csrc, cmeta, do_stamp,
            ):
                # Prefix the contact name on each inner progress label so
                # the bulk progress bar stays narratively coherent.
                if ev.get("type") == "progress":
                    label = ev.get("label") or ""
                    ev = {**ev, "label": f"[{cmeta.get('name') or csrc}] {label}"}
                yield ev
            imported["contacts"] += 1

        done += 1
        yield {"type": "progress",
               "label": f"Contact: {cmeta.get('name') or csrc}",
               "phase": "bulk",
               "current": done, "total": total, "failed": 0}

        # Walk spaces once, building per-space plans so the scenario
        # uuid we mint here matches the chat's ``contact_scenario_id``
        # downstream — even when ``copy`` mints fresh ids that aren't
        # deterministic.
        space_paths_by_src = {sp["src_id"]: sp for sp in paths["spaces"]}
        space_plans: list[dict] = []  # {ssrc, sp, smeta, chat_action, scen_uuid|None, fork}
        for ssel in csel.get("spaces") or []:
            if not isinstance(ssel, dict):
                continue
            ssrc = str(ssel.get("src_id") or "").strip()
            sp = space_paths_by_src.get(ssrc)
            if sp is None:
                continue
            chat_action = str(ssel.get("chat_action") or "skip").lower()
            if chat_action not in _VALID_CHAT_ACTIONS:
                log.warning("Invalid chat action %r — skipping", chat_action)
                continue
            if chat_action == "skip":
                continue
            try:
                smeta = _zip_read_json(zf, sp["meta"])
            except (KeyError, msgspec.DecodeError) as e:
                log.warning("Could not read %s: %s", sp["meta"], e)
                continue
            fork = (action == "copy" or chat_action == "copy")
            if _meta_space_has_overrides(smeta):
                scen_uuid = (
                    new_id() if fork
                    else _aer_to_uuid("scenario", ssrc)
                )
            else:
                scen_uuid = None
            space_plans.append({
                "ssrc": ssrc, "sp": sp, "smeta": smeta,
                "chat_action": chat_action,
                "scen_uuid": scen_uuid, "fork": fork,
            })

        # Translate scenarios up-front and attach to the contact (lossless
        # merge — anything already there with a matching UUID wins).
        scenarios_to_add: list[ContactScenario] = []
        for plan in space_plans:
            if plan["scen_uuid"] is None:
                continue
            if plan["fork"]:
                cs = _translate_meta_space_to_scenario(
                    plan["smeta"], plan["scen_uuid"],
                    src_id=None, source_revision=None,
                )
            else:
                cs = _translate_meta_space_to_scenario(
                    plan["smeta"], plan["scen_uuid"],
                    src_id=plan["ssrc"],
                    source_revision=_meta_space_signature(plan["smeta"]),
                )
            scenarios_to_add.append(cs)

        if scenarios_to_add:
            async with storage.lock(f"contact:{contact_uuid}"):
                pre = storage.get_contact(contact_uuid)
                pre_count = len(pre.scenarios) if pre is not None else 0
                _attach_scenarios(contact_uuid, scenarios_to_add)
                post = storage.get_contact(contact_uuid)
                post_count = len(post.scenarios) if post is not None else 0
                imported["scenarios"] += max(0, post_count - pre_count)

        # Chats — one per surviving plan.
        for plan in space_plans:
            ssrc = plan["ssrc"]
            sp = plan["sp"]
            smeta = plan["smeta"]
            chat_action = plan["chat_action"]
            fork = plan["fork"]

            try:
                msgs_entries = _zip_read_json(zf, sp["messages"])
            except (KeyError, msgspec.DecodeError) as e:
                log.warning("Could not read %s: %s", sp["messages"], e)
                continue

            chat_uuid = new_id() if fork else _aer_to_uuid("chat", ssrc)
            if chat_action == "import" and storage.get_chat(chat_uuid) is not None:
                # Defensive: stale picker decision. Skip rather than
                # crash; the user can retry from a fresh manifest.
                continue

            await _process_chat_action(
                chat_action,
                chat_uuid,
                contact_uuid,
                plan["scen_uuid"],
                ssrc,
                smeta,
                msgs_entries,
                do_stamp=not fork,
            )
            imported["chats"] += 1
            done += 1
            yield {"type": "progress",
                   "label": f"Chat: {smeta.get('stream_name') or '(unnamed)'}",
                   "phase": "bulk",
                   "current": done, "total": total, "failed": 0}

    yield {"type": "done", "kind": "bulk", "imported": imported}


# ---------------------------------------------------------------------------
# Flat-JSON zip variant — a zip full of standalone exports (single contact /
# user / scenario / chat composite per file). Each member is dispatched
# through ``import_streaming`` exactly as if uploaded individually.
# ---------------------------------------------------------------------------


def _id_for_kind(data: dict, kind: str) -> str | None:
    if kind == "chat":
        chat = data.get("chat") or {}
        return _strip_id(chat.get("id"))
    return _strip_id(data.get("id"))


def _name_for_kind(data: dict, kind: str) -> str:
    if kind == "chat":
        return str((data.get("chat") or {}).get("title") or "")
    return str(data.get("name") or "")


def _description_for_kind(data: dict, kind: str) -> str:
    if kind == "chat":
        bits: list[str] = []
        for role in ("character", "user", "scenario"):
            sub = data.get(role) or {}
            n = str(sub.get("name") or "").strip()
            if n:
                bits.append(n)
        return " · ".join(bits)
    if kind == "scenario":
        env = str(data.get("environment") or "").strip()
        if env:
            return env
    return str(data.get("description") or "")


def _tags_for_kind(data: dict, kind: str) -> list[str]:
    """Extract tags as a list of strings. Tags in our exported JSON are
    stored comma-separated; the AER bulk format uses a list. Returns
    ``[]`` for chats (whose composites would expose chat / character /
    user / scenario tags — too noisy for a single picker row)."""
    if kind == "chat":
        return []
    if kind == "user":
        raw = data.get("tags") or data.get("userTags") or ""
    elif kind == "scenario":
        raw = data.get("tags") or data.get("chatTags") or ""
    else:
        raw = data.get("tags") or ""
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    if isinstance(raw, str):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []


def _avatar_for_kind(data: dict, kind: str) -> str | None:
    if kind == "chat":
        char = data.get("character") or {}
        v = char.get("avatarUri") or char.get("avatar_uri")
    elif kind == "scenario":
        v = data.get("backgroundImageUri")
    else:
        v = data.get("avatarUri") or data.get("avatar_uri")
    return v if isinstance(v, str) and v.strip() else None


def _entity_exists(kind: str, entity_id: str) -> bool:
    getters = {
        "contact": storage.get_contact,
        "user": storage.get_user,
        "scenario": storage.get_scenario,
        "brain_library": storage.get_brain_library,
        "context_preset": storage.get_context_preset,
        "chat": storage.get_chat,
    }
    g = getters.get(kind)
    return g is not None and g(entity_id) is not None


def _flat_search_haystack(data: dict, kind: str) -> str:
    """Lowercased haystack for the picker's substring search. Per kind:

    - chat composite: chat title + each sidecar's name.
    - others: name + description (+ persona / environment / scene where
      applicable) + tags (string or list).
    """
    parts: list[str] = []
    if kind == "chat":
        chat = data.get("chat") or {}
        parts.append(str(chat.get("title") or ""))
        for role in ("character", "user", "scenario"):
            sub = data.get(role) or {}
            parts.append(str(sub.get("name") or ""))
    else:
        parts.append(str(data.get("name") or ""))
        parts.append(str(data.get("description") or ""))
        if kind == "user":
            parts.append(str(data.get("persona") or ""))
        if kind == "scenario":
            parts.append(str(data.get("environment") or ""))
            parts.append(str(data.get("scene") or ""))
        tags = data.get("tags")
        if isinstance(tags, str):
            parts.append(tags)
        elif isinstance(tags, list):
            parts.append(" ".join(str(t) for t in tags))
    return " ".join(p for p in parts if p).lower()


def _read_flat_zip_toc(zf: zipfile.ZipFile) -> Iterator[dict]:
    """Iterator yielding ``progress`` events as it parses each ``*.json``
    or ``*.png`` member, ending with a ``manifest`` event carrying the
    flat manifest.

    PNG members are parsed for embedded card data via the same
    chunk-preference order as the single-file PNG path
    (``aertavern_data`` > ``naidata`` > ``chara``). PNGs without
    recognised chunks are skipped silently — the walker treats them as
    plain image attachments the user didn't intend to import.

    Caller (the route layer) is expected to consume progress events as
    SSE and apply :func:`_decorate_manifest` to add proxy URLs before
    surfacing the final manifest.
    """
    candidates: list[str] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        lname = info.filename.lower()
        if lname.endswith(".json") or lname.endswith(".png"):
            candidates.append(info.filename)

    total = len(candidates)
    items: list[dict] = []
    for i, name in enumerate(candidates):
        yield {
            "type": "progress",
            "label": f"Examining {os.path.basename(name)}",
            "current": i, "total": total,
        }
        is_png = name.lower().endswith(".png")
        try:
            data, kind = _flat_zip_classify_member(zf, name, is_png=is_png)
        except _FlatZipSkip as exc:
            if exc.warn:
                log.warning("Skipping %s: %s", name, exc)
            continue
        src_uuid = _id_for_kind(data, kind)
        state = (
            "up_to_date"
            if (src_uuid and _entity_exists(kind, src_uuid))
            else "not_imported"
        )
        # PNG entries surface the embedded card image as the picker
        # thumbnail (vs. JSON entries that have an avatarUri inside the
        # payload). The preview proxy reads the zip member at
        # ``path`` directly, so we can point at the PNG itself instead
        # of embedding a large data URI in the manifest.
        avatar_uri = (
            f"png-member:{name}" if is_png else _avatar_for_kind(data, kind)
        )
        items.append({
            "path": name,
            "kind": kind,
            "name": _name_for_kind(data, kind),
            "description": _description_for_kind(data, kind),
            "tags": _tags_for_kind(data, kind),
            "avatar_uri": avatar_uri,
            "src_uuid": src_uuid,
            "state": state,
            "_search": (
                _flat_search_haystack(data, kind)
                + " " + name.lower()
            ),
        })
    items.sort(key=lambda i: (i["kind"], (i["name"] or "").lower()))
    yield {"type": "progress", "label": "Done",
           "current": total, "total": total}
    yield {"type": "manifest",
           "manifest": {"format": "flat_json", "items": items}}


class _FlatZipSkip(Exception):
    """Internal — raised when a zip member can't be turned into a
    picker row. ``warn=True`` emits a log message; the silent variant
    (e.g. PNG with no recognised chunk) just skips."""

    def __init__(self, msg: str = "", *, warn: bool = True):
        super().__init__(msg)
        self.warn = warn


def _flat_zip_classify_member(
    zf: zipfile.ZipFile, name: str, *, is_png: bool
) -> tuple[dict, str]:
    """Return ``(normalised_data, kind)`` for one zip member.

    For JSON: parse + ``detect_format`` + ``normalize_foreign``.
    For PNG: read chunks via ``external_formats.parse_external``; if
    the PNG has no recognised chunk, raise :class:`_FlatZipSkip` silently.
    """
    if is_png:
        try:
            raw = zf.read(name)
        except (KeyError, OSError) as exc:
            raise _FlatZipSkip(f"could not read PNG: {exc}") from exc
        try:
            parsed = external_formats.parse_external(raw, name)
        except ValueError:
            # Chunk present but malformed — note loudly so the user can
            # diagnose the source archive.
            raise _FlatZipSkip("PNG card data malformed")
        if parsed is None:
            # No recognised chunk — skip silently.
            raise _FlatZipSkip("PNG carries no embedded card data", warn=False)
        return parsed["payload"], parsed["kind"]

    try:
        data = _zip_read_json(zf, name)
    except (KeyError, msgspec.DecodeError, UnicodeDecodeError) as exc:
        raise _FlatZipSkip(f"malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _FlatZipSkip("not a JSON object")
    try:
        kind = detect_format(data)
    except ValueError:
        raise _FlatZipSkip("unrecognised format")
    return normalize_foreign(data, name), kind


_VALID_FLAT_ACTIONS = {"import", "replace", "copy", "skip"}


async def _import_flat_zip_streaming(zf: zipfile.ZipFile, selection: dict):
    """Iterate over the user's selected files, dispatching each through
    :func:`import_streaming` with the chosen mode.

    For chat composites we pre-resolve every sidecar to ``"new"`` so the
    name-match prompt never blocks the bulk stream — id-matched sidecars
    still reuse silently because that resolution kicks in *after* the
    id-match path in :func:`_preresolve_sidecar`.
    """
    items_in: list[dict] = []
    if isinstance(selection, dict):
        items = selection.get("items")
        if isinstance(items, list):
            items_in = [i for i in items if isinstance(i, dict)]

    total = sum(
        1 for i in items_in
        if str(i.get("action") or "skip").lower() in ("import", "replace", "copy")
    )
    done = 0
    imported = {"contacts": 0, "users": 0, "scenarios": 0, "chats": 0, "libraries": 0}

    yield {"type": "progress", "label": "Starting import",
           "phase": "bulk", "current": done, "total": total, "failed": 0}

    for sel in items_in:
        path = str(sel.get("path") or "").strip()
        action = str(sel.get("action") or "skip").lower()
        if action == "skip":
            continue
        if action not in _VALID_FLAT_ACTIONS:
            log.warning("Invalid flat action %r — skipping", action)
            continue

        is_png = path.lower().endswith(".png")
        try:
            data, kind = _flat_zip_classify_member(zf, path, is_png=is_png)
        except _FlatZipSkip as exc:
            if exc.warn:
                log.warning("Skipping %s: %s", path, exc)
            continue

        # PNG entries carry their card image alongside the JSON. Fold
        # the bytes into the payload as a data URI so the existing
        # import pipeline persists them on the new entity.
        if is_png:
            png_bytes = zf.read(path)
            encoded = base64.b64encode(png_bytes).decode("ascii")
            data = dict(data)
            data["cardImageUri"] = f"data:image/png;base64,{encoded}"

        # Defensive: if the picker said "import" but the entity already
        # exists by id (stale manifest, race with another tab), downgrade
        # to skip rather than letting import_streaming yield an unhandled
        # ``conflict`` event mid-stream.
        if action == "import":
            sid = _id_for_kind(data, kind)
            if sid and _entity_exists(kind, sid):
                continue

        # ``mode=ask`` for action=import is safe here: by the defensive
        # check above the id (if any) is free, so ``_resolve_target_id``
        # returns it without a conflict event. Preserves the source UUID
        # so re-imports of the same archive detect "already imported".
        mode = "ask" if action == "import" else action

        resolutions = (
            {"character": "new", "user": "new", "scenario": "new"}
            if kind == "chat" else None
        )

        item_label = _name_for_kind(data, kind) or kind
        try:
            async for ev in import_streaming(
                data, mode=mode, resolutions=resolutions, filename=path,
            ):
                ev_type = ev.get("type")
                if ev_type == "progress":
                    yield {**ev, "label": f"[{item_label}] {ev.get('label') or ''}"}
                elif ev_type == "done":
                    bucket = {
                        "contact": "contacts", "user": "users",
                        "scenario": "scenarios", "chat": "chats",
                        "brain_library": "libraries",
                    }.get(ev.get("kind"))
                    if bucket is not None:
                        imported[bucket] += 1
                # Drop ``conflict`` / ``name_matches`` events — the
                # defensive choices above mean they shouldn't fire, and we
                # don't have the interactive UI hooks here to resolve them.
        except Exception as e:
            log.exception("flat import failed for %s", path)
            yield {"type": "error", "message": f"{path}: {e}"}
            return

        done += 1
        yield {"type": "progress",
               "label": f"{kind.capitalize()}: {item_label}",
               "phase": "bulk", "current": done, "total": total, "failed": 0}

    yield {"type": "done", "kind": "bulk", "imported": imported}


# ---------------------------------------------------------------------------
# Public dispatchers — pick the right format handler from a zip's contents.
# ---------------------------------------------------------------------------


def _zip_format(zf: zipfile.ZipFile) -> str:
    """Return ``"aer_bulk"`` if any AER ``meta-contact.json`` is present;
    ``"flat_json"`` if there are bare ``*.json`` or ``*.png`` members
    but no AER metadata; ``"empty"`` if neither.

    Mixed-format zips (an AER tree alongside loose files) are treated as
    ``"aer_bulk"`` — AER takes precedence and the loose files are ignored,
    so the user gets a deterministic outcome instead of a surprise mix.
    """
    has_aer_meta = False
    has_loose = False
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if _CONTACT_META_RE.match(name):
            has_aer_meta = True
            break
        lname = name.lower()
        if lname.endswith(".json") or lname.endswith(".png"):
            has_loose = True
    if has_aer_meta:
        return "aer_bulk"
    if has_loose:
        return "flat_json"
    return "empty"


def read_zip_toc(zf: zipfile.ZipFile) -> Iterator[dict]:
    """Iterator yielding ``progress`` events while parsing the archive,
    terminating with a ``manifest`` event whose ``manifest`` field is the
    format-specific dict. Caller is expected to surface progress events
    over SSE and apply :func:`_decorate_manifest` before showing the
    manifest in the picker.
    """
    fmt = _zip_format(zf)
    if fmt == "aer_bulk":
        yield from _read_aer_zip_toc(zf)
    elif fmt == "flat_json":
        yield from _read_flat_zip_toc(zf)
    else:
        yield {"type": "manifest",
               "manifest": {"format": "empty", "items": []}}


# Threshold below which a data-URI avatar passes through to the picker
# inline rather than going through the proxy. ~4 KB ≈ a 64×64 thumbnail
# already in WebP, so the proxy round-trip would be wasted.
_DATA_URI_INLINE_LIMIT = 4096


def _maybe_proxy_avatar(item: dict, token: str) -> None:
    """Mutate ``item`` in place: replace ``avatar_uri`` with a proxy URL
    when the source is a large data URI or any HTTP URL.

    Without proxying, the picker would either inline 100s of MBs of
    base64 in the manifest (data-URI case) or have the browser hold
    every catbox PNG at full resolution in memory (URL case). Proxying
    serves a ≤5KB WebP per row instead.
    """
    avatar = item.get("avatar_uri") or ""
    path = item.get("path")
    if not (avatar and path):
        return
    is_data_uri = avatar.startswith("data:")
    is_http = avatar.startswith("http://") or avatar.startswith("https://")
    if is_data_uri and len(avatar) <= _DATA_URI_INLINE_LIMIT:
        return  # small inline preview — no proxy needed
    if not (is_data_uri or is_http):
        return
    item["avatar_uri"] = (
        f"/api/import-zip-preview/{token}"
        f"?path={urllib.parse.quote(path, safe='')}"
    )


def _decorate_manifest(token: str, manifest: dict) -> None:
    """Replace large / remote avatar references in the manifest with
    proxy-URL pointers. Mutates ``manifest`` in place. Called by the
    route layer right before emitting the final ``manifest`` SSE event."""
    fmt = manifest.get("format")
    if fmt == "aer_bulk":
        for c in manifest.get("contacts") or []:
            _maybe_proxy_avatar(c, token)
    elif fmt == "flat_json":
        for item in manifest.get("items") or []:
            _maybe_proxy_avatar(item, token)


async def import_zip_streaming(zip_path: Path, selection: dict):
    """Stream-import the entities the user selected from a manifest.

    Dispatches on the zip's detected format. SSE event shape:

    - ``progress`` ``{label, phase, current, total, failed}`` while running
    - ``done`` ``{kind: "bulk", imported: {...}}`` on success
    - ``error`` ``{message}`` if the zip is malformed

    ``zip_path`` is the on-disk staging file (see the upload route);
    the function opens / closes it itself.
    """
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        yield {"type": "error", "message": f"Invalid zip: {e}"}
        return
    with zf:
        # ``selection.format`` (when set) overrides detection so the caller
        # can be explicit; otherwise we walk the central directory.
        fmt = (
            (selection.get("format") if isinstance(selection, dict) else None)
            or _zip_format(zf)
        )
        if fmt == "aer_bulk":
            async for ev in _import_aer_zip_streaming(zf, selection):
                yield ev
        elif fmt == "flat_json":
            async for ev in _import_flat_zip_streaming(zf, selection):
                yield ev
        else:
            yield {"type": "error",
                   "message": "Archive contained no contacts or JSON files"}

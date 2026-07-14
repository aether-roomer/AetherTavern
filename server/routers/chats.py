"""Chat metadata + message-tree routes (CRUD, branching, bookmarks)."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from server import storage
from server.aer.macros import apply_macros
from server.aer.rollover import get_active_path
from server.conflicts import check_version
from server.list_models import ChatListPage, ChatSummary, FavoriteUpdate
from server.proxy_rules import proxy_rules_scope
from server.routers.files import sniff_image
from server.models import (
    EMPTY_SENTINEL,
    ROOT_PARENT_KEY,
    Attachment,
    Bookmark,
    BookmarkHistoryEntry,
    Brain,
    Chat,
    ChatBookmarks,
    ChatMessage,
    ChatMessages,
    Contact,
    ContactScenario,
    Emotion,
    Sender,
    SubMessage,
    new_id,
    now_seconds,
)


router = APIRouter(prefix="/api/chats", tags=["chats"])


# ---------------------------------------------------------------------------
# Chat CRUD
# ---------------------------------------------------------------------------


class CreateChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    contact_id: str
    user_id: str
    scenario_id: str | None = None
    contact_scenario_id: str | None = None
    brain_library_ids: list[str] = Field(default_factory=list)
    title: str = ""
    tags: str = ""
    # Per-chat generation overrides (all default to inherit). See Chat.
    provider_override: str | None = None
    model_overrides: dict[str, str] = Field(default_factory=dict)
    context_preset_override: str | None = None


def _find_contact_scenario(contact: Contact, cs_id: str) -> ContactScenario | None:
    return next((cs for cs in contact.scenarios if cs.id == cs_id), None)


_LIMIT_MAX = 100


@router.get("")
async def list_chats(
    q: str = "",
    contact_id: str | None = None,
    user_id: str | None = None,
    scenario_id: str | None = None,
    ref_library_id: str | None = None,
    favorites_first: bool = False,
    sort: Literal["updated_at", "created_at", "title"] = "updated_at",
    direction: Literal["asc", "desc"] = "desc",
    offset: int | None = None,
    limit: int | None = None,
) -> list[ChatSummary] | ChatListPage:
    """List chats — Summary projections, sorted by ``updated_at`` desc by default.

    Backwards-compat: with no query params and no pagination set, returns
    the flat ``list[ChatSummary]`` consumers have always seen. As soon as
    any filter / search / sort / pagination param is set, the response
    becomes a ``ChatListPage`` envelope (``{items, total, offset, limit}``)
    so the paged client can size its scrollbar via ``total``.

    Params:
      * ``q``               case-insensitive substring on ``title`` plus
                            the joined contact name; AND across whitespace-
                            separated tokens.
      * ``contact_id`` /
        ``user_id`` /
        ``scenario_id``     equality filters on the chat's foreign keys.
      * ``ref_library_id``  membership filter on ``chat.brain_library_ids``;
                            separate name so it's clear it's a list match.
      * ``favorites_first`` partition favourites to the top while preserving
                            the secondary sort within each partition.
      * ``sort`` /
        ``direction``       chat-level sort keys (``updated_at`` /
                            ``created_at`` / ``title``) and order.
      * ``offset`` / ``limit``  pagination. ``limit`` capped at
                            ``_LIMIT_MAX`` (100).
    """
    has_any_param = any([
        q, contact_id, user_id, scenario_id, ref_library_id,
        favorites_first, sort != "updated_at", direction != "desc",
        offset is not None, limit is not None,
    ])
    chats: list[Chat] = list(storage.index.chats.values())

    # Filters.
    if contact_id:
        chats = [c for c in chats if c.contact_id == contact_id]
    if user_id:
        chats = [c for c in chats if c.user_id == user_id]
    if scenario_id:
        chats = [c for c in chats if c.scenario_id == scenario_id]
    if ref_library_id:
        chats = [c for c in chats if ref_library_id in c.brain_library_ids]

    # Free-text search across title + joined contact.name. We don't
    # precompute a per-chat search index — at hundreds-to-low-thousands
    # the linear scan is microseconds, and a sparse index would have to
    # be invalidated on every Contact rename + Chat update.
    if q:
        tokens = [t for t in q.lower().split() if t]
        def _haystack(c: Chat) -> str:
            contact = storage.index.contacts.get(c.contact_id)
            cname = (contact.name if contact else "") or ""
            return f"{c.title} {cname}".lower()
        chats = [c for c in chats if all(t in _haystack(c) for t in tokens)]

    # Sort.
    key_fn = {
        "updated_at": lambda c: c.updated_at,
        "created_at": lambda c: c.created_at,
        "title": lambda c: (c.title or "").lower(),
    }[sort]
    chats.sort(key=key_fn, reverse=(direction == "desc"))
    if favorites_first:
        chats.sort(key=lambda c: 0 if c.favorite else 1)  # stable

    total = len(chats)

    if not has_any_param:
        # Legacy flat-list shape.
        return [ChatSummary.from_chat(c) for c in chats]

    # Paginate.
    eff_offset = max(offset or 0, 0)
    eff_limit = min(limit or _LIMIT_MAX, _LIMIT_MAX)
    sliced = chats[eff_offset:eff_offset + eff_limit]
    return ChatListPage(
        items=[ChatSummary.from_chat(c) for c in sliced],
        total=total,
        offset=eff_offset,
        limit=eff_limit,
    )


@router.post("")
async def create_chat(req: CreateChatRequest) -> Chat:
    contact = storage.get_contact(req.contact_id)
    user = storage.get_user(req.user_id)
    if contact is None:
        raise HTTPException(400, f"contact {req.contact_id!r} not found")
    if user is None:
        raise HTTPException(400, f"user {req.user_id!r} not found")
    if req.scenario_id and req.contact_scenario_id:
        raise HTTPException(400, "scenario_id and contact_scenario_id are mutually exclusive")
    scenario = None
    if req.scenario_id:
        scenario = storage.get_scenario(req.scenario_id)
        if scenario is None:
            raise HTTPException(400, f"scenario {req.scenario_id!r} not found")
    contact_scenario = None
    if req.contact_scenario_id:
        contact_scenario = _find_contact_scenario(contact, req.contact_scenario_id)
        if contact_scenario is None:
            raise HTTPException(
                400, f"contact scenario {req.contact_scenario_id!r} not found on contact",
            )

    title = req.title.strip() or f"Chat with {contact.name}"
    tags = req.tags
    if not tags:
        if contact_scenario is not None:
            tags = contact_scenario.tags
        elif scenario is not None:
            tags = scenario.tags

    # Style / intimacy / response_length: a contact-scenario can override the
    # contact's defaults at chat-creation time (None = inherit). Global
    # scenarios don't override these — they only contribute scene/env/tags.
    intimacy = contact.default_intimacy
    style = contact.default_style
    response_length = contact.default_response_length
    if contact_scenario is not None:
        if contact_scenario.intimacy is not None:
            intimacy = contact_scenario.intimacy
        if contact_scenario.style is not None:
            style = contact_scenario.style
        if contact_scenario.response_length is not None:
            response_length = contact_scenario.response_length

    # Brain libraries are validated cheaply (unknown ids are silently skipped
    # at generation time anyway), but we de-dupe + drop the empty string here
    # so the persisted list matches what the user actually picked.
    library_ids = [lid for lid in (req.brain_library_ids or []) if lid]
    seen: set[str] = set()
    dedup_libs: list[str] = []
    for lid in library_ids:
        if lid in seen:
            continue
        seen.add(lid)
        dedup_libs.append(lid)

    chat = Chat(
        title=title,
        tags=tags,
        contact_id=req.contact_id,
        user_id=req.user_id,
        scenario_id=req.scenario_id,
        contact_scenario_id=req.contact_scenario_id,
        brain_library_ids=dedup_libs,
        intimacy=intimacy,
        style=style,
        response_length=response_length,
        # CJK is a chat-level flag; seed it from the OR of the participants'
        # defaults so a CJK contact / persona / scenario reliably triggers
        # CJK output on a new chat. The user can flip it per-chat afterwards.
        cjk=(
            contact.cjk
            or user.cjk
            or (scenario.cjk if scenario is not None else False)
            or (contact_scenario.cjk if contact_scenario is not None else False)
        ),
        provider_override=req.provider_override,
        model_overrides=dict(req.model_overrides),
        context_preset_override=req.context_preset_override,
    )

    # Seed the chat with a greeting (if any) so the user lands on a real
    # opening line rather than an empty thread. Macros are expanded against
    # the chosen personas before persisting. A contact-scenario with a
    # non-empty ``greeting`` overrides the contact-level greeting.
    messages = ChatMessages()
    if contact_scenario is not None and (contact_scenario.greeting or "").strip():
        greeting_text = contact_scenario.greeting.strip()
        greeting_emotion = contact_scenario.greeting_emotion or Emotion.NEUTRAL
    else:
        greeting_text = (contact.greeting or "").strip()
        greeting_emotion = contact.greeting_emotion or Emotion.NEUTRAL
    if greeting_text:
        greeting_msg = ChatMessage(
            sender="contact",
            sender_name=contact.name,
            body=[SubMessage(
                text=apply_macros(
                    greeting_text, contact, user,
                    scenario=scenario,
                    contact_scenario=contact_scenario,
                    chat=chat,
                    settings=storage.load_settings(),
                ),
                emotion=greeting_emotion,
            )],
            # Greetings are always AER-origin even in Generic mode — AER form
            # is strictly more information (preserves emotion, multi-bubble
            # structure), so storing the greeting this way keeps round-trip
            # fidelity across mode switches. Generation-metadata stays None
            # since no LLM call happened.
            origin="aer",
        )
        messages.messages.append(greeting_msg)
        chat.selected_child_id[ROOT_PARENT_KEY] = greeting_msg.id

    # New chat — no concurrent writers possible yet. Use the safe-order
    # helper so chat.yaml (which references the greeting) is written last.
    storage.save_chat_with_all(chat, messages, ChatBookmarks())
    return chat


@router.get("/{chat_id}")
async def get_chat(chat_id: str) -> Chat:
    chat = storage.get_chat(chat_id)
    if chat is None:
        raise HTTPException(404, f"chat {chat_id!r} not found")
    return chat


@router.put("/{chat_id}")
async def update_chat(chat_id: str, chat: Chat) -> Chat:
    if chat.id != chat_id:
        raise HTTPException(400, "chat id in body does not match URL")
    async with storage.lock(f"chat:{chat_id}"):
        existing = storage.get_chat(chat_id)
        if existing is None:
            raise HTTPException(404, f"chat {chat_id!r} not found")
        if not check_version(chat, existing):
            return existing
        # Validate referenced entities — the chat info modal lets users
        # reassign contact/user/scenario, so we can't trust the body to
        # point at live IDs.
        contact = storage.get_contact(chat.contact_id)
        if contact is None:
            raise HTTPException(400, f"contact {chat.contact_id!r} not found")
        if storage.get_user(chat.user_id) is None:
            raise HTTPException(400, f"user {chat.user_id!r} not found")
        if chat.scenario_id and chat.contact_scenario_id:
            raise HTTPException(400, "scenario_id and contact_scenario_id are mutually exclusive")
        if chat.scenario_id and storage.get_scenario(chat.scenario_id) is None:
            raise HTTPException(400, f"scenario {chat.scenario_id!r} not found")
        if chat.contact_scenario_id and _find_contact_scenario(contact, chat.contact_scenario_id) is None:
            raise HTTPException(
                400, f"contact scenario {chat.contact_scenario_id!r} not found on contact",
            )
        chat.created_at = existing.created_at
        chat.updated_at = now_seconds()
        return storage.save_chat(chat)


@router.patch("/{chat_id}/favorite")
async def set_chat_favorite(chat_id: str, body: FavoriteUpdate) -> ChatSummary:
    """Toggle ``favorite`` without bumping ``version_id`` — the chat info
    modal's open draft elsewhere stays valid. Returns the updated
    Summary so the client can patch list state without a full re-fetch."""
    async with storage.lock(f"chat:{chat_id}"):
        c = storage.get_chat(chat_id)
        if c is None:
            raise HTTPException(404, f"chat {chat_id!r} not found")
        c.favorite = body.favorite
        c.updated_at = now_seconds()
        storage.save_chat(c, bump_version=False)
        return ChatSummary.from_chat(c)


@router.delete("/{chat_id}")
async def delete_chat(chat_id: str) -> dict:
    async with storage.lock(f"chat:{chat_id}"):
        if not storage.delete_chat(chat_id):
            raise HTTPException(404, f"chat {chat_id!r} not found")
        return {"deleted": chat_id}


# ---------------------------------------------------------------------------
# Messages (tree)
# ---------------------------------------------------------------------------


class CreateMessageRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    parent_id: Optional[str] = None
    sender: Sender
    sender_name: Optional[str] = None
    body: list[SubMessage]
    brains: list[Brain] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)


class UpdateMessageRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    body: list[SubMessage] | None = None
    brains: list[Brain] | None = None


class SelectChildRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    parent_id: Optional[str] = None  # None = root
    child_id: str  # message id, or "__empty__"


def _ensure_chat(chat_id: str) -> Chat:
    chat = storage.get_chat(chat_id)
    if chat is None:
        raise HTTPException(404, f"chat {chat_id!r} not found")
    return chat


def _rewrite_message_for_wire(msg: ChatMessage, chat_id: str) -> ChatMessage:
    """Rewrite ``msg.body[*].text`` so each ``msg.image_refs[remote_url]``
    occurrence becomes the proxy URL ``/api/chats/{chat_id}/images/{uuid}/{filename}``.

    The on-disk message keeps the original remote URL (so future clients
    can still resolve it if the proxy file goes missing). This rewrite
    only affects the response sent to the client.

    Returns a model_copy with the bubble texts replaced when any rewrite
    fires; the original instance when nothing changes.
    """
    if not msg.image_refs:
        return msg
    new_body = []
    changed = False
    for bubble in msg.body or []:
        text = bubble.text or ""
        rewritten = text
        for remote_url, uuid in msg.image_refs.items():
            if remote_url and remote_url in rewritten:
                filename = remote_url.rsplit("/", 1)[-1] or "image.bin"
                if "?" in filename:
                    filename = filename.split("?", 1)[0] or "image.bin"
                proxy_url = f"/api/chats/{chat_id}/images/{uuid}/{filename}"
                rewritten = rewritten.replace(remote_url, proxy_url)
        if rewritten != text:
            changed = True
            new_body.append(bubble.model_copy(update={"text": rewritten}))
        else:
            new_body.append(bubble)
    if not changed:
        return msg
    return msg.model_copy(update={"body": new_body})


@router.get("/{chat_id}/messages")
async def list_messages(chat_id: str) -> list[ChatMessage]:
    _ensure_chat(chat_id)
    return [
        _rewrite_message_for_wire(m, chat_id)
        for m in storage.load_chat_messages(chat_id).messages
    ]


_MSG_MACRO_ALLOWLIST = {"roll"}


def _bake_message_macros(body: list[SubMessage], contact, user) -> list[SubMessage]:
    """Run the message-scope macro pass (just ``{{roll}}``) over each bubble's
    text. Other macros stay literal in stored message bodies — only ``{{roll}}``
    expands here so dice in user-typed text persist their rolled result."""
    out: list[SubMessage] = []
    for bubble in body:
        text = bubble.text or ""
        if "{{" in text:
            text = apply_macros(text, contact, user, allowlist=_MSG_MACRO_ALLOWLIST)
        out.append(bubble.model_copy(update={"text": text}))
    return out


@router.post("/{chat_id}/messages")
async def create_message(chat_id: str, req: CreateMessageRequest) -> ChatMessage:
    async with storage.lock(f"chat:{chat_id}"):
        chat = _ensure_chat(chat_id)
        contact = storage.get_contact(chat.contact_id)
        user = storage.get_user(chat.user_id)
        if contact is None or user is None:
            raise HTTPException(400, "chat references missing entity")
        sender_name = req.sender_name or (
            user.name if req.sender == "user" else contact.name
        )
        msgs = storage.load_chat_messages(chat_id)
        if req.parent_id is not None:
            if not any(m.id == req.parent_id for m in msgs.messages):
                raise HTTPException(400, f"parent message {req.parent_id!r} not in chat")
        # Validate attachments: drop any whose file no longer exists under
        # the chat's attachments dir (defence in depth — the client could
        # send a stale id from an aborted upload).
        attachments_validated: list[Attachment] = []
        bound_att_ids: set[str] = set()
        if req.attachments:
            att_dir = storage.chat_attachments_dir(chat_id)
            if att_dir and att_dir.exists():
                for att in req.attachments:
                    if any(att_dir.glob(f"{att.id}.*")):
                        attachments_validated.append(att)
                        bound_att_ids.add(att.id)
        new_msg = ChatMessage(
            parent_id=req.parent_id,
            sender=req.sender,
            sender_name=sender_name,
            body=_bake_message_macros(req.body, contact, user),
            brains=req.brains,
            attachments=attachments_validated,
            # Tag user-typed messages so the renderer routes them through the
            # AER renderer (plain text, unobtrusive highlighter) rather than
            # the markdown path. Contact-side messages persisted by this
            # endpoint are operator-authored ports of AER content — they
            # keep the default ``"aer"`` origin via Pydantic.
            origin="manual" if req.sender == "user" else "aer",
        )
        msgs.messages.append(new_msg)
        key = req.parent_id if req.parent_id is not None else ROOT_PARENT_KEY
        chat.selected_child_id[key] = new_msg.id
        chat.updated_at = now_seconds()
        # Atomically move the bound attachments from chat.pending_attachments
        # into the new message. Same chat lock, same chat.yaml write — a
        # concurrent reload can't observe a state where the message has
        # the attachments AND the pending list still does.
        if bound_att_ids and chat.pending_attachments:
            chat.pending_attachments = [
                a for a in chat.pending_attachments if a.id not in bound_att_ids
            ]
        # messages.yaml first so chat.selected_child_id's reference resolves
        # if the process dies between the two writes. bump_version=False so
        # the chat info modal's open draft doesn't 409 on its next autosave
        # — tree mutations don't conflict with entity edits, locks serialize.
        storage.save_chat_messages(chat_id, msgs)
        storage.save_chat(chat, bump_version=False)
        return _rewrite_message_for_wire(new_msg, chat_id)


@router.put("/{chat_id}/messages/{msg_id}")
async def update_message(chat_id: str, msg_id: str, req: UpdateMessageRequest) -> ChatMessage:
    async with storage.lock(f"chat:{chat_id}"):
        chat = _ensure_chat(chat_id)
        contact = storage.get_contact(chat.contact_id)
        user = storage.get_user(chat.user_id)
        msgs = storage.load_chat_messages(chat_id)
        for i, m in enumerate(msgs.messages):
            if m.id == msg_id:
                new_body = req.body
                if new_body is not None and contact is not None and user is not None:
                    new_body = _bake_message_macros(new_body, contact, user)
                updated = m.model_copy(update={
                    **({"body": new_body} if req.body is not None else {}),
                    **({"brains": req.brains} if req.brains is not None else {}),
                })
                msgs.messages[i] = updated
                storage.save_chat_messages(chat_id, msgs)
                return _rewrite_message_for_wire(updated, chat_id)
        raise HTTPException(404, f"message {msg_id!r} not found")


@router.delete("/{chat_id}/messages/{msg_id}")
async def delete_message(chat_id: str, msg_id: str) -> dict:
    """Soft-delete: mark this branch as ``__empty__`` on its parent."""
    async with storage.lock(f"chat:{chat_id}"):
        chat = _ensure_chat(chat_id)
        msgs = storage.load_chat_messages(chat_id)
        target = next((m for m in msgs.messages if m.id == msg_id), None)
        if target is None:
            raise HTTPException(404, f"message {msg_id!r} not found")
        key = target.parent_id if target.parent_id is not None else ROOT_PARENT_KEY
        chat.last_deleted_child[key] = msg_id
        chat.selected_child_id[key] = EMPTY_SENTINEL
        chat.updated_at = now_seconds()
        storage.recount_active_path(chat, msgs)
        storage.save_chat(chat, bump_version=False)
        return {"deleted": msg_id, "soft": True}


@router.post("/{chat_id}/messages/{msg_id}/restore")
async def restore_message(chat_id: str, msg_id: str) -> dict:
    """Undo a soft delete: pick this message as the chosen child of its parent."""
    async with storage.lock(f"chat:{chat_id}"):
        chat = _ensure_chat(chat_id)
        msgs = storage.load_chat_messages(chat_id)
        target = next((m for m in msgs.messages if m.id == msg_id), None)
        if target is None:
            raise HTTPException(404, f"message {msg_id!r} not found")
        key = target.parent_id if target.parent_id is not None else ROOT_PARENT_KEY
        chat.selected_child_id[key] = msg_id
        chat.updated_at = now_seconds()
        storage.recount_active_path(chat, msgs)
        storage.save_chat(chat, bump_version=False)
        return {"restored": msg_id}


@router.post("/{chat_id}/select")
async def select_child(chat_id: str, req: SelectChildRequest) -> Chat:
    async with storage.lock(f"chat:{chat_id}"):
        chat = _ensure_chat(chat_id)
        msgs = storage.load_chat_messages(chat_id)
        if req.child_id != EMPTY_SENTINEL:
            if not any(m.id == req.child_id for m in msgs.messages):
                raise HTTPException(400, f"child message {req.child_id!r} not in chat")
        key = req.parent_id if req.parent_id is not None else ROOT_PARENT_KEY
        chat.selected_child_id[key] = req.child_id
        chat.updated_at = now_seconds()
        storage.recount_active_path(chat, msgs)
        storage.save_chat(chat, bump_version=False)
        return chat


@router.get("/{chat_id}/active-path")
async def active_path(chat_id: str) -> list[ChatMessage]:
    chat = _ensure_chat(chat_id)
    msgs = storage.load_chat_messages(chat_id)
    return get_active_path(chat, msgs.messages)


@router.post("/{chat_id}/reroll-picks")
async def reroll_picks(chat_id: str) -> Chat:
    """Bump the chat's pick-reroll nonce so every ``{{pick}}`` re-evaluates
    to a fresh option on the next generation."""
    async with storage.lock(f"chat:{chat_id}"):
        chat = _ensure_chat(chat_id)
        chat.pick_reroll_nonce = new_id()
        chat.updated_at = now_seconds()
        storage.save_chat(chat, bump_version=False)
        return chat


_PICK_MARKER = "{{pick"


def _scan_fields(obj: object, fields: list[str]) -> bool:
    for f in fields:
        v = getattr(obj, f, None)
        if isinstance(v, str) and _PICK_MARKER in v:
            return True
    return False


def _scan_brains(brains: list[Brain] | None) -> bool:
    for b in brains or []:
        if not b.disabled and _PICK_MARKER in (b.content or ""):
            return True
    return False


def _chat_uses_pick_macro(chat: Chat) -> bool:
    """True iff anything that feeds the chat's prompt header references
    ``{{pick``. Drives the reroll-pick button's visibility.

    Walks the full Contact + ContactScenarios, User, the chat's active
    global Scenario, and any attached BrainLibraries. The client-side
    list state is ``*Summary`` projections that strip ``persona`` /
    ``appearance`` / ``brains`` / ``environment`` etc., so detection has
    to live here against the indexed full entities.
    """
    if chat.contact_id:
        contact = storage.get_contact(chat.contact_id)
        if contact is not None:
            if _scan_fields(contact, [
                "persona", "appearance", "species", "gender", "pronouns",
                "description", "greeting", "tags",
            ]):
                return True
            if _scan_brains(contact.brains):
                return True
            for cs in contact.scenarios:
                if _scan_fields(cs, [
                    "environment", "scene", "description", "tags", "greeting",
                ]):
                    return True
                if _scan_brains(cs.brains):
                    return True
    if chat.user_id:
        user = storage.get_user(chat.user_id)
        if user is not None:
            if _scan_fields(user, [
                "persona", "appearance", "species", "gender", "pronouns",
                "description", "tags",
            ]):
                return True
            if _scan_brains(user.brains):
                return True
    if chat.scenario_id:
        scenario = storage.get_scenario(chat.scenario_id)
        if scenario is not None:
            if _scan_fields(scenario, ["environment", "scene", "description", "tags"]):
                return True
            if _scan_brains(scenario.brains):
                return True
    for lib_id in chat.brain_library_ids or []:
        lib = storage.get_brain_library(lib_id)
        if lib is not None and _scan_brains(lib.brains):
            return True
    return False


class UsesPickMacroResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    uses: bool


@router.get("/{chat_id}/uses-pick-macro")
async def uses_pick_macro(chat_id: str) -> UsesPickMacroResponse:
    chat = _ensure_chat(chat_id)
    return UsesPickMacroResponse(uses=_chat_uses_pick_macro(chat))


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------


class CreateBookmarkRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str = ""
    snippet: str
    selected_child_id: dict[str, str]
    favorite: bool = False


class UpdateBookmarkRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    title: str | None = None
    snippet: str | None = None
    favorite: bool | None = None


@router.get("/{chat_id}/bookmarks")
async def list_bookmarks(chat_id: str) -> ChatBookmarks:
    _ensure_chat(chat_id)
    return storage.load_chat_bookmarks(chat_id)


@router.post("/{chat_id}/bookmarks")
async def create_bookmark(chat_id: str, req: CreateBookmarkRequest) -> Bookmark:
    async with storage.lock(f"chat:{chat_id}"):
        _ensure_chat(chat_id)
        bm = Bookmark(
            title=req.title,
            snippet=req.snippet,
            selected_child_id=req.selected_child_id,
            favorite=req.favorite,
        )
        book = storage.load_chat_bookmarks(chat_id)
        book.bookmarks.append(bm)
        storage.save_chat_bookmarks(chat_id, book)
        return bm


@router.put("/{chat_id}/bookmarks/{bookmark_id}")
async def update_bookmark(
    chat_id: str, bookmark_id: str, req: UpdateBookmarkRequest
) -> Bookmark:
    async with storage.lock(f"chat:{chat_id}"):
        _ensure_chat(chat_id)
        book = storage.load_chat_bookmarks(chat_id)
        for i, b in enumerate(book.bookmarks):
            if b.id == bookmark_id:
                patch = {k: v for k, v in req.model_dump().items() if v is not None}
                updated = b.model_copy(update=patch)
                book.bookmarks[i] = updated
                storage.save_chat_bookmarks(chat_id, book)
                return updated
        raise HTTPException(404, f"bookmark {bookmark_id!r} not found")


@router.delete("/{chat_id}/bookmarks/{bookmark_id}")
async def delete_bookmark(chat_id: str, bookmark_id: str) -> dict:
    async with storage.lock(f"chat:{chat_id}"):
        _ensure_chat(chat_id)
        book = storage.load_chat_bookmarks(chat_id)
        n = len(book.bookmarks)
        book.bookmarks = [b for b in book.bookmarks if b.id != bookmark_id]
        if len(book.bookmarks) == n:
            raise HTTPException(404, f"bookmark {bookmark_id!r} not found")
        storage.save_chat_bookmarks(chat_id, book)
        return {"deleted": bookmark_id}


class RestorePathRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    selected_child_id: dict[str, str]


def _lock_active_path(chat: Chat, messages: list[ChatMessage]) -> None:
    """Walk the chat's active path and set every parent → chosen-child entry
    explicitly in ``selected_child_id``.

    Without this, parents that lack an explicit entry fall back to "latest
    sibling" during traversal, which drifts as new siblings are generated and
    causes two distinct bookmarks to converge once the active-but-implicit
    tail happens to match the most recently jumped-to path.
    """
    active = get_active_path(chat, messages)
    prev_key = ROOT_PARENT_KEY
    for m in active:
        chat.selected_child_id[prev_key] = m.id
        prev_key = m.id


def _snapshot_current_path(chat: Chat, messages: list[ChatMessage]) -> dict[str, str]:
    """Build a *full* snapshot of the current active path — the inverse of
    ``_lock_active_path`` (returns rather than mutates) used when recording
    history entries so they stay deterministic on replay."""
    snapshot = dict(chat.selected_child_id)
    active = get_active_path(chat, messages)
    prev_key = ROOT_PARENT_KEY
    for m in active:
        snapshot[prev_key] = m.id
        prev_key = m.id
    return snapshot


def _path_snippet(chat: Chat, messages: list[ChatMessage]) -> str:
    path = get_active_path(chat, messages)
    if not path or not path[-1].body:
        return "(empty)"
    return (path[-1].body[0].text or "").replace("\n", " ")[:80] or "(empty)"


def _push_history(chat_id: str, snippet: str, snapshot: dict[str, str]) -> None:
    """Prepend an entry to the chat's bookmark history, trimming to the most
    recent 20 entries. Skips the push if the most recent entry already matches
    the same snapshot — prevents consecutive duplicates when bouncing between
    the same two paths."""
    from datetime import datetime

    book = storage.load_chat_bookmarks(chat_id)
    if book.history and book.history[0].selected_child_id == snapshot:
        return
    entry = BookmarkHistoryEntry(
        snippet=snippet or "(empty)",
        selected_child_id=snapshot,
        time=datetime.now().strftime("%H:%M"),
    )
    book.history = ([entry] + book.history)[:20]
    storage.save_chat_bookmarks(chat_id, book)


@router.post("/{chat_id}/restore-path")
async def restore_path(chat_id: str, req: RestorePathRequest) -> Chat:
    """Replace the chat's ``selected_child_id`` wholesale (e.g. jump to a
    specific saved snapshot from the bookmark history). Pushes the *current*
    path onto bookmark history before jumping, so the user can hop back."""
    async with storage.lock(f"chat:{chat_id}"):
        chat = _ensure_chat(chat_id)
        msgs = storage.load_chat_messages(chat_id)
        _push_history(chat_id, _path_snippet(chat, msgs.messages),
                      _snapshot_current_path(chat, msgs.messages))
        chat.selected_child_id = dict(req.selected_child_id)
        _lock_active_path(chat, msgs.messages)
        chat.updated_at = now_seconds()
        storage.recount_active_path(chat, msgs)
        storage.save_chat(chat, bump_version=False)
        return chat


def _find_image_on_disk(chat_path, uuid: str):
    """Return the on-disk image path for ``uuid`` in this chat, or None.

    Files are stored as ``images/{uuid}.{ext}`` — extension may vary
    (png/jpg/webp/gif/bin). Glob to find it.
    """
    images_dir = chat_path / "images"
    if not images_dir.exists():
        return None
    for candidate in images_dir.glob(f"{uuid}.*"):
        if candidate.is_file():
            return candidate
    return None


def _lookup_remote_url_for_uuid(chat_id: str, uuid: str) -> str | None:
    """Reverse-lookup ``uuid`` to the remote URL stored in any of the chat's
    messages. Returns ``None`` if no message has this uuid registered."""
    try:
        messages = storage.load_chat_messages(chat_id).messages
    except Exception:
        return None
    for m in messages:
        for url, mapped_uuid in (m.image_refs or {}).items():
            if mapped_uuid == uuid:
                return url
    return None


@router.get("/{chat_id}/images/{uuid}/{filename}")
async def get_chat_image(
    chat_id: str,
    uuid: str,
    filename: str,
    request: Request,
) -> Response:
    """Lazy-downloading image proxy for Generic-mode chat assets.

    Disk lookup first (covers both data-URL files written at stream time
    and previously-downloaded HTTP cache). If the file isn't on disk,
    reverse-lookup the uuid via ``ChatMessage.image_refs``, download via
    ``_ImageSession.fetch``, sniff content (refuse HTML), decode-verify
    with PIL, and write byte-identical to disk. Serves with
    ``Content-Disposition: inline; filename=...`` so the browser tags
    the saved file with the original name.
    """
    chat_path = storage.chat_dir(chat_id)
    if chat_path is None:
        raise HTTPException(404, f"chat {chat_id!r} not found")

    # 1. Disk lookup — covers both data-URL bytes and prior HTTP downloads.
    on_disk = _find_image_on_disk(chat_path, uuid)
    if on_disk is not None:
        try:
            data = on_disk.read_bytes()
        except OSError:
            raise HTTPException(500, "Failed to read cached image")
        try:
            _, mime = sniff_image(data)
        except HTTPException:
            mime = "application/octet-stream"
        return Response(
            content=data,
            media_type=mime,
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "private, max-age=86400",
            },
        )

    # 2. Reverse-lookup remote URL.
    remote_url = _lookup_remote_url_for_uuid(chat_id, uuid)
    if remote_url is None:
        raise HTTPException(404, f"image {uuid!r} not registered with chat {chat_id!r}")

    # 3. Download via _ImageSession.fetch.
    from server.importers import _ImageSession

    rules = getattr(request.app.state, "proxy_rules", None)
    with proxy_rules_scope(rules):
        session = _ImageSession(proxy_rules=rules)
        try:
            data = await session.fetch(remote_url)
        except Exception as e:
            raise HTTPException(502, f"Failed to fetch image: {e}") from e

    # 4. Sniff + decode-verify.
    try:
        ext, mime = sniff_image(data)
    except HTTPException:
        raise HTTPException(502, "Upstream did not return a recognised image format")
    try:
        from io import BytesIO
        from PIL import Image
        Image.open(BytesIO(data)).verify()
    except Exception as e:
        raise HTTPException(502, f"Image decode-verify failed: {e}") from e

    # 5. Write byte-identical to disk. Atomic write so a partial file
    # never serves on a parallel request.
    images_dir = chat_path / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / f"{uuid}.{ext}"
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        import os as _os
        _os.replace(tmp, target)
    except OSError as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise HTTPException(500, f"Failed to cache image: {e}") from e

    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, max-age=86400",
        },
    )


@router.post("/{chat_id}/bookmarks/jump")
async def bookmark_jump(chat_id: str, bookmark_id: str) -> Chat:
    """Restore the chat's selected_child_id from a bookmark; record current
    path in history."""
    async with storage.lock(f"chat:{chat_id}"):
        chat = _ensure_chat(chat_id)
        book = storage.load_chat_bookmarks(chat_id)
        bm = next((b for b in book.bookmarks if b.id == bookmark_id), None)
        if bm is None:
            raise HTTPException(404, f"bookmark {bookmark_id!r} not found")

        msgs = storage.load_chat_messages(chat_id)
        _push_history(chat_id, _path_snippet(chat, msgs.messages),
                      _snapshot_current_path(chat, msgs.messages))

        chat.selected_child_id = dict(bm.selected_child_id)
        _lock_active_path(chat, msgs.messages)
        chat.updated_at = now_seconds()
        storage.recount_active_path(chat, msgs)
        storage.save_chat(chat, bump_version=False)
        return chat

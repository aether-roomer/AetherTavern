"""Context-preset routes.

A context preset is the macro-driven prompt template the Generic-mode
chat-completions builder consumes. The entity carries a list of
system-prompt blocks plus a list of additional messages; each message can be
floating (depth 0..10, ST reminder-brain style). The default preset is
seeded on first boot from :func:`server.storage._build_default_context_preset`
and its id is backfilled into every ``settings.generic.*.context_preset_id``
that was previously ``None``.

The ``/preview`` endpoint owns macro expansion so the live editor can render
the system prompt + additional-messages output against either user-supplied
dummies or built-in defaults that exercise every wart-case in the seeded
prompt.

The ``/macros`` endpoint introspects the macro registry plus the manually
documented control-flow tokens to power the editor's Macros help modal.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from server import storage
from server.aer.context_preset_render import (
    render_additional_messages,
    render_system_prompt,
)
from server.aer.macros import MacroCtx, _REGISTRY
from server.conflicts import check_version
from server.duplication import copy_entity_files, next_copy_name
from server.imaging import build_display_file
from server.list_models import ContextPresetSummary, FavoriteUpdate
from server.models import (
    Chat,
    Contact,
    ContextPreset,
    Intimacy,
    ResponseLength,
    Scenario,
    Style,
    User,
    new_id,
    now_seconds,
)


router = APIRouter(prefix="/api/context-presets", tags=["context_presets"])

# The macros help endpoint lives at ``/api/macros`` — a top-level path
# rather than nested under context-presets, because the help modal is
# reachable from anywhere in the UI that surfaces macro authoring.
macros_router = APIRouter(prefix="/api", tags=["context_presets"])


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.get("")
async def list_context_presets() -> list[ContextPresetSummary]:
    return [
        ContextPresetSummary.from_preset(p)
        for p in storage.list_context_presets()
    ]


@router.get("/{preset_id}")
async def get_context_preset(preset_id: str) -> ContextPreset:
    p = storage.get_context_preset(preset_id)
    if p is None:
        raise HTTPException(404, f"context preset {preset_id!r} not found")
    return p


@router.post("")
async def create_context_preset(preset: ContextPreset) -> ContextPreset:
    if not preset.id or storage.get_context_preset(preset.id) is not None:
        preset = preset.model_copy(update={"id": new_id()})
    async with storage.lock(f"context_preset:{preset.id}"):
        return storage.save_context_preset(preset)


@router.put("/{preset_id}")
async def update_context_preset(
    preset_id: str, preset: ContextPreset,
) -> ContextPreset:
    if preset.id != preset_id:
        raise HTTPException(400, "preset id in body does not match URL")
    async with storage.lock(f"context_preset:{preset_id}"):
        prev = storage.get_context_preset(preset_id)
        if prev is None:
            raise HTTPException(404, f"context preset {preset_id!r} not found")
        if not check_version(preset, prev):
            return prev
        saved = storage.save_context_preset(preset)
        # Rebuild the avatar display sibling if the crop changed — mirrors
        # contact / library flows.
        pdir = storage.context_preset_dir(preset_id)
        if (
            pdir is not None and saved.avatar
            and saved.avatar_crop != prev.avatar_crop
        ):
            build_display_file(pdir / saved.avatar, saved.avatar_crop)
        return saved


@router.patch("/{preset_id}/favorite")
async def set_context_preset_favorite(
    preset_id: str, body: FavoriteUpdate,
) -> ContextPresetSummary:
    async with storage.lock(f"context_preset:{preset_id}"):
        preset = storage.get_context_preset(preset_id)
        if preset is None:
            raise HTTPException(404, f"context preset {preset_id!r} not found")
        preset.favorite = body.favorite
        storage.save_context_preset(preset, bump_version=False)
        return ContextPresetSummary.from_preset(preset)


@router.delete("/{preset_id}")
async def delete_context_preset(preset_id: str) -> dict:
    async with storage.lock(f"context_preset:{preset_id}"):
        if not storage.delete_context_preset(preset_id):
            raise HTTPException(404, f"context preset {preset_id!r} not found")
        return {"deleted": preset_id}


@router.post("/{preset_id}/duplicate")
async def duplicate_context_preset(preset_id: str) -> ContextPreset:
    src = storage.get_context_preset(preset_id)
    if src is None:
        raise HTTPException(404, f"context preset {preset_id!r} not found")
    src_dir = storage.context_preset_dir(preset_id)
    existing_names = [p.name for p in storage.list_context_presets()]
    now = now_seconds()
    new_preset = src.model_copy(update={
        "id": new_id(),
        "version_id": new_id(),
        "created_at": now,
        "updated_at": now,
        "name": next_copy_name(src.name, existing_names),
    })
    async with storage.lock(f"context_preset:{new_preset.id}"):
        if src_dir is not None:
            dst_dir = storage.CONTEXT_PRESETS_DIR / storage.entity_dir_name(
                new_preset.name, new_preset.id,
            )
            copy_entity_files(src_dir, dst_dir)
        return storage.save_context_preset(new_preset)


# ---------------------------------------------------------------------------
# Preview — server-side macro expansion against dummies
# ---------------------------------------------------------------------------


class _PreviewDummies(BaseModel):
    """Optional overrides for the synthetic Contact / User / Scenario / Chat
    used in the preview. Any field omitted gets a built-in default that
    exercises the corresponding wart-case in the seeded prompt (multi-line
    persona / appearance / scene / environment; non-empty chat.tags;
    populated chat.title / intimacy / style / response_length / cjk)."""

    model_config = ConfigDict(extra="ignore")

    # Contact
    contact_name: str | None = None
    contact_species: str | None = None
    contact_pronouns: str | None = None
    contact_persona: str | None = None
    contact_appearance: str | None = None
    contact_tags: str | None = None
    # User
    user_name: str | None = None
    user_species: str | None = None
    user_pronouns: str | None = None
    user_persona: str | None = None
    user_appearance: str | None = None
    user_tags: str | None = None
    # Scenario
    scenario_environment: str | None = None
    scenario_scene: str | None = None
    # Chat
    chat_title: str | None = None
    chat_tags: str | None = None
    chat_intimacy: Intimacy | None = None
    chat_style: Style | None = None
    chat_response_length: ResponseLength | None = None
    chat_cjk: bool | None = None


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    preset: ContextPreset
    dummies: _PreviewDummies | None = None


class PreviewMessage(BaseModel):
    # ``id`` mirrors the source preset's ``ContextPresetAdditionalMessage.id``
    # so the editor can match this rendered output back to its source card.
    id: str = ""
    role: Literal["system", "user", "assistant"]
    content: str
    float_enabled: bool
    float_depth: int


class PreviewResponse(BaseModel):
    system_prompt: str
    additional_messages: list[PreviewMessage]


_DEFAULT_DUMMIES = _PreviewDummies(
    contact_name="Alice",
    contact_species="Human",
    contact_pronouns="she/her",
    contact_persona="Curious and playful.\nDeeply loyal to her friends.",
    contact_appearance="Tall, with silver hair and bright green eyes.\nUsually in a long dark coat.",
    contact_tags="adventurous, sharp-witted",
    user_name="Bob",
    user_species="Human",
    user_pronouns="he/him",
    user_persona="Steady and thoughtful.\nA careful planner.",
    user_appearance="Average height, brown eyes, casual style.\nFond of warm-toned clothes.",
    user_tags="reliable, observant",
    scenario_environment="A cobblestone alley winding between sandstone buildings.\nLanterns line the walls.",
    scenario_scene="Late evening. Alice and Bob are walking back from the market.\nA storm is gathering.",
    chat_title="Sandstone evening",
    chat_tags="slice-of-life, adventure",
    chat_intimacy=Intimacy.CLOSE,
    chat_style=Style.ROLEPLAY,
    chat_response_length=ResponseLength.MEDIUM,
    chat_cjk=False,
)


def _merged_dummies(supplied: _PreviewDummies | None) -> _PreviewDummies:
    if supplied is None:
        return _DEFAULT_DUMMIES
    out: dict[str, Any] = _DEFAULT_DUMMIES.model_dump()
    for k, v in supplied.model_dump(exclude_none=True).items():
        out[k] = v
    return _PreviewDummies(**out)


def _build_preview_ctx(d: _PreviewDummies) -> MacroCtx:
    contact = Contact(
        name=d.contact_name or "",
        species=d.contact_species or "",
        pronouns=d.contact_pronouns or "",
        persona=d.contact_persona or "",
        appearance=d.contact_appearance or "",
        tags=d.contact_tags or "",
    )
    user = User(
        name=d.user_name or "",
        species=d.user_species or "",
        pronouns=d.user_pronouns or "",
        persona=d.user_persona or "",
        appearance=d.user_appearance or "",
        tags=d.user_tags or "",
    )
    scenario = Scenario(
        name="Preview Scenario",
        environment=d.scenario_environment or "",
        scene=d.scenario_scene or "",
    )
    chat = Chat(
        contact_id=contact.id, user_id=user.id,
        title=d.chat_title or "",
        tags=d.chat_tags or "",
        intimacy=d.chat_intimacy or Intimacy.STRANGER,
        style=d.chat_style or Style.CHAT,
        response_length=d.chat_response_length,
        cjk=bool(d.chat_cjk),
    )
    return MacroCtx(
        contact=contact,
        user=user,
        scenario=scenario,
        chat=chat,
        # Live libraries on disk so the author sees what {{global_brains}}
        # would render against the user's real brain content.
        libraries=storage.list_brain_libraries(),
    )


@router.post("/preview")
async def preview_context_preset(body: PreviewRequest) -> PreviewResponse:
    dummies = _merged_dummies(body.dummies)
    ctx = _build_preview_ctx(dummies)
    system_prompt = render_system_prompt(body.preset, ctx)
    messages = render_additional_messages(body.preset, ctx)
    return PreviewResponse(
        system_prompt=system_prompt,
        additional_messages=[PreviewMessage(**m) for m in messages],
    )


# ---------------------------------------------------------------------------
# Macros help endpoint
# ---------------------------------------------------------------------------


class MacroDoc(BaseModel):
    name: str
    description: str = ""
    example: str = ""
    category: str = "other"


# Hand-written descriptions for the registered macros. Keep them one-line
# and concrete — they appear in a help modal, not a reference manual.
_MACRO_DOCS: dict[str, dict[str, str]] = {
    # Identity
    "char": {"category": "identity", "description": "Contact's name.", "example": "{{char}}"},
    "user": {"category": "identity", "description": "User persona's name.", "example": "{{user}}"},
    "persona": {"category": "identity", "description": "User persona's bio.", "example": "{{persona}}"},
    "description": {"category": "identity", "description": "Contact's persona/bio.", "example": "{{description}}"},
    "personality": {"category": "identity", "description": "Contact's appearance.", "example": "{{personality}}"},
    "scenario": {"category": "identity", "description": "Active scenario scene.", "example": "{{scenario}}"},
    "charfirstmessage": {"category": "identity", "description": "Contact's greeting (or active scenario's greeting).", "example": "{{charfirstmessage}}"},
    "charcreatornotes": {"category": "identity", "description": "Contact's description (creator notes).", "example": "{{charcreatornotes}}"},
    "charversion": {"category": "identity", "description": "Always empty in AER.", "example": "{{charversion}}"},
    "chardepthprompt": {"category": "identity", "description": "Contact's reminder-brain content.", "example": "{{chardepthprompt}}"},
    "mesexamples": {"category": "identity", "description": "Example chats flattened to ``Sender: text``.", "example": "{{mesexamples}}"},
    "group": {"category": "identity", "description": "Contact name (group-mode alias).", "example": "{{group}}"},
    "notchar": {"category": "identity", "description": "Always empty (SillyTavern compat).", "example": "{{notchar}}"},
    # Contact / user / scenario fields
    "contact.name": {"category": "fields", "description": "Contact's name.", "example": "{{contact.name}}"},
    "contact.species": {"category": "fields", "description": "Contact's species.", "example": "{{contact.species}}"},
    "contact.gender": {"category": "fields", "description": "Contact's gender.", "example": "{{contact.gender}}"},
    "contact.pronouns": {"category": "fields", "description": "Contact's pronouns string.", "example": "{{contact.pronouns}}"},
    "contact.pronouns.subject": {"category": "fields", "description": "Subject pronoun (e.g. ``she``).", "example": "{{contact.pronouns.subject}}"},
    "contact.pronouns.object": {"category": "fields", "description": "Object pronoun (e.g. ``her``).", "example": "{{contact.pronouns.object}}"},
    "contact.persona": {"category": "fields", "description": "Contact's persona text.", "example": "{{contact.persona}}"},
    "contact.appearance": {"category": "fields", "description": "Contact's appearance text.", "example": "{{contact.appearance}}"},
    "contact.description": {"category": "fields", "description": "Contact's description (creator notes).", "example": "{{contact.description}}"},
    "contact.greeting": {"category": "fields", "description": "Contact's greeting.", "example": "{{contact.greeting}}"},
    "contact.tags": {"category": "fields", "description": "Contact's tag list.", "example": "{{contact.tags}}"},
    "user.name": {"category": "fields", "description": "User persona's name.", "example": "{{user.name}}"},
    "user.species": {"category": "fields", "description": "User persona's species.", "example": "{{user.species}}"},
    "user.gender": {"category": "fields", "description": "User persona's gender.", "example": "{{user.gender}}"},
    "user.pronouns": {"category": "fields", "description": "User persona's pronouns.", "example": "{{user.pronouns}}"},
    "user.pronouns.subject": {"category": "fields", "description": "User persona's subject pronoun.", "example": "{{user.pronouns.subject}}"},
    "user.pronouns.object": {"category": "fields", "description": "User persona's object pronoun.", "example": "{{user.pronouns.object}}"},
    "user.persona": {"category": "fields", "description": "User persona's bio.", "example": "{{user.persona}}"},
    "user.appearance": {"category": "fields", "description": "User persona's appearance.", "example": "{{user.appearance}}"},
    "user.description": {"category": "fields", "description": "User persona's description.", "example": "{{user.description}}"},
    "user.tags": {"category": "fields", "description": "User persona's tags.", "example": "{{user.tags}}"},
    "scenario.name": {"category": "fields", "description": "Active scenario's name.", "example": "{{scenario.name}}"},
    "scenario.environment": {"category": "fields", "description": "Active scenario's environment.", "example": "{{scenario.environment}}"},
    "scenario.scene": {"category": "fields", "description": "Active scenario's scene.", "example": "{{scenario.scene}}"},
    "scenario.description": {"category": "fields", "description": "Active scenario's description.", "example": "{{scenario.description}}"},
    "scenario.tags": {"category": "fields", "description": "Active scenario's tags.", "example": "{{scenario.tags}}"},
    "scenario.greeting": {"category": "fields", "description": "Active scenario's greeting.", "example": "{{scenario.greeting}}"},
    # Chat
    "chat.title": {"category": "chat", "description": "Chat title.", "example": "{{chat.title}}"},
    "chat.tags": {"category": "chat", "description": "Chat-level tags.", "example": "{{chat.tags}}"},
    "chat.intimacy": {"category": "chat", "description": "Chat intimacy (stranger / acquaintance / close / romantic).", "example": "{{chat.intimacy}}"},
    "chat.style": {"category": "chat", "description": "Chat style (chat / roleplay / novel).", "example": "{{chat.style}}"},
    "chat.response_length": {"category": "chat", "description": "Chat response-length hint.", "example": "{{chat.response_length}}"},
    "chat.cjk": {"category": "chat", "description": "``\"true\"`` when CJK mode is on, ``\"false\"`` otherwise.", "example": "{{chat.cjk}}"},
    # Time
    "date": {"category": "time", "description": "ISO date (YYYY-MM-DD).", "example": "{{date}}"},
    "isodate": {"category": "time", "description": "Alias for ``{{date}}``.", "example": "{{isodate}}"},
    "time": {"category": "time", "description": "12-hour wall-clock time with AM/PM.", "example": "{{time}}"},
    "time24": {"category": "time", "description": "24-hour wall-clock time.", "example": "{{time24}}"},
    "isotime": {"category": "time", "description": "Alias for ``{{time24}}``.", "example": "{{isotime}}"},
    "weekday": {"category": "time", "description": "Full weekday name (Monday).", "example": "{{weekday}}"},
    "datetimeformat": {"category": "time", "description": "moment.js-style format string. ``HH`` zero-padded, ``H`` unpadded.", "example": "{{datetimeformat::HH:mm}}"},
    # Random
    "random": {"category": "random", "description": "Pick one arg uniformly at random.", "example": "{{random::a::b::c}}"},
    "pick": {"category": "random", "description": "Pick one arg deterministically per chat (rerolled by the user).", "example": "{{pick::a::b::c}}"},
    "roll": {"category": "random", "description": "Roll dice (NdM±K).", "example": "{{roll::2d6+1}}"},
    # Chat history
    "lastmessage": {"category": "history", "description": "Body of the last message in the active path.", "example": "{{lastmessage}}"},
    "lastusermessage": {"category": "history", "description": "Body of the last user message.", "example": "{{lastusermessage}}"},
    "lastcharmessage": {"category": "history", "description": "Body of the last contact message.", "example": "{{lastcharmessage}}"},
    "lastmessageid": {"category": "history", "description": "Index of the last message in the active path.", "example": "{{lastmessageid}}"},
    "allchatrange": {"category": "history", "description": "``0-N`` range for the active path.", "example": "{{allchatrange}}"},
    "firstincludedmessageid": {"category": "history", "description": "Index of the first message above the rollover cursor.", "example": "{{firstincludedmessageid}}"},
    "idleduration": {"category": "history", "description": "Time since the last user message (human-readable).", "example": "{{idleduration}}"},
    "lastswipeid": {"category": "history", "description": "Number of swipes at the tip.", "example": "{{lastswipeid}}"},
    "currentswipeid": {"category": "history", "description": "Currently selected swipe at the tip (1-based).", "example": "{{currentswipeid}}"},
    # Runtime
    "model": {"category": "runtime", "description": "AER default model name.", "example": "{{model}}"},
    "maxcontext": {"category": "runtime", "description": "Total context budget in tokens.", "example": "{{maxcontext}}"},
    "maxprompt": {"category": "runtime", "description": "Prompt budget in tokens.", "example": "{{maxprompt}}"},
    "maxresponse": {"category": "runtime", "description": "Response budget in tokens.", "example": "{{maxresponse}}"},
    "ismobile": {"category": "runtime", "description": "``\"true\"``/``\"false\"`` for mobile UI.", "example": "{{ismobile}}"},
    "sanitize_html": {"category": "runtime", "description": "``\"true\"`` when Generic HTML sanitization is on (default), ``\"false\"`` when off (raw HTML/JS reaches the bubble).", "example": "{{#if !sanitize_html}}...{{/if}}"},
    "lastgenerationtype": {"category": "runtime", "description": "``regenerate``/``edit``/``swipe``/...", "example": "{{lastgenerationtype}}"},
    # Format / utility
    "space": {"category": "format", "description": "N spaces.", "example": "{{space::4}}"},
    "newline": {"category": "format", "description": "N newlines.", "example": "{{newline::2}}"},
    "noop": {"category": "format", "description": "Empty string (useful for line-noise hiding).", "example": "{{noop}}"},
    "reverse": {"category": "format", "description": "Reverse the joined arg string.", "example": "{{reverse::hello}}"},
    # Brains
    "global_brains": {"category": "brains", "description": "Render every unconditional global brain (contact + user + scenario + libraries).", "example": "{{global_brains}}"},
    # Control flow
    "if": {"category": "control_flow", "description": "Inline ternary form. Scoped form is ``{{if X}}...{{else}}...{{/if}}``.", "example": "{{if contact.persona::has::missing}}"},
    "eq": {"category": "comparison", "description": "``\"true\"`` if both args match (case-sensitive).", "example": "{{eq::a::a}}"},
    "neq": {"category": "comparison", "description": "Inverse of ``{{eq}}``.", "example": "{{neq::a::b}}"},
    "lt": {"category": "comparison", "description": "``\"true\"`` if args are numeric and ``a < b``.", "example": "{{lt::1::2}}"},
    "gt": {"category": "comparison", "description": "``\"true\"`` if args are numeric and ``a > b``.", "example": "{{gt::2::1}}"},
    "lte": {"category": "comparison", "description": "``a <= b``.", "example": "{{lte::1::1}}"},
    "gte": {"category": "comparison", "description": "``a >= b``.", "example": "{{gte::2::1}}"},
    "and": {"category": "boolean", "description": "``\"true\"`` if every arg is truthy.", "example": "{{and::a::b}}"},
    "or": {"category": "boolean", "description": "``\"true\"`` if any arg is truthy.", "example": "{{or::::a}}"},
}


# Synthetic non-registry tokens — documented manually so the help modal
# surfaces the scoped-if syntax. The pre-pass resolves them before macro
# substitution; they have no entry in _REGISTRY.
_VIRTUAL_MACROS: list[MacroDoc] = [
    MacroDoc(
        name="if (scoped)",
        category="control_flow",
        description="Scoped form. Body is trimmed + dedented unless prefixed with ``#``.",
        example="{{if contact.persona}}has{{else}}missing{{/if}}",
    ),
    MacroDoc(
        name="#if",
        category="control_flow",
        description="Preserve-whitespace flag. Body's leading/trailing whitespace + indentation survives.",
        example="{{#if contact.persona}}\\nPersona:\\n{{contact.persona}}{{/if}}",
    ),
    MacroDoc(
        name="else",
        category="control_flow",
        description="Splits a scoped ``{{if}}`` at depth 0.",
        example="{{if X}}then{{else}}otherwise{{/if}}",
    ),
    MacroDoc(
        name="/if",
        category="control_flow",
        description="Closes a scoped ``{{if}}``.",
        example="{{if X}}body{{/if}}",
    ),
]


@macros_router.get("/macros")
async def list_macros() -> list[MacroDoc]:
    """Introspect the macro registry + virtual control-flow tokens for the
    editor's Macros help modal. Sorted alphabetically; the frontend groups
    by category."""
    out: list[MacroDoc] = []
    for name in sorted(_REGISTRY.keys()):
        info = _MACRO_DOCS.get(name, {})
        out.append(MacroDoc(
            name=name,
            description=info.get("description", ""),
            example=info.get("example", ""),
            category=info.get("category", "other"),
        ))
    out.extend(_VIRTUAL_MACROS)
    return out

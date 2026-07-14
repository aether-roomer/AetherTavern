"""Summary projections for the list endpoints.

Defined as explicit Pydantic models (NOT subclasses of the entity models)
so adding a heavy field to ``Contact`` / ``Chat`` / etc. doesn't silently
leak it into the list-endpoint response.

Frontend list rows consume only these fields plus a handful of derived
labels (e.g. contact name on a chat row) that come from joining against
``state.contacts`` etc. The editor for a specific entity fetches the
full object via ``GET /api/{kind}/{id}`` on row click — Summaries never
reach the editor.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from server.models import (
    BrainLibrary,
    Chat,
    Contact,
    ContactScenario,
    ContextPreset,
    CropRect,
    Intimacy,
    ResponseLength,
    Scenario,
    Style,
    User,
)


class _SummaryBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class FavoriteUpdate(BaseModel):
    """Body for the PATCH ``/favorite`` endpoints. Sidesteps a
    full-entity GET-then-PUT round-trip so the list-row star toggle
    works against Summary payloads (which omit editor-only fields).
    The PATCH endpoint bumps ``updated_at`` but NOT ``version_id`` so
    toggling a favourite in the list doesn't 409 an open editor draft."""

    model_config = ConfigDict(extra="ignore")
    favorite: bool


class ChatSummary(_SummaryBase):
    """Per-row chat payload for ``/api/chats``.

    Includes the lightweight config fields (intimacy / style / preset_id /
    cjk / response_length / brain_library_ids) because the chat view,
    chat info modal, new-chat wizard, and the inline chat-config bar all
    read them. Excluded fields are the chat-tree state — ``selected_child_id``,
    ``last_deleted_child``, the rollover cache, ``pick_reroll_nonce`` —
    which can be tens of KB on a long-branched chat and are only used
    inside the chat view itself (which fetches the full Chat via
    ``GET /api/chats/{id}`` on entry)."""

    id: str
    title: str
    tags: str
    favorite: bool
    contact_id: str
    user_id: str
    scenario_id: str | None
    contact_scenario_id: str | None
    brain_library_ids: list[str]
    intimacy: Intimacy
    style: Style
    response_length: ResponseLength | None
    cjk: bool
    preset_id: str | None
    # Per-chat generation overrides — surfaced so the chat-config bar's
    # "Model" override indicator is correct from first paint (before the
    # chat view fetches the full Chat).
    provider_override: str | None
    model_overrides: dict[str, str]
    context_preset_override: str | None
    message_count: int
    updated_at: float
    created_at: float
    # Bumped on every entity-level save; the conflict-modal flow at the
    # PUT path compares incoming vs stored to detect concurrent edits.
    version_id: str

    @classmethod
    def from_chat(cls, c: Chat) -> "ChatSummary":
        return cls(
            id=c.id,
            title=c.title,
            tags=c.tags,
            favorite=c.favorite,
            contact_id=c.contact_id,
            user_id=c.user_id,
            scenario_id=c.scenario_id,
            contact_scenario_id=c.contact_scenario_id,
            brain_library_ids=list(c.brain_library_ids),
            intimacy=c.intimacy,
            style=c.style,
            response_length=c.response_length,
            cjk=c.cjk,
            preset_id=c.preset_id,
            provider_override=c.provider_override,
            model_overrides=dict(c.model_overrides),
            context_preset_override=c.context_preset_override,
            message_count=c.message_count,
            updated_at=c.updated_at,
            created_at=c.created_at,
            version_id=c.version_id,
        )


class ContactSummary(_SummaryBase):
    """Per-row contact payload for ``/api/contacts``.

    Includes ``scenarios`` and ``default_scenario_id`` because the
    new-chat wizard, the chat-info modal, and the chat-config bar all
    enumerate the contact's character-scenarios at open time without
    a separate per-contact fetch. The heavy fields the editor needs
    (``persona`` / ``appearance`` / ``brains`` / ``example_chats`` /
    ``reminder_brain`` / ``tts``) stay off the wire here; the detail
    view fetches them via ``GET /api/contacts/{id}`` on row click."""

    id: str
    name: str
    description: str
    tags: str
    favorite: bool
    avatar: str | None
    avatar_crop: CropRect | None
    # Used by the chat-bubble emotion sprite lookup on chat rows.
    emotions: dict[str, str]
    cjk: bool
    default_intimacy: Intimacy
    default_style: Style
    default_response_length: ResponseLength | None
    scenarios: list[ContactScenario]
    default_scenario_id: str | None
    updated_at: float
    created_at: float
    last_used_at: float | None
    # Surfaced so the zip-import picker can flag "already imported".
    aer_source_id: str | None

    @classmethod
    def from_contact(cls, c: Contact, *, last_used_at: float | None) -> "ContactSummary":
        return cls(
            id=c.id,
            name=c.name,
            description=c.description,
            tags=c.tags,
            favorite=c.favorite,
            avatar=c.avatar,
            avatar_crop=c.avatar_crop,
            emotions=c.emotions,
            cjk=c.cjk,
            default_intimacy=c.default_intimacy,
            default_style=c.default_style,
            default_response_length=c.default_response_length,
            scenarios=c.scenarios,
            default_scenario_id=c.default_scenario_id,
            updated_at=c.updated_at,
            created_at=c.created_at,
            last_used_at=last_used_at,
            aer_source_id=c.aer_source_id,
        )


class UserSummary(_SummaryBase):
    id: str
    name: str
    description: str
    tags: str
    favorite: bool
    avatar: str | None
    avatar_crop: CropRect | None
    cjk: bool
    updated_at: float
    created_at: float
    last_used_at: float | None

    @classmethod
    def from_user(cls, u: User, *, last_used_at: float | None) -> "UserSummary":
        return cls(
            id=u.id,
            name=u.name,
            description=u.description,
            tags=u.tags,
            favorite=u.favorite,
            avatar=u.avatar,
            avatar_crop=u.avatar_crop,
            cjk=u.cjk,
            updated_at=u.updated_at,
            created_at=u.created_at,
            last_used_at=last_used_at,
        )


class ScenarioSummary(_SummaryBase):
    id: str
    name: str
    description: str
    tags: str
    favorite: bool
    background_image: str | None
    avatar_crop: CropRect | None
    # Frontend reads these to position the sidebar background sprite.
    background_focal: tuple[float, float] | None
    background_mode: Literal["cover", "tile"]
    cjk: bool
    updated_at: float
    created_at: float
    last_used_at: float | None

    @classmethod
    def from_scenario(cls, s: Scenario, *, last_used_at: float | None) -> "ScenarioSummary":
        return cls(
            id=s.id,
            name=s.name,
            description=s.description,
            tags=s.tags,
            favorite=s.favorite,
            background_image=s.background_image,
            avatar_crop=s.avatar_crop,
            background_focal=s.background_focal,
            background_mode=s.background_mode,
            cjk=s.cjk,
            updated_at=s.updated_at,
            created_at=s.created_at,
            last_used_at=last_used_at,
        )


class ChatListPage(_SummaryBase):
    """Paged response for ``GET /api/chats`` with any query param set.

    The legacy unparameterised endpoint still returns ``list[ChatSummary]``
    for backwards compat — the page envelope only kicks in once a filter,
    search, or pagination param is present."""

    items: list[ChatSummary]
    total: int
    offset: int
    limit: int


class BrainLibrarySummary(_SummaryBase):
    id: str
    name: str
    description: str
    tags: str
    favorite: bool
    avatar: str | None
    avatar_crop: CropRect | None
    brain_count: int
    updated_at: float
    created_at: float
    last_used_at: float | None

    @classmethod
    def from_library(
        cls, lib: BrainLibrary, *, last_used_at: float | None
    ) -> "BrainLibrarySummary":
        return cls(
            id=lib.id,
            name=lib.name,
            description=lib.description,
            tags=lib.tags,
            favorite=lib.favorite,
            avatar=lib.avatar,
            avatar_crop=lib.avatar_crop,
            brain_count=len(lib.brains),
            updated_at=lib.updated_at,
            created_at=lib.created_at,
            last_used_at=last_used_at,
        )


class ContextPresetSummary(_SummaryBase):
    """Per-row context-preset payload for ``/api/context-presets``.

    Drops ``system_prompt_blocks`` / ``additional_messages`` (the editor
    fetches them via the per-id GET) and surfaces ``block_count`` /
    ``message_count`` so the list row can show counts without a heavy
    payload."""

    id: str
    name: str
    description: str
    author: str
    favorite: bool
    avatar: str | None
    avatar_crop: CropRect | None
    card_image: str | None
    block_count: int
    message_count: int
    updated_at: float
    created_at: float
    version_id: str

    @classmethod
    def from_preset(cls, p: ContextPreset) -> "ContextPresetSummary":
        return cls(
            id=p.id,
            name=p.name,
            description=p.description,
            author=p.author,
            favorite=p.favorite,
            avatar=p.avatar,
            avatar_crop=p.avatar_crop,
            card_image=p.card_image,
            block_count=len(p.system_prompt_blocks),
            message_count=len(p.additional_messages),
            updated_at=p.updated_at,
            created_at=p.created_at,
            version_id=p.version_id,
        )

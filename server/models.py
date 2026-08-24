"""Pydantic domain models for AetherRoom."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums & primitives
# ---------------------------------------------------------------------------


class Emotion(str, Enum):
    ANGRY = "angry"
    AROUSED = "aroused"
    BORED = "bored"
    CONFUSED = "confused"
    DETERMINED = "determined"
    DISGUSTED = "disgusted"
    EMBARRASSED = "embarrassed"
    EXCITED = "excited"
    HAPPY = "happy"
    HURT = "hurt"
    IRRITATED = "irritated"
    LAUGHING = "laughing"
    LOVE = "love"
    NERVOUS = "nervous"
    NEUTRAL = "neutral"
    PLAYFUL = "playful"
    SAD = "sad"
    SCARED = "scared"
    SHY = "shy"
    SMUG = "smug"
    SURPRISED = "surprised"
    THINKING = "thinking"
    TIRED = "tired"
    WORRIED = "worried"


EMOTIONS: tuple[Emotion, ...] = tuple(Emotion)
EMOTION_VALUES: frozenset[str] = frozenset(e.value for e in Emotion)


class Intimacy(str, Enum):
    STRANGER = "stranger"
    ACQUAINTANCE = "acquaintance"
    CLOSE = "close"
    ROMANTIC = "romantic"


class Style(str, Enum):
    CHAT = "chat"
    ROLEPLAY = "roleplay"
    NOVEL = "roleplay, novel style"


class ResponseLength(str, Enum):
    SHORT = "short"
    MEDIUM = "medium"
    LONG = "long"
    VERY_LONG = "very long"
    PARA = "para"
    RAPID_SPAM = "rapid spam"
    SPAM = "spam"
    SPAMMY_SHORT = "spammy short"
    SPAMMY_MEDIUM = "spammy medium"
    SPAMMY_LONG = "spammy long"
    SPAMMY_VERY_LONG = "spammy very long"
    SPAMMY_PARA = "spammy para"


Sender = Literal["contact", "user"]
EMPTY_SENTINEL = "__empty__"
ROOT_PARENT_KEY = ""  # selected_child_id key for root-level messages

# Per-call generation flag. Mirrors the canonical SillyTavern strings so
# {{lastgenerationtype}} macros stay portable across imports.
GenerationMode = Literal["normal", "swipe", "continue", "impersonate"]

ThemeId = Literal[
    "dark",
    "oled",
    "light",
    "noir",
    "pastel",
    "forest",
    "terminal",
    "nebula",
    "sepia",
    "solarized",
]
TOGGLEABLE_THEMES: frozenset[str] = frozenset({"dark", "light"})


def now_seconds() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Building blocks shared by entities
# ---------------------------------------------------------------------------


class BrainKey(BaseModel):
    """One activation pattern on a Brain.

    A non-empty pattern matches when ``pattern`` is found within the last
    ``search_range`` characters AND the last ``search_messages`` messages
    of the current search text — both windows narrow the search; ``None``
    means unbounded on that axis. For ``is_regex`` keys, ``case_sensitive``
    and ``match_whole_words`` are ignored — the regex carries its own
    flags / ``\\b`` boundaries if the author wants them.
    """

    pattern: str = ""
    is_regex: bool = False
    case_sensitive: bool = False
    match_whole_words: bool = False
    search_range: int | None = None     # characters; None = whole search text
    search_messages: int | None = None  # messages from end; None = whole search text


# Activation conditions (advanced logic-tree builder)
# ---------------------------------------------------
#
# Each leaf condition returns a bool. ``and`` / ``or`` / ``not`` compose them.
# ``BrainCondition`` is a discriminated union; recursive types (``and`` / ``or``
# / ``not``) hold forward refs that get resolved after the union itself is
# defined, via ``model_rebuild`` calls below.


class _CondBase(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CondTrue(_CondBase):
    type: Literal["true"] = "true"


class CondKeyword(_CondBase):
    type: Literal["keyword"] = "keyword"
    keys: list[BrainKey] = Field(default_factory=list)


class CondBrainActive(_CondBase):
    type: Literal["brain_active"] = "brain_active"
    brain_id: str = ""


class CondRelationship(_CondBase):
    type: Literal["relationship"] = "relationship"
    relationship: Intimacy = Intimacy.STRANGER


class CondStyle(_CondBase):
    type: Literal["style"] = "style"
    style: Style = Style.CHAT


class CondLength(_CondBase):
    type: Literal["length"] = "length"
    length: ResponseLength = ResponseLength.MEDIUM


class CondJapanese(_CondBase):
    type: Literal["japanese"] = "japanese"
    expected: bool = True


class CondRandomChance(_CondBase):
    type: Literal["random_chance"] = "random_chance"
    percent: float = Field(default=50.0, ge=0.0, le=100.0)


NumericVariable = Literal["message_count", "user_message_count", "contact_message_count"]
StringVariable = Literal["story_text", "memory_text", "authors_note_text", "tags"]


class NumericValue(BaseModel):
    """Either a literal number or a named variable resolved from chat facts."""

    kind: Literal["literal", "variable"] = "literal"
    literal: float = 0.0
    variable: NumericVariable = "message_count"


class StringValue(BaseModel):
    """Either a literal string or a named variable resolved from chat facts."""

    kind: Literal["literal", "variable"] = "literal"
    literal: str = ""
    variable: StringVariable = "story_text"


class CondNumericCompare(_CondBase):
    type: Literal["numeric_compare"] = "numeric_compare"
    lhs: NumericValue = Field(default_factory=NumericValue)
    op: Literal["==", "!=", "<", ">", "<=", ">="] = "=="
    rhs: NumericValue = Field(default_factory=NumericValue)


class CondStringCompare(_CondBase):
    type: Literal["string_compare"] = "string_compare"
    lhs: StringValue = Field(default_factory=StringValue)
    op: Literal["equals", "includes", "starts_with", "ends_with"] = "includes"
    rhs: StringValue = Field(default_factory=StringValue)
    case_sensitive: bool = False


class CondAnd(_CondBase):
    type: Literal["and"] = "and"
    children: list["BrainCondition"] = Field(default_factory=list)


class CondOr(_CondBase):
    type: Literal["or"] = "or"
    children: list["BrainCondition"] = Field(default_factory=list)


class CondNot(_CondBase):
    type: Literal["not"] = "not"
    child: "BrainCondition | None" = None


BrainCondition = Annotated[
    Union[
        CondTrue,
        CondKeyword,
        CondBrainActive,
        CondRelationship,
        CondStyle,
        CondLength,
        CondJapanese,
        CondRandomChance,
        CondNumericCompare,
        CondStringCompare,
        CondAnd,
        CondOr,
        CondNot,
    ],
    Field(discriminator="type"),
]


CondAnd.model_rebuild()
CondOr.model_rebuild()
CondNot.model_rebuild()


class Brain(BaseModel):
    """A persistent definition of a narrative entity (character, location, item, etc.).

    A brain is *unconditional* (always injected) when ``keys`` is empty and
    ``advanced`` is None. Otherwise it is *conditional*: the activation engine
    checks whether any key matches OR the advanced tree evaluates to true; if
    so, the brain is included in a single relocated system message ~2048 tokens
    before the end of the rendered prompt instead of at its natural position.
    See ``server/aer/activation.py``.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    name: str
    content: str
    keys: list[BrainKey] = Field(default_factory=list)
    # When this brain activates, append its content to the search text used
    # to evaluate the *remaining* candidates.
    cascades: bool = False
    # When set, this brain can only activate against the original (pass-0)
    # search text — never via a cascade-augmented one.
    blocks_recursion: bool = False
    advanced: BrainCondition | None = None
    # Soft-disable. Skipped in unconditional shipping and conditional
    # activation alike; preserved on the entity so the user can re-enable.
    disabled: bool = False


class ReminderBrain(BaseModel):
    """A contact-only "tail instruction" injected just above the AER style
    marker at a configurable depth from the end of history.

    Distinct from :class:`Brain` because it carries no activation machinery
    — no keys, no advanced condition, no cascade/recursion flags. It's
    always-on (unless ``disabled``), and its only positional control is
    ``depth``: how many messages from the end of the prompt the system
    message lands (``0`` = directly above the style marker).
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    name: str = ""
    content: str = ""
    depth: int = Field(default=0, ge=0, le=10)
    disabled: bool = False


class CropRect(BaseModel):
    """Square crop rectangle, normalised to ``[0, 1]`` against the image's natural size.

    Used so that contacts whose avatar / emotion images are full-body or
    landscape-format can specify which square portion to display in the
    chat bubbles. ``w`` should equal ``h`` for square crops.
    """

    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0


class SubMessage(BaseModel):
    """One bubble within a chat message."""

    text: str
    # AER-origin bubbles always carry a non-null emotion (NEUTRAL by default);
    # Generic-origin bubbles default to None ("no emotion set") and the user
    # can optionally pick one in the editor. The Generic context builder maps
    # None -> NEUTRAL when an AER mode prompt consumes the same chat history.
    emotion: Emotion | None = Emotion.NEUTRAL


class ExampleMessage(BaseModel):
    is_contact: bool
    text: str
    emotion: Emotion | None = None  # only meaningful for contact messages


class ExampleChat(BaseModel):
    name: str = ""
    style: Style = Style.CHAT
    user_name: str = ""
    messages: list[ExampleMessage] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Text-to-speech configuration
# ---------------------------------------------------------------------------


class TTSGlobalMode(str, Enum):
    OFF = "off"
    DEFAULT_OFF = "default_off"   # active, contacts opt-in via Enabled
    DEFAULT_ON = "default_on"     # active, contacts opt-out via Disabled


class TTSProviderKind(str, Enum):
    NOVELAI = "novelai"
    OPENROUTER = "openrouter"
    NANOGPT = "nanogpt"
    GENERIC = "generic"           # one of TTSSettings.custom_apis[i].id


class NovelAIVoiceVersion(str, Enum):
    V1 = "v1"
    V2 = "v2"


# Sentinel for "(custom)" voice picker option. When voice == this string,
# the proxy sends voice=-1 + the entity's custom_seed string to NovelAI.
NAI_CUSTOM_SENTINEL = "__custom__"


class EntityTTSMode(str, Enum):
    DEFAULT = "default"           # follow global mode
    ENABLED = "enabled"
    DISABLED = "disabled"


class EntityTTSOverride(BaseModel):
    """Per-entity TTS override picked when ``use_custom=True``.

    Holds no API key — keys live only on global Settings. Fields
    irrelevant to the selected ``kind`` are preserved (so toggling
    kinds doesn't lose the user's other-provider drafts) but ignored
    at request build time.
    """

    model_config = ConfigDict(extra="ignore")

    kind: TTSProviderKind = TTSProviderKind.NOVELAI
    custom_id: str = ""                       # when kind == GENERIC

    # NovelAI voice is stored per-version so V1↔V2 switches preserve
    # each version's pick. The active version selects which one is
    # shipped upstream at request time.
    novelai_version: NovelAIVoiceVersion = NovelAIVoiceVersion.V2
    novelai_voice_v1: str = "Cyllene"         # preset name or NAI_CUSTOM_SENTINEL
    novelai_voice_v2: str = "Aini"
    novelai_custom_seed_v1: str = ""          # used when voice_v1 == sentinel
    novelai_custom_seed_v2: str = ""

    openrouter_model: str = ""
    openrouter_voice: str = ""
    openrouter_speed: float = Field(default=1.0, ge=0.25, le=4.0)

    nanogpt_model: str = ""
    nanogpt_voice: str = ""
    nanogpt_speed: float = Field(default=1.0, ge=0.25, le=4.0)

    generic_model: str = ""
    generic_voice: str = ""
    generic_speed: float = Field(default=1.0, ge=0.25, le=4.0)


class EntityTTSConfig(BaseModel):
    """Per-entity TTS settings. Shared between Contact and User; the
    only difference between the two is the default factory's ``mode``
    value (Contact defaults to DEFAULT — follow global toggle; User
    defaults to DISABLED — sending a message doesn't auto-play)."""

    model_config = ConfigDict(extra="ignore")

    mode: EntityTTSMode = EntityTTSMode.DEFAULT
    use_custom: bool = False
    override: EntityTTSOverride = Field(default_factory=EntityTTSOverride)


def _user_default_tts() -> EntityTTSConfig:
    return EntityTTSConfig(mode=EntityTTSMode.DISABLED)


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------


class ContactScenario(BaseModel):
    """A character-specific scenario, nested under a Contact.

    Carries everything a global Scenario does, plus optional overrides for
    the contact-level greeting and the chat-creation defaults (style,
    intimacy, response_length). When a chat is created with a contact
    scenario selected, those override fields seed the new Chat — leaving
    them as ``None`` falls through to the contact / global defaults.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    name: str
    description: str = ""
    environment: str = ""
    scene: str = ""
    tags: str = ""
    cjk: bool = False
    greeting: str = ""
    greeting_emotion: "Emotion | None" = None
    style: "Style | None" = None
    intimacy: "Intimacy | None" = None
    response_length: "ResponseLength | None" = None
    brains: list[Brain] = Field(default_factory=list)
    # See ``Contact.aer_revision`` — same zip-import bookkeeping.
    aer_source_id: str | None = None
    aer_revision: str | None = None
    aer_imported_at: float | None = None


class Contact(BaseModel):
    """A character the user chats with."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    # Re-randomised by ``storage.save_contact`` on every write. The PUT
    # endpoint compares the incoming ``version_id`` against the stored one
    # and rejects a mismatched save with HTTP 409 — protects against blind
    # overwrites when two browser tabs / devices edit the same entity.
    version_id: str = Field(default_factory=new_id)
    created_at: float = Field(default_factory=now_seconds)
    updated_at: float = Field(default_factory=now_seconds)
    name: str
    description: str = ""
    author: str = ""
    species: str = ""
    gender: str = ""
    pronouns: str = ""
    persona: str = ""
    appearance: str = ""
    greeting: str = ""
    greeting_emotion: Emotion | None = None
    default_intimacy: Intimacy = Intimacy.STRANGER
    default_style: Style = Style.CHAT
    # ``None`` means "no length preference" — new chats start with
    # ``response_length=None`` (no length instruction in the prompt) unless
    # the chosen contact-scenario explicitly overrides.
    default_response_length: ResponseLength | None = None
    cjk: bool = False
    tags: str = ""
    avatar: str | None = None  # filename within the contact directory
    emotions: dict[str, str] = Field(default_factory=dict)  # emotion-name -> filename
    avatar_crop: CropRect | None = None
    emotions_crop: CropRect | None = None
    # Original character-card image (e.g. the PNG an ST card was imported
    # from). Sits next to ``avatar`` as a separate file; used by the
    # card-export route to bake the entity's JSON into a shareable PNG.
    card_image: str | None = None
    example_chats: list[ExampleChat] = Field(default_factory=list)
    brains: list[Brain] = Field(default_factory=list)
    # Contact-only "tail instruction" slot. ST cards' ``post_history_instructions``
    # imports into here. See :class:`ReminderBrain`.
    reminder_brain: ReminderBrain | None = None
    # Character-specific scenarios. ``default_scenario_id`` (if set) names the
    # one preselected by the new-chat wizard for this contact.
    scenarios: list[ContactScenario] = Field(default_factory=list)
    default_scenario_id: str | None = None
    # Favourite flag. Float-to-top in list panes / new-chat wizard when the
    # user enables the "favourites first" sort toggle.
    favorite: bool = False
    # AER zip-import bookkeeping. ``aer_revision`` is the last-imported
    # source signature (the source's ``revision_id`` for contacts, a
    # last-update timestamp for scenarios, ``count:tail-ts`` for chats).
    # ``aer_imported_at`` is ``updated_at`` snapshotted right after the
    # import save, so the manifest can flag entities the user has edited
    # since import (``updated_at > aer_imported_at + tolerance``). All three
    # are ``None`` for entities that came in via any other path (manual
    # creation, single-JSON import, etc.).
    aer_source_id: str | None = None
    aer_revision: str | None = None
    aer_imported_at: float | None = None
    tts: EntityTTSConfig = Field(default_factory=EntityTTSConfig)


class User(BaseModel):
    """A user persona."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    # See ``Contact.version_id`` — same optimistic-concurrency mechanism.
    version_id: str = Field(default_factory=new_id)
    created_at: float = Field(default_factory=now_seconds)
    updated_at: float = Field(default_factory=now_seconds)
    name: str
    description: str = ""
    author: str = ""
    species: str = ""
    gender: str = ""
    pronouns: str = ""
    persona: str = ""
    appearance: str = ""
    tags: str = ""
    cjk: bool = False  # default contributes to Chat.cjk via OR on chat creation
    avatar: str | None = None  # filename within the user directory
    avatar_crop: CropRect | None = None
    card_image: str | None = None  # original character-card image, if any
    brains: list[Brain] = Field(default_factory=list)
    favorite: bool = False
    # User personas default to TTS=Disabled (their messages don't auto-play
    # when sent). Power users can flip to Enabled (always voice this persona)
    # or Default (follow global toggle) per-persona.
    tts: EntityTTSConfig = Field(default_factory=_user_default_tts)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    # See ``Contact.version_id`` — same optimistic-concurrency mechanism.
    version_id: str = Field(default_factory=new_id)
    created_at: float = Field(default_factory=now_seconds)
    updated_at: float = Field(default_factory=now_seconds)
    name: str
    description: str = ""
    author: str = ""
    environment: str = ""
    scene: str = ""
    tags: str = ""
    cjk: bool = False  # default contributes to Chat.cjk via OR on chat creation
    brains: list[Brain] = Field(default_factory=list)
    favorite: bool = False
    card_image: str | None = None  # original character-card image, if any
    # Atmospheric chat-pane background. The same source image fills the
    # scenario list's avatar slot via ``avatar_crop`` (baked into a small
    # display.webp sibling) and the chat reading area in full resolution.
    background_image: str | None = None  # filename within the scenario directory
    avatar_crop: CropRect | None = None
    # Focal point in normalised (x, y) coords. Drives ``object-position`` /
    # ``background-position`` in cover mode; ignored in tile mode. ``None``
    # means "centre" at render time.
    background_focal: tuple[float, float] | None = None
    background_mode: Literal["cover", "tile"] = "cover"
    background_blur: int = Field(default=0, ge=0, le=20)         # pixels
    background_dim: int = Field(default=0, ge=0, le=100)         # % black scrim
    background_brighten: int = Field(default=0, ge=0, le=100)    # % white scrim
    background_tint_strength: int = Field(default=0, ge=0, le=100)  # % themed multiply


class BrainLibrary(BaseModel):
    """A reusable bundle of brains that a chat can attach 0..N of.

    Library brains land in the AER system prompt *after* the contact / user /
    scenario unconditionals, in the order the chat attaches them. Conditional
    brains in a library flow through the same activation engine as any other
    brain. No ``cjk`` default (CJK is a chat-level flag), no greeting, no
    deletion-response — libraries have no chat behaviour of their own.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    # See ``Contact.version_id`` — same optimistic-concurrency mechanism.
    version_id: str = Field(default_factory=new_id)
    created_at: float = Field(default_factory=now_seconds)
    updated_at: float = Field(default_factory=now_seconds)
    name: str
    description: str = ""
    author: str = ""
    tags: str = ""
    avatar: str | None = None  # filename within the library directory
    avatar_crop: CropRect | None = None
    card_image: str | None = None  # original character-card image, if any
    brains: list[Brain] = Field(default_factory=list)
    favorite: bool = False


# ---------------------------------------------------------------------------
# Context presets (Generic mode)
# ---------------------------------------------------------------------------


class ContextPresetBlock(BaseModel):
    """One block of the system-prompt list (or the blocks-mode body of an
    additional message). Content is a macro string; ``enabled`` is a parking
    flag (unconditional on/off — conditionality lives inside the macro)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    name: str = ""
    enabled: bool = True
    content: str = ""


class ContextPresetAdditionalMessage(BaseModel):
    """An additional API message appended after the system prompt.

    ``simple`` mode renders ``simple_content`` directly; ``blocks`` mode runs
    the system-prompt block pipeline against ``blocks``. ``float_enabled``
    plus ``float_depth`` (0..10) emulate AER reminder-brain depth — the
    Generic context builder slots floating messages at
    ``len(api_messages) - depth`` at insertion time.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    name: str = ""
    enabled: bool = True
    role: Literal["system", "user", "assistant"] = "system"
    mode: Literal["simple", "blocks"] = "simple"
    simple_content: str = ""
    blocks: list[ContextPresetBlock] = Field(default_factory=list)
    float_enabled: bool = False
    float_depth: int = Field(default=0, ge=0, le=10)


class ContextPreset(BaseModel):
    """Macro-driven Generic-mode prompt template.

    The system prompt is the ``\\n``-join of every enabled block whose
    expansion is non-empty after strip (un-stripped expansion is appended so
    leading ``\\n`` on a block survives). Additional messages are emitted in
    authored order with floating entries stably sorted by depth at the tail.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    version_id: str = Field(default_factory=new_id)
    created_at: float = Field(default_factory=now_seconds)
    updated_at: float = Field(default_factory=now_seconds)
    name: str
    description: str = ""
    author: str = ""
    prefix_names: bool = True
    avatar: str | None = None
    avatar_crop: CropRect | None = None
    card_image: str | None = None
    favorite: bool = False
    system_prompt_blocks: list[ContextPresetBlock] = Field(default_factory=list)
    additional_messages: list[ContextPresetAdditionalMessage] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Chat messages (tree)
# ---------------------------------------------------------------------------


class Attachment(BaseModel):
    """A file attached to a chat message — generic mode only (phase 1: images).

    Files are stored next to the chat under
    ``data/chats/{slug}-{id8}/attachments/{id}.{ext}`` and served via
    ``GET /api/files/chats/{chat_id}/attachments/{id}``. Exporters embed
    each as a base64 data URI; importers decode it back to disk.
    """
    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    mime: str
    filename: str
    byte_size: int
    # Uploads and NovelAI-generated pictures share the same durable chat
    # attachment store.  Optional provenance lets the UI distinguish them
    # without changing the long-standing attachment wire shape.
    source: Literal["upload", "generated"] = "upload"
    prompt: str | None = None
    seed: int | None = None


class ChatMessage(BaseModel):
    id: str = Field(default_factory=new_id)
    parent_id: str | None = None  # None for root-level messages
    sender: Sender
    sender_name: str
    body: list[SubMessage]
    # Per-message attachments (images, phase 1). Generic mode only —
    # forwarded as ``image_url`` multimodal content to providers that
    # support it; ignored otherwise. AER mode keeps the field on disk
    # but doesn't surface attachments in the prompt.
    attachments: list[Attachment] = Field(default_factory=list)
    brains: list[Brain] = Field(default_factory=list)
    timestamp: float = Field(default_factory=now_seconds)
    # Provenance: which brains were active when this assistant message was
    # generated. Random conditional brains would otherwise re-roll on every
    # /context-tokens fetch, making the chat-stats ``brains: N (i)``
    # indicator disagree with what was actually shipped last turn. The
    # provenance is honest — preserves owner_name etc. as they were at
    # gen time, even if entities have been renamed or deleted since.
    # Empty for user messages and for pre-feature assistant messages.
    active_brains: list[dict] = Field(default_factory=list)
    # Which generation pipeline produced this message. The renderer dispatches
    # per-message on this so a chat can hold a mix of kinds (mode switches
    # mid-chat don't repaint history). Default ``"aer"`` so existing on-disk
    # assistant messages classify correctly without migration; a one-shot
    # migration in storage.initialize backfills ``"manual"`` onto pre-existing
    # user-side messages.
    #
    # User-side messages typically carry ``"manual"`` (typed by the user),
    # but impersonate-generated user messages carry ``"aer"`` or ``"generic"``
    # like any other LLM-produced turn — and may also carry generation
    # metadata below (provider, model, …) and per-bubble emotions.
    origin: Literal["aer", "generic", "manual"] = "aer"
    # Reasoning text emitted alongside the response (Generic mode only today;
    # AER doesn't currently emit reasoning, but the field lives at message
    # level so future AER reasoning slots in without a schema change).
    reasoning: str | None = None
    # ``{remote_url: uuid}`` for HTTP(S) image refs the model emitted inside
    # markdown ``![alt](...)``. Populated at persist time; drives the
    # ``/api/chats/{id}/images/{uuid}/{filename}`` proxy. ``data:`` URLs are
    # decoded to a real file at stream time and do not appear in this dict.
    image_refs: dict[str, str] = Field(default_factory=dict)
    # Generation-metadata. All six default ``None`` so existing messages load
    # cleanly. Filled at persist time for assistant turns (both AER and
    # Generic) and for impersonate-generated user turns; stay ``None`` for
    # manually-typed user messages and for greetings (no LLM call).
    generation_started_at: float | None = None       # wall-clock seconds
    generation_duration_seconds: float | None = None  # monotonic delta
    provider: str | None = None
    model: str | None = None
    generation_preset_id: str | None = None
    context_preset_id: str | None = None  # always None for AER (no concept)


class ChatMessages(BaseModel):
    """Container persisted to messages.yaml."""

    messages: list[ChatMessage] = Field(default_factory=list)


class ImagePromptState(BaseModel):
    """Persistent, user-stepped scene-to-image prompt work for one chat."""

    model_config = ConfigDict(extra="ignore")
    # Active-path message selected as the final history item for this prompt.
    # It may have newer descendants; those are deliberately excluded.
    context_tip_id: str | None = None
    response: str = ""
    prompt: str | None = None
    complete: bool = False
    # The canvas used while reasoning. Keeping it with the workflow prevents
    # a continuation from silently changing composition halfway through.
    aspect: Literal["portrait", "landscape", "square"] | None = None
    updated_at: float = Field(default_factory=now_seconds)
    generated_message_id: str | None = None


# ---------------------------------------------------------------------------
# Chat metadata (chat.yaml)
# ---------------------------------------------------------------------------


class Chat(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    # See ``Contact.version_id`` — bumps on every ``save_chat``, including
    # the implicit saves during message tree mutations (so a metadata edit
    # in one tab notices when another tab is mid-conversation).
    version_id: str = Field(default_factory=new_id)
    title: str = ""
    tags: str = ""
    contact_id: str
    user_id: str
    scenario_id: str | None = None
    # Mutually exclusive with ``scenario_id``. Names a ``ContactScenario`` on
    # the chat's contact (looked up by ``Contact.scenarios[i].id``).
    contact_scenario_id: str | None = None
    # Global brain libraries attached to this chat, in display + prompt order.
    # Their brains land in the AER system prompt after the contact / user /
    # scenario unconditionals. DELETE of a library does NOT strip ids here —
    # stale ids surface as "missing library" markers in the chat UI so a
    # later re-import of the library reattaches automatically.
    brain_library_ids: list[str] = Field(default_factory=list)
    intimacy: Intimacy = Intimacy.STRANGER
    style: Style = Style.CHAT
    response_length: ResponseLength | None = None
    cjk: bool = False
    preset_id: str | None = None  # falls back to settings.default_preset_id

    # --- Per-chat generation overrides (all default to "inherit"). -----------
    # Unified provider selection spanning AER + every Generic sub-provider, so
    # the AER-vs-Generic mode is effectively per-chat. Generation branches on
    # the RESOLVED provider (see server/generation_target.py), NOT on
    # settings.provider_mode. Encoding:
    #   None                      -> inherit settings.provider_mode + generic.provider
    #   "aetherroom"              -> force AER
    #   "novelai"/"openrouter"/"nanogpt" -> that named Generic provider
    #   "openai_compatible:<id>"  -> a specific custom OpenAI-compatible entry
    #                                (carries the entry id so a chat can use a
    #                                 non-active entry's own token). A bare
    #                                 "openai_compatible" is invalid -> resolver
    #                                 error.
    provider_override: str | None = None
    # Per-provider model picks, keyed by the canonical provider slug
    # ("aetherroom", "novelai"/"openrouter"/"nanogpt", "openai_compatible:<id>")
    # so switching providers and back remembers each provider's model. A missing
    # key falls back to that provider's configured default model.
    model_overrides: dict[str, str] = Field(default_factory=dict)
    # Generic ContextPreset id override. None -> the resolved provider's
    # configured context_preset_id. Ignored when the chat resolves to AER
    # (AER context sizing stays the global settings.context_preset).
    context_preset_override: str | None = None

    # Tree navigation: parent_id -> chosen child_id (or EMPTY_SENTINEL for soft-deleted branches).
    # Root-level messages are keyed by ROOT_PARENT_KEY.
    selected_child_id: dict[str, str] = Field(default_factory=dict)

    # Most recently soft-deleted child per parent — used by the Undelete row
    # to restore the message the user *just* deleted, rather than picking
    # whichever sibling happens to have the latest timestamp.
    last_deleted_child: dict[str, str] = Field(default_factory=dict)

    # Rollover state — cursor + cached path so the trimmer can skip rebuilding
    # from the start of the chat each turn. Invalidated by ``rollover.py`` when
    # the active path's prefix above the cursor diverges from ``rollover_path_ids``.
    rollover_start_index: int = 0
    rollover_path_ids: list[str] = Field(default_factory=list)
    rolled_over: bool = False
    last_context_tokens: int | None = None

    pick_reroll_nonce: str = ""

    favorite: bool = False

    # Cached length of the active branch (``selected_child_id`` walk).
    # Maintained by ``storage.save_chat_messages`` and the tree-mutation
    # routes; the list endpoint reads it directly instead of re-walking
    # messages.yaml per chat. Backfilled on first boot post-upgrade via
    # ``_migrate_missing_message_count``.
    message_count: int = 0

    created_at: float = Field(default_factory=now_seconds)
    updated_at: float = Field(default_factory=now_seconds)

    # See ``Contact.aer_revision`` — same zip-import bookkeeping.
    aer_source_id: str | None = None
    aer_revision: str | None = None
    aer_imported_at: float | None = None

    # Set once when the legacy "force user-side ``origin=manual``"
    # migration runs on this chat (storage.py:_migrate_legacy_message_origin).
    # Subsequent boots skip already-migrated chats so a future tweak to
    # the migration cannot accidentally overwrite impersonate-generated
    # user-side origins.
    user_origin_migrated_at: float | None = None

    # Pending attachments the user uploaded but hasn't sent yet — the
    # chip row in the chat input bar reads from this so attachments
    # survive a chat switch / page reload / tab close. The upload /
    # delete attachment routes maintain this list under the same chat
    # lock; ``POST /messages`` consumes (binds) them onto the new
    # ChatMessage and clears them here atomically.
    pending_attachments: list[Attachment] = Field(default_factory=list)

    # Scene-prompt reasoning is kept outside the message tree: it is visible
    # in the image workflow, survives reloads, and never contaminates normal
    # roleplay context. Every continuation remains a separate user action.
    image_prompt_state: ImagePromptState | None = None


# ---------------------------------------------------------------------------
# Bookmarks
# ---------------------------------------------------------------------------


class BookmarkHistoryEntry(BaseModel):
    snippet: str
    selected_child_id: dict[str, str]
    time: str  # "HH:MM"


class Bookmark(BaseModel):
    id: str = Field(default_factory=new_id)
    title: str = ""
    snippet: str
    selected_child_id: dict[str, str]
    favorite: bool = False
    created_at: float = Field(default_factory=now_seconds)


class ChatBookmarks(BaseModel):
    bookmarks: list[Bookmark] = Field(default_factory=list)
    history: list[BookmarkHistoryEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Settings & generation presets
# ---------------------------------------------------------------------------


class Preset(BaseModel):
    """A named generation parameter preset.

    Presets describe *sampling* behaviour for AER mode. In Generic mode
    they also carry the context-window sizing knobs below; AER mode
    ignores those and reads ``Settings.context_preset`` instead.
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    name: str = "Default"
    temperature: float = 0.85
    top_p: float = 0.95
    top_k: int = 250
    min_p: float = 0.0
    # Generic-mode sizing knobs. AER mode ignores these entirely —
    # context window comes from ``Settings.context_preset`` and the
    # output budget is a hardcoded constant in ``server.aer.rollover``.
    max_context_tokens: int = 28672            # 28 * 1024 — generic NAI's limit
    rollover_window_tokens: int = 8192
    max_new_tokens: int = 1536
    # Generic-mode-only repetition penalties. Defaults of 0.0 are no-op
    # at the upstream — most providers accept them as optional fields
    # and skip when they're absent / zero. AER's completions client
    # doesn't surface these; the AER editor hides the inputs.
    presence_penalty: float = 0.0
    frequency_penalty: float = 0.0


class PresetLibrary(BaseModel):
    presets: list[Preset] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Global TTS configuration (held inside Settings)
# ---------------------------------------------------------------------------


class TTSNovelAIConfig(BaseModel):
    """Voice selection is per-version so switching V1↔V2 doesn't lose
    the picked voice. Only the active version's pick is sent upstream
    at request time."""

    model_config = ConfigDict(extra="ignore")

    api_key: str = ""                          # write-only; sentinel pattern
    version: NovelAIVoiceVersion = NovelAIVoiceVersion.V2
    voice_v1: str = "Cyllene"                  # preset name or NAI_CUSTOM_SENTINEL
    voice_v2: str = "Aini"
    custom_seed_v1: str = ""                   # used when voice_v1 == sentinel
    custom_seed_v2: str = ""


class TTSOpenRouterConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    api_key: str = ""                          # write-only
    model: str = ""
    voice: str = ""
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


class TTSNanoGPTConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    api_key: str = ""                          # write-only
    model: str = ""
    voice: str = ""
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


class TTSCustomAPI(BaseModel):
    """A user-defined OpenAI-compatible TTS endpoint."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=new_id)
    name: str = "Custom TTS"
    base_url: str = ""                         # full URL up to and including /v1
    api_key: str = ""                          # write-only
    models: list[str] = Field(default_factory=list)
    # Per-model voice lists: different models on the same endpoint
    # carry disjoint voice sets. Keys are a subset of ``models``; orphan
    # keys are silently ignored at request time.
    voices: dict[str, list[str]] = Field(default_factory=dict)
    default_model: str = ""                    # should appear in ``models``
    default_voice: str = ""                    # should appear in ``voices[default_model]``
    speed: float = Field(default=1.0, ge=0.25, le=4.0)


class TTSSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    mode: TTSGlobalMode = TTSGlobalMode.OFF
    # Which provider is "the default" — used when a contact's TTS
    # config says ``mode=default`` / ``use_custom=False``.
    active_kind: TTSProviderKind = TTSProviderKind.NOVELAI
    active_custom_id: str = ""                 # only consulted when active_kind == GENERIC
    novelai: TTSNovelAIConfig = Field(default_factory=TTSNovelAIConfig)
    openrouter: TTSOpenRouterConfig = Field(default_factory=TTSOpenRouterConfig)
    nanogpt: TTSNanoGPTConfig = Field(default_factory=TTSNanoGPTConfig)
    custom_apis: list[TTSCustomAPI] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Generic (non-AER) LLM provider configuration (held inside Settings)
# ---------------------------------------------------------------------------


# Where reminder / dynamically-activated brain bodies appear in the
# message stream. Generic mode lets the user pick this per provider —
# some upstream models behave better with brains as ``user`` or
# ``assistant`` rather than the AER-default ``system``.
LLMBrainMessageRole = Literal["system", "user", "assistant"]


class GenericNovelAIConfig(BaseModel):
    """Configuration for the NovelAI provider in Generic mode.

    Independent of ``Settings.endpoint_url`` (which is the AER-mode
    endpoint). The token field cascades to ``Settings.api_token`` if
    blank — see ``server.secrets.resolve_llm_token``.
    """

    model_config = ConfigDict(extra="ignore")
    base_url: str = "https://text.novelai.net/oa"
    api_token: str = ""                        # write-only; sentinel handled at router layer
    model_id: str = ""
    cache_minutes: int | None = None
    streaming: bool = True
    brain_message_role: LLMBrainMessageRole = "system"
    context_preset_id: str | None = None


class GenericOpenRouterConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    base_url: str = "https://openrouter.ai/api"
    api_token: str = ""
    model_id: str = ""
    cache_minutes: int | None = None
    streaming: bool = True
    brain_message_role: LLMBrainMessageRole = "system"
    context_preset_id: str | None = None


class GenericNanoGPTConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    base_url: str = "https://nano-gpt.com/api"
    api_token: str = ""
    model_id: str = ""
    cache_minutes: int | None = None
    streaming: bool = True
    brain_message_role: LLMBrainMessageRole = "system"
    context_preset_id: str | None = None


class OpenAICompatibleCustomProvider(BaseModel):
    """One entry in the user's list of OpenAI-compatible LLM endpoints.

    Free-form base URL + token + the same per-provider knobs as the
    named entries. Discovery (``/v1/models``) populates the available
    model list.
    """

    model_config = ConfigDict(extra="ignore")
    id: str = Field(default_factory=new_id)
    label: str = "Custom provider"
    base_url: str = ""
    api_token: str = ""
    model_id: str = ""
    cache_minutes: int | None = None
    streaming: bool = True
    brain_message_role: LLMBrainMessageRole = "system"
    context_preset_id: str | None = None


class OpenAICompatibleProviderConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    custom_providers: list[OpenAICompatibleCustomProvider] = Field(default_factory=list)
    # ``None`` while no entry is selected (bare-generic state). Token
    # resolution returns ``None`` in that case rather than guessing.
    active_id: str | None = None


GenericProviderKind = Literal["novelai", "openrouter", "nanogpt", "openai_compatible"]


class GenericSettings(BaseModel):
    """Per-provider drafts plus the currently active provider.

    Toggling the active provider does NOT wipe the other drafts —
    flipping back restores the previous token / model / etc. exactly.
    """

    model_config = ConfigDict(extra="ignore")
    provider: GenericProviderKind = "novelai"
    novelai: GenericNovelAIConfig = Field(default_factory=GenericNovelAIConfig)
    openrouter: GenericOpenRouterConfig = Field(default_factory=GenericOpenRouterConfig)
    nanogpt: GenericNanoGPTConfig = Field(default_factory=GenericNanoGPTConfig)
    openai_compatible: OpenAICompatibleProviderConfig = Field(
        default_factory=OpenAICompatibleProviderConfig,
    )


DEFAULT_IMAGE_SYSTEM_PROMPT = """Create one standalone image prompt for the story's latest physically realized instant. Freeze that instant exactly. Do not continue an unfinished action, stage a new scene, jump in time, or replace the current moment with a more dramatic alternative.

Use the transcript chronologically as evidence. Speech, thoughts, wishes, commands, plans, and hypotheticals do not change physical reality unless narration shows the change occurring. If the newest message is only dialogue or thought, preserve the previous physical scene. Carry forward established positions, actions, objects, clothing, and exposure until a later realized event changes them. Never invent a participant, prop, garment, pose, action, or environmental event.

The camera is the user character's eyes at their current position, height, posture, and direction of attention. Never switch to a detached third-person or externally staged view. Include the user's body only where it naturally enters this first-person view or appears in a story-supported reflection.

Reason inside the open <think> element in this order:
1. Freeze the moment: state the location, latest realized action, visible cast, positions, orientation, gaze, expressions, contact emotion, interactions, and props.
2. Establish the first-person geometry: field of view, distance, foreground and background, and only story-supported occluders. Use the TARGET IMAGE FORMAT to plan a composition suited to its exact dimensions without cropping out scene-critical facts.
3. Build an identity ledger from the CANONICAL VISUAL REFERENCE. Preserve every known visible trait, especially hair color and style, eye color, species features, skin tone, build, and distinctive marks. Omit only unknown or genuinely hidden traits.
4. Build a visibility ledger for every relevant garment layer and body region. Record whether each garment is normally worn, open, unfastened, displaced, or removed; where a displaced or removed garment is; what anatomy this reveals; whether the region is in view; and what established object actually occludes it, if anything.
5. Outline a complete final prompt covering composition, people, pose and action, expression, visible clothing and anatomy, foreground interaction, setting, props, depth, lighting, color, weather, and atmosphere.

Clothing and exposure are persistent physical state. Never collapse a displaced garment into fully worn or fully removed. Mention hidden layers only when they affect visible pixels. Do not reveal a hidden absence through outer clothing or pose, and never assume standard underwear or replacement clothing. Conversely, never invent modesty using a crop, angle, pose, hand, arm, hair, bedding, shadow, or garment. If the transcript places an exposed region or interaction in view, it belongs in the composition until a later action covers it. Preserve exact side, count, and degree of exposure. Name visible anatomy directly and specifically rather than weakening it to vague phrases such as revealing pose, partial nudity, intimate area, or exposed skin.

The diffusion model receives only the text inside <image_prompt>. It knows no transcript, profile, role label, or character name. The final prompt must therefore identify each visible person entirely through appearance and position. Never use a character name or call anyone the user, contact, assistant, or viewer. Restate every visible identity trait, garment state, exposed region, action, expression, and spatial relationship needed to reproduce the frame. Do not mention facts outside the frame and do not invent unsupported details.

Write natural language in complete grammatical sentences, not tags. Do not use comma chains, booru vocabulary, quality tags, weights, emphasis syntax, or model-control instructions. Produce a self-contained visual specification rather than a synopsis. A visually rich scene should normally take several substantial paragraphs and roughly 250 to 600 words; use less only when little is visibly supported, never pad with invention, and never exceed roughly 900 words. Before closing, audit the prompt against the identity and visibility ledgers and expand any supported category that is still merely implied.

There are two output phases. During reasoning, finish the audit and emit </think> as the final characters; do not begin the prompt in that response. When the existing context ends with an open <image_prompt>, write only the standalone prompt, close it with </image_prompt>, and end immediately."""

DEFAULT_IMAGE_USER_MESSAGE = """Capture the transcript's latest physically realized instant through the user character's eyes. Preserve the exact scene, current action, visible identities, emotions, garment states, exposure, and established occlusions. Use the target resolution to plan the frame. During reasoning, complete the moment, viewpoint, identity, visibility, and composition ledgers, then close </think>. When <image_prompt> is already open, write the detailed self-contained natural-language image prompt without names, role labels, invented events, vague anatomy, or tag-style comma chains; then close </image_prompt>."""

DEFAULT_IMAGE_NEGATIVE_PROMPT = (
    "lowres, artistic error, film grain, scan artifacts, worst quality, "
    "bad quality, jpeg artifacts, very displeasing, chromatic aberration, "
    "dithering, halftone, screentone, multiple views, logo, too many "
    "watermarks, negative space, blank page, @_@, mismatched pupils, "
    "glowing eyes, bad anatomy"
)


class ImageGenerationSettings(BaseModel):
    """NovelAI scene-prompt and diffusion defaults (token is shared)."""

    model_config = ConfigDict(extra="ignore")
    base_url: str = "https://image.novelai.net"
    model: str = "nai-diffusion-5-full"
    prompt_model: str = ""  # blank -> Generic NAI model, then AER default
    system_prompt: str = DEFAULT_IMAGE_SYSTEM_PROMPT
    user_message: str = DEFAULT_IMAGE_USER_MESSAGE
    negative_prompt: str = DEFAULT_IMAGE_NEGATIVE_PROMPT
    width: int = Field(default=832, ge=64, le=2048)
    height: int = Field(default=1216, ge=64, le=2048)
    steps: int = Field(default=23, ge=1, le=50)
    scale: float = Field(default=7.0, ge=0.0, le=20.0)
    sampler: str = "k_euler_ancestral"
    # This is the complete text-response budget: visible reasoning plus the
    # final prompt.  The larger default leaves room for a final image prompt
    # of up to 1,471 Qwen 3.5 tokens without forcing an automatic follow-up.
    prompt_max_tokens: int = Field(default=4096, ge=128, le=8192)


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    endpoint_url: str = "https://text.novelai.net/oa"
    api_token: str = ""  # write-only; never returned via the API
    default_model: str = "xialong-v1"
    default_preset_id: str | None = None
    theme: ThemeId = "dark"
    font_size: int = 14            # UI font size (rail, headers, buttons, lists)
    content_font_size: int = 14    # chat bubbles + multi-line content textareas
    # Cap on chat-bubble width, in ``em`` relative to the chat font size, so
    # long lines stay readable. ``None`` (default) means unlimited — the bubble
    # fills its column as before. The settings slider exposes 20–140 plus an
    # "unlimited" tick that maps back to ``None``.
    max_bubble_width_em: int | None = Field(default=None, ge=20, le=140)
    # Named context-window preset; ``server.aer.rollover.CONTEXT_PRESETS``
    # maps each name to ``base_context_size`` + ``rollover_window`` token counts.
    context_preset: Literal["opus", "scroll", "tablet"] = "opus"
    # Global "AER" vs "Generic" switch. Generic mode reveals the
    # ``generic`` config below and routes generation through the
    # selected non-AER provider.
    provider_mode: Literal["aetherroom", "generic"] = "aetherroom"
    generic: GenericSettings = Field(default_factory=GenericSettings)
    # Generic-only HTML sanitizer toggle. When True (default), the frontend
    # markdown renderer escapes ``& < > " '`` in assistant message content
    # before passing to ``marked``, so no raw ``<script>`` / ``<a>`` /
    # ``<img>`` outside markdown-generated ones can reach the DOM. Adventurous
    # users can flip it off to let the model emit raw HTML.
    sanitize_generic_html: bool = True
    # Generic-only image compression for multimodal uploads. When True, a
    # chat message's image attachments are re-encoded as JPEG at
    # ``image_compression_quality`` before being sent to the provider, which
    # cuts upload size for photo-style content. The original is sent untouched
    # whenever it's already smaller than the JPEG (common for simple palette
    # PNGs). Originals are always kept on disk; the compressed copies are a
    # regenerable cache (``attachments/.cached/``) that is never exported.
    # Ignored in AER mode, which doesn't put attachments in the prompt.
    compress_images: bool = True
    image_compression_quality: int = Field(default=85, ge=1, le=100)
    image_generation: ImageGenerationSettings = Field(
        default_factory=ImageGenerationSettings,
    )
    tts: TTSSettings = Field(default_factory=TTSSettings)
    # Play a short ping when a generation finishes. Default off so the app
    # stays silent unless the user opts in. ``notification_sound`` is the
    # filename of a user-uploaded audio file under ``data/`` (e.g.
    # ``notification_sound.mp3``); ``None`` means the synthetic Web Audio
    # ping in ``static/notification.js`` is used.
    notify_on_complete: bool = False
    notification_sound: str | None = None

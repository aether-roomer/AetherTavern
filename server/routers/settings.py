"""Settings routes (single global config object)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from server import storage
from server.models import (
    GenericProviderKind,
    GenericSettings,
    LLMBrainMessageRole,
    NovelAIVoiceVersion,
    Settings,
    ThemeId,
    TTSGlobalMode,
    TTSProviderKind,
    TTSSettings,
)


router = APIRouter(prefix="/api/settings", tags=["settings"])


# Sentinel value the GET response uses to indicate the API token is set without
# revealing it. PUT echoes this back to mean "leave the token alone".
TOKEN_PRESENT_SENTINEL = "__present__"


# --- TTS sub-views (api_key replaced with an indicator) -------------------


class TTSNovelAIView(BaseModel):
    api_key_indicator: str
    version: NovelAIVoiceVersion
    voice_v1: str
    voice_v2: str
    custom_seed_v1: str
    custom_seed_v2: str


class TTSOpenRouterView(BaseModel):
    api_key_indicator: str
    model: str
    voice: str
    speed: float


class TTSNanoGPTView(BaseModel):
    api_key_indicator: str
    model: str
    voice: str
    speed: float


class TTSCustomAPIView(BaseModel):
    id: str
    name: str
    base_url: str
    api_key_indicator: str
    models: list[str]
    voices: dict[str, list[str]]
    default_model: str
    default_voice: str
    speed: float


class TTSSettingsView(BaseModel):
    mode: TTSGlobalMode
    active_kind: TTSProviderKind
    active_custom_id: str
    novelai: TTSNovelAIView
    openrouter: TTSOpenRouterView
    nanogpt: TTSNanoGPTView
    custom_apis: list[TTSCustomAPIView]


# --- Generic-provider sub-views (api_token replaced with an indicator) -----


class _GenericProviderViewBase(BaseModel):
    base_url: str
    api_token_indicator: str
    model_id: str
    cache_minutes: int | None
    streaming: bool
    brain_message_role: LLMBrainMessageRole
    context_preset_id: str | None


class GenericNovelAIView(_GenericProviderViewBase):
    pass


class GenericOpenRouterView(_GenericProviderViewBase):
    pass


class GenericNanoGPTView(_GenericProviderViewBase):
    pass


class OpenAICompatibleCustomProviderView(_GenericProviderViewBase):
    id: str
    label: str


class OpenAICompatibleProviderConfigView(BaseModel):
    custom_providers: list[OpenAICompatibleCustomProviderView]
    active_id: str | None


class GenericSettingsView(BaseModel):
    provider: GenericProviderKind
    novelai: GenericNovelAIView
    openrouter: GenericOpenRouterView
    nanogpt: GenericNanoGPTView
    openai_compatible: OpenAICompatibleProviderConfigView


# --- Settings view + update --------------------------------------------


class SettingsView(BaseModel):
    """Settings as returned by GET — never includes raw API keys."""

    model_config = ConfigDict(extra="ignore")

    endpoint_url: str
    api_token_indicator: str  # "" if unset, sentinel if set
    default_model: str
    default_preset_id: str | None
    theme: ThemeId
    font_size: int
    content_font_size: int
    max_bubble_width_em: int | None  # None = unlimited (no cap)
    context_preset: Literal["opus", "scroll", "tablet"]
    provider_mode: Literal["aetherroom", "generic"]
    generic: GenericSettingsView
    sanitize_generic_html: bool
    compress_images: bool
    image_compression_quality: int
    tts: TTSSettingsView
    notify_on_complete: bool
    notification_sound: str | None


class SettingsUpdate(BaseModel):
    """All fields optional; missing keys preserve existing values.

    For ``tts`` and ``generic``, sending the block replaces it
    wholesale — but each token field within (including per-custom
    entries) honours the sentinel: ``"__present__"`` means "preserve
    existing token", ``""`` clears, any other string sets a new value.
    """

    model_config = ConfigDict(extra="ignore")

    endpoint_url: str | None = None
    api_token: str | None = None  # None or sentinel = preserve; "" = clear; else set
    default_model: str | None = None
    default_preset_id: str | None = None
    theme: ThemeId | None = None
    font_size: int | None = None
    content_font_size: int | None = None
    # ``None`` is ambiguous here (preserve vs. set-unlimited); ``put_settings``
    # disambiguates via ``model_fields_set``. The bound rejects out-of-range
    # input so a bad value can't reach disk and break the next load.
    max_bubble_width_em: int | None = Field(default=None, ge=20, le=140)
    context_preset: Literal["opus", "scroll", "tablet"] | None = None
    provider_mode: Literal["aetherroom", "generic"] | None = None
    generic: GenericSettings | None = None
    sanitize_generic_html: bool | None = None
    compress_images: bool | None = None
    image_compression_quality: int | None = Field(default=None, ge=1, le=100)
    tts: TTSSettings | None = None
    notify_on_complete: bool | None = None
    # notification_sound is a server-managed filename — set via the
    # dedicated upload/clear routes below, not via this generic PUT.


# --- Helpers ------------------------------------------------------------


def _indicator(value: str) -> str:
    """Reveal whether a secret is set without disclosing the value."""
    return TOKEN_PRESENT_SENTINEL if value else ""


def _to_tts_view(s: TTSSettings) -> TTSSettingsView:
    return TTSSettingsView(
        mode=s.mode,
        active_kind=s.active_kind,
        active_custom_id=s.active_custom_id,
        novelai=TTSNovelAIView(
            api_key_indicator=_indicator(s.novelai.api_key),
            version=s.novelai.version,
            voice_v1=s.novelai.voice_v1,
            voice_v2=s.novelai.voice_v2,
            custom_seed_v1=s.novelai.custom_seed_v1,
            custom_seed_v2=s.novelai.custom_seed_v2,
        ),
        openrouter=TTSOpenRouterView(
            api_key_indicator=_indicator(s.openrouter.api_key),
            model=s.openrouter.model,
            voice=s.openrouter.voice,
            speed=s.openrouter.speed,
        ),
        nanogpt=TTSNanoGPTView(
            api_key_indicator=_indicator(s.nanogpt.api_key),
            model=s.nanogpt.model,
            voice=s.nanogpt.voice,
            speed=s.nanogpt.speed,
        ),
        custom_apis=[
            TTSCustomAPIView(
                id=e.id,
                name=e.name,
                base_url=e.base_url,
                api_key_indicator=_indicator(e.api_key),
                models=list(e.models),
                voices={k: list(v) for k, v in e.voices.items()},
                default_model=e.default_model,
                default_voice=e.default_voice,
                speed=e.speed,
            )
            for e in s.custom_apis
        ],
    )


def _to_generic_view(g: GenericSettings) -> GenericSettingsView:
    """Replace every raw ``api_token`` with an indicator for the GET response."""
    def _np(cfg) -> dict:
        return {
            "base_url": cfg.base_url,
            "api_token_indicator": _indicator(cfg.api_token),
            "model_id": cfg.model_id,
            "cache_minutes": cfg.cache_minutes,
            "streaming": cfg.streaming,
            "brain_message_role": cfg.brain_message_role,
            "context_preset_id": cfg.context_preset_id,
        }
    return GenericSettingsView(
        provider=g.provider,
        novelai=GenericNovelAIView(**_np(g.novelai)),
        openrouter=GenericOpenRouterView(**_np(g.openrouter)),
        nanogpt=GenericNanoGPTView(**_np(g.nanogpt)),
        openai_compatible=OpenAICompatibleProviderConfigView(
            active_id=g.openai_compatible.active_id,
            custom_providers=[
                OpenAICompatibleCustomProviderView(
                    id=e.id, label=e.label, **_np(e),
                )
                for e in g.openai_compatible.custom_providers
            ],
        ),
    )


def _merge_generic_secrets(incoming: GenericSettings, current: GenericSettings) -> GenericSettings:
    """Replace sentinel ``api_token`` values with the existing token.

    Mirrors :func:`_merge_tts_secrets`. Operates in-place on
    ``incoming`` and returns it. Newly-created custom-provider entries
    that carry the sentinel (no matching id in ``current``) are treated
    as "no token set yet" rather than an error — the frontend follows
    up with a real value on the next edit.
    """
    if incoming.novelai.api_token == TOKEN_PRESENT_SENTINEL:
        incoming.novelai.api_token = current.novelai.api_token
    if incoming.openrouter.api_token == TOKEN_PRESENT_SENTINEL:
        incoming.openrouter.api_token = current.openrouter.api_token
    if incoming.nanogpt.api_token == TOKEN_PRESENT_SENTINEL:
        incoming.nanogpt.api_token = current.nanogpt.api_token

    by_id = {e.id: e for e in current.openai_compatible.custom_providers}
    for entry in incoming.openai_compatible.custom_providers:
        if entry.api_token == TOKEN_PRESENT_SENTINEL:
            existing = by_id.get(entry.id)
            entry.api_token = existing.api_token if existing else ""

    return incoming


def _merge_tts_secrets(incoming: TTSSettings, current: TTSSettings) -> TTSSettings:
    """Replace sentinel ``api_key`` values with the existing key.

    Operates in-place on ``incoming`` and returns it. Top-level
    provider configs and each per-id custom API entry honour the
    sentinel. Custom-API entries that don't exist in ``current`` (i.e.
    newly created in this PUT) and carry the sentinel are treated as
    "no key set yet" rather than an error — the frontend will follow
    up with a real key on the next edit.
    """
    if incoming.novelai.api_key == TOKEN_PRESENT_SENTINEL:
        incoming.novelai.api_key = current.novelai.api_key
    if incoming.openrouter.api_key == TOKEN_PRESENT_SENTINEL:
        incoming.openrouter.api_key = current.openrouter.api_key
    if incoming.nanogpt.api_key == TOKEN_PRESENT_SENTINEL:
        incoming.nanogpt.api_key = current.nanogpt.api_key

    by_id = {e.id: e for e in current.custom_apis}
    for entry in incoming.custom_apis:
        if entry.api_key == TOKEN_PRESENT_SENTINEL:
            existing = by_id.get(entry.id)
            entry.api_key = existing.api_key if existing else ""
        # Prune orphan voice keys: any voices[model] whose model isn't
        # in models[] is silently dropped. Cheap to do here; keeps disk
        # state tidy.
        if entry.voices:
            valid_models = set(entry.models)
            entry.voices = {m: v for m, v in entry.voices.items() if m in valid_models}

    return incoming


def _to_view(s: Settings) -> SettingsView:
    return SettingsView(
        endpoint_url=s.endpoint_url,
        api_token_indicator=_indicator(s.api_token),
        default_model=s.default_model,
        default_preset_id=s.default_preset_id,
        theme=s.theme,
        font_size=s.font_size,
        content_font_size=s.content_font_size,
        max_bubble_width_em=s.max_bubble_width_em,
        context_preset=s.context_preset,
        provider_mode=s.provider_mode,
        generic=_to_generic_view(s.generic),
        sanitize_generic_html=s.sanitize_generic_html,
        compress_images=s.compress_images,
        image_compression_quality=s.image_compression_quality,
        tts=_to_tts_view(s.tts),
        notify_on_complete=s.notify_on_complete,
        notification_sound=s.notification_sound,
    )


@router.get("")
async def get_settings() -> SettingsView:
    return _to_view(storage.load_settings())


@router.put("")
async def put_settings(update: SettingsUpdate) -> SettingsView:
    async with storage.lock("settings"):
        current = storage.load_settings()
        payload = update.model_dump(exclude_none=True)

        if "api_token" in payload:
            token = payload.pop("api_token")
            if token != TOKEN_PRESENT_SENTINEL:
                current.api_token = token

        if "generic" in payload:
            payload.pop("generic")
            # ``update.generic`` is the validated Pydantic instance;
            # mutate its secrets, then assign wholesale.
            current.generic = _merge_generic_secrets(update.generic, current.generic)

        if "tts" in payload:
            payload.pop("tts")
            # update.tts is the validated Pydantic instance; mutate
            # its secrets, then assign.
            current.tts = _merge_tts_secrets(update.tts, current.tts)

        # ``max_bubble_width_em`` is nullable, and ``None`` is a real value the
        # user sets (slider to the "unlimited" tick) — not just "preserve". So
        # ``exclude_none`` can't carry it; honour whether the client actually
        # sent the field.
        payload.pop("max_bubble_width_em", None)
        if "max_bubble_width_em" in update.model_fields_set:
            current.max_bubble_width_em = update.max_bubble_width_em

        for k, v in payload.items():
            setattr(current, k, v)

        storage.save_settings(current)
        return _to_view(current)

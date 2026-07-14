"""Resolve a chat's effective generation target from its per-chat overrides.

Single source of truth for turning ``(chat, settings)`` into a normalized
target: which mode (AER vs a specific Generic provider), which model, which
context preset (Generic only), the brain-message role, and the bearer token.
Generation, context-token estimation, and message stamping all consume this so
the override semantics live in exactly one place.

A note on naming: ``provider_slug`` is a plain identifier string — the
``model_overrides`` key and the ``provider_override`` value space — NOT an auth
credential. The bearer auth token is the separate ``token`` field.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from server.models import (
    Chat,
    LLMBrainMessageRole,
    OpenAICompatibleCustomProvider,
    Settings,
)
from server.secrets import resolve_llm_token_for_entry


class GenerationTargetError(Exception):
    """An explicitly-selected override (or unconfigured global) is unusable.

    ``kind`` matches the SSE error-kind contract (``no_provider`` /
    ``no_preset`` / ``no_token``). ``from_override`` is True when the chat's own
    override is the culprit (deleted custom entry, bare ``openai_compatible``, an
    override model that resolves empty) so the caller can point the user at the
    chat's provider picker rather than global Settings.
    """

    def __init__(self, message: str, kind: str, *, from_override: bool):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.from_override = from_override


@dataclass
class EffectiveTarget:
    mode: Literal["aetherroom", "generic"]
    # Generic-only fields are None / "system" when mode == "aetherroom".
    provider_kind: Optional[str]          # novelai/openrouter/nanogpt/openai_compatible
    provider_cfg: object | None           # resolved Generic*Config / custom entry
    provider_slug: str                    # canonical slug (the model_overrides key)
    model: str                            # effective model id
    context_preset_id: Optional[str]      # Generic only; None for AER
    brain_message_role: LLMBrainMessageRole
    base_url: Optional[str]
    cache_minutes: Optional[int]
    token: str                            # bearer auth token (may be "" for AER, as today)
    provider_override: Optional[str]      # echo of chat.provider_override


def _find_custom(
    settings: Settings, entry_id: str | None,
) -> OpenAICompatibleCustomProvider | None:
    if not entry_id:
        return None
    return next(
        (p for p in settings.generic.openai_compatible.custom_providers
         if p.id == entry_id),
        None,
    )


def _resolve_mode_and_provider(chat: Chat, settings: Settings):
    """Return ``(mode, provider_kind, custom_entry, provider_slug)``.

    Raises ``GenerationTargetError`` (no_provider, from_override=True) only when
    an EXPLICIT override is structurally invalid (bare ``openai_compatible`` or a
    deleted custom id). An inherited-but-unconfigured global (no ``active_id``)
    is NOT raised here — it returns ``custom_entry=None`` for the caller to
    surface as a global ``no_provider``.
    """
    ov = chat.provider_override
    if ov is None:
        if settings.provider_mode == "aetherroom":
            return "aetherroom", None, None, "aetherroom"
        g = settings.generic
        if g.provider == "openai_compatible":
            entry = _find_custom(settings, g.openai_compatible.active_id)
            slug = f"openai_compatible:{g.openai_compatible.active_id or ''}"
            return "generic", "openai_compatible", entry, slug
        return "generic", g.provider, None, g.provider

    if ov == "aetherroom":
        return "aetherroom", None, None, "aetherroom"
    if ov in ("novelai", "openrouter", "nanogpt"):
        return "generic", ov, None, ov
    if ov.startswith("openai_compatible:"):
        entry_id = ov.split(":", 1)[1]
        entry = _find_custom(settings, entry_id)
        if entry is None:
            raise GenerationTargetError(
                "This chat's provider override points at a custom provider that "
                "no longer exists. Pick a provider for this chat, or clear the "
                "override to follow the global setting.",
                kind="no_provider", from_override=True,
            )
        return "generic", "openai_compatible", entry, ov
    raise GenerationTargetError(
        "This chat's provider override is invalid. Pick a provider for this chat.",
        kind="no_provider", from_override=True,
    )


def chat_provider_slug(chat: Chat, settings: Settings) -> str | None:
    """Canonical provider slug for a chat (the ``model_overrides`` key), or
    ``None`` if the selection can't be resolved. Best-effort: swallows
    ``GenerationTargetError`` so callers that only want the slug needn't guard.
    """
    try:
        _mode, _kind, _entry, slug = _resolve_mode_and_provider(chat, settings)
    except GenerationTargetError:
        return None
    return slug


def resolve_generation_target(chat: Chat, settings: Settings) -> EffectiveTarget:
    mode, provider_kind, custom_entry, provider_slug = _resolve_mode_and_provider(
        chat, settings,
    )

    if mode == "aetherroom":
        # AER has one endpoint/model; the override map may carry an "aetherroom"
        # key (e.g. from an import) but the UI doesn't set one. Faithful to
        # today: do not hard-error on empty model/token — pass through.
        model = chat.model_overrides.get("aetherroom") or settings.default_model
        token = resolve_llm_token_for_entry("aer", settings, None) or ""
        return EffectiveTarget(
            mode="aetherroom",
            provider_kind=None,
            provider_cfg=None,
            provider_slug="aetherroom",
            model=model,
            context_preset_id=None,
            brain_message_role="system",
            base_url=None,
            cache_minutes=None,
            token=token,
            provider_override=chat.provider_override,
        )

    # --- Generic ---
    if provider_kind == "openai_compatible":
        cfg = custom_entry
        if cfg is None:
            raise GenerationTargetError(
                "No provider configured for Generic mode. Pick one in Settings.",
                kind="no_provider",
                from_override=chat.provider_override is not None,
            )
    else:
        g = settings.generic
        cfg = {
            "novelai": g.novelai,
            "openrouter": g.openrouter,
            "nanogpt": g.nanogpt,
        }[provider_kind]

    model = chat.model_overrides.get(provider_slug) or cfg.model_id
    if not model:
        # Neither the per-provider override nor the provider's configured model
        # is set -> a global-config gap, not an override gap.
        raise GenerationTargetError(
            "No model selected for this chat's provider. Pick one in Settings, "
            "or set a model for this chat.",
            kind="no_provider", from_override=False,
        )

    context_preset_id = chat.context_preset_override or cfg.context_preset_id
    if not context_preset_id:
        raise GenerationTargetError(
            "No Context Preset selected for this chat's provider. Pick one in "
            "Settings.",
            kind="no_preset",
            from_override=bool(chat.context_preset_override),
        )

    token = resolve_llm_token_for_entry(provider_kind, settings, custom_entry) or ""
    if not token:
        raise GenerationTargetError(
            "No API token configured for this chat's provider. Set one in "
            "Settings.",
            kind="no_token",
            from_override=(
                chat.provider_override is not None
                and provider_kind == "openai_compatible"
            ),
        )

    return EffectiveTarget(
        mode="generic",
        provider_kind=provider_kind,
        provider_cfg=cfg,
        provider_slug=provider_slug,
        model=model,
        context_preset_id=context_preset_id,
        brain_message_role=cfg.brain_message_role,
        base_url=cfg.base_url,
        cache_minutes=cfg.cache_minutes,
        token=token,
        provider_override=chat.provider_override,
    )

"""Single source of truth for resolving API tokens.

Every call site that needs a bearer token for an upstream HTTP call must
go through these two helpers, so the LLM vs TTS fallback chains stay
consistent across providers.

Cascades reflect the user-facing rule "TTS-only never falls back to LLM,
but LLM-only fields can feed TTS for the same provider."
"""
from __future__ import annotations

from server.models import (
    OpenAICompatibleCustomProvider,
    Settings,
    TTSProviderKind,
)


def resolve_llm_token(provider: str, settings: Settings) -> str | None:
    """Bearer token for an LLM provider, or ``None`` if none configured.

    ``provider`` cascade:

    - ``aer``: ``settings.api_token`` (the long-standing AER token).
    - ``novelai``: ``generic.novelai.api_token`` → ``settings.api_token``.
      Generic NAI shares NAI's auth surface; falling back to the AER
      token lets a single NAI subscriber drop their key once and use
      either mode.
    - ``openrouter`` / ``nanogpt``: each provider's own
      ``generic.<p>.api_token``. No cross-provider fallback.
    - ``openai_compatible``: token of the entry pointed at by
      ``generic.openai_compatible.active_id``; ``None`` if no entry is
      selected (or the selected entry has no token set).
    """
    g = settings.generic
    if provider == "aer":
        return settings.api_token or None
    if provider == "novelai":
        return g.novelai.api_token or settings.api_token or None
    if provider == "openrouter":
        return g.openrouter.api_token or None
    if provider == "nanogpt":
        return g.nanogpt.api_token or None
    if provider == "openai_compatible":
        active_id = g.openai_compatible.active_id
        if not active_id:
            return None
        entry = next(
            (p for p in g.openai_compatible.custom_providers if p.id == active_id),
            None,
        )
        return (entry.api_token or None) if entry else None
    return None


def resolve_llm_token_for_entry(
    provider: str,
    settings: Settings,
    entry: OpenAICompatibleCustomProvider | None,
) -> str | None:
    """Bearer token for an already-resolved provider target.

    Same cascade as :func:`resolve_llm_token` for ``aer`` / ``novelai`` (NAI
    still falls back to ``settings.api_token``) / ``openrouter`` / ``nanogpt``.
    For ``openai_compatible`` the token comes from the PASSED ``entry`` (the
    per-chat-resolved custom provider) rather than from
    ``generic.openai_compatible.active_id`` — this is what lets a chat override
    to a non-active custom entry use that entry's own key.
    """
    if provider == "openai_compatible":
        return (entry.api_token or None) if entry is not None else None
    return resolve_llm_token(provider, settings)


def resolve_tts_token(tts_provider: str, settings: Settings) -> str | None:
    """Bearer token for a TTS provider, or ``None`` if none configured.

    Cascade (TTS-own → upstream LLM cousin → null):

    - NAI: ``tts.novelai.api_key`` → ``settings.api_token`` (AER) →
      ``generic.novelai.api_token``. Mirrors the long-standing xialong
      shortcut: an AER subscriber already has a valid key for NAI's
      audio API.
    - OpenRouter: ``tts.openrouter.api_key`` → ``generic.openrouter.api_token``.
    - NanoGPT: ``tts.nanogpt.api_key`` → ``generic.nanogpt.api_token``.
    - Generic custom TTS (``TTSProviderKind.GENERIC``): the upstream
      URL + key are tied to a specific ``custom_id``, so resolution
      stays in ``server.tts.keys.resolve_key_and_url``. This helper
      returns ``None`` for the generic case.
    """
    tts = settings.tts
    g = settings.generic
    if tts_provider == TTSProviderKind.NOVELAI.value:
        return (
            tts.novelai.api_key
            or settings.api_token
            or g.novelai.api_token
            or None
        )
    if tts_provider == TTSProviderKind.OPENROUTER.value:
        return tts.openrouter.api_key or g.openrouter.api_token or None
    if tts_provider == TTSProviderKind.NANOGPT.value:
        return tts.nanogpt.api_key or g.nanogpt.api_token or None
    return None

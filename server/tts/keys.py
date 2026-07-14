"""Resolve the API key + full upstream URL for a TTS proxy request.

The proxy router does the rest of the work (per-provider payload shape,
header style, response handling). This module owns the bit that must
read ``Settings``: the URL we POST to, plus the *generic* custom entry
lookup. The per-named-provider key cascades all run through
``server.secrets.resolve_tts_token`` so inference and TTS share the
same fallback rules.
"""
from __future__ import annotations

from server.models import Settings, TTSProviderKind
from server.secrets import resolve_tts_token


class TTSKeyMissing(Exception):
    """No API key configured for the resolved provider — surfaced as HTTP 412."""


class TTSUnknownProvider(Exception):
    """Unknown ``api`` value or ``custom_id`` points at a non-existent entry — HTTP 400."""


_NAI_URL = "https://api.novelai.net/ai/generate-voice"
_OR_URL = "https://openrouter.ai/api/v1/audio/speech"
_NGPT_URL = "https://nano-gpt.com/api/tts"


def resolve_key_and_url(api: str, custom_id: str, settings: Settings) -> tuple[str, str]:
    """Returns ``(api_key, upstream_url)``.

    Raises :class:`TTSKeyMissing` if the resolved provider has no key set
    (after the cascade through ``resolve_tts_token``). Raises
    :class:`TTSUnknownProvider` if ``api`` is unrecognised or
    ``custom_id`` doesn't match any entry.
    """
    if api == TTSProviderKind.NOVELAI.value:
        key = resolve_tts_token(api, settings)
        if not key:
            raise TTSKeyMissing("NovelAI TTS key is not configured.")
        return key, _NAI_URL

    if api == TTSProviderKind.OPENROUTER.value:
        key = resolve_tts_token(api, settings)
        if not key:
            raise TTSKeyMissing("OpenRouter TTS key is not configured.")
        return key, _OR_URL

    if api == TTSProviderKind.NANOGPT.value:
        key = resolve_tts_token(api, settings)
        if not key:
            raise TTSKeyMissing("NanoGPT TTS key is not configured.")
        return key, _NGPT_URL

    if api == TTSProviderKind.GENERIC.value:
        # Generic custom TTS doesn't share with LLM-side configs — its
        # key + URL are tied to the specific custom entry the user
        # picked. ``resolve_tts_token`` is therefore not consulted here.
        if not custom_id:
            raise TTSUnknownProvider("Generic TTS requires a custom_id query param.")
        entry = next((e for e in settings.tts.custom_apis if e.id == custom_id), None)
        if entry is None:
            raise TTSUnknownProvider(f"No custom TTS API matches id {custom_id!r}.")
        if not entry.api_key:
            raise TTSKeyMissing(f"Custom TTS API {entry.name!r} has no key configured.")
        base = entry.base_url.rstrip("/")
        if not base:
            raise TTSUnknownProvider(f"Custom TTS API {entry.name!r} has no base URL configured.")
        return entry.api_key, f"{base}/audio/speech"

    raise TTSUnknownProvider(f"Unknown TTS api value {api!r}.")

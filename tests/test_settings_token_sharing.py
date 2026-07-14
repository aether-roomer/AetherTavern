"""Token-resolution matrix for ``server.secrets``.

Every row asserts an explicit cascade. The spec rule: TTS-only tokens
never fall back to LLM, but LLM tokens *can* feed TTS for the same
provider (NAI TTS additionally falls back to the AER token).
"""
from __future__ import annotations

from server.models import (
    OpenAICompatibleCustomProvider,
    Settings,
    TTSProviderKind,
)
from server.secrets import resolve_llm_token, resolve_tts_token


# ---------- LLM resolver ----------------------------------------------------


def test_llm_aer_reads_settings_api_token():
    s = Settings(api_token="aer-key")
    assert resolve_llm_token("aer", s) == "aer-key"


def test_llm_aer_none_when_unset():
    s = Settings()
    assert resolve_llm_token("aer", s) is None


def test_llm_novelai_own_token_wins():
    s = Settings(api_token="aer-key")
    s.generic.novelai.api_token = "gnai-key"
    assert resolve_llm_token("novelai", s) == "gnai-key"


def test_llm_novelai_falls_back_to_aer():
    s = Settings(api_token="aer-key")
    assert resolve_llm_token("novelai", s) == "aer-key"


def test_llm_novelai_none_when_neither_set():
    s = Settings()
    assert resolve_llm_token("novelai", s) is None


def test_llm_openrouter_own_only():
    s = Settings(api_token="aer-key")  # must NOT bleed into OR
    s.generic.openrouter.api_token = "or-key"
    assert resolve_llm_token("openrouter", s) == "or-key"


def test_llm_openrouter_none_without_own():
    s = Settings(api_token="aer-key")
    assert resolve_llm_token("openrouter", s) is None


def test_llm_nanogpt_own_only():
    s = Settings()
    s.generic.nanogpt.api_token = "ngpt-key"
    assert resolve_llm_token("nanogpt", s) == "ngpt-key"


def test_llm_nanogpt_none_without_own():
    s = Settings(api_token="aer-key")
    assert resolve_llm_token("nanogpt", s) is None


def test_llm_openai_compatible_no_active():
    s = Settings()
    s.generic.openai_compatible.custom_providers = [
        OpenAICompatibleCustomProvider(id="a", label="A", api_token="aaa-k"),
    ]
    # No active_id set — resolver returns None even though entries exist.
    assert resolve_llm_token("openai_compatible", s) is None


def test_llm_openai_compatible_active_with_token():
    s = Settings()
    s.generic.openai_compatible.custom_providers = [
        OpenAICompatibleCustomProvider(id="a", label="A", api_token="aaa-k"),
        OpenAICompatibleCustomProvider(id="b", label="B", api_token="bbb-k"),
    ]
    s.generic.openai_compatible.active_id = "b"
    assert resolve_llm_token("openai_compatible", s) == "bbb-k"


def test_llm_openai_compatible_active_without_token():
    s = Settings()
    s.generic.openai_compatible.custom_providers = [
        OpenAICompatibleCustomProvider(id="a", label="A", api_token=""),
    ]
    s.generic.openai_compatible.active_id = "a"
    assert resolve_llm_token("openai_compatible", s) is None


def test_llm_openai_compatible_dangling_active_id():
    s = Settings()
    s.generic.openai_compatible.active_id = "ghost"
    assert resolve_llm_token("openai_compatible", s) is None


def test_llm_unknown_provider_returns_none():
    s = Settings(api_token="aer-key")
    assert resolve_llm_token("not-a-provider", s) is None


# ---------- TTS resolver ----------------------------------------------------


def test_tts_nai_own_wins():
    s = Settings(api_token="aer-key")
    s.generic.novelai.api_token = "gnai-key"
    s.tts.novelai.api_key = "tts-nai-key"
    assert resolve_tts_token(TTSProviderKind.NOVELAI.value, s) == "tts-nai-key"


def test_tts_nai_falls_back_to_aer():
    s = Settings(api_token="aer-key")
    assert resolve_tts_token(TTSProviderKind.NOVELAI.value, s) == "aer-key"


def test_tts_nai_falls_back_to_generic_when_aer_unset():
    s = Settings()
    s.generic.novelai.api_token = "gnai-key"
    assert resolve_tts_token(TTSProviderKind.NOVELAI.value, s) == "gnai-key"


def test_tts_nai_none_when_nothing_set():
    s = Settings()
    assert resolve_tts_token(TTSProviderKind.NOVELAI.value, s) is None


def test_tts_or_own_wins():
    s = Settings()
    s.generic.openrouter.api_token = "or-llm-key"
    s.tts.openrouter.api_key = "or-tts-key"
    assert resolve_tts_token(TTSProviderKind.OPENROUTER.value, s) == "or-tts-key"


def test_tts_or_falls_back_to_generic_or():
    s = Settings()
    s.generic.openrouter.api_token = "or-llm-key"
    assert resolve_tts_token(TTSProviderKind.OPENROUTER.value, s) == "or-llm-key"


def test_tts_or_does_not_fall_back_to_aer():
    s = Settings(api_token="aer-key")  # must NOT count
    assert resolve_tts_token(TTSProviderKind.OPENROUTER.value, s) is None


def test_tts_ngpt_own_wins():
    s = Settings()
    s.generic.nanogpt.api_token = "ngpt-llm-key"
    s.tts.nanogpt.api_key = "ngpt-tts-key"
    assert resolve_tts_token(TTSProviderKind.NANOGPT.value, s) == "ngpt-tts-key"


def test_tts_ngpt_falls_back_to_generic_ngpt():
    s = Settings()
    s.generic.nanogpt.api_token = "ngpt-llm-key"
    assert resolve_tts_token(TTSProviderKind.NANOGPT.value, s) == "ngpt-llm-key"


def test_tts_ngpt_does_not_fall_back_to_aer():
    s = Settings(api_token="aer-key")
    assert resolve_tts_token(TTSProviderKind.NANOGPT.value, s) is None


def test_tts_generic_kind_returns_none_no_custom_id_resolution():
    # Custom-TTS resolution is by ``custom_id`` and stays in
    # ``server.tts.keys.resolve_key_and_url``; this helper returns None.
    s = Settings(api_token="aer-key")
    s.generic.openrouter.api_token = "or-key"
    assert resolve_tts_token(TTSProviderKind.GENERIC.value, s) is None


# ---------- AER-only setup drives NAI TTS ----------------------------------


def test_regression_aer_only_setup_drives_nai_tts():
    """When only the AER (xialong) token is set, NAI TTS still works:
    the TTS resolver falls back to ``settings.api_token``.
    """
    s = Settings(api_token="xialong-key")
    assert resolve_llm_token("aer", s) == "xialong-key"
    assert resolve_tts_token(TTSProviderKind.NOVELAI.value, s) == "xialong-key"
    # And the new generic-LLM chains stay empty as expected.
    assert resolve_llm_token("openrouter", s) is None
    assert resolve_llm_token("nanogpt", s) is None
    assert resolve_llm_token("openai_compatible", s) is None

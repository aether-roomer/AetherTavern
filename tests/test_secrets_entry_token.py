"""``resolve_llm_token_for_entry`` — the entry-aware token resolver that lets a
per-chat override to a non-active custom provider use that entry's own token.

Named providers (and AER) delegate to ``resolve_llm_token``; only
``openai_compatible`` differs (passed entry, not the global ``active_id``).
"""
from __future__ import annotations

from server.models import OpenAICompatibleCustomProvider, Settings
from server.secrets import resolve_llm_token, resolve_llm_token_for_entry


def test_entry_token_aer_delegates():
    s = Settings(api_token="aer-key")
    assert resolve_llm_token_for_entry("aer", s, None) == "aer-key"


def test_entry_token_novelai_keeps_aer_fallback():
    s = Settings(api_token="aer-key")
    assert resolve_llm_token_for_entry("novelai", s, None) == "aer-key"


def test_entry_token_openrouter_delegates():
    s = Settings()
    s.generic.openrouter.api_token = "or-key"
    assert resolve_llm_token_for_entry("openrouter", s, None) == "or-key"


def test_entry_token_openai_compatible_uses_passed_entry():
    s = Settings()
    # Active entry is "a" with token "aaa"; we resolve for entry "b".
    s.generic.openai_compatible.custom_providers = [
        OpenAICompatibleCustomProvider(id="a", api_token="aaa"),
        OpenAICompatibleCustomProvider(id="b", api_token="bbb"),
    ]
    s.generic.openai_compatible.active_id = "a"
    entry_b = s.generic.openai_compatible.custom_providers[1]
    assert resolve_llm_token_for_entry("openai_compatible", s, entry_b) == "bbb"


def test_entry_token_openai_compatible_none_entry():
    s = Settings()
    assert resolve_llm_token_for_entry("openai_compatible", s, None) is None


def test_resolve_llm_token_unchanged_still_uses_active_id():
    """Guard: the original resolver must keep its active_id semantics so the
    discovery path / existing callers are unaffected."""
    s = Settings()
    s.generic.openai_compatible.custom_providers = [
        OpenAICompatibleCustomProvider(id="a", api_token="aaa"),
        OpenAICompatibleCustomProvider(id="b", api_token="bbb"),
    ]
    s.generic.openai_compatible.active_id = "a"
    assert resolve_llm_token("openai_compatible", s) == "aaa"

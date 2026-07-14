"""Unit tests for ``server.generation_target.resolve_generation_target``.

The resolver turns a chat's per-chat overrides (provider/model/context-preset)
plus global Settings into a single normalized target. These are pure-data tests
(no storage) — the only storage-touching step (loading the ContextPreset object)
stays in the route, not the resolver.
"""
from __future__ import annotations

import pytest

from server.models import Chat, OpenAICompatibleCustomProvider, Settings
from server.generation_target import (
    GenerationTargetError,
    chat_provider_slug,
    resolve_generation_target,
)


def _chat(**kw) -> Chat:
    return Chat(contact_id="c", user_id="u", **kw)


def _generic_settings(provider="openrouter") -> Settings:
    """Settings in generic mode with the named provider fully configured."""
    s = Settings(provider_mode="generic")
    s.generic.provider = provider
    cfg = getattr(s.generic, provider)
    cfg.model_id = f"{provider}-model"
    cfg.api_token = f"{provider}-key"
    cfg.context_preset_id = "cp-global"
    cfg.brain_message_role = "user"
    return s


# ---------- inherit (no override) ------------------------------------------


def test_inherit_aer_mode():
    s = Settings()  # provider_mode defaults to aetherroom
    t = resolve_generation_target(_chat(), s)
    assert t.mode == "aetherroom"
    assert t.model == s.default_model
    assert t.context_preset_id is None
    assert t.provider_slug == "aetherroom"


def test_inherit_generic_named():
    s = _generic_settings("openrouter")
    t = resolve_generation_target(_chat(), s)
    assert t.mode == "generic"
    assert t.provider_kind == "openrouter"
    assert t.model == "openrouter-model"
    assert t.context_preset_id == "cp-global"
    assert t.brain_message_role == "user"
    assert t.token == "openrouter-key"
    assert t.provider_slug == "openrouter"


def test_inherit_generic_openai_compatible_uses_active_id():
    s = Settings(provider_mode="generic")
    s.generic.provider = "openai_compatible"
    s.generic.openai_compatible.custom_providers = [
        OpenAICompatibleCustomProvider(id="a", api_token="aaa", model_id="ma",
                                       context_preset_id="cp", base_url="http://a"),
        OpenAICompatibleCustomProvider(id="b", api_token="bbb", model_id="mb",
                                       context_preset_id="cp", base_url="http://b"),
    ]
    s.generic.openai_compatible.active_id = "b"
    t = resolve_generation_target(_chat(), s)
    assert t.provider_kind == "openai_compatible"
    assert t.token == "bbb"
    assert t.model == "mb"
    assert t.provider_slug == "openai_compatible:b"


def test_inherit_generic_openai_compatible_no_active_id_errors():
    s = Settings(provider_mode="generic")
    s.generic.provider = "openai_compatible"
    with pytest.raises(GenerationTargetError) as ei:
        resolve_generation_target(_chat(), s)
    assert ei.value.kind == "no_provider"
    assert ei.value.from_override is False


# ---------- override beats global ------------------------------------------


def test_override_aetherroom_from_generic_global():
    s = _generic_settings("openrouter")
    t = resolve_generation_target(_chat(provider_override="aetherroom"), s)
    assert t.mode == "aetherroom"  # mode is per-chat


def test_override_named_provider_from_aer_global_threads_brain_role():
    # Global is AER; chat pins nanogpt. The resolved brain role must come from
    # nanogpt's config, not the AER default — the bug Correction 2 fixed.
    s = Settings()  # aetherroom global
    s.generic.nanogpt.model_id = "ng-model"
    s.generic.nanogpt.api_token = "ng-key"
    s.generic.nanogpt.context_preset_id = "cp"
    s.generic.nanogpt.brain_message_role = "assistant"
    t = resolve_generation_target(_chat(provider_override="nanogpt"), s)
    assert t.mode == "generic"
    assert t.provider_kind == "nanogpt"
    assert t.brain_message_role == "assistant"


def test_override_openai_compatible_non_active_entry_uses_own_token():
    # The key token-refactor regression: a chat pinning custom entry "b" must
    # use b's own token even though "a" is the global active entry.
    s = Settings(provider_mode="generic")
    s.generic.provider = "openai_compatible"
    s.generic.openai_compatible.custom_providers = [
        OpenAICompatibleCustomProvider(id="a", api_token="aaa", model_id="ma",
                                       context_preset_id="cp", base_url="http://a"),
        OpenAICompatibleCustomProvider(id="b", api_token="bbb", model_id="mb",
                                       context_preset_id="cp", base_url="http://b"),
    ]
    s.generic.openai_compatible.active_id = "a"
    t = resolve_generation_target(_chat(provider_override="openai_compatible:b"), s)
    assert t.token == "bbb"
    assert t.model == "mb"
    assert t.base_url == "http://b"


def test_model_override_beats_provider_default_generic():
    s = _generic_settings("openrouter")
    t = resolve_generation_target(
        _chat(model_overrides={"openrouter": "my-model"}), s,
    )
    assert t.model == "my-model"


def test_model_override_keyed_by_other_provider_is_ignored():
    # A model stored under a different provider slug must not leak into the
    # resolved provider (per-provider memory).
    s = _generic_settings("openrouter")
    t = resolve_generation_target(
        _chat(model_overrides={"nanogpt": "ng-only"}), s,
    )
    assert t.model == "openrouter-model"  # falls back to provider default


def test_model_override_aer():
    s = Settings()
    t = resolve_generation_target(
        _chat(model_overrides={"aetherroom": "aer-x"}), s,
    )
    assert t.mode == "aetherroom"
    assert t.model == "aer-x"


def test_context_preset_override_beats_provider_default():
    s = _generic_settings("openrouter")
    t = resolve_generation_target(_chat(context_preset_override="cp-chat"), s)
    assert t.context_preset_id == "cp-chat"


def test_context_preset_override_ignored_for_aer():
    s = Settings()
    t = resolve_generation_target(
        _chat(provider_override="aetherroom", context_preset_override="cp-x"), s,
    )
    assert t.context_preset_id is None


# ---------- error cases -----------------------------------------------------


def test_deleted_custom_override_errors_from_override():
    s = Settings(provider_mode="generic")
    t_chat = _chat(provider_override="openai_compatible:ghost")
    with pytest.raises(GenerationTargetError) as ei:
        resolve_generation_target(t_chat, s)
    assert ei.value.kind == "no_provider"
    assert ei.value.from_override is True


def test_bare_openai_compatible_override_errors():
    s = Settings(provider_mode="generic")
    with pytest.raises(GenerationTargetError) as ei:
        resolve_generation_target(_chat(provider_override="openai_compatible"), s)
    assert ei.value.kind == "no_provider"
    assert ei.value.from_override is True


def test_generic_override_no_model_errors():
    s = Settings()  # nanogpt has empty model_id
    s.generic.nanogpt.api_token = "ng-key"
    s.generic.nanogpt.context_preset_id = "cp"
    with pytest.raises(GenerationTargetError) as ei:
        resolve_generation_target(_chat(provider_override="nanogpt"), s)
    assert ei.value.kind == "no_provider"


def test_generic_override_no_context_preset_errors():
    s = Settings()
    s.generic.nanogpt.model_id = "ng-model"
    s.generic.nanogpt.api_token = "ng-key"
    # context_preset_id left unset
    with pytest.raises(GenerationTargetError) as ei:
        resolve_generation_target(_chat(provider_override="nanogpt"), s)
    assert ei.value.kind == "no_preset"


def test_generic_override_no_token_errors():
    s = Settings()
    s.generic.openrouter.model_id = "or-model"
    s.generic.openrouter.context_preset_id = "cp"
    # api_token left unset
    with pytest.raises(GenerationTargetError) as ei:
        resolve_generation_target(_chat(provider_override="openrouter"), s)
    assert ei.value.kind == "no_token"


# ---------- slug helper -----------------------------------------------------


def test_chat_provider_slug_helper():
    s = _generic_settings("openrouter")
    assert chat_provider_slug(_chat(), s) == "openrouter"
    assert chat_provider_slug(_chat(provider_override="nanogpt"), s) == "nanogpt"
    # Unresolvable -> None (best-effort).
    s2 = Settings(provider_mode="generic")
    s2.generic.provider = "openai_compatible"
    assert chat_provider_slug(_chat(), s2) == "openai_compatible:"
    assert chat_provider_slug(_chat(provider_override="openai_compatible:ghost"), s2) is None

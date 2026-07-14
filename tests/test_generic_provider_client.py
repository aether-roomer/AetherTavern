"""Generic-mode provider client.

Cache wiring is per-provider: OR uses top-level ``cache_control`` +
``provider.allow_fallbacks: false``; NGPT uses body-level ``promptCaching``;
NAI generic and openai_compatible send no cache fields. Reasoning is
extracted from both ``delta.reasoning`` and ``delta.reasoning_content``
shapes so we cover the two upstream conventions we've seen.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from server.inference_generic import (
    DeltaEvent,
    DoneEvent,
    GenerationParams,
    ReasoningDeltaEvent,
    StartEvent,
    UsageEvent,
    _build_request_body,
    _extract_reasoning,
    _ttl_label,
)


def test_ttl_label_maps_minutes_to_provider_strings():
    assert _ttl_label(None) is None
    assert _ttl_label(0) is None
    assert _ttl_label(5) == "5m"
    assert _ttl_label(60) == "1h"
    # Anything <30 rounds to 5m; >=30 to 1h.
    assert _ttl_label(15) == "5m"
    assert _ttl_label(45) == "1h"


def test_openrouter_cache_control_top_level_with_fallbacks_off():
    body = _build_request_body(
        [{"role": "user", "content": "hi"}],
        GenerationParams(model="m"),
        "openrouter",
        cache_minutes=5,
    )
    assert body["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert body["provider"]["allow_fallbacks"] is False


def test_openrouter_no_cache_when_disabled():
    body = _build_request_body(
        [{"role": "user", "content": "hi"}],
        GenerationParams(model="m"),
        "openrouter",
        cache_minutes=None,
    )
    assert "cache_control" not in body
    # provider.allow_fallbacks is also untouched when caching is off — we
    # only stick to a single provider when we'd otherwise blow a cache.
    assert "provider" not in body or "allow_fallbacks" not in body.get("provider", {})


def test_nanogpt_prompt_caching_body_level():
    body = _build_request_body(
        [{"role": "user", "content": "hi"}],
        GenerationParams(model="m"),
        "nanogpt",
        cache_minutes=60,
    )
    assert body["promptCaching"] == {"enabled": True, "ttl": "1h"}
    # No OR-shaped cache_control or provider block.
    assert "cache_control" not in body
    assert "provider" not in body


def test_novelai_sends_no_cache_fields():
    body = _build_request_body(
        [{"role": "user", "content": "hi"}],
        GenerationParams(model="m"),
        "novelai",
        cache_minutes=5,
    )
    assert "cache_control" not in body
    assert "promptCaching" not in body
    assert "provider" not in body


def test_openai_compatible_sends_no_cache_fields():
    body = _build_request_body(
        [{"role": "user", "content": "hi"}],
        GenerationParams(model="m"),
        "openai_compatible",
        cache_minutes=5,
    )
    assert "cache_control" not in body
    assert "promptCaching" not in body


def test_stream_options_include_usage_always_set():
    """Usage stats from the final chunk drive ``Chat.last_context_tokens``
    so the next turn's pre-flight token estimate has an honest baseline."""
    for provider in ("novelai", "openrouter", "nanogpt", "openai_compatible"):
        body = _build_request_body(
            [{"role": "user", "content": "hi"}],
            GenerationParams(model="m"),
            provider,
            cache_minutes=None,
        )
        assert body["stream_options"] == {"include_usage": True}


def test_reasoning_extraction_top_level_field():
    """OR / NGPT convention: ``delta.reasoning`` is a top-level string."""
    delta = {"content": "answer text", "reasoning": "step 1 step 2"}
    assert _extract_reasoning(delta) == "step 1 step 2"


def test_reasoning_extraction_nested_field():
    """Some providers nest reasoning under ``reasoning_content``."""
    delta = {"content": "answer", "reasoning_content": "step 3"}
    assert _extract_reasoning(delta) == "step 3"


def test_reasoning_extraction_prefers_top_level_when_both_present():
    """Doesn't really happen in the wild, but defining the contract."""
    delta = {"reasoning": "top", "reasoning_content": "nested"}
    assert _extract_reasoning(delta) == "top"


def test_reasoning_extraction_empty_when_absent():
    delta = {"content": "answer"}
    assert _extract_reasoning(delta) == ""

"""HTTPX-backed streaming client for OpenAI-compatible ``/v1/chat/completions``.

Generic mode counterpart to :mod:`server.inference`. One module covers all
four supported providers (NovelAI generic / OpenRouter / NanoGPT /
openai-compatible) — they differ only in URL + auth + a thin cache-config
layer, so a switch on ``provider`` for the cache wiring is enough.

Always streams regardless of the caller's UI "streaming" toggle. Streaming
keeps long generations from timing out and gives us one codepath; the
caller's toggle decides whether the UI fills the bubble incrementally
or after ``done``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Optional

import httpx


log = logging.getLogger("aether.inference_generic")


Provider = Literal["novelai", "openrouter", "nanogpt", "openai_compatible"]


@dataclass(frozen=True)
class GenerationParams:
    """Sampling parameters forwarded to the upstream.

    Mirrors the AER ``CompletionParams`` shape but swaps the AER-specific
    ``min_p`` / ``top_k`` defaults for chat-completions-friendly values.
    Providers that don't recognise a field tend to ignore it silently;
    OR/NGPT pass-through unknown fields by default, NAI returns 400 on
    unrecognised keys so it stays at the documented set.
    """
    model: str
    temperature: float = 0.85
    top_p: float = 0.95
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    max_tokens: int = 1536
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None


class InferenceError(RuntimeError):
    """Surface upstream / configuration / cache errors to the caller."""


# ---------------------------------------------------------------------------
# Provider events
# ---------------------------------------------------------------------------


@dataclass
class StartEvent:
    kind: str = "start"
    model: Optional[str] = None
    request_id: Optional[str] = None


@dataclass
class DeltaEvent:
    text: str
    kind: str = "delta"


@dataclass
class ReasoningDeltaEvent:
    text: str
    kind: str = "reasoning_delta"


@dataclass
class UsageEvent:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    kind: str = "usage"


@dataclass
class DoneEvent:
    finish_reason: Optional[str] = None
    kind: str = "done"


@dataclass
class ErrorEvent:
    message: str
    error_kind: str  # auth | rate_limit | upstream | network | cache | format
    kind: str = "error"


ProviderEvent = StartEvent | DeltaEvent | ReasoningDeltaEvent | UsageEvent | DoneEvent | ErrorEvent


# ---------------------------------------------------------------------------
# URL + request-body assembly
# ---------------------------------------------------------------------------


def _normalise_chat_url(base_url: str) -> str:
    """Accept either a base URL or a full ``/v1/chat/completions`` URL."""
    base = (base_url or "").rstrip("/")
    if not base:
        raise InferenceError("Provider base URL is not configured.")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _ttl_label(cache_minutes: Optional[int]) -> Optional[str]:
    """Map UI cache-minutes value to a provider TTL string.

    The cache-hints endpoint returns ``5`` / ``60`` for the only two
    supported windows on OR + NGPT today; ``None`` means caching is
    disabled. Other values aren't reachable from the UI but degrade
    gracefully — anything <30min rounds to ``"5m"``, >=30min to ``"1h"``.
    """
    if cache_minutes is None or cache_minutes <= 0:
        return None
    if cache_minutes < 30:
        return "5m"
    return "1h"


def _build_request_body(
    api_messages: list[dict],
    params: GenerationParams,
    provider: Provider,
    cache_minutes: Optional[int],
) -> dict:
    """Assemble the JSON request body with per-provider cache wiring."""
    body: dict = {
        "model": params.model,
        "messages": list(api_messages),
        "max_tokens": params.max_tokens,
        "temperature": params.temperature,
        "top_p": params.top_p,
        "stream": True,
        # Request usage stats on the final chunk so the route can populate
        # ``Chat.last_context_tokens`` honestly. OpenAI-compatible upstreams
        # widely support this option; providers that don't recognise it
        # simply ignore it.
        "stream_options": {"include_usage": True},
    }
    if params.top_k is not None:
        body["top_k"] = params.top_k
    if params.min_p is not None:
        body["min_p"] = params.min_p
    # Repetition penalties — only emit when explicitly set (non-None and
    # non-zero) so we don't poke providers that interpret a literal 0.0
    # as "disable sampling" or otherwise flinch at unfamiliar fields.
    if params.presence_penalty:
        body["presence_penalty"] = params.presence_penalty
    if params.frequency_penalty:
        body["frequency_penalty"] = params.frequency_penalty

    ttl = _ttl_label(cache_minutes)
    if provider == "openrouter" and ttl is not None:
        # Top-level cache_control + sticky provider routing so a fallback
        # doesn't invalidate the cache. OR auto-advances the cache breakpoint
        # as the conversation grows for Anthropic-family models, so a
        # top-level marker effectively caches everything up to the latest
        # turn — exactly what we want for long roleplays.
        body["cache_control"] = {"type": "ephemeral", "ttl": ttl}
        body.setdefault("provider", {})["allow_fallbacks"] = False
    elif provider == "nanogpt" and ttl is not None:
        # NGPT's helper handles Anthropic ``cache_control`` block placement
        # internally — no ``anthropic-beta`` header needed when using this
        # body field.
        body["promptCaching"] = {"enabled": True, "ttl": ttl}
    # NAI generic and openai_compatible: no cache hooks.
    return body


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _extract_reasoning(delta: dict) -> str:
    """Pull reasoning text from a delta dict.

    Two shapes seen in the wild:
    - ``delta.reasoning`` (top-level, OR / NGPT / Together convention).
    - ``delta.reasoning_content`` (nested under ``content`` shape used by
      some upstream providers).
    Empty / non-string → empty string.
    """
    val = delta.get("reasoning")
    if isinstance(val, str) and val:
        return val
    val = delta.get("reasoning_content")
    if isinstance(val, str) and val:
        return val
    return ""


def _classify_status(status_code: int) -> str:
    if status_code in (401, 403):
        return "auth"
    if status_code == 429:
        return "rate_limit"
    return "upstream"


async def stream_chat(
    api_messages: list[dict],
    model: str,
    base_url: str,
    token: str,
    params: GenerationParams,
    provider: Provider,
    cache_minutes: Optional[int],
    cancel_event: Optional[asyncio.Event] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> AsyncIterator[ProviderEvent]:
    """Stream chat-completions events from any of the four providers.

    Yields :class:`ProviderEvent` variants. The caller translates these
    into SSE events for the browser. Always streams regardless of the
    UI streaming toggle (see module docstring).

    When ``client`` is omitted, the httpx client is built via
    :func:`server.proxy_rules.make_async_client` so any proxy rules bound
    by :func:`server.proxy_rules.proxy_rules_scope` apply. The
    ``client=`` override is for tests.
    """
    if not token:
        yield ErrorEvent(
            message="No API token configured for this provider.",
            error_kind="auth",
        )
        return

    try:
        url = _normalise_chat_url(base_url)
    except InferenceError as e:
        yield ErrorEvent(message=str(e), error_kind="upstream")
        return

    # Resolve model: caller may pass an explicit ``model`` override, but
    # default to ``params.model``. This split exists so the persist seam
    # can record the resolved id without depending on params plumbing.
    resolved_model = model or params.model
    request_params = params if params.model == resolved_model else GenerationParams(
        model=resolved_model,
        temperature=params.temperature,
        top_p=params.top_p,
        top_k=params.top_k,
        min_p=params.min_p,
        max_tokens=params.max_tokens,
    )
    body = _build_request_body(api_messages, request_params, provider, cache_minutes)
    headers: dict[str, str] = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    timeout = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)

    owns_client = client is None
    if owns_client:
        from server.proxy_rules import make_async_client
        client = make_async_client("generic_llm", timeout=timeout)
    assert client is not None

    yield StartEvent(model=resolved_model)

    try:
        try:
            async with client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    raw = await response.aread()
                    detail = raw.decode("utf-8", errors="replace")[:500]
                    yield ErrorEvent(
                        message=f"Upstream {response.status_code}: {detail}",
                        error_kind=_classify_status(response.status_code),
                    )
                    return
                finish_reason: Optional[str] = None
                async for line in response.aiter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        log.warning("Skipping malformed SSE payload: %r", payload[:120])
                        continue
                    usage = chunk.get("usage")
                    if usage:
                        yield UsageEvent(
                            prompt_tokens=int(usage.get("prompt_tokens") or 0),
                            completion_tokens=int(usage.get("completion_tokens") or 0),
                            total_tokens=int(usage.get("total_tokens") or 0),
                        )
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if not isinstance(delta, dict):
                        continue
                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        yield DeltaEvent(text=text)
                    reasoning = _extract_reasoning(delta)
                    if reasoning:
                        yield ReasoningDeltaEvent(text=reasoning)
                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = fr
                yield DoneEvent(finish_reason=finish_reason)
        except httpx.RequestError as e:
            yield ErrorEvent(message=f"Network error: {e}", error_kind="network")
    finally:
        if owns_client:
            await client.aclose()

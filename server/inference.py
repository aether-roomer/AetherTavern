"""HTTPX-backed streaming client for OpenAI-compatible ``/v1/completions``.

We hit the *raw text* completions endpoint, not chat completions — chat
templating is done server-side so we can apply the AER ``<|system|>`` patch
ourselves and feed the upstream a single raw prompt string.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx


log = logging.getLogger("aether.inference")


@dataclass(frozen=True)
class CompletionParams:
    model: str
    temperature: float = 0.85
    top_p: float = 0.95
    top_k: int = 250
    min_p: float = 0.0
    max_tokens: int = 1536


class InferenceError(RuntimeError):
    """Surface upstream errors / configuration problems to the caller."""


def _build_request_body(prompt: str, params: CompletionParams) -> dict:
    body: dict = {
        "model": params.model,
        "prompt": prompt,
        "max_tokens": params.max_tokens,
        "temperature": params.temperature,
        "top_p": params.top_p,
        "stream": True,
    }
    # ``top_k`` and ``min_p`` aren't standard OpenAI fields — we send them
    # always and let the upstream reject if it can't handle them.
    body["top_k"] = params.top_k
    body["min_p"] = params.min_p
    return body


async def stream_completion(
    *,
    endpoint_url: str,
    api_token: str,
    prompt: str,
    params: CompletionParams,
    stop_event: asyncio.Event | None = None,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[str]:
    """Yield text deltas from a streaming completions endpoint.

    Stops early (without raising) when ``stop_event`` is set between deltas.
    Raises :class:`InferenceError` on configuration / upstream / network errors.
    Raises :class:`server.proxy_rules.NoMatchingProxyRule` when a proxy
    rules file is in scope and no rule matches the endpoint host.

    When ``client`` is omitted, the client is built via
    :func:`server.proxy_rules.make_async_client`, which honours any
    proxy rules bound by :func:`server.proxy_rules.proxy_rules_scope`
    in the caller's async context. The ``client=`` override is kept
    for tests.
    """
    if not endpoint_url:
        raise InferenceError(
            "Inference endpoint URL is not configured. Set it under Settings."
        )
    base = endpoint_url.rstrip("/")
    # Allow the user to paste either the base URL or the full /v1/completions URL.
    url = base if base.endswith("/v1/completions") else f"{base}/v1/completions"
    headers: dict[str, str] = {"Accept": "text/event-stream"}
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
    body = _build_request_body(prompt, params)
    timeout = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)

    owns_client = client is None
    if owns_client:
        from server.proxy_rules import make_async_client
        client = make_async_client("aetherroom", timeout=timeout)
    assert client is not None
    try:
        try:
            async with client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code >= 400:
                    raw = await response.aread()
                    detail = raw.decode("utf-8", errors="replace")[:500]
                    raise InferenceError(f"Upstream {response.status_code}: {detail}")
                async for line in response.aiter_lines():
                    if stop_event is not None and stop_event.is_set():
                        return
                    if not line:
                        continue
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):].strip()
                    if payload == "[DONE]":
                        return
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        log.warning("Skipping malformed SSE payload: %r", payload[:120])
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    text = choices[0].get("text") or ""
                    if text:
                        yield text
        except httpx.RequestError as e:
            raise InferenceError(f"Request error: {e}") from e
    finally:
        if owns_client:
            await client.aclose()

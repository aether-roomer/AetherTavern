"""Discovery endpoints for generic (non-AER) LLM providers.

Three routes, all server-side cached through ``app.state.discovery_cache``:

- ``GET /api/generic/models?provider=&refresh=`` — fetches the
  provider's ``/v1/models`` for the dropdown.
- ``GET /api/generic/probe?provider=&refresh=`` — HEAD-only capability
  probe for likely tokenize / token-count endpoints. The Generic
  generation path consumes the result to decide between upstream
  tokenization vs the SillyTavern-style guesstimator.
- ``GET /api/generic/cache-hints?provider=&model=`` — provider-specific
  cache-duration options for the Settings dropdown.

Cache keys include the SHA of the bearer token so a self-hosted
custom OpenAI-compatible endpoint that serves different model lists
per key doesn't leak across users.
"""
from __future__ import annotations

import hashlib
import logging

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from server import storage
from server.models import Settings
from server.proxy_rules import NoMatchingProxyRule, make_async_client, proxy_rules_scope
from server.secrets import resolve_llm_token, resolve_llm_token_for_entry


log = logging.getLogger("aether.generic")


router = APIRouter(prefix="/api/generic", tags=["generic"])


# Unbounded read mirrors the inference timeout — /v1/models can be slow
# on a cold custom endpoint.
_TIMEOUT = httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=15.0)


_EMPTY_PROBE = {
    "has_tokenize": False,
    "has_token_count": False,
    "tokenize_path": None,
    "token_count_path": None,
}


# --- helpers --------------------------------------------------------------


def _active_base_url_and_token(
    provider: str,
    settings: Settings,
    custom_id: str | None = None,
) -> tuple[str, str | None]:
    """Resolve ``(base_url, api_token)`` for a generic provider.

    For named providers (``novelai`` / ``openrouter`` / ``nanogpt``)
    returns that provider's settings. For ``openai_compatible``,
    ``custom_id`` (when given) picks a specific custom entry; otherwise
    the currently-active entry is used. Raises HTTP 400 when no entry
    can be resolved.
    """
    g = settings.generic
    if provider == "novelai":
        return g.novelai.base_url, resolve_llm_token("novelai", settings)
    if provider == "openrouter":
        return g.openrouter.base_url, resolve_llm_token("openrouter", settings)
    if provider == "nanogpt":
        return g.nanogpt.base_url, resolve_llm_token("nanogpt", settings)
    if provider == "openai_compatible":
        target_id = custom_id or g.openai_compatible.active_id
        if not target_id:
            raise HTTPException(400, "No active custom OpenAI-compatible entry.")
        entry = next(
            (p for p in g.openai_compatible.custom_providers if p.id == target_id),
            None,
        )
        if entry is None:
            raise HTTPException(400, "custom_id does not match any custom entry.")
        if not entry.base_url:
            raise HTTPException(400, "Custom entry has no base URL configured.")
        # Use the resolved entry's OWN token — a per-chat override (or just
        # inspecting a non-active card) must discover models against the same
        # credentials generation will use, not the globally-active entry's.
        return entry.base_url, resolve_llm_token_for_entry(
            "openai_compatible", settings, entry,
        )
    raise HTTPException(400, f"Unknown generic provider {provider!r}.")


def _token_hash(tok: str | None) -> str:
    return hashlib.sha256((tok or "").encode("utf-8")).hexdigest()[:16]


# --- /models --------------------------------------------------------------


async def _fetch_models(base_url: str, token: str | None) -> dict:
    url = f"{base_url.rstrip('/')}/v1/models"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with make_async_client("aetherroom", timeout=_TIMEOUT) as client:
        r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"/v1/models failed ({r.status_code}): {r.text[:200]}",
            )
        data = r.json()
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        entries = []
    return {"models": [e for e in entries if isinstance(e, dict)]}


@router.get("/models")
async def list_models(
    request: Request,
    provider: str = Query(...),
    refresh: bool = Query(default=False),
    custom_id: str | None = Query(default=None),
) -> JSONResponse:
    settings = storage.load_settings()
    base_url, token = _active_base_url_and_token(provider, settings, custom_id=custom_id)
    key = ("llm_models", provider, base_url, _token_hash(token))
    try:
        with proxy_rules_scope(getattr(request.app.state, "proxy_rules", None)):
            cache = request.app.state.discovery_cache
            data = await cache.get_or_fetch(
                key, lambda: _fetch_models(base_url, token), force=refresh,
            )
    except httpx.RequestError as e:
        raise HTTPException(504, f"Model discovery network error: {e}") from e
    except NoMatchingProxyRule as e:
        raise HTTPException(502, str(e)) from e
    return JSONResponse(data)


# --- /probe ---------------------------------------------------------------


# Candidate paths and which capability flag they satisfy. First non-404
# response per kind wins.
_PROBE_PATHS = (
    ("tokenize",    "/v1/tokenize"),
    ("tokenize",    "/tokenize"),
    ("token_count", "/v1/token-count"),
)


async def _fetch_probe(base_url: str, _token: str | None) -> dict:
    """HEAD-only capability probe.

    HEAD is cheap, almost never billed, and distinguishes path-missing
    (404) from path-exists-but-different-method (405) and
    path-exists-and-allowed (2xx / other 4xx). We treat any non-404
    response as "path exists" — false positives only downgrade the
    generation path to its existing guesstimator fallback, which is
    the safer failure mode. No auth header — a server that 401s
    HEAD-without-auth still answers with 401 (not 404), which is the
    right "present" signal.
    """
    found = dict(_EMPTY_PROBE)
    async with make_async_client("aetherroom", timeout=_TIMEOUT) as client:
        for kind, path in _PROBE_PATHS:
            try:
                r = await client.head(f"{base_url.rstrip('/')}{path}")
            except httpx.RequestError:
                continue
            if r.status_code == 404:
                continue
            if kind == "tokenize" and not found["has_tokenize"]:
                found["has_tokenize"] = True
                found["tokenize_path"] = path
            elif kind == "token_count" and not found["has_token_count"]:
                found["has_token_count"] = True
                found["token_count_path"] = path
    return found


@router.get("/probe")
async def probe(
    request: Request,
    provider: str = Query(...),
    refresh: bool = Query(default=False),
) -> JSONResponse:
    settings = storage.load_settings()
    try:
        base_url, token = _active_base_url_and_token(provider, settings)
    except HTTPException:
        # Misconfigured (no active entry, etc.) — return graceful
        # no-capabilities. The Settings page is informational here, not
        # a hard error path.
        return JSONResponse(dict(_EMPTY_PROBE))
    key = ("probe", provider, base_url)  # no token in key — shape is auth-agnostic
    try:
        with proxy_rules_scope(getattr(request.app.state, "proxy_rules", None)):
            cache = request.app.state.discovery_cache
            data = await cache.get_or_fetch(
                key, lambda: _fetch_probe(base_url, token), force=refresh,
            )
    except (httpx.RequestError, NoMatchingProxyRule):
        # The probe never surfaces as a hard failure — the generation
        # path retries at gen time and degrades to the guesstimator if
        # upstream behaviour conflicts with the cached probe.
        data = dict(_EMPTY_PROBE)
    return JSONResponse(data)


# --- /cache-hints ---------------------------------------------------------


def _or_cache_options(models_data: dict | None, model_id: str) -> list[dict]:
    """Translate OpenRouter's per-model cache hints into option list.

    OR's ``/v1/models`` exposes two cache-related pricing fields:

    - ``input_cache_read`` — present on every model that benefits from
      caching, *including* implicit-caching providers (OpenAI, Grok,
      DeepSeek, Moonshot, Groq). For these, the provider transparently
      caches and there's no user-selectable TTL — the right UI is "No
      cache" (nothing to configure).
    - ``input_cache_write`` — only present on **explicit-caching**
      providers (Anthropic Claude, Gemini, Alibaba) where the caller
      sets ``cache_control`` with a TTL. These are the models for
      which a duration dropdown is meaningful.

    So we gate on the *write* field, not the read field. No selection,
    unknown model, or read-only cache pricing ⇒ single "No cache"
    option (the frontend hides the row entirely).
    """
    no_cache_only = [{"value": None, "label": "No cache"}]
    if not models_data or not isinstance(models_data, dict):
        return no_cache_only
    if not model_id:
        return no_cache_only
    models = models_data.get("models") or []
    entry = next(
        (m for m in models if isinstance(m, dict) and m.get("id") == model_id),
        None,
    )
    if entry is None:
        return no_cache_only
    pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}
    write_price = pricing.get("input_cache_write")
    try:
        explicit_cache = write_price is not None and float(write_price) > 0
    except (TypeError, ValueError):
        explicit_cache = False
    if explicit_cache:
        return [
            {"value": None, "label": "No cache"},
            {"value": 5,    "label": "5 minutes"},
            {"value": 60,   "label": "60 minutes"},
        ]
    return no_cache_only


def _ngpt_cache_options(model_id: str) -> list[dict]:
    """NGPT exposes user-selectable cache TTLs (5m / 1h) only for the
    Claude family — see ``docs.nano-gpt.com``'s "Prompt caching" page.
    Other models may benefit from implicit provider caching but have
    no user-tunable duration, so the dropdown stays single-option.
    """
    no_cache_only = [{"value": None, "label": "No cache"}]
    if not model_id:
        return no_cache_only
    mid = model_id.lower()
    # Both bare ``claude-*`` ids and ``anthropic/claude-*`` aliases.
    if "claude" in mid or mid.startswith("anthropic/"):
        return [
            {"value": None, "label": "No cache"},
            {"value": 5,    "label": "5 minutes"},
            {"value": 60,   "label": "60 minutes"},
        ]
    return no_cache_only


@router.get("/cache-hints")
async def cache_hints(
    request: Request,
    provider: str = Query(...),
    model: str = Query(default=""),
) -> JSONResponse:
    """Per-provider cache-duration options for the Settings dropdown.

    NGPT and OR both expose per-model caching; NAI generic mirrors AER
    (no cache control); openai-compatible is too provider-shaped to
    predict. OR is derived from its already-cached ``/v1/models`` —
    NGPT uses a static-table heuristic since its ``/v1/models``
    payload doesn't carry per-model cache flags.
    """
    if provider == "openrouter":
        settings = storage.load_settings()
        try:
            base_url, token = _active_base_url_and_token(provider, settings)
        except HTTPException:
            return JSONResponse({
                "options": [{"value": None, "label": "No cache"}],
                "default": None,
            })
        cache = request.app.state.discovery_cache
        key = ("llm_models", provider, base_url, _token_hash(token))
        models_data = cache.peek(key)
        return JSONResponse({
            "options": _or_cache_options(models_data, model),
            "default": None,
        })
    if provider == "nanogpt":
        return JSONResponse({
            "options": _ngpt_cache_options(model),
            "default": None,
        })
    # novelai + openai_compatible — no provider-side cache controls.
    return JSONResponse({
        "options": [{"value": None, "label": "No cache"}],
        "default": None,
    })

"""Text-to-speech proxy + model discovery.

The frontend resolves the effective TTS config (which provider, which
voice, etc.) and sends an explicit GET request. This module is a thin
translator that builds the provider-specific payload, looks up the API
key from ``Settings``, opens a streaming POST to the upstream, and
pipes the audio bytes back to the browser. The browser plays the
result through a plain ``<audio>`` element so we get progressive
playback for free.

Every provider returns MP3 (``audio/mpeg``) — pinning the format keeps
Safari (especially iOS, which doesn't play Opus-in-WebM through
``<audio>``) happy.
"""
from __future__ import annotations

import json as _json
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from server import storage
from server.models import NAI_CUSTOM_SENTINEL, TTSProviderKind
from server.proxy_rules import NoMatchingProxyRule, make_async_client, proxy_rules_scope
from server.tts.keys import (
    TTSKeyMissing,
    TTSUnknownProvider,
    resolve_key_and_url,
)
from server.tts.nai_voices import NAI_V1_VOICE_IDS, NAI_V2_VOICE_SEEDS


log = logging.getLogger("aether.tts")


router = APIRouter(prefix="/api/tts", tags=["tts"])


# Same timeout shape as ``inference.py`` — unbounded ``read`` so the
# streaming response can take however long the upstream needs.
_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=15.0, pool=15.0)


_OR_MODELS_URL = "https://openrouter.ai/api/v1/models?output_modalities=speech"
_NGPT_MODELS_URL = "https://nano-gpt.com/api/v1/audio-models?type=tts"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _normalise_or_entry(raw: dict) -> dict:
    voices = raw.get("supported_voices") or []
    params = raw.get("supported_parameters") or []
    return {
        "id": raw.get("id") or "",
        "name": raw.get("name") or raw.get("id") or "",
        "description": raw.get("description") or "",
        "voices": [v for v in voices if isinstance(v, str)],
        "speed_supported": "speed" in (params if isinstance(params, list) else []),
        "max_chars": None,
    }


def _normalise_ngpt_entry(raw: dict) -> dict:
    params = raw.get("supported_parameters") or {}
    voices = params.get("voices") if isinstance(params, dict) else None
    max_chars = params.get("max_chars") if isinstance(params, dict) else None
    return {
        "id": raw.get("id") or "",
        "name": raw.get("name") or raw.get("id") or "",
        "description": raw.get("description") or "",
        "voices": [v for v in (voices or []) if isinstance(v, str)],
        # NGPT's documented per-model speed support: tts-1, tts-1-hd,
        # Kokoro, Elevenlabs-Turbo-V2.5. We don't ship a hardcoded
        # allowlist (it'd rot fast); we just always show the slider for
        # NGPT and let the upstream silently ignore it where it doesn't
        # apply. Same convention as the OpenAI spec recommends.
        "speed_supported": True,
        "max_chars": max_chars if isinstance(max_chars, int) else None,
    }


async def _fetch_openrouter_models() -> dict:
    async with make_async_client("tts", timeout=_TIMEOUT) as client:
        r = await client.get(_OR_MODELS_URL)
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"OpenRouter discovery failed ({r.status_code}): {r.text[:200]}",
            )
        data = r.json()
    entries = data.get("data") or []
    return {"models": [_normalise_or_entry(e) for e in entries if isinstance(e, dict)]}


async def _fetch_nanogpt_models() -> dict:
    async with make_async_client("tts", timeout=_TIMEOUT) as client:
        r = await client.get(_NGPT_MODELS_URL)
        if r.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"NanoGPT discovery failed ({r.status_code}): {r.text[:200]}",
            )
        data = r.json()
    entries = data.get("data") or []
    return {"models": [_normalise_ngpt_entry(e) for e in entries if isinstance(e, dict)]}


@router.get("/openrouter/models")
async def openrouter_models(
    request: Request,
    refresh: bool = Query(default=False),
) -> JSONResponse:
    try:
        with proxy_rules_scope(getattr(request.app.state, "proxy_rules", None)):
            cache = request.app.state.discovery_cache
            data = await cache.get_or_fetch(
                ("tts_models", "openrouter"),
                _fetch_openrouter_models,
                force=refresh,
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=504, detail=f"OpenRouter discovery network error: {e}") from e
    except NoMatchingProxyRule as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return JSONResponse(data)


@router.get("/nanogpt/models")
async def nanogpt_models(
    request: Request,
    refresh: bool = Query(default=False),
) -> JSONResponse:
    try:
        with proxy_rules_scope(getattr(request.app.state, "proxy_rules", None)):
            cache = request.app.state.discovery_cache
            data = await cache.get_or_fetch(
                ("tts_models", "nanogpt"),
                _fetch_nanogpt_models,
                force=refresh,
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=504, detail=f"NanoGPT discovery network error: {e}") from e
    except NoMatchingProxyRule as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return JSONResponse(data)


# --------------------------------------------------------------------------
# Speak — translate to upstream payload, stream audio
# --------------------------------------------------------------------------


def _build_nai_payload(text: str, version: str, voice: str, custom_seed: str) -> dict:
    """Translate our unified voice picker into NovelAI's request body."""
    if version == "v1":
        if voice == NAI_CUSTOM_SENTINEL:
            if not custom_seed:
                raise HTTPException(
                    status_code=400,
                    detail="NovelAI V1 custom voice requires a non-empty seed.",
                )
            return {
                "text": text,
                "voice": -1,
                "seed": custom_seed,
                "opus": False,
                "version": "v1",
            }
        voice_id = NAI_V1_VOICE_IDS.get(voice)
        if voice_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown NovelAI v1 voice {voice!r}.",
            )
        # NAI rejects an empty `seed` even when `voice` is a preset id.
        # ``kurumuz12`` is the convention used by their own client for
        # the placeholder seed when a preset voice is selected.
        return {
            "text": text,
            "voice": voice_id,
            "seed": "kurumuz12",
            "opus": False,
            "version": "v1",
        }
    # v2 — voice is always -1, seed drives.
    if voice == NAI_CUSTOM_SENTINEL:
        seed = custom_seed
    else:
        seed = NAI_V2_VOICE_SEEDS.get(voice)
        if seed is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown NovelAI v2 voice {voice!r}.",
            )
    return {
        "text": text,
        "voice": -1,
        "seed": seed,
        "opus": False,
        "version": "v2",
    }


def _build_openai_payload(text: str, model: str, voice: str, speed: float) -> dict:
    """Standard OpenAI ``/v1/audio/speech`` shape — used by OR + Generic."""
    return {
        "model": model,
        "input": text,
        "voice": voice,
        "response_format": "mp3",
        "speed": speed,
    }


def _build_ngpt_payload(text: str, model: str, voice: str, speed: float) -> dict:
    """NanoGPT shape — ``text`` (not ``input``), plus ``stream`` for the
    OpenAI-family models that support it."""
    return {
        "text": text,
        "model": model,
        "voice": voice,
        "response_format": "mp3",
        "speed": speed,
        "stream": True,
    }


async def _proxy_binary(
    method: str,
    url: str,
    *,
    json: dict | None = None,
    headers: dict[str, str],
) -> StreamingResponse:
    """Open the upstream POST and return a StreamingResponse that pipes
    bytes through.

    Handles the NGPT JSON-{audioUrl} fallback: if the upstream answers
    with ``Content-Type: application/json``, we read the JSON, follow
    the ``audioUrl``, and stream those bytes back instead.

    Expects a :func:`proxy_rules_scope` to be active in the caller so
    :func:`make_async_client` can resolve the right proxy rules.
    """
    client = make_async_client("tts", timeout=_TIMEOUT)
    try:
        upstream_ctx = client.stream(method, url, json=json, headers=headers)
        upstream = await upstream_ctx.__aenter__()

        # Translate upstream errors before we commit to a streaming
        # response — once we return StreamingResponse there's no way to
        # surface an HTTP error code to the browser.
        if upstream.status_code == 202:
            await upstream_ctx.__aexit__(None, None, None)
            await client.aclose()
            raise HTTPException(
                status_code=502,
                detail=(
                    "Upstream returned 202 (async polling) — not supported "
                    "in this version. Pick a model that streams directly."
                ),
            )
        if upstream.status_code >= 400:
            raw = await upstream.aread()
            detail = raw.decode("utf-8", errors="replace")[:500]
            await upstream_ctx.__aexit__(None, None, None)
            await client.aclose()
            raise HTTPException(
                status_code=502,
                detail=f"Upstream {upstream.status_code}: {detail}",
            )

        upstream_ct = upstream.headers.get("content-type", "").lower()

        if upstream_ct.startswith("application/json"):
            # NGPT non-streaming path: read the small JSON, follow
            # audioUrl. Close the upstream streaming context first.
            raw_json = await upstream.aread()
            await upstream_ctx.__aexit__(None, None, None)
            try:
                doc = _json.loads(raw_json.decode("utf-8", errors="replace"))
            except Exception:
                await client.aclose()
                raise HTTPException(
                    status_code=502,
                    detail="Upstream returned JSON we could not parse.",
                )
            audio_url = (
                doc.get("audioUrl")
                if isinstance(doc, dict)
                else None
            )
            if not audio_url:
                await client.aclose()
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "Upstream JSON did not include an audioUrl. "
                        "Pick a model that streams directly."
                    ),
                )
            return await _proxy_secondary(client, audio_url)

        # Happy path: binary audio. Stream it back.
        out_ct = upstream_ct or "audio/mpeg"

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    if chunk:
                        yield chunk
            finally:
                await upstream_ctx.__aexit__(None, None, None)
                await client.aclose()

        return StreamingResponse(
            gen(),
            media_type=out_ct,
            headers={"Cache-Control": "no-store"},
        )
    except HTTPException:
        raise
    except httpx.RequestError as e:
        await client.aclose()
        raise HTTPException(status_code=504, detail=f"Upstream network error: {e}") from e
    except NoMatchingProxyRule as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=str(e)) from e


async def _proxy_secondary(
    client: httpx.AsyncClient, audio_url: str
) -> StreamingResponse:
    """Second leg of NGPT's JSON path: GET the audioUrl, stream it."""
    upstream_ctx = client.stream("GET", audio_url)
    upstream = await upstream_ctx.__aenter__()
    if upstream.status_code >= 400:
        raw = await upstream.aread()
        detail = raw.decode("utf-8", errors="replace")[:500]
        await upstream_ctx.__aexit__(None, None, None)
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"Upstream audioUrl {upstream.status_code}: {detail}",
        )
    out_ct = upstream.headers.get("content-type", "audio/mpeg") or "audio/mpeg"

    async def gen() -> AsyncIterator[bytes]:
        try:
            async for chunk in upstream.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await upstream_ctx.__aexit__(None, None, None)
            await client.aclose()

    return StreamingResponse(
        gen(),
        media_type=out_ct,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/speak")
async def speak(
    request: Request,
    api: str = Query(...),
    text: str = Query(...),
    voice: str = Query(""),
    model: str = Query(""),
    version: str = Query(""),
    custom_seed: str = Query(""),
    custom_id: str = Query(""),
    speed: float = Query(1.0, ge=0.25, le=4.0),
) -> StreamingResponse:
    if not text:
        raise HTTPException(status_code=400, detail="`text` is required.")

    settings = storage.load_settings()
    try:
        api_key, upstream_url = resolve_key_and_url(api, custom_id, settings)
    except TTSKeyMissing as e:
        raise HTTPException(status_code=412, detail=str(e)) from e
    except TTSUnknownProvider as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    with proxy_rules_scope(getattr(request.app.state, "proxy_rules", None)):
        if api == TTSProviderKind.NOVELAI.value:
            if version not in {"v1", "v2"}:
                raise HTTPException(status_code=400, detail="NovelAI requires `version=v1|v2`.")
            if not voice:
                raise HTTPException(status_code=400, detail="NovelAI requires `voice`.")
            if voice == NAI_CUSTOM_SENTINEL and not custom_seed:
                raise HTTPException(
                    status_code=400,
                    detail="NovelAI custom voice requires a `custom_seed`.",
                )
            body = _build_nai_payload(text, version, voice, custom_seed)
            headers = {"Authorization": f"Bearer {api_key}"}
            return await _proxy_binary("POST", upstream_url, json=body, headers=headers)

        if api == TTSProviderKind.OPENROUTER.value:
            if not model:
                raise HTTPException(status_code=400, detail="OpenRouter requires `model`.")
            if not voice:
                raise HTTPException(status_code=400, detail="OpenRouter requires `voice`.")
            body = _build_openai_payload(text, model, voice, speed)
            headers = {"Authorization": f"Bearer {api_key}"}
            return await _proxy_binary("POST", upstream_url, json=body, headers=headers)

        if api == TTSProviderKind.NANOGPT.value:
            if not model:
                raise HTTPException(status_code=400, detail="NanoGPT requires `model`.")
            body = _build_ngpt_payload(text, model, voice, speed)
            headers = {"x-api-key": api_key}
            return await _proxy_binary("POST", upstream_url, json=body, headers=headers)

        if api == TTSProviderKind.GENERIC.value:
            if not model:
                raise HTTPException(status_code=400, detail="Generic TTS requires `model`.")
            if not voice:
                raise HTTPException(status_code=400, detail="Generic TTS requires `voice`.")
            body = _build_openai_payload(text, model, voice, speed)
            headers = {"Authorization": f"Bearer {api_key}"}
            return await _proxy_binary("POST", upstream_url, json=body, headers=headers)

        # resolve_key_and_url would have already raised on an unknown api,
        # but keep a tail in case of future drift.
        raise HTTPException(status_code=400, detail=f"Unknown api {api!r}.")

"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from server import storage
from server.aer.tokenizer import load_tokenizer
from server.discovery_cache import DiscoveryCache
from server.proxy_rules import ProxyRules
from server.routers import (
    brain_libraries,
    chats,
    contacts,
    context_presets,
    files,
    generate,
    generic as generic_router,
    import_export,
    presets,
    scenarios,
    settings as settings_router,
    tts,
    users,
)


logging.basicConfig(
    level=os.environ.get("AETHER_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)


STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


log = logging.getLogger("aether.main")


def _load_proxy_rules() -> ProxyRules | None:
    """Resolve the proxy-rules file path with precedence:

    1. ``AETHER_PROXY_RULES`` env var (also set by ``--proxy-rules`` CLI).
    2. ``<data_dir>/proxy_config.yaml`` if it exists.
    3. None — all traffic goes DIRECT (legacy behavior).

    Each loaded path emits an INFO log line so it's obvious which one
    took effect.
    """
    env_path = os.environ.get("AETHER_PROXY_RULES")
    if env_path:
        path = Path(env_path)
        log.info("Loading proxy rules from %s (via AETHER_PROXY_RULES)", path)
        return ProxyRules.load(path)
    default_path = storage.DATA_DIR / "proxy_config.yaml"
    if default_path.is_file():
        log.info("Loading proxy rules from %s (auto-detected)", default_path)
        return ProxyRules.load(default_path)
    log.info("No proxy rules configured; all outbound traffic goes DIRECT.")
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    storage.initialize()
    if os.environ.get("AETHER_SKIP_TOKENIZER") != "1":
        load_tokenizer()
    app.state.proxy_rules = _load_proxy_rules()
    app.state.discovery_cache = DiscoveryCache()
    yield


app = FastAPI(title="AetherTavern", lifespan=lifespan)

app.include_router(settings_router.router)
app.include_router(presets.router)
app.include_router(contacts.router)
app.include_router(users.router)
app.include_router(scenarios.router)
app.include_router(brain_libraries.router)
app.include_router(context_presets.router)
app.include_router(context_presets.macros_router)
app.include_router(chats.router)
app.include_router(generate.router)
app.include_router(generate.contacts_router)
app.include_router(files.router)
app.include_router(import_export.router)
app.include_router(tts.router)
app.include_router(generic_router.router)


# Static frontend
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Force browsers to revalidate /static/ assets — prevents stale JS during dev."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/", response_model=None)
async def root():
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return RedirectResponse("/docs")


# SPA fallback (must come last). Anything that isn't /api/, /static/, /files/,
# /healthz, /docs, or /openapi.json serves the SPA shell so the client-side
# router can take over (e.g. direct visits to ``/contacts/alice-1438d31b``).
@app.get("/{full_path:path}", response_model=None)
async def spa_fallback(full_path: str):
    if full_path.startswith(("api/", "static/", "files/", "healthz", "docs", "openapi.json")):
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    index = STATIC_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return RedirectResponse("/docs")

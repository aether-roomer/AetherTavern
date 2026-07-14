"""Tests for the TTS proxy + key resolver + settings round-trip.

The proxy itself is a thin translator — we mock the upstream via
``httpx.MockTransport`` and confirm that:

  - the right URL + method + headers are sent per-provider,
  - the body payload is correctly translated from our unified query
    params to each upstream's bespoke shape,
  - binary upstream responses are streamed through to the client,
  - NGPT's JSON ``{audioUrl}`` non-streaming response is followed,
  - HTTP 202 (Elevenlabs async polling) surfaces as a clean 502.

Settings PUT exercises the sentinel pattern on every API key —
``__present__`` preserves, ``""`` clears, anything else sets.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from server.main import app
from server.models import Settings, TTSCustomAPI, TTSGlobalMode, TTSSettings
from server import storage
from server.routers.settings import TOKEN_PRESENT_SENTINEL
from server.routers import tts as tts_router
from server.tts.keys import (
    TTSKeyMissing,
    TTSUnknownProvider,
    resolve_key_and_url,
)


# ---------------------------------------------------------------------------
# resolve_key_and_url
# ---------------------------------------------------------------------------


def test_resolve_nai_direct():
    s = Settings()
    s.tts.novelai.api_key = "nai-key"
    key, url = resolve_key_and_url("novelai", "", s)
    assert key == "nai-key"
    assert url.endswith("/ai/generate-voice")


def test_resolve_nai_xialong_fallback():
    s = Settings(api_token="xialong-key")  # no novelai.api_key
    key, _ = resolve_key_and_url("novelai", "", s)
    assert key == "xialong-key"


def test_resolve_nai_missing():
    s = Settings()
    with pytest.raises(TTSKeyMissing):
        resolve_key_and_url("novelai", "", s)


def test_resolve_openrouter():
    s = Settings()
    s.tts.openrouter.api_key = "or-key"
    key, url = resolve_key_and_url("openrouter", "", s)
    assert key == "or-key"
    assert "openrouter.ai/api/v1/audio/speech" in url


def test_resolve_nanogpt():
    s = Settings()
    s.tts.nanogpt.api_key = "ngpt-key"
    key, url = resolve_key_and_url("nanogpt", "", s)
    assert key == "ngpt-key"
    assert "nano-gpt.com/api/tts" in url


def test_resolve_generic_lookup_by_id():
    entry = TTSCustomAPI(name="Local", base_url="http://x.test/v1", api_key="sk")
    s = Settings(tts=TTSSettings(custom_apis=[entry]))
    key, url = resolve_key_and_url("generic", entry.id, s)
    assert key == "sk"
    assert url == "http://x.test/v1/audio/speech"


def test_resolve_generic_unknown_id():
    s = Settings()
    with pytest.raises(TTSUnknownProvider):
        resolve_key_and_url("generic", "no-such-id", s)


def test_resolve_unknown_api():
    s = Settings()
    with pytest.raises(TTSUnknownProvider):
        resolve_key_and_url("bogus", "", s)


# ---------------------------------------------------------------------------
# Settings PUT — sentinel preservation on every API key field
# ---------------------------------------------------------------------------


def _put_settings(client: TestClient, tts_block: dict):
    r = client.put("/api/settings", json={"tts": tts_block})
    assert r.status_code == 200, r.text
    return r.json()


def test_settings_put_sets_then_preserves_novelai(tmp_storage):
    client = TestClient(app)
    # Set a key
    v = _put_settings(client, {
        "mode": "off",
        "active_kind": "novelai",
        "active_custom_id": "",
        "novelai": {"api_key": "nai-real", "version": "v2"},
        "openrouter": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "nanogpt": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "custom_apis": [],
    })
    assert v["tts"]["novelai"]["api_key_indicator"] == TOKEN_PRESENT_SENTINEL
    assert storage.load_settings().tts.novelai.api_key == "nai-real"

    # Sentinel preserves
    _put_settings(client, {
        "mode": "default_on",
        "active_kind": "novelai",
        "active_custom_id": "",
        "novelai": {"api_key": TOKEN_PRESENT_SENTINEL, "version": "v2"},
        "openrouter": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "nanogpt": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "custom_apis": [],
    })
    s = storage.load_settings()
    assert s.tts.novelai.api_key == "nai-real"
    assert s.tts.mode == TTSGlobalMode.DEFAULT_ON

    # Empty string clears
    _put_settings(client, {
        "mode": "default_on",
        "active_kind": "novelai",
        "active_custom_id": "",
        "novelai": {"api_key": "", "version": "v2"},
        "openrouter": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "nanogpt": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "custom_apis": [],
    })
    assert storage.load_settings().tts.novelai.api_key == ""


def test_settings_put_custom_api_keyed_by_id(tmp_storage):
    client = TestClient(app)
    entry_id = "cust-abc"
    # Create entry with a key
    _put_settings(client, {
        "mode": "off",
        "active_kind": "novelai",
        "active_custom_id": "",
        "novelai": {"api_key": "", "version": "v2"},
        "openrouter": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "nanogpt": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "custom_apis": [{
            "id": entry_id,
            "name": "Local",
            "base_url": "http://x.test/v1",
            "api_key": "sk-real",
            "models": ["tts-1"],
            "voices": {"tts-1": ["alloy"]},
            "default_model": "tts-1",
            "default_voice": "alloy",
            "speed": 1.0,
        }],
    })
    assert storage.load_settings().tts.custom_apis[0].api_key == "sk-real"

    # Sentinel preserves the key while other fields change
    v = _put_settings(client, {
        "mode": "off",
        "active_kind": "generic",
        "active_custom_id": entry_id,
        "novelai": {"api_key": "", "version": "v2"},
        "openrouter": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "nanogpt": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "custom_apis": [{
            "id": entry_id,
            "name": "Renamed",
            "base_url": "http://x.test/v1",
            "api_key": TOKEN_PRESENT_SENTINEL,
            "models": ["tts-1"],
            "voices": {"tts-1": ["alloy"]},
            "default_model": "tts-1",
            "default_voice": "alloy",
            "speed": 1.0,
        }],
    })
    on_disk = storage.load_settings().tts.custom_apis[0]
    assert on_disk.api_key == "sk-real"
    assert on_disk.name == "Renamed"
    assert v["tts"]["custom_apis"][0]["api_key_indicator"] == TOKEN_PRESENT_SENTINEL

    # Removing an entry deletes it
    _put_settings(client, {
        "mode": "off",
        "active_kind": "novelai",
        "active_custom_id": "",
        "novelai": {"api_key": "", "version": "v2"},
        "openrouter": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "nanogpt": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "custom_apis": [],
    })
    assert storage.load_settings().tts.custom_apis == []


def test_settings_view_never_leaks_keys(tmp_storage):
    """GET /api/settings must never expose a raw key."""
    client = TestClient(app)
    s = storage.load_settings()
    s.tts.novelai.api_key = "secret-1"
    s.tts.openrouter.api_key = "secret-2"
    s.tts.nanogpt.api_key = "secret-3"
    s.tts.custom_apis = [
        TTSCustomAPI(name="L", base_url="http://x", api_key="secret-4"),
    ]
    storage.save_settings(s)

    r = client.get("/api/settings")
    blob = r.text
    for needle in ("secret-1", "secret-2", "secret-3", "secret-4"):
        assert needle not in blob, f"raw key {needle} leaked into GET response"


def test_settings_custom_apis_orphan_voices_pruned(tmp_storage):
    """``voices`` keys that don't match a model in ``models[]`` get
    dropped on save — keeps disk state tidy after a rename."""
    client = TestClient(app)
    entry_id = "cust-x"
    _put_settings(client, {
        "mode": "off",
        "active_kind": "novelai",
        "active_custom_id": "",
        "novelai": {"api_key": "", "version": "v2"},
        "openrouter": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "nanogpt": {"api_key": "", "model": "", "voice": "", "speed": 1.0},
        "custom_apis": [{
            "id": entry_id,
            "name": "L", "base_url": "http://x", "api_key": "k",
            "models": ["tts-1"],
            "voices": {"tts-1": ["alloy"], "ghost-model": ["echo"]},
            "default_model": "tts-1",
            "default_voice": "alloy",
            "speed": 1.0,
        }],
    })
    on_disk = storage.load_settings().tts.custom_apis[0]
    assert list(on_disk.voices.keys()) == ["tts-1"]


# ---------------------------------------------------------------------------
# speak proxy — payload translation + binary streaming
# ---------------------------------------------------------------------------


def _install_mock_transport(monkeypatch, handler):
    """Patch tts_router's httpx.AsyncClient to use a MockTransport."""
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    class _Wrap(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(tts_router.httpx, "AsyncClient", _Wrap)
    return transport


def test_speak_nai_v1_preset(tmp_storage, monkeypatch):
    client = TestClient(app)
    s = storage.load_settings()
    s.tts.novelai.api_key = "nai-key"
    storage.save_settings(s)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"\xff\xfb\x90\x00" * 16,
                              headers={"content-type": "audio/mpeg"})

    _install_mock_transport(monkeypatch, handler)

    r = client.get(
        "/api/tts/speak",
        params={"api": "novelai", "text": "hello", "voice": "Cyllene",
                "version": "v1"},
    )
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content.startswith(b"\xff\xfb")  # MP3 frame sync

    assert seen["url"].endswith("/ai/generate-voice")
    assert seen["headers"].get("authorization") == "Bearer nai-key"
    body = seen["body"]
    assert body["version"] == "v1"
    assert body["voice"] == 17        # Cyllene preset id
    assert body["seed"] == "kurumuz12"  # placeholder for V1 preset
    assert body["opus"] is False       # MP3 for Safari compatibility


def test_speak_nai_v1_custom_requires_seed(tmp_storage, monkeypatch):
    client = TestClient(app)
    s = storage.load_settings()
    s.tts.novelai.api_key = "nai-key"
    storage.save_settings(s)

    _install_mock_transport(monkeypatch, lambda r: httpx.Response(
        200, content=b"x", headers={"content-type": "audio/mpeg"}))

    r = client.get("/api/tts/speak", params={
        "api": "novelai", "text": "hi", "voice": "__custom__",
        "version": "v1", "custom_seed": "",
    })
    assert r.status_code == 400
    assert "custom_seed" in r.json()["detail"]


def test_speak_nai_v2_preset_uses_seed(tmp_storage, monkeypatch):
    client = TestClient(app)
    s = storage.load_settings()
    s.tts.novelai.api_key = "nai-key"
    storage.save_settings(s)

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"x", headers={"content-type": "audio/mpeg"})

    _install_mock_transport(monkeypatch, handler)

    client.get("/api/tts/speak", params={
        "api": "novelai", "text": "hi", "voice": "Ligeia", "version": "v2",
    })
    assert seen["body"]["voice"] == -1
    assert seen["body"]["seed"] == "Anananan"   # Ligeia's preset seed


def test_speak_openrouter(tmp_storage, monkeypatch):
    client = TestClient(app)
    s = storage.load_settings()
    s.tts.openrouter.api_key = "or-key"
    storage.save_settings(s)

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"\xff\xfb",
                              headers={"content-type": "audio/mpeg"})

    _install_mock_transport(monkeypatch, handler)

    r = client.get("/api/tts/speak", params={
        "api": "openrouter", "text": "hello",
        "model": "openai/gpt-4o-mini-tts", "voice": "alloy", "speed": 1.25,
    })
    assert r.status_code == 200
    assert "openrouter.ai/api/v1/audio/speech" in seen["url"]
    assert seen["headers"].get("authorization") == "Bearer or-key"
    assert seen["body"]["model"] == "openai/gpt-4o-mini-tts"
    assert seen["body"]["input"] == "hello"
    assert seen["body"]["voice"] == "alloy"
    assert seen["body"]["response_format"] == "mp3"
    assert seen["body"]["speed"] == 1.25


def test_speak_nanogpt_uses_x_api_key_and_text_field(tmp_storage, monkeypatch):
    client = TestClient(app)
    s = storage.load_settings()
    s.tts.nanogpt.api_key = "ngpt-key"
    storage.save_settings(s)

    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"\xff\xfb",
                              headers={"content-type": "audio/mpeg"})

    _install_mock_transport(monkeypatch, handler)

    client.get("/api/tts/speak", params={
        "api": "nanogpt", "text": "hello",
        "model": "tts-1", "voice": "alloy",
    })
    assert "nano-gpt.com/api/tts" in seen["url"]
    # NanoGPT uses ``x-api-key``, NOT Bearer.
    assert seen["headers"].get("x-api-key") == "ngpt-key"
    assert "authorization" not in seen["headers"]
    # Body uses ``text``, not ``input``.
    assert seen["body"]["text"] == "hello"
    assert "input" not in seen["body"]
    assert seen["body"]["stream"] is True


def test_speak_nanogpt_follows_audio_url(tmp_storage, monkeypatch):
    """Some NGPT models answer ``{audioUrl}``; the proxy chases the URL
    and pipes those bytes back to the client."""
    client = TestClient(app)
    s = storage.load_settings()
    s.tts.nanogpt.api_key = "k"
    storage.save_settings(s)

    audio_bytes = b"\xff\xfb" + b"a" * 32

    def handler(request):
        if "audio.example/file.mp3" in str(request.url):
            return httpx.Response(200, content=audio_bytes,
                                  headers={"content-type": "audio/mpeg"})
        # Initial NGPT call returns JSON pointing at audioUrl
        return httpx.Response(
            200,
            content=json.dumps({"audioUrl": "https://audio.example/file.mp3"}).encode(),
            headers={"content-type": "application/json"},
        )

    _install_mock_transport(monkeypatch, handler)

    r = client.get("/api/tts/speak", params={
        "api": "nanogpt", "text": "hi", "model": "kokoro-82m", "voice": "af_alloy",
    })
    assert r.status_code == 200
    assert r.content == audio_bytes


def test_speak_returns_502_on_202(tmp_storage, monkeypatch):
    client = TestClient(app)
    s = storage.load_settings()
    s.tts.nanogpt.api_key = "k"
    storage.save_settings(s)

    _install_mock_transport(monkeypatch, lambda r: httpx.Response(
        202, content=json.dumps({"runId": "abc"}).encode(),
        headers={"content-type": "application/json"}))

    r = client.get("/api/tts/speak", params={
        "api": "nanogpt", "text": "hi", "model": "elevenlabs", "voice": "Sarah",
    })
    assert r.status_code == 502
    assert "async polling" in r.json()["detail"]


def test_speak_412_when_no_key(tmp_storage):
    client = TestClient(app)
    r = client.get("/api/tts/speak", params={
        "api": "openrouter", "text": "x", "model": "m", "voice": "v",
    })
    assert r.status_code == 412
    assert "not configured" in r.json()["detail"].lower()


def test_speak_400_on_missing_required_param(tmp_storage):
    client = TestClient(app)
    s = storage.load_settings()
    s.tts.openrouter.api_key = "or-key"
    storage.save_settings(s)
    # Missing `model` for openrouter
    r = client.get("/api/tts/speak", params={
        "api": "openrouter", "text": "x", "voice": "alloy",
    })
    assert r.status_code == 400

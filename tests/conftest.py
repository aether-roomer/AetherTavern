"""Shared fixtures: per-test isolated storage rooted under ``tmp_path``."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# The tokenizer caches its ~20 MB of files under the data dir, and
# ``tmp_storage`` hands every test a throwaway one. Pin the cache outside
# it so the suite downloads once ever instead of once per run. Set before
# any test imports ``server.aer.tokenizer``.
os.environ.setdefault(
    "AETHER_TOKENIZER_DIR", str(Path(__file__).parent / ".tokenizer-cache")
)

from server import storage
from server.discovery_cache import DiscoveryCache
from server.main import app


@pytest.fixture(autouse=True)
def _reset_discovery_cache():
    """Tests that drive ``TestClient(app)`` without ``with`` skip the
    lifespan, so ``app.state.discovery_cache`` isn't set. This fixture
    installs a fresh cache per test, both providing the attribute and
    isolating state across tests."""
    app.state.discovery_cache = DiscoveryCache()
    yield


@pytest.fixture
def tmp_storage(tmp_path, monkeypatch):
    """Point ``AETHER_DATA_DIR`` at a fresh temp directory for the test, then
    reinitialise the storage index so list/save/get operate on it."""
    monkeypatch.setenv("AETHER_DATA_DIR", str(tmp_path))
    storage._refresh_paths()
    # Recreate the in-memory index against the new dir.
    _reset_index()
    storage.initialize()
    yield tmp_path
    # Tear down: reset paths so unrelated tests don't see this dir.
    monkeypatch.delenv("AETHER_DATA_DIR", raising=False)
    storage._refresh_paths()
    _reset_index()


def _reset_index() -> None:
    storage.index.contacts = {}
    storage.index.users = {}
    storage.index.scenarios = {}
    storage.index.brain_libraries = {}
    storage.index.context_presets = {}
    storage.index.chats = {}
    storage.index.contact_paths = {}
    storage.index.user_paths = {}
    storage.index.scenario_paths = {}
    storage.index.brain_library_paths = {}
    storage.index.context_preset_paths = {}
    storage.index.chat_paths = {}
    storage.index.last_used_contact = {}
    storage.index.last_used_user = {}
    storage.index.last_used_scenario = {}
    storage.index.last_used_library = {}

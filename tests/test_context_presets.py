"""Context-preset CRUD: storage helpers, router endpoints, version handling."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server import storage
from server.main import app
from server.models import (
    ContextPreset,
    ContextPresetAdditionalMessage,
    ContextPresetBlock,
)


@pytest.fixture
def client(tmp_storage, monkeypatch):
    monkeypatch.setenv("AETHER_SKIP_TOKENIZER", "1")
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Storage layer
# ---------------------------------------------------------------------------


def test_storage_round_trip(tmp_storage):
    p = ContextPreset(
        name="My preset",
        description="Notes",
        author="Me",
        system_prompt_blocks=[
            ContextPresetBlock(name="Intro", enabled=True, content="Welcome."),
            ContextPresetBlock(name="Off", enabled=False, content="never"),
        ],
        additional_messages=[
            ContextPresetAdditionalMessage(
                name="Prefill", role="assistant", mode="simple",
                simple_content="…", float_enabled=True, float_depth=0,
            ),
        ],
    )
    storage.save_context_preset(p)
    reloaded = storage.get_context_preset(p.id)
    assert reloaded is not None
    assert reloaded.name == "My preset"
    assert len(reloaded.system_prompt_blocks) == 2
    assert reloaded.system_prompt_blocks[1].enabled is False
    assert reloaded.additional_messages[0].float_enabled is True


def test_storage_directory_naming(tmp_storage):
    p = ContextPreset(name="Special preset")
    storage.save_context_preset(p)
    pdir = storage.context_preset_dir(p.id)
    assert pdir is not None
    assert pdir.name == f"special-preset-{p.id[:8]}"


def test_storage_delete_removes_from_index(tmp_storage):
    p = ContextPreset(name="Trash me")
    storage.save_context_preset(p)
    assert storage.get_context_preset(p.id) is not None
    assert storage.delete_context_preset(p.id) is True
    assert storage.get_context_preset(p.id) is None


def test_storage_save_bumps_version_id(tmp_storage):
    p = ContextPreset(name="X")
    storage.save_context_preset(p)
    v1 = p.version_id
    storage.save_context_preset(p)
    assert p.version_id != v1


def test_storage_save_bump_version_false_preserves_version_id(tmp_storage):
    p = ContextPreset(name="X")
    storage.save_context_preset(p)
    v1 = p.version_id
    storage.save_context_preset(p, bump_version=False)
    assert p.version_id == v1


# ---------------------------------------------------------------------------
# Router endpoints — basic CRUD
# ---------------------------------------------------------------------------


def test_router_create_and_list(client):
    r = client.post("/api/context-presets", json={"name": "From API"})
    assert r.status_code == 200
    p = r.json()
    assert p["id"]
    assert p["name"] == "From API"

    r = client.get("/api/context-presets")
    assert r.status_code == 200
    rows = r.json()
    assert any(x["id"] == p["id"] for x in rows)
    # Summary projection drops block bodies, surfaces counts.
    row = next(x for x in rows if x["id"] == p["id"])
    assert row["block_count"] == 0
    assert row["message_count"] == 0


def test_router_get_returns_full_preset(client):
    r = client.post("/api/context-presets", json={
        "name": "Full",
        "system_prompt_blocks": [
            {"name": "Intro", "enabled": True, "content": "hello"},
        ],
    })
    pid = r.json()["id"]
    r = client.get(f"/api/context-presets/{pid}")
    assert r.status_code == 200
    full = r.json()
    assert len(full["system_prompt_blocks"]) == 1
    assert full["system_prompt_blocks"][0]["content"] == "hello"


def test_router_put_round_trip(client):
    r = client.post("/api/context-presets", json={"name": "X"})
    p = r.json()
    p["description"] = "edited"
    r = client.put(f"/api/context-presets/{p['id']}", json=p)
    assert r.status_code == 200
    assert r.json()["description"] == "edited"


def test_router_put_stale_version_returns_409(client):
    r = client.post("/api/context-presets", json={"name": "Conflict"})
    p = r.json()
    # Mutate behind the back.
    server_copy = storage.get_context_preset(p["id"])
    server_copy.description = "remote change"
    storage.save_context_preset(server_copy)

    p["description"] = "local change"
    r = client.put(f"/api/context-presets/{p['id']}", json=p)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "version_conflict"
    assert detail["current_version_id"]


def test_router_put_semantically_identical_stale_returns_existing(client):
    # When the incoming payload is semantically identical but the
    # version_id is stale, check_version returns False and the route
    # returns ``existing`` rather than 409. The client picks up the fresh
    # version_id without a spurious conflict modal.
    r = client.post("/api/context-presets", json={"name": "Same"})
    p = r.json()
    server_copy = storage.get_context_preset(p["id"])
    # Fresh save bumps version_id but the payload is identical.
    storage.save_context_preset(server_copy)
    # Client PUTs with the OLD version_id but a semantically identical body.
    r = client.put(f"/api/context-presets/{p['id']}", json=p)
    assert r.status_code == 200
    # The returned body should carry the fresh version_id.
    assert r.json()["version_id"] != p["version_id"]


def test_router_delete_404(client):
    r = client.delete("/api/context-presets/no-such-id-32characters00000000")
    assert r.status_code == 404


def test_router_favorite_toggle(client):
    r = client.post("/api/context-presets", json={"name": "Star"})
    pid = r.json()["id"]
    r = client.patch(f"/api/context-presets/{pid}/favorite", json={"favorite": True})
    assert r.status_code == 200
    assert r.json()["favorite"] is True


def test_router_duplicate(client):
    r = client.post("/api/context-presets", json={"name": "Original"})
    pid = r.json()["id"]
    r = client.post(f"/api/context-presets/{pid}/duplicate")
    assert r.status_code == 200
    copy = r.json()
    assert copy["id"] != pid
    # ``next_copy_name`` produces "Original (1)" — distinct from the source.
    assert copy["name"] != "Original"
    assert copy["name"].startswith("Original")


def test_list_ordering_matches_storage(client):
    """Smoke check: the list endpoint surfaces every saved preset."""
    for i in range(3):
        client.post("/api/context-presets", json={"name": f"P{i}"})
    rows = client.get("/api/context-presets").json()
    names = {r["name"] for r in rows}
    # The default seed also lives in here — but only its name.
    assert {"P0", "P1", "P2"} <= names

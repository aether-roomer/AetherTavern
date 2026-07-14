"""Round-trip the generic-mode ``Preset`` size fields and verify the
lazy-migration semantics (old YAML stays byte-identical until next save).
"""
from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from server import storage
from server.main import app
from server.models import Preset


def test_preset_round_trips_new_fields(tmp_storage):
    """A preset PUT with the three new fields persists and re-reads."""
    client = TestClient(app)
    presets = client.get("/api/presets").json()
    pid = presets[0]["id"]

    payload = dict(presets[0])
    payload["max_context_tokens"] = 16384
    payload["rollover_window_tokens"] = 4096
    payload["max_new_tokens"] = 512
    r = client.put(f"/api/presets/{pid}", json=payload)
    assert r.status_code == 200, r.text

    fresh = client.get("/api/presets").json()[0]
    assert fresh["max_context_tokens"] == 16384
    assert fresh["rollover_window_tokens"] == 4096
    assert fresh["max_new_tokens"] == 512


def test_preset_defaults_when_unset_in_yaml(tmp_storage):
    """An old-shape ``presets.yaml`` (no size fields) loads with defaults."""
    old_yaml = yaml.safe_dump({
        "presets": [
            {"id": "old", "name": "Old", "temperature": 0.7,
             "top_p": 0.95, "top_k": 250, "min_p": 0.0},
        ],
    })
    storage.PRESETS_PATH.write_text(old_yaml, encoding="utf-8")

    lib = storage.load_presets()
    assert len(lib.presets) == 1
    p = lib.presets[0]
    assert p.max_context_tokens == 28672
    assert p.rollover_window_tokens == 8192
    assert p.max_new_tokens == 1536


def test_lazy_migration_does_not_touch_old_presets_yaml(tmp_storage):
    """Booting against an old-shape ``presets.yaml`` doesn't rewrite it.

    The size fields come in via Pydantic defaults on load; the file
    stays byte-identical until the user explicitly saves a preset.
    Verifying the no-write invariant prevents accidental boot-time
    churn of the user's hand-edited YAML.
    """
    old_yaml = yaml.safe_dump({
        "presets": [
            {"id": "old", "name": "Old", "temperature": 0.7,
             "top_p": 0.95, "top_k": 250, "min_p": 0.0},
        ],
    }, sort_keys=False)
    storage.PRESETS_PATH.write_text(old_yaml, encoding="utf-8")

    # Re-running storage.initialize() should NOT alter presets.yaml.
    storage.initialize()
    assert storage.PRESETS_PATH.read_text(encoding="utf-8") == old_yaml


def test_preset_model_construction_uses_new_defaults():
    p = Preset()
    assert p.max_context_tokens == 28672
    assert p.rollover_window_tokens == 8192
    assert p.max_new_tokens == 1536

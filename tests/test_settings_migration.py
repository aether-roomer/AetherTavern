"""Settings migration: ``provider_mode`` / ``generic`` written back to
old settings.yaml files; preset migration intentionally lazy.
"""
from __future__ import annotations

import yaml

from server import storage


def test_old_settings_yaml_gets_provider_mode_and_generic_block(tmp_storage):
    """``settings.yaml`` lacking provider_mode/generic gets both keys
    written on boot, defaults to ``aetherroom``."""
    old = {
        "endpoint_url": "https://example.test",
        "api_token": "",
        "default_model": "xialong-v1",
        "default_preset_id": None,
        "theme": "dark",
        "font_size": 14,
        "content_font_size": 14,
        "context_preset": "opus",
        "tts": {"mode": "off"},
    }
    storage.SETTINGS_PATH.write_text(yaml.safe_dump(old), encoding="utf-8")

    # Re-run initialize — this is what happens on every boot.
    storage.initialize()

    raw = yaml.safe_load(storage.SETTINGS_PATH.read_text(encoding="utf-8"))
    assert "provider_mode" in raw
    assert raw["provider_mode"] == "aetherroom"
    assert "generic" in raw
    g = raw["generic"]
    assert g["provider"] == "novelai"
    assert g["novelai"]["base_url"] == "https://text.novelai.net/oa"
    assert g["openrouter"]["base_url"] == "https://openrouter.ai/api"
    assert g["nanogpt"]["base_url"] == "https://nano-gpt.com/api"
    assert g["openai_compatible"]["custom_providers"] == []
    assert g["openai_compatible"]["active_id"] is None
    # Pre-existing fields are preserved.
    assert raw["endpoint_url"] == "https://example.test"


def test_migration_is_idempotent(tmp_storage):
    """Running initialize twice doesn't rewrite a settings.yaml that
    already has the keys."""
    storage.initialize()  # first run already added the keys
    first = storage.SETTINGS_PATH.read_text(encoding="utf-8")
    storage.initialize()
    second = storage.SETTINGS_PATH.read_text(encoding="utf-8")
    assert first == second


def test_presets_yaml_lazily_migrated_not_rewritten_on_boot(tmp_storage):
    """``presets.yaml`` without the new size fields stays byte-identical
    until the user next saves a preset."""
    old_yaml = yaml.safe_dump({
        "presets": [
            {"id": "old", "name": "Old", "temperature": 0.7,
             "top_p": 0.95, "top_k": 250, "min_p": 0.0},
        ],
    }, sort_keys=False)
    storage.PRESETS_PATH.write_text(old_yaml, encoding="utf-8")

    storage.initialize()
    assert storage.PRESETS_PATH.read_text(encoding="utf-8") == old_yaml

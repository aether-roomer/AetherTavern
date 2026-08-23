"""Theme settings: every advertised theme round-trips through the API, and
unknown values are rejected. The server is the gate on the theme enum."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.main import app


ALL_THEMES = [
    "dark",
    "oled",
    "light",
    "noir",
    "pastel",
    "forest",
    "terminal",
    "nebula",
    "sepia",
    "solarized",
]


@pytest.fixture
def client(tmp_storage):
    return TestClient(app)


@pytest.mark.parametrize("theme", ALL_THEMES)
def test_settings_theme_roundtrips(client, theme):
    r = client.put("/api/settings", json={"theme": theme})
    assert r.status_code == 200, r.text
    assert r.json()["theme"] == theme

    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["theme"] == theme


def test_settings_rejects_unknown_theme(client):
    r = client.put("/api/settings", json={"theme": "neon-rainbow"})
    assert r.status_code == 422


def test_settings_theme_default_is_dark(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["theme"] == "dark"

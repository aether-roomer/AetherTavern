"""Max-bubble-width setting.

``None`` means "unlimited" and is a value the user sets explicitly (slider to
the top tick), so the PUT must distinguish "set to null" from "field omitted".
Plain ``exclude_none`` can't — ``put_settings`` checks ``model_fields_set``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.main import app


@pytest.fixture
def client(tmp_storage):
    return TestClient(app)


def test_default_is_unlimited(client):
    assert client.get("/api/settings").json()["max_bubble_width_em"] is None


def test_set_value_roundtrips(client):
    r = client.put("/api/settings", json={"max_bubble_width_em": 60})
    assert r.status_code == 200, r.text
    assert r.json()["max_bubble_width_em"] == 60
    assert client.get("/api/settings").json()["max_bubble_width_em"] == 60


def test_minimum_20_allowed(client):
    r = client.put("/api/settings", json={"max_bubble_width_em": 20})
    assert r.status_code == 200, r.text
    assert r.json()["max_bubble_width_em"] == 20


def test_explicit_null_sets_unlimited(client):
    """Setting a value then sending ``null`` must clear back to unlimited —
    not preserve the old value (the exclude_none trap)."""
    client.put("/api/settings", json={"max_bubble_width_em": 60})
    r = client.put("/api/settings", json={"max_bubble_width_em": None})
    assert r.status_code == 200, r.text
    assert r.json()["max_bubble_width_em"] is None
    assert client.get("/api/settings").json()["max_bubble_width_em"] is None


def test_omitting_field_preserves_value(client):
    client.put("/api/settings", json={"max_bubble_width_em": 80})
    # A PUT that doesn't mention the field must leave it untouched.
    r = client.put("/api/settings", json={"theme": "dark"})
    assert r.status_code == 200, r.text
    assert r.json()["max_bubble_width_em"] == 80


def test_out_of_range_rejected(client):
    # Below the 20 em floor and above the 140 em ceiling are both rejected.
    assert client.put("/api/settings", json={"max_bubble_width_em": 19}).status_code == 422
    assert client.put("/api/settings", json={"max_bubble_width_em": 200}).status_code == 422

"""Preview endpoint coverage — block / message enabled toggles, drop-empty rules."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from server.main import app


@pytest.fixture
def client(tmp_storage, monkeypatch):
    monkeypatch.setenv("AETHER_SKIP_TOKENIZER", "1")
    with TestClient(app) as c:
        yield c


def _block(name, content, enabled=True):
    return {"name": name, "enabled": enabled, "content": content}


def _msg(name, content, **kwargs):
    base = {
        "name": name, "enabled": True, "role": "system", "mode": "simple",
        "simple_content": content, "blocks": [],
        "float_enabled": False, "float_depth": 0,
    }
    base.update(kwargs)
    return base


def _preset(blocks=None, messages=None):
    return {
        "id": "test",
        "name": "Test",
        "system_prompt_blocks": blocks or [],
        "additional_messages": messages or [],
    }


def test_preview_joins_blocks_with_newline(client):
    r = client.post("/api/context-presets/preview", json={"preset": _preset(blocks=[
        _block("A", "alpha"),
        _block("B", "beta"),
    ])})
    assert r.status_code == 200
    assert r.json()["system_prompt"] == "alpha\nbeta"


def test_preview_disabled_block_dropped(client):
    r = client.post("/api/context-presets/preview", json={"preset": _preset(blocks=[
        _block("A", "alpha"),
        _block("B", "beta", enabled=False),
        _block("C", "gamma"),
    ])})
    assert r.json()["system_prompt"] == "alpha\ngamma"


def test_preview_empty_after_strip_block_dropped(client):
    # Block whose macros resolve to whitespace-only is dropped entirely.
    r = client.post("/api/context-presets/preview", json={"preset": _preset(blocks=[
        _block("A", "alpha"),
        _block("B", "   \n  "),       # whitespace-only
        _block("C", "{{if !user.persona}}{{noop}}{{/if}}"),  # if-block resolves to empty
        _block("D", "delta"),
    ])})
    assert r.json()["system_prompt"] == "alpha\ndelta"


def test_preview_preserves_leading_newline_in_block_content(client):
    # Block 1 ends without a trailing newline; block 2 begins with one.
    # The join is "\n", so leading \n on block 2 produces a blank line.
    r = client.post("/api/context-presets/preview", json={"preset": _preset(blocks=[
        _block("A", "alpha"),
        _block("B", "\nbeta"),
    ])})
    assert r.json()["system_prompt"] == "alpha\n\nbeta"


def test_preview_additional_messages_simple_round_trip(client):
    r = client.post("/api/context-presets/preview", json={"preset": _preset(messages=[
        _msg("Static", "hello"),
        _msg("Floating", "world", float_enabled=True, float_depth=3),
    ])})
    msgs = r.json()["additional_messages"]
    assert len(msgs) == 2
    assert msgs[0]["content"] == "hello"
    assert msgs[1]["content"] == "world"
    assert msgs[1]["float_enabled"] is True
    assert msgs[1]["float_depth"] == 3


def test_preview_disabled_message_dropped(client):
    r = client.post("/api/context-presets/preview", json={"preset": _preset(messages=[
        _msg("Static", "kept"),
        _msg("Off", "skipped", enabled=False),
    ])})
    msgs = r.json()["additional_messages"]
    assert [m["content"] for m in msgs] == ["kept"]


def test_preview_empty_message_dropped(client):
    r = client.post("/api/context-presets/preview", json={"preset": _preset(messages=[
        _msg("Empty", ""),
        _msg("Real", "kept"),
    ])})
    msgs = r.json()["additional_messages"]
    assert [m["content"] for m in msgs] == ["kept"]


def test_preview_blocks_mode_additional_message(client):
    r = client.post("/api/context-presets/preview", json={"preset": _preset(messages=[
        _msg(
            "Layered", "", mode="blocks", blocks=[
                _block("A", "alpha"),
                _block("Off", "skipped", enabled=False),
                _block("B", "beta"),
            ],
        ),
    ])})
    msgs = r.json()["additional_messages"]
    assert msgs[0]["content"] == "alpha\nbeta"


def test_preview_macros_resolve_against_dummies(client):
    # No explicit dummies → server's built-in defaults provide a
    # non-empty contact.persona. The block should render against that.
    r = client.post("/api/context-presets/preview", json={"preset": _preset(blocks=[
        _block("X", "{{if contact.persona}}has{{else}}empty{{/if}}"),
    ])})
    assert r.json()["system_prompt"] == "has"


def test_preview_user_supplied_dummies_override_defaults(client):
    r = client.post("/api/context-presets/preview", json={
        "preset": _preset(blocks=[
            _block("X", "{{contact.name}}:{{contact.tags}}"),
        ]),
        "dummies": {
            "contact_name": "Override",
            "contact_tags": "custom",
        },
    })
    assert r.json()["system_prompt"] == "Override:custom"

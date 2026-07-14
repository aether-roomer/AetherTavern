"""Pin the structured fields in the ``brain_budget`` SSE error event.

The frontend renders the failure via a richer toast that lists offenders by
name + token count. That UI parses ``total``, ``cap``, and ``offenders[]``
off the SSE payload — so the server's event must keep emitting them.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from server import storage
from server.aer.rollover import BrainBudgetExceeded
from server.main import app
from server.models import Brain, Contact, User


def test_brain_budget_sse_event_carries_structured_fields(tmp_storage, monkeypatch):
    contact = storage.save_contact(Contact(name="Alice"))
    user = storage.save_user(User(name="Me"))

    def _raise_budget(**kwargs):
        raise BrainBudgetExceeded(
            total=9000,
            cap=8000,
            offenders=[
                ("HugeBrain", 7000, "id-huge"),
                ("MediumBrain", 1500, "id-medium"),
                ("Orphaned", 500, None),
            ],
        )

    monkeypatch.setattr(
        "server.routers.generate.build_messages_for_generation",
        _raise_budget,
    )

    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
        chat_id = chat["id"]

        events: list[tuple[str, dict]] = []
        with client.stream(
            "GET", f"/api/chats/{chat_id}/generate?expected_tip_id=",
        ) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers["content-type"]
            current_event: str | None = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    current_event = line[7:]
                elif line.startswith("data: ") and current_event is not None:
                    payload = json.loads(line[6:])
                    events.append((current_event, payload))
                    current_event = None

    errors = [(name, payload) for name, payload in events if name == "error"]
    assert errors, f"expected an 'error' event, got {events!r}"
    _, payload = errors[0]

    assert payload["kind"] == "brain_budget"
    assert payload["total"] == 9000
    assert payload["cap"] == 8000
    # Offenders carry name + tokens unconditionally. ``brain_id`` is
    # included whenever the rollover layer produced one; ``kind`` +
    # ``owner_id`` are best-effort attribution and only land when the
    # brain.id matches an entry in the chat's entity tree. The synthetic
    # ids in this test don't correspond to any real brain on Alice/Me, so
    # only ``brain_id`` carries through.
    assert payload["offenders"] == [
        {"name": "HugeBrain", "tokens": 7000, "brain_id": "id-huge"},
        {"name": "MediumBrain", "tokens": 1500, "brain_id": "id-medium"},
        {"name": "Orphaned", "tokens": 500},
    ]
    # ``message`` is still the human-readable fallback used by the generic
    # toast path when ``kind`` doesn't match a specialized handler.
    assert isinstance(payload["message"], str)
    assert "Brain budget exceeded" in payload["message"]


def test_brain_budget_offenders_attribute_owner_by_id(tmp_storage, monkeypatch):
    """When a brain.id matches a real brain on the chat's contact / user /
    scenario / per-message tree, the SSE payload carries the owning entity's
    kind + id so the toast can deep-link to its edit view."""
    contact = storage.save_contact(Contact(
        name="Alice",
        brains=[
            Brain(id="cb1", name="GlobalLore", content="big"),
        ],
    ))
    user = storage.save_user(User(
        name="Me",
        brains=[Brain(id="ub1", name="UserLore", content="big")],
    ))

    def _raise_budget(**kwargs):
        raise BrainBudgetExceeded(
            total=9000,
            cap=8000,
            offenders=[
                ("GlobalLore", 5000, "cb1"),
                ("UserLore", 2500, "ub1"),
                ("MysteryName", 1500, "no-such-id"),
            ],
        )

    monkeypatch.setattr(
        "server.routers.generate.build_messages_for_generation",
        _raise_budget,
    )

    with TestClient(app) as client:
        chat = client.post(
            "/api/chats",
            json={"contact_id": contact.id, "user_id": user.id},
        ).json()
        chat_id = chat["id"]

        events: list[tuple[str, dict]] = []
        with client.stream(
            "GET", f"/api/chats/{chat_id}/generate?expected_tip_id=",
        ) as r:
            current = None
            for line in r.iter_lines():
                if line.startswith("event: "):
                    current = line[7:]
                elif line.startswith("data: ") and current is not None:
                    events.append((current, json.loads(line[6:])))
                    current = None

    payload = next(p for name, p in events if name == "error")
    assert payload["offenders"] == [
        {"name": "GlobalLore", "tokens": 5000, "brain_id": "cb1",
         "kind": "contact", "owner_id": contact.id, "owner_name": contact.name},
        {"name": "UserLore", "tokens": 2500, "brain_id": "ub1",
         "kind": "user", "owner_id": user.id, "owner_name": user.name},
        # No match on either entity tree — falls through to bare shape +
        # brain_id (no kind / owner_id / owner_name).
        {"name": "MysteryName", "tokens": 1500, "brain_id": "no-such-id"},
    ]

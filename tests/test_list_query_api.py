"""Server pagination + filter + search on ``GET /api/chats``.

Covers the new query-param surface added in Fix C.3: ``q``,
``contact_id`` / ``user_id`` / ``scenario_id`` / ``ref_library_id``,
``favorites_first``, ``sort``, ``direction``, ``offset``, ``limit``.

Legacy shape (no params at all) returns ``list[ChatSummary]``;
any param present switches the response to a ``ChatListPage`` envelope.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from server import storage
from server.main import app
from server.models import Chat, Contact, User


def _mk_contact(name: str) -> Contact:
    return storage.save_contact(Contact(name=name))


def _mk_user(name: str) -> User:
    return storage.save_user(User(name=name))


def _mk_chat(title: str, contact: Contact, user: User, *, favorite: bool = False,
             scenario_id: str | None = None,
             brain_library_ids: list[str] | None = None) -> Chat:
    chat = storage.save_chat(Chat(
        title=title,
        contact_id=contact.id,
        user_id=user.id,
        scenario_id=scenario_id,
        favorite=favorite,
        brain_library_ids=brain_library_ids or [],
    ))
    return chat


def test_list_chats_legacy_shape_no_params(tmp_storage):
    """``GET /api/chats`` with no query params returns the legacy flat
    ``list[ChatSummary]`` so existing single-user clients keep working."""
    c = _mk_contact("Alpha")
    u = _mk_user("Me")
    _mk_chat("First", c, u)
    _mk_chat("Second", c, u)

    with TestClient(app) as client:
        r = client.get("/api/chats")
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, list)
        assert {item["title"] for item in body} == {"First", "Second"}


def test_list_chats_paged_envelope_when_params_present(tmp_storage):
    """Any query param flips the response to ``ChatListPage``."""
    c = _mk_contact("Alpha")
    u = _mk_user("Me")
    for i in range(5):
        _mk_chat(f"Chat {i}", c, u)

    with TestClient(app) as client:
        r = client.get("/api/chats?limit=3")
        assert r.status_code == 200
        body = r.json()
        assert set(body.keys()) >= {"items", "total", "offset", "limit"}
        assert body["total"] == 5
        assert body["limit"] == 3
        assert len(body["items"]) == 3


def test_list_chats_q_search_title_and_contact_name(tmp_storage):
    """``q`` AND-matches whitespace-split tokens against title + the
    joined contact name. Case-insensitive."""
    alice = _mk_contact("Alice")
    bob = _mk_contact("Bob")
    u = _mk_user("Me")
    _mk_chat("Coffee Talk", alice, u)
    _mk_chat("Lunch Plans", alice, u)
    _mk_chat("Coffee Date", bob, u)

    with TestClient(app) as client:
        # Title-only match.
        r = client.get("/api/chats?q=coffee").json()
        assert {x["title"] for x in r["items"]} == {"Coffee Talk", "Coffee Date"}
        # Joined contact-name match.
        r = client.get("/api/chats?q=alice").json()
        assert {x["title"] for x in r["items"]} == {"Coffee Talk", "Lunch Plans"}
        # Multi-token AND.
        r = client.get("/api/chats?q=coffee%20alice").json()
        assert {x["title"] for x in r["items"]} == {"Coffee Talk"}


def test_list_chats_filter_by_contact_user_scenario(tmp_storage):
    """Equality filters on the chat's foreign keys."""
    alice = _mk_contact("Alice")
    bob = _mk_contact("Bob")
    u1 = _mk_user("MeOne")
    u2 = _mk_user("MeTwo")
    _mk_chat("AliceMeOne", alice, u1)
    _mk_chat("AliceMeTwo", alice, u2)
    _mk_chat("BobMeOne", bob, u1)

    with TestClient(app) as client:
        r = client.get(f"/api/chats?contact_id={alice.id}").json()
        assert {x["title"] for x in r["items"]} == {"AliceMeOne", "AliceMeTwo"}
        r = client.get(f"/api/chats?user_id={u1.id}").json()
        assert {x["title"] for x in r["items"]} == {"AliceMeOne", "BobMeOne"}
        # Combined filter — AND across params.
        r = client.get(f"/api/chats?contact_id={alice.id}&user_id={u1.id}").json()
        assert {x["title"] for x in r["items"]} == {"AliceMeOne"}


def test_list_chats_filter_by_ref_library_id(tmp_storage):
    """``ref_library_id`` is a membership filter on
    ``chat.brain_library_ids`` — list-typed match, distinct from the
    scalar foreign-key filters above."""
    c = _mk_contact("Alpha")
    u = _mk_user("Me")
    _mk_chat("WithLib", c, u, brain_library_ids=["lib1", "lib2"])
    _mk_chat("DifferentLib", c, u, brain_library_ids=["lib3"])
    _mk_chat("NoLibs", c, u)

    with TestClient(app) as client:
        r = client.get("/api/chats?ref_library_id=lib1").json()
        assert {x["title"] for x in r["items"]} == {"WithLib"}
        r = client.get("/api/chats?ref_library_id=missing").json()
        assert r["items"] == []


def test_list_chats_sort_modes(tmp_storage):
    """``sort`` accepts updated_at / created_at / title; ``direction``
    flips ascending vs descending. Default ``updated_at desc``."""
    c = _mk_contact("Alpha")
    u = _mk_user("Me")
    a = _mk_chat("Apple", c, u)
    b = _mk_chat("Banana", c, u)
    z = _mk_chat("Zebra", c, u)

    # Force a specific updated_at ordering (Apple < Banana < Zebra).
    for chat, ts in [(a, 100.0), (b, 200.0), (z, 300.0)]:
        chat.updated_at = ts
        storage.save_chat(chat, bump_version=False)

    with TestClient(app) as client:
        # ``limit`` forces the envelope shape even when ``sort`` /
        # ``direction`` match server defaults.
        r = client.get("/api/chats?sort=title&direction=asc&limit=10").json()
        assert [x["title"] for x in r["items"]] == ["Apple", "Banana", "Zebra"]
        r = client.get("/api/chats?sort=title&direction=desc&limit=10").json()
        assert [x["title"] for x in r["items"]] == ["Zebra", "Banana", "Apple"]
        r = client.get("/api/chats?sort=updated_at&direction=desc&limit=10").json()
        assert [x["title"] for x in r["items"]] == ["Zebra", "Banana", "Apple"]
        r = client.get("/api/chats?sort=updated_at&direction=asc&limit=10").json()
        assert [x["title"] for x in r["items"]] == ["Apple", "Banana", "Zebra"]


def test_list_chats_favorites_first(tmp_storage):
    """``favorites_first`` partitions favourites to the top, preserving
    the secondary sort within each partition (stable Python sort)."""
    c = _mk_contact("Alpha")
    u = _mk_user("Me")
    _mk_chat("AAA", c, u, favorite=False)
    _mk_chat("BBB", c, u, favorite=True)
    _mk_chat("CCC", c, u, favorite=False)
    _mk_chat("DDD", c, u, favorite=True)

    with TestClient(app) as client:
        r = client.get("/api/chats?favorites_first=true&sort=title&direction=asc").json()
        titles = [x["title"] for x in r["items"]]
        # Favourites in title order, then non-favourites in title order.
        assert titles == ["BBB", "DDD", "AAA", "CCC"]


def test_list_chats_offset_limit(tmp_storage):
    """Slicing by ``offset`` + ``limit`` against the post-sort ordering."""
    c = _mk_contact("Alpha")
    u = _mk_user("Me")
    for i in range(7):
        chat = _mk_chat(f"Chat {i}", c, u)
        chat.updated_at = float(100 + i)
        storage.save_chat(chat, bump_version=False)

    with TestClient(app) as client:
        # Default sort: updated_at desc → newest first.
        r = client.get("/api/chats?offset=2&limit=3").json()
        titles = [x["title"] for x in r["items"]]
        # Sorted desc: 6, 5, 4, 3, 2, 1, 0. Slice [2:5] → 4, 3, 2.
        assert titles == ["Chat 4", "Chat 3", "Chat 2"]
        assert r["total"] == 7
        assert r["offset"] == 2
        assert r["limit"] == 3


def test_list_chats_limit_capped_at_100(tmp_storage):
    """``limit`` is hard-capped at 100 — guards against a runaway client
    request asking for tens of thousands of rows in one shot."""
    c = _mk_contact("Alpha")
    u = _mk_user("Me")
    _mk_chat("solo", c, u)
    with TestClient(app) as client:
        r = client.get("/api/chats?limit=999").json()
        assert r["limit"] == 100

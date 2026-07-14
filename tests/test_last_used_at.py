"""``storage.index.last_used_*`` maintenance.

Each entity kind (contact, user, scenario, brain library) tracks the most
recent ``chat.updated_at`` of a chat that references it. This drives the
"Last used" sort mode in the four non-chat list panes — moving the
computation server-side means the client doesn't have to walk every
chat to figure out the order.

The index is in-memory only: rebuilt at boot from the parsed chats
(``_rebuild_last_used``), updated incrementally on ``save_chat``
(``bump_last_used``), and recomputed wholesale on ``delete_chat``.
"""
from __future__ import annotations

from server import storage
from server.models import BrainLibrary, Chat, Contact, Scenario, User


def test_last_used_rebuilt_from_existing_chats(tmp_storage):
    """``_rebuild_last_used`` walks every chat once at boot, stamping the
    referenced contact/user/scenario/library with that chat's
    ``updated_at`` (max across multiple chats per entity)."""
    c = storage.save_contact(Contact(name="C"))
    u = storage.save_user(User(name="U"))
    s = storage.save_scenario(Scenario(name="S"))
    lib = storage.save_brain_library(BrainLibrary(name="L"))

    chat = storage.save_chat(Chat(
        title="T", contact_id=c.id, user_id=u.id, scenario_id=s.id,
        brain_library_ids=[lib.id],
    ))
    chat.updated_at = 1000.0
    storage.save_chat(chat, bump_version=False)

    # Wipe the in-memory cache, then re-derive from the persisted chats.
    storage.index.last_used_contact = {}
    storage.index.last_used_user = {}
    storage.index.last_used_scenario = {}
    storage.index.last_used_library = {}
    storage.index._rebuild_last_used()

    assert storage.index.last_used_contact[c.id] == 1000.0
    assert storage.index.last_used_user[u.id] == 1000.0
    assert storage.index.last_used_scenario[s.id] == 1000.0
    assert storage.index.last_used_library[lib.id] == 1000.0


def test_last_used_bumps_on_save_chat(tmp_storage):
    """``save_chat`` bumps the four caches via ``index.bump_last_used``."""
    c = storage.save_contact(Contact(name="C"))
    u = storage.save_user(User(name="U"))
    chat = storage.save_chat(Chat(
        title="T", contact_id=c.id, user_id=u.id,
    ))
    initial = storage.index.last_used_contact[c.id]
    chat.updated_at = initial + 100.0
    storage.save_chat(chat, bump_version=False)
    assert storage.index.last_used_contact[c.id] == initial + 100.0
    # bump_last_used never lowers a stored value — older save shouldn't win.
    chat.updated_at = initial - 50.0
    storage.save_chat(chat, bump_version=False)
    assert storage.index.last_used_contact[c.id] == initial + 100.0


def test_last_used_takes_max_across_multiple_chats(tmp_storage):
    """One entity referenced by N chats: the cached value is the max
    over the chats' ``updated_at``."""
    c = storage.save_contact(Contact(name="C"))
    u = storage.save_user(User(name="U"))
    a = storage.save_chat(Chat(title="A", contact_id=c.id, user_id=u.id))
    a.updated_at = 100.0
    storage.save_chat(a, bump_version=False)
    b = storage.save_chat(Chat(title="B", contact_id=c.id, user_id=u.id))
    b.updated_at = 200.0
    storage.save_chat(b, bump_version=False)
    storage.index._rebuild_last_used()
    assert storage.index.last_used_contact[c.id] == 200.0


def test_last_used_recomputes_on_delete(tmp_storage):
    """When the chat that owned an entity's max ``updated_at`` is
    deleted, the cache rebuilds and the next-most-recent chat (or 0)
    takes its place."""
    c = storage.save_contact(Contact(name="C"))
    u = storage.save_user(User(name="U"))
    a = storage.save_chat(Chat(title="A", contact_id=c.id, user_id=u.id))
    a.updated_at = 100.0
    storage.save_chat(a, bump_version=False)
    b = storage.save_chat(Chat(title="B", contact_id=c.id, user_id=u.id))
    b.updated_at = 200.0
    storage.save_chat(b, bump_version=False)
    storage.index._rebuild_last_used()
    assert storage.index.last_used_contact[c.id] == 200.0
    # Delete the newer one — cache drops back to A's timestamp.
    storage.delete_chat(b.id)
    assert storage.index.last_used_contact[c.id] == 100.0

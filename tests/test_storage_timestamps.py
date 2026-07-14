"""``created_at`` is set once on creation and preserved across saves;
``updated_at`` is bumped on every save. The frontend's "Recently added"
vs "Recently edited" sort modes rely on this distinction."""
from __future__ import annotations

import time

from server import storage
from server.models import Contact, Scenario, User


def test_save_contact_preserves_created_at_bumps_updated_at(tmp_storage):
    c = Contact(name="Alice")
    original_created = c.created_at
    storage.save_contact(c)
    first_updated = c.updated_at

    # Mutate something and save again — created_at must not change, updated_at must.
    time.sleep(0.01)
    c.description = "edited"
    storage.save_contact(c)

    assert c.created_at == original_created
    assert c.updated_at > first_updated

    reloaded = storage.get_contact(c.id)
    assert reloaded.created_at == original_created
    assert reloaded.updated_at == c.updated_at


def test_save_user_preserves_created_at_bumps_updated_at(tmp_storage):
    u = User(name="Anon")
    original_created = u.created_at
    storage.save_user(u)
    first_updated = u.updated_at

    time.sleep(0.01)
    u.description = "edited"
    storage.save_user(u)

    assert u.created_at == original_created
    assert u.updated_at > first_updated


def test_save_scenario_preserves_created_at_bumps_updated_at(tmp_storage):
    s = Scenario(name="Coffee shop")
    original_created = s.created_at
    storage.save_scenario(s)
    first_updated = s.updated_at

    time.sleep(0.01)
    s.scene = "edited"
    storage.save_scenario(s)

    assert s.created_at == original_created
    assert s.updated_at > first_updated

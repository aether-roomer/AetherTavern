"""Importer replace-mode .bak staging.

Replace renames the original to ``<dir>.bak``, runs the import, then
either commits (controlled-deletes the .bak on success) or restores
(renames .bak back on failure) — so a partial-failure import (network
glitch, malformed source) leaves the user with the original intact.
"""
from __future__ import annotations

import pytest

from server import storage, importers
from server.models import Contact


def _make_existing_contact() -> Contact:
    return storage.save_contact(Contact(
        id="abc12345abc12345abc12345abc12345",
        name="OldAlice",
        description="original description",
    ))


@pytest.mark.asyncio
async def test_replace_commits_on_success(tmp_storage):
    """Successful replace import controlled-deletes the .bak."""
    existing = _make_existing_contact()
    original_dir = storage.contact_dir(existing.id)
    bak_path = original_dir.with_name(original_dir.name + ".bak")
    assert not bak_path.exists()

    # Import a new contact at the same UUID with mode=replace. No images,
    # so no network calls — just rebuilds the entity.
    # ``greeting`` (or any other contact-only field) makes detect_format
    # pick the "contact" branch.
    new_data = {"id": existing.id, "name": "NewAlice", "greeting": ""}
    events = []
    async for ev in importers.import_streaming(new_data, mode="replace"):
        events.append(ev)

    # The .bak should be gone after a successful commit.
    assert not bak_path.exists()
    # The contact is replaced — new name, same id.
    refreshed = storage.get_contact(existing.id)
    assert refreshed is not None
    assert refreshed.name == "NewAlice"
    assert any(e.get("type") == "done" for e in events)


@pytest.mark.asyncio
async def test_replace_restores_bak_on_exception(tmp_storage, monkeypatch):
    """If the import raises mid-flight, the .bak is renamed back so the
    original survives."""
    existing = _make_existing_contact()
    original_dir = storage.contact_dir(existing.id)
    original_info = (original_dir / "info.yaml").read_text()

    # Patch the inner streaming function to raise after a few yields. This
    # simulates a network failure during the image-download phase.
    orig_inner = importers._import_contact_streaming_inner

    async def boom(*args, **kwargs):
        # yield a couple of progress events before crashing — the .bak is
        # already in place at this point, mirroring a partial failure.
        async for ev in orig_inner(*args, **kwargs):
            if ev.get("type") == "progress":
                yield ev
                continue
            yield ev
            break
        raise RuntimeError("network died")

    monkeypatch.setattr(importers, "_import_contact_streaming_inner", boom)

    new_data = {"id": existing.id, "name": "Imposter",
                "description": "should not land", "greeting": ""}
    with pytest.raises(RuntimeError, match="network died"):
        async for _ev in importers.import_streaming(new_data, mode="replace"):
            pass

    # The original survives — restored from .bak.
    refreshed = storage.get_contact(existing.id)
    assert refreshed is not None
    assert refreshed.name == "OldAlice"
    assert refreshed.description == "original description"
    # .bak is gone (renamed back to original location).
    bak_path = original_dir.with_name(original_dir.name + ".bak")
    assert not bak_path.exists()
    # info.yaml content is exactly the same as before the failed import.
    restored_dir = storage.contact_dir(existing.id)
    assert (restored_dir / "info.yaml").read_text() == original_info

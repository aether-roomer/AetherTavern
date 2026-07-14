"""Optimistic-concurrency helper for entity PUT endpoints.

Each Contact / User / Scenario / Chat carries a ``version_id`` that
``storage.save_*`` rerolls on every write. PUT endpoints call
:func:`check_version` with the incoming body and the freshly-loaded
server copy: matching version_ids fall through (the route proceeds with
save), a mismatch raises a structured ``HTTPException(409)`` the
frontend can use to prompt the user with both timestamps before they
decide whether to overwrite.

A version_id mismatch with **semantically-identical payloads** is NOT a
conflict — there's nothing to overwrite. ``check_version`` short-
circuits in that case and tells the caller to skip the save (returning
the existing entity). Without this, an autosave that fires after an
out-of-band server write (e.g. a file upload) would 409 even though
the user's draft already matches the stored state.
"""
from __future__ import annotations

from typing import Protocol

from fastapi import HTTPException
from pydantic import BaseModel


class _Versioned(Protocol):
    version_id: str
    updated_at: float


# Fields that are server-managed bookkeeping rather than user-meaningful
# content. Excluded from the "are these payloads equivalent?" comparison
# so a stale version_id alone never causes a spurious 409.
_BOOKKEEPING_FIELDS = {"version_id", "updated_at"}


def _semantically_equal(a: BaseModel, b: BaseModel) -> bool:
    """True if ``a`` and ``b`` have identical user-meaningful content.

    Compares ``model_dump`` output minus :data:`_BOOKKEEPING_FIELDS`.
    Used to decide whether a version-id mismatch is a real conflict or
    just a bookkeeping drift (e.g. the autosave fires after an avatar
    upload bumped ``updated_at`` server-side).
    """
    a_dump = a.model_dump(mode="json", exclude=_BOOKKEEPING_FIELDS)
    b_dump = b.model_dump(mode="json", exclude=_BOOKKEEPING_FIELDS)
    return a_dump == b_dump


def check_version(incoming: BaseModel, existing: BaseModel) -> bool:
    """Optimistic-concurrency check.

    Returns ``True`` when the caller should proceed with the save
    (version_ids match — the normal happy path). Returns ``False`` when
    the payloads are semantically identical so the caller can skip the
    save and return ``existing`` directly (no real conflict, just a
    stale version_id). Raises ``HTTPException(409)`` on a real conflict.

    The 409 ``detail`` body carries the live ``version_id`` + ``updated_at``
    so the client can render a meaningful conflict prompt and re-issue
    the save with the fresh id once the user decides to overwrite.
    """
    if incoming.version_id == existing.version_id:
        return True
    # Stale version_id, but maybe the payload still matches what's on
    # disk — that means the user has nothing to save. Skip and let the
    # caller return ``existing`` (which carries the live version_id, so
    # the client's draft picks up the fresh id naturally).
    if _semantically_equal(incoming, existing):
        return False
    raise HTTPException(
        status_code=409,
        detail={
            "code": "version_conflict",
            "current_version_id": existing.version_id,
            "current_updated_at": existing.updated_at,
        },
    )

"""Helpers for the "Duplicate" action on contacts / users / scenarios /
brain libraries.

Two concerns:

1. Picking a name for the copy. ``next_copy_name`` appends ``(1)`` to the
   source name, or increments an existing ``(N)`` suffix. The optional
   ``existing`` set lets the caller keep bumping until the result doesn't
   collide with another entity of the same kind — a polite default, but
   it's only "polite": names are not uniquely keyed anywhere.

2. Cloning the on-disk files alongside the entity record. The entity's
   YAML marker is rewritten by ``storage.save_*``; everything else
   (avatar, card image, emotion sprites, background, display siblings)
   is byte-copied from the source directory.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Iterable

_SUFFIX_RE = re.compile(r"^(.*?)\s*\((\d+)\)\s*$")


def next_copy_name(name: str, existing: Iterable[str] = ()) -> str:
    """Return the duplicate name for ``name``.

    ``Foo`` → ``Foo (1)``. ``Foo (1)`` → ``Foo (2)``. ``Foo (5)`` →
    ``Foo (6)``. If ``existing`` is given, the counter keeps bumping
    until the result isn't in the set (matched case-insensitively, so
    "Foo (1)" and "foo (1)" count as a collision).
    """
    m = _SUFFIX_RE.match(name or "")
    if m:
        base = m.group(1).rstrip()
        n = int(m.group(2)) + 1
    else:
        base = (name or "").rstrip()
        n = 1
    taken = {s.casefold() for s in existing}
    while True:
        candidate = f"{base} ({n})" if base else f"({n})"
        if candidate.casefold() not in taken:
            return candidate
        n += 1


def copy_entity_files(src_dir: Path, dst_dir: Path, *, marker: str = "info.yaml") -> None:
    """Copy every file under ``src_dir`` into ``dst_dir`` except ``marker``.

    Recursively handles subdirectories (the ``emotions/`` folder under a
    contact). The marker file is skipped because ``storage.save_*`` has
    already written a fresh copy for the new entity.

    Best-effort: missing source dir is a no-op (a freshly-imported entity
    with no avatar / emotions has nothing to copy). Individual file
    failures propagate so the caller sees a bad disk early rather than
    silently shipping a half-cloned entity.
    """
    if not src_dir.exists():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for entry in src_dir.iterdir():
        if entry.name == marker or entry.name.endswith(".tmp"):
            continue
        if entry.is_dir():
            copy_entity_files(entry, dst_dir / entry.name, marker=marker)
        else:
            shutil.copy2(entry, dst_dir / entry.name)

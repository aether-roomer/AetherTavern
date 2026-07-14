"""Filesystem-backed storage with atomic YAML writes.

Layout::

    data/
        settings.yaml
        presets.yaml
        contacts/{slug}-{shortid}/info.yaml + avatar.* + emotions/*
        users/{slug}-{shortid}/info.yaml
        scenarios/{slug}-{shortid}/info.yaml
        chats/{slug}-{shortid}/{chat.yaml,messages.yaml,bookmarks.yaml}

Reads are sync (cheap, in-memory parsing). Writes are sync but atomic via
``.tmp`` + ``os.replace`` + fsync on the file and the parent directory.
Concurrency is funneled through per-key ``asyncio.Lock``s held by the route
handlers — every load-modify-write must run inside ``async with
storage.lock(key)`` to be safe against concurrent requests on the same
entity.

Deletion uses controlled-removal of known files plus ``os.rmdir`` (allowed
to fail with ENOTEMPTY) so any user-stashed files in an entity directory
survive. ``shutil.rmtree`` is intentionally NOT used.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from collections import defaultdict
from pathlib import Path
from typing import TypeVar

import asyncio
import msgspec.yaml
from pydantic import BaseModel, ValidationError

from server.aer.rollover import get_active_path
from server.models import (
    EMOTIONS,
    BrainLibrary,
    Chat,
    ChatBookmarks,
    ChatMessage,
    ChatMessages,
    Contact,
    ContextPreset,
    ContextPresetAdditionalMessage,
    ContextPresetBlock,
    Preset,
    PresetLibrary,
    Scenario,
    Settings,
    User,
    new_id,
    now_seconds,
)


log = logging.getLogger("aether.storage")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DATA_DIR: Path = Path(os.environ.get("AETHER_DATA_DIR", "data")).resolve()
CONTACTS_DIR: Path = DATA_DIR / "contacts"
USERS_DIR: Path = DATA_DIR / "users"
SCENARIOS_DIR: Path = DATA_DIR / "scenarios"
LIBRARIES_DIR: Path = DATA_DIR / "libraries"
CONTEXT_PRESETS_DIR: Path = DATA_DIR / "context_presets"
CHATS_DIR: Path = DATA_DIR / "chats"
SETTINGS_PATH: Path = DATA_DIR / "settings.yaml"
PRESETS_PATH: Path = DATA_DIR / "presets.yaml"
# Scratch space for in-flight bulk-import staging (uploaded zips + lazily
# generated picker thumbnails). Wiped at startup — see ``_clean_temp_dir``.
# Leading dot keeps it out of ``_scan_dir`` (which only looks for
# directories with marker YAML files).
TEMP_DIR: Path = DATA_DIR / ".tmp"


def _refresh_paths() -> None:
    """Recompute path globals from current ``AETHER_DATA_DIR`` env var."""
    global DATA_DIR, CONTACTS_DIR, USERS_DIR, SCENARIOS_DIR, LIBRARIES_DIR
    global CONTEXT_PRESETS_DIR, CHATS_DIR
    global SETTINGS_PATH, PRESETS_PATH, TEMP_DIR
    DATA_DIR = Path(os.environ.get("AETHER_DATA_DIR", "data")).resolve()
    CONTACTS_DIR = DATA_DIR / "contacts"
    USERS_DIR = DATA_DIR / "users"
    SCENARIOS_DIR = DATA_DIR / "scenarios"
    LIBRARIES_DIR = DATA_DIR / "libraries"
    CONTEXT_PRESETS_DIR = DATA_DIR / "context_presets"
    CHATS_DIR = DATA_DIR / "chats"
    SETTINGS_PATH = DATA_DIR / "settings.yaml"
    PRESETS_PATH = DATA_DIR / "presets.yaml"
    TEMP_DIR = DATA_DIR / ".tmp"


# ---------------------------------------------------------------------------
# Slug + dir name helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str, fallback: str = "untitled") -> str:
    s = _SLUG_RE.sub("-", (name or "").lower()).strip("-")
    return s or fallback


def entity_dir_name(name: str, entity_id: str) -> str:
    return f"{slugify(name)}-{entity_id[:8]}"


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically and durably.

    Sequence: write to ``path.tmp`` → flush + fsync the file → ``os.replace``
    onto ``path`` → fsync the parent directory. The fsyncs make the most
    recent write durable across hard power-cuts; without them the rename
    is atomic w.r.t. concurrent reads but the kernel may not have flushed
    the new inode contents or the directory entry by the time we return.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    # fsync the parent directory so the rename is durable. Best-effort:
    # some filesystems (e.g. older NFS) don't support directory fsync;
    # in that case the rename is still atomic, just less durable.
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def atomic_write_yaml(path: Path, model: BaseModel) -> None:
    # model_dump(mode="json") returns a dict whose values are already
    # JSON-primitives — enums emitted as ``.value`` strings, datetimes as
    # ISO strings, etc. msgspec.yaml.encode takes that dict and produces
    # YAML bytes; no intermediate JSON string is involved.
    atomic_write_bytes(path, msgspec.yaml.encode(model.model_dump(mode="json")))


T = TypeVar("T", bound=BaseModel)


# ---------------------------------------------------------------------------
# Controlled deletion (preserves user-stashed files)
# ---------------------------------------------------------------------------


def remove_known_files(*paths: Path) -> None:
    """Best-effort ``unlink`` for each path. Missing files and permission
    errors are ignored — used in cleanup paths where partial success is
    acceptable (the next save / next boot will reconcile)."""
    for p in paths:
        try:
            p.unlink()
        except OSError:
            pass


def remove_dir_if_empty(path: Path) -> None:
    """``os.rmdir`` if ``path`` is empty; silently no-op otherwise.

    Used after :func:`remove_known_files` walks a directory's known files —
    if the user stashed extra files in the entity directory, ``rmdir`` will
    fail with ``ENOTEMPTY`` and we leave the (now marker-less) dir behind
    for the user to inspect. The indexer skips directories without the
    expected marker file, so the entity is correctly gone from the index
    even though the directory survives.
    """
    try:
        os.rmdir(path)
    except OSError:
        pass


def _glob_unlink(parent: Path, pattern: str) -> None:
    """Best-effort glob+unlink. Skips ``.tmp`` siblings (left over from a
    crashed atomic write) so they're cleaned up on the next save instead."""
    if not parent.exists():
        return
    for p in parent.glob(pattern):
        if p.name.endswith(".tmp"):
            continue
        try:
            p.unlink()
        except OSError:
            pass


def remove_contact_dir(path: Path) -> None:
    """Controlled removal of a contact directory, preserving user files."""
    remove_known_files(path / "info.yaml")
    _glob_unlink(path, "avatar.*")
    _glob_unlink(path, "card.*")
    em_dir = path / "emotions"
    if em_dir.exists():
        for emotion in EMOTIONS:
            _glob_unlink(em_dir, f"{emotion.value}.*")
        remove_dir_if_empty(em_dir)
    remove_dir_if_empty(path)


def remove_user_dir(path: Path) -> None:
    """Controlled removal of a user-persona directory."""
    remove_known_files(path / "info.yaml")
    _glob_unlink(path, "avatar.*")
    _glob_unlink(path, "card.*")
    remove_dir_if_empty(path)


def remove_scenario_dir(path: Path) -> None:
    """Controlled removal of a scenario directory."""
    remove_known_files(path / "info.yaml")
    _glob_unlink(path, "background.*")
    _glob_unlink(path, "card.*")
    remove_dir_if_empty(path)


def remove_chat_dir(path: Path) -> None:
    """Controlled removal of a chat directory.

    Sweeps the Generic-mode ``images/`` subdir and the per-message
    ``attachments/`` subdir if present — both are chat-scoped assets that
    share the chat's lifetime and have no use outside it.
    """
    for sub_name in ("images", "attachments"):
        subdir = path / sub_name
        if not subdir.exists():
            continue
        # Sweep our regenerable compressed-attachment cache first. It's a
        # dir we own, so controlled-removing it is safe — and necessary,
        # since the rmdir below would ENOTEMPTY on a leftover subdir.
        cache = subdir / ".cached"
        if cache.exists():
            for child in cache.iterdir():
                try:
                    if child.is_file():
                        child.unlink()
                except OSError:
                    pass
            try:
                cache.rmdir()
            except OSError:
                pass
        # Controlled rmtree: only files we recognise live here. Any
        # unrelated user-stashed files are left behind (same
        # convention as the parent-dir cleanup).
        for child in subdir.iterdir():
            try:
                if child.is_file():
                    child.unlink()
            except OSError:
                pass
        try:
            subdir.rmdir()
        except OSError:
            pass
    remove_known_files(
        path / "chat.yaml",
        path / "messages.yaml",
        path / "bookmarks.yaml",
    )
    remove_dir_if_empty(path)


def remove_brain_library_dir(path: Path) -> None:
    """Controlled removal of a brain-library directory."""
    remove_known_files(path / "info.yaml")
    _glob_unlink(path, "avatar.*")
    _glob_unlink(path, "card.*")
    remove_dir_if_empty(path)


def remove_context_preset_dir(path: Path) -> None:
    """Controlled removal of a context-preset directory."""
    remove_known_files(path / "info.yaml")
    _glob_unlink(path, "avatar.*")
    _glob_unlink(path, "card.*")
    remove_dir_if_empty(path)


_REMOVERS: dict[str, callable] = {
    "contact": remove_contact_dir,
    "user": remove_user_dir,
    "scenario": remove_scenario_dir,
    "chat": remove_chat_dir,
    "brain_library": remove_brain_library_dir,
    "context_preset": remove_context_preset_dir,
}


def load_yaml(path: Path, model_cls: type[T]) -> T | None:
    if not path.exists():
        return None
    data = path.read_bytes()
    if not data:
        return None
    raw = msgspec.yaml.decode(data)
    if raw is None:
        return None
    try:
        return model_cls.model_validate(raw)
    except ValidationError as e:
        raise RuntimeError(f"Failed to parse {path}: {e}") from e


def _read_yaml_raw(path: Path) -> dict | None:
    """Read a YAML file as a raw dict. Used by boot migrations that need
    to detect missing keys before calling :func:`load_yaml` (which would
    fill them in via Pydantic defaults). Returns ``None`` if the file is
    missing, empty, unreadable, or not a top-level mapping.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data:
        return None
    try:
        raw = msgspec.yaml.decode(data)
    except msgspec.DecodeError:
        return None
    return raw if isinstance(raw, dict) else None


# ---------------------------------------------------------------------------
# Per-key asyncio locks
# ---------------------------------------------------------------------------

_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def lock(key: str) -> asyncio.Lock:
    """Return a process-wide ``asyncio.Lock`` keyed by ``key``."""
    return _locks[key]


# ---------------------------------------------------------------------------
# In-memory index of {entity_id: dir_path}
# ---------------------------------------------------------------------------


class _Index:
    """In-memory parsed-entity cache + parallel path lookup.

    The primary dicts (``contacts`` / ``users`` / ``scenarios`` /
    ``brain_libraries`` / ``chats``) hold *parsed Pydantic models* — every
    ``list_*`` and ``get_*`` helper reads from these in memory. The
    ``*_paths`` siblings hold the on-disk directory for each entity, used
    by file-upload routes, exporters, and importer staging that need to
    name a path independently of the model.

    Convention: callers MUST go through ``save_*`` / ``delete_*`` to
    mutate persisted state. Mutating the returned model in-place without
    saving will desync the in-memory cache from disk on next restart but
    is otherwise safe — the bug surfaces immediately as wrong UI state.
    """

    contacts: dict[str, Contact]
    users: dict[str, User]
    scenarios: dict[str, Scenario]
    brain_libraries: dict[str, BrainLibrary]
    context_presets: dict[str, ContextPreset]
    chats: dict[str, Chat]
    contact_paths: dict[str, Path]
    user_paths: dict[str, Path]
    scenario_paths: dict[str, Path]
    brain_library_paths: dict[str, Path]
    context_preset_paths: dict[str, Path]
    chat_paths: dict[str, Path]
    # Per-entity ``max(chat.updated_at over chats referencing it)``.
    # In-memory only — recomputed at boot from ``chats``, then updated
    # incrementally on ``save_chat`` / ``delete_chat``. Read by the
    # ``*Summary.last_used_at`` field so the "Last used" sort mode in
    # list views doesn't need to walk every chat client-side.
    last_used_contact: dict[str, float]
    last_used_user: dict[str, float]
    last_used_scenario: dict[str, float]
    last_used_library: dict[str, float]

    def __init__(self) -> None:
        self.contacts = {}
        self.users = {}
        self.scenarios = {}
        self.brain_libraries = {}
        self.context_presets = {}
        self.chats = {}
        self.contact_paths = {}
        self.user_paths = {}
        self.scenario_paths = {}
        self.brain_library_paths = {}
        self.context_preset_paths = {}
        self.chat_paths = {}
        self.last_used_contact = {}
        self.last_used_user = {}
        self.last_used_scenario = {}
        self.last_used_library = {}

    def populate(self) -> None:
        self.contacts, self.contact_paths = _scan_dir(
            CONTACTS_DIR, "info.yaml", Contact)
        self.users, self.user_paths = _scan_dir(
            USERS_DIR, "info.yaml", User)
        self.scenarios, self.scenario_paths = _scan_dir(
            SCENARIOS_DIR, "info.yaml", Scenario)
        self.brain_libraries, self.brain_library_paths = _scan_dir(
            LIBRARIES_DIR, "info.yaml", BrainLibrary)
        self.context_presets, self.context_preset_paths = _scan_dir(
            CONTEXT_PRESETS_DIR, "info.yaml", ContextPreset)
        self.chats, self.chat_paths = _scan_dir(
            CHATS_DIR, "chat.yaml", Chat)
        self._rebuild_last_used()

    def _rebuild_last_used(self) -> None:
        """Walk every chat and stamp last_used_at on the four referenced
        entity kinds. O(N chats); cheap because the chat objects are
        already in memory after ``_scan_dir``."""
        self.last_used_contact = {}
        self.last_used_user = {}
        self.last_used_scenario = {}
        self.last_used_library = {}
        for chat in self.chats.values():
            ts = chat.updated_at
            cur = self.last_used_contact.get(chat.contact_id, 0.0)
            if ts > cur:
                self.last_used_contact[chat.contact_id] = ts
            cur = self.last_used_user.get(chat.user_id, 0.0)
            if ts > cur:
                self.last_used_user[chat.user_id] = ts
            if chat.scenario_id:
                cur = self.last_used_scenario.get(chat.scenario_id, 0.0)
                if ts > cur:
                    self.last_used_scenario[chat.scenario_id] = ts
            for lid in chat.brain_library_ids:
                cur = self.last_used_library.get(lid, 0.0)
                if ts > cur:
                    self.last_used_library[lid] = ts

    def bump_last_used(self, chat: Chat) -> None:
        """Update the per-entity ``last_used_at`` cache from ``chat``'s
        current ``updated_at``. Idempotent — only raises an existing
        entry. Called from ``save_chat``."""
        ts = chat.updated_at
        if ts > self.last_used_contact.get(chat.contact_id, 0.0):
            self.last_used_contact[chat.contact_id] = ts
        if ts > self.last_used_user.get(chat.user_id, 0.0):
            self.last_used_user[chat.user_id] = ts
        if chat.scenario_id and ts > self.last_used_scenario.get(chat.scenario_id, 0.0):
            self.last_used_scenario[chat.scenario_id] = ts
        for lid in chat.brain_library_ids:
            if ts > self.last_used_library.get(lid, 0.0):
                self.last_used_library[lid] = ts


index = _Index()


def _scan_dir(
    parent: Path, marker: str, model_cls: type[T],
) -> tuple[dict[str, T], dict[str, Path]]:
    objs: dict[str, T] = {}
    paths: dict[str, Path] = {}
    if not parent.exists():
        return objs, paths
    for child in sorted(parent.iterdir()):
        if not child.is_dir():
            continue
        marker_path = child / marker
        if not marker_path.exists():
            continue
        try:
            obj = load_yaml(marker_path, model_cls)
        except RuntimeError:
            continue
        if obj is not None and hasattr(obj, "id"):
            entity_id: str = obj.id  # type: ignore[attr-defined]
            objs[entity_id] = obj
            paths[entity_id] = child
    return objs, paths


# ---------------------------------------------------------------------------
# Initialization (called from FastAPI lifespan)
# ---------------------------------------------------------------------------


def _clean_temp_dir() -> None:
    """Wipe the bulk-import scratch dir on startup.

    Anything in ``data/.tmp/`` is, by definition, stale: the in-memory
    token map is empty after a restart and nothing else writes here, so
    a leftover staging dir is just a half-uploaded archive from a
    previous process. Removing it eagerly avoids litter accumulating
    across restarts when an import was abandoned.
    """
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR, ignore_errors=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def _html_output_block() -> ContextPresetBlock:
    """The shared "HTML output" block seeded into default context presets.

    Tells the model about live-HTML rendering — but only when it's actually
    in effect. Gated on ``{{#if !sanitize_html}}`` so the whole block
    collapses to empty (and ``render_system_prompt`` drops it) while Sanitize
    HTML is on, which is the default. The block ships enabled; the macro,
    not the on/off flag, decides whether it contributes anything. The ``#``
    flag preserves the leading ``\\n`` so the section keeps its blank-line
    separation like the others."""

    html_output = (
        "{{#if !sanitize_html}}\n"
        "<FORMATTING>\n"
        "Your replies render as live HTML in the user's chat client. Markup "
        "you write takes effect directly: inline styles, images, and "
        "<script> blocks, which run once the message has rendered. The "
        "surrounding page loaded long before your message, so do your setup "
        "immediately — DOMContentLoaded and the window load event have "
        "already fired and will not call back. A message's scripts also "
        "re-run every time it is re-rendered — on reload, on branch "
        "navigation, or when the user just scrolls it back into view — so "
        "write them to be idempotent. Global state, timers, and listeners "
        "are fine, as long as each run first checks for what an earlier run "
        "already created and builds on that instead of duplicating it: keep a "
        "handle or flag on a namespace you own (e.g. window.myWidget) and, "
        "when it already exists, reuse it and skip the setup rather than "
        "starting a second interval or stacking another listener. To emit "
        "HTML or JavaScript exactly as written — especially markup containing "
        "*, _ or backticks that would otherwise be read as text styling, or "
        "HTML the page would otherwise interpret (a document you build in a "
        "string) — wrap that region in <|RAWHTML|> and <|/RAWHTML|>; everything "
        "between is inserted verbatim and the wrapper tags are dropped. Reach "
        "for this only when a visual or interactive element genuinely serves "
        "the scene; plain prose remains the default.\n"
        "</FORMATTING>{{/if}}"
    )
    return ContextPresetBlock(name="HTML output", enabled=True, content=html_output)


def _build_assistant_context_preset() -> ContextPreset:
    """Construct the seeded "Assistant" :class:`ContextPreset` — a plain
    helpful-assistant system prompt plus the shared HTML-output block."""

    return ContextPreset(
        name="Assistant",
        description="",
        author="",
        prefix_names=False,
        system_prompt_blocks=[
            ContextPresetBlock(
                name="System",
                enabled=True,
                content="You are a helpful, unfettered assistant.",
            ),
            _html_output_block(),
        ],
    )


def _build_default_context_preset() -> ContextPreset:
    """Construct the seeded "Default" :class:`ContextPreset`.

    Six blocks plus a floating Prefill assistant message. Per-field
    optional sections use the ``#`` flag so their load-bearing leading
    ``\\n`` survives the body trim; the Tags lines use the inline
    ``{{if X::\\n\\n::\\n}}`` ternary to introduce a blank line above
    ``Tags:`` only when a multi-line field preceded it. The final HTML-output
    block is always enabled but ``{{#if !sanitize_html}}``-gated, so it only
    reaches the model when raw-HTML rendering is actually on."""

    intro = (
        "You are roleplaying a character that interacts with the user's "
        "character. The user is looking for an immersive experience and "
        "consents to all kinds of content, including darker and adult "
        "themes and NSFW. You are allowed to freely explore any theme "
        "that comes up within the context of this fictional and "
        "consensual roleplay."
    )

    # Tags-wart fix: when a multi-line field (persona / appearance)
    # preceded, emit a blank line above ``Tags:``; otherwise flush. We
    # wrap the OR check in ``{{or}}`` so the inline-if's cond receives
    # ``"true"`` / ``""`` (neither colliding with any registered macro
    # name, so the bare-name auto-resolve doesn't fire on substituted
    # values that might happen to match a macro like ``{{persona}}``).
    character = (
        "\n<CHARACTER>\n"
        "Name: {{contact.name}}"
        "{{#if contact.species}}\n"
        "Species: {{contact.species}}{{/if}}"
        "{{#if contact.pronouns}}\n"
        "Pronouns: {{contact.pronouns}}{{/if}}"
        "{{#if contact.persona}}\n\n"
        "Personality:\n"
        "{{contact.persona}}{{/if}}"
        "{{#if contact.appearance}}\n\n"
        "Appearance:\n"
        "{{contact.appearance}}{{/if}}"
        "{{#if contact.tags}}"
        "{{if {{or::{{contact.persona}}::{{contact.appearance}}}}::\n\n::\n}}"
        "Tags: {{contact.tags}}{{/if}}\n"
        "</CHARACTER>"
    )

    # The User block is structurally identical to Character with
    # contact.* → user.*. No other ``contact.`` substrings appear in
    # Character, so a straight replace is safe.
    user_block = (
        character
        .replace("contact.", "user.")
        .replace("<CHARACTER>", "<USER>")
        .replace("</CHARACTER>", "</USER>")
    )

    scenario = (
        "{{#if {{or::{{scenario.environment}}::{{scenario.scene}}::{{chat.tags}}}}}}\n"
        "<SCENARIO>"
        "{{#if scenario.environment}}\n"
        "Environment:\n"
        "{{scenario.environment}}{{/if}}"
        "{{#if scenario.scene}}"
        "{{if scenario.environment::\n\n::\n}}"
        "Scene:\n"
        "{{scenario.scene}}{{/if}}"
        "{{#if chat.tags}}"
        "{{if {{or::{{scenario.environment}}::{{scenario.scene}}}}::\n\n::\n}}"
        "Tags: {{chat.tags}}{{/if}}\n"
        "</SCENARIO>{{/if}}"
    )

    lore = (
        "{{#if global_brains}}\n"
        "<LORE>\n"
        "{{global_brains}}\n"
        "</LORE>{{/if}}"
    )

    prefill = (
        "I love this setup, let me dive right into the roleplay. "
        "Going into character...3...2...1... Ready:\n\n"
        "{{contact.name}}:\n"
    )

    return ContextPreset(
        name="Default",
        description="",
        author="",
        prefix_names=True,
        system_prompt_blocks=[
            ContextPresetBlock(name="Intro", enabled=True, content=intro),
            ContextPresetBlock(name="Character", enabled=True, content=character),
            ContextPresetBlock(name="User", enabled=True, content=user_block),
            ContextPresetBlock(name="Scenario", enabled=True, content=scenario),
            ContextPresetBlock(name="Lore", enabled=True, content=lore),
            _html_output_block(),
        ],
        additional_messages=[
            ContextPresetAdditionalMessage(
                name="Prefill",
                enabled=True,
                role="assistant",
                mode="simple",
                simple_content=prefill,
                float_enabled=True,
                float_depth=0,
            ),
        ],
    )


def _backfill_context_preset_id(preset_id: str) -> None:
    """Backfill ``settings.generic.*.context_preset_id`` after the default
    context preset is seeded — sets the id on every provider config (named
    + custom) that's still ``None``. Never overwrites a user-set value.
    Bypasses ``save_settings`` so ``updated_at`` doesn't shift; this fires
    on the first-boot seed only, in the same window as the migration
    writers."""
    settings = load_settings()
    changed = False
    g = settings.generic
    for cfg in (g.novelai, g.openrouter, g.nanogpt):
        if cfg.context_preset_id is None:
            cfg.context_preset_id = preset_id
            changed = True
    for entry in g.openai_compatible.custom_providers:
        if entry.context_preset_id is None:
            entry.context_preset_id = preset_id
            changed = True
    if changed:
        atomic_write_yaml(SETTINGS_PATH, settings)


def initialize() -> None:
    """Create the data tree, seed defaults, populate the in-memory index."""
    _refresh_paths()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _clean_temp_dir()
    CONTACTS_DIR.mkdir(exist_ok=True)
    USERS_DIR.mkdir(exist_ok=True)
    SCENARIOS_DIR.mkdir(exist_ok=True)
    LIBRARIES_DIR.mkdir(exist_ok=True)
    CONTEXT_PRESETS_DIR.mkdir(exist_ok=True)
    CHATS_DIR.mkdir(exist_ok=True)

    if not SETTINGS_PATH.exists():
        atomic_write_yaml(SETTINGS_PATH, Settings())

    if not PRESETS_PATH.exists():
        default_preset = Preset()
        atomic_write_yaml(PRESETS_PATH, PresetLibrary(presets=[default_preset]))
        settings = load_settings()
        if settings.default_preset_id is None:
            settings.default_preset_id = default_preset.id
            atomic_write_yaml(SETTINGS_PATH, settings)

    index.populate()
    _warn_about_bak_dirs()
    _migrate_missing_version_ids()
    _migrate_missing_brain_ids()
    _migrate_missing_generic_block()
    _migrate_missing_message_count()
    _migrate_missing_message_attachments()
    _migrate_legacy_message_origin()

    # On a brand-new install, seed "Anon" and "User" personas so the
    # new-chat wizard has at least one persona to pick from.
    if not index.users:
        for persona in (User(name="Anon"), User(name="User")):
            _save_entity(
                persona, index.users, index.user_paths, USERS_DIR,
                "info.yaml", User, "user",
            )

    # Seed the default context presets for Generic mode. Skipped on
    # subsequent boots — the user may have deleted them. The "Default"
    # preset's id is backfilled into ``settings.generic.*.context_preset_id``
    # for any provider config that was still ``None``.
    if not index.context_presets:
        default_preset = _build_default_context_preset()
        _save_entity(
            default_preset,
            index.context_presets, index.context_preset_paths,
            CONTEXT_PRESETS_DIR, "info.yaml", ContextPreset, "context_preset",
        )
        _save_entity(
            _build_assistant_context_preset(),
            index.context_presets, index.context_preset_paths,
            CONTEXT_PRESETS_DIR, "info.yaml", ContextPreset, "context_preset",
        )
        _backfill_context_preset_id(default_preset.id)


def _migrate_missing_version_ids() -> None:
    """One-shot migration: persist a stable ``version_id`` for entities
    whose YAML on disk doesn't have the field.

    Background: ``version_id`` was added later as the optimistic-concurrency
    handle. Entities created before that have YAML files without the field,
    and pydantic's ``default_factory=new_id`` generates a *fresh* id on
    every load. That breaks ``check_version`` in the entity PUT route —
    every save 409s with a different ``current_version_id``, and Overwrite
    keeps re-conflicting because the next load generates yet another id.

    The migration walks each entity directory, reads the raw YAML, and if
    ``version_id`` is missing, persists the model (which has a freshly-
    generated id from the load) back via ``atomic_write_yaml`` — bypassing
    ``save_*`` so we don't bump ``updated_at``. Idempotent: subsequent
    starts find the field present and skip.
    """
    targets = (
        (index.contacts, index.contact_paths, "info.yaml", Contact, "contact"),
        (index.users, index.user_paths, "info.yaml", User, "user"),
        (index.scenarios, index.scenario_paths, "info.yaml", Scenario, "scenario"),
        (index.brain_libraries, index.brain_library_paths, "info.yaml", BrainLibrary, "brain_library"),
        (index.chats, index.chat_paths, "chat.yaml", Chat, "chat"),
    )
    migrated = 0
    for entities, paths, marker, model_cls, kind in targets:
        for entity_id, path in list(paths.items()):
            marker_path = path / marker
            raw = _read_yaml_raw(marker_path)
            if raw is None or "version_id" in raw:
                continue
            obj = load_yaml(marker_path, model_cls)
            if obj is None:
                continue
            atomic_write_yaml(marker_path, obj)
            entities[entity_id] = obj  # refresh cache with the version_id
            migrated += 1
    if migrated:
        log.info("Migrated %d entities to add stable version_id", migrated)


def _migrate_missing_brain_ids() -> None:
    """One-shot migration: backfill stable ``Brain.id`` for entities whose
    YAML on disk has brains without that field.

    Same shape as ``_migrate_missing_version_ids``: read raw YAML, scan
    every brain list for missing ``id`` keys, and if any are present,
    re-load via Pydantic (which fills in defaults via ``new_id``) and
    write back via ``atomic_write_yaml`` — bypassing ``save_*`` so we
    don't bump ``updated_at``.

    Brain lists checked:
    - ``Contact``: ``brains`` and ``scenarios[i].brains``
    - ``User``: ``brains``
    - ``Scenario``: ``brains``
    - ``ChatMessages``: ``messages[i].brains``
    """
    def _missing_id_in_any_list(*lists: object) -> bool:
        for lst in lists:
            if not isinstance(lst, list):
                continue
            for item in lst:
                if isinstance(item, dict) and "id" not in item:
                    return True
        return False

    def _contact_has_missing(raw: dict) -> bool:
        if _missing_id_in_any_list(raw.get("brains")):
            return True
        for s in raw.get("scenarios", []) or []:
            if isinstance(s, dict) and _missing_id_in_any_list(s.get("brains")):
                return True
        return False

    def _messages_has_missing(raw: dict) -> bool:
        for m in raw.get("messages", []) or []:
            if isinstance(m, dict) and _missing_id_in_any_list(m.get("brains")):
                return True
        return False

    migrated = 0

    # Entity-marker files (info.yaml / scenario info.yaml / chat.yaml)
    targets = (
        (index.contacts, index.contact_paths, "info.yaml", Contact, _contact_has_missing),
        (index.users, index.user_paths, "info.yaml", User, lambda raw: _missing_id_in_any_list(raw.get("brains"))),
        (index.scenarios, index.scenario_paths, "info.yaml", Scenario, lambda raw: _missing_id_in_any_list(raw.get("brains"))),
        (index.brain_libraries, index.brain_library_paths, "info.yaml", BrainLibrary, lambda raw: _missing_id_in_any_list(raw.get("brains"))),
    )
    for entities, paths, marker, model_cls, predicate in targets:
        for entity_id, path in list(paths.items()):
            marker_path = path / marker
            raw = _read_yaml_raw(marker_path)
            if raw is None or not predicate(raw):
                continue
            obj = load_yaml(marker_path, model_cls)
            if obj is None:
                continue
            atomic_write_yaml(marker_path, obj)
            entities[entity_id] = obj  # refresh cache with backfilled Brain.ids
            migrated += 1

    # Chat messages live in a sibling file. (Not cached in _Index; only the
    # Chat marker is.)
    for chat_id, path in list(index.chat_paths.items()):
        msg_path = path / "messages.yaml"
        raw = _read_yaml_raw(msg_path)
        if raw is None or not _messages_has_missing(raw):
            continue
        obj = load_yaml(msg_path, ChatMessages)
        if obj is None:
            continue
        atomic_write_yaml(msg_path, obj)
        migrated += 1

    if migrated:
        log.info("Migrated %d entities to add stable Brain.id", migrated)


def _migrate_missing_message_attachments() -> None:
    """One-shot migration: ensure every persisted ChatMessage carries
    the ``attachments`` field on disk.

    ``ChatMessage.attachments`` is a ``default_factory=list`` field
    (CLAUDE.md invariant 9). Without an explicit backfill, Pydantic
    materializes ``[]`` on each load and the missing-key state survives
    on disk indefinitely, which would let a future schema addition
    interpret the absence as something other than "empty list". We
    detect "missing key" via raw YAML, then load + write back through
    the Pydantic model so the field lands at its default value.

    Skips ``updated_at`` bump (raw write); idempotent on subsequent
    boots since the key is now present.
    """
    migrated_files = 0
    migrated_messages = 0
    for chat_id, path in list(index.chat_paths.items()):
        msg_path = path / "messages.yaml"
        raw = _read_yaml_raw(msg_path)
        if raw is None:
            continue
        raw_messages = raw.get("messages")
        if not isinstance(raw_messages, list):
            continue
        missing = [
            i for i, m in enumerate(raw_messages)
            if isinstance(m, dict) and "attachments" not in m
        ]
        if not missing:
            continue
        obj = load_yaml(msg_path, ChatMessages)
        if obj is None:
            continue
        atomic_write_yaml(msg_path, obj)
        migrated_files += 1
        migrated_messages += len(missing)
    if migrated_files:
        log.info(
            "Backfilled empty 'attachments' on %d messages across %d chats",
            migrated_messages,
            migrated_files,
        )


def _migrate_legacy_message_origin() -> None:
    """One-shot migration: stamp ``origin: "manual"`` on user-side messages
    whose YAML on disk doesn't carry the field.

    The ``origin`` field defaults to ``"aer"`` via Pydantic so existing
    contact-side messages classify correctly on load. User-side messages
    are the exception — they were typed by the user, not generated by AER,
    so their on-disk form needs an explicit ``origin: "manual"`` to render
    correctly going forward.

    Inspect raw YAML to distinguish "key missing" from "stored as 'aer'
    because Pydantic defaulted on a prior load". Load via Pydantic, patch
    the specific positions where the raw form lacked the key AND the
    message is user-side, write back via ``atomic_write_yaml`` (bypassing
    ``save_*`` so ``updated_at`` doesn't bump). Idempotent.

    Impersonate-generated user-side messages legitimately carry
    ``origin="aer"`` / ``"generic"``; this migration only touches entries
    where the key is missing on disk, so already-stamped impersonate
    messages are left alone. Gated by ``Chat.user_origin_migrated_at`` —
    chats that have already run through this migration are skipped on
    subsequent boots, so a future change to the migration cannot
    accidentally overwrite impersonate-stamped user origins.
    """
    migrated_files = 0
    migrated_messages = 0
    gated_skips = 0
    for chat_id, path in list(index.chat_paths.items()):
        chat_obj = index.chats.get(chat_id)
        if chat_obj is not None and getattr(chat_obj, "user_origin_migrated_at", None):
            gated_skips += 1
            continue
        msg_path = path / "messages.yaml"
        raw = _read_yaml_raw(msg_path)
        if raw is None:
            continue
        raw_messages = raw.get("messages")
        if not isinstance(raw_messages, list):
            continue
        positions: list[int] = []
        for i, m in enumerate(raw_messages):
            if not isinstance(m, dict):
                continue
            if m.get("sender") == "user" and "origin" not in m:
                positions.append(i)
        # Always stamp the gate (even for chats with no eligible messages)
        # so this migration's per-chat work is genuinely one-shot.
        if positions:
            obj = load_yaml(msg_path, ChatMessages)
            if obj is None:
                continue
            for i in positions:
                if 0 <= i < len(obj.messages):
                    obj.messages[i].origin = "manual"
            atomic_write_yaml(msg_path, obj)
            migrated_files += 1
            migrated_messages += len(positions)
        if chat_obj is not None:
            chat_obj.user_origin_migrated_at = time.time()
            chat_path_yaml = path / "chat.yaml"
            atomic_write_yaml(chat_path_yaml, chat_obj)
    if migrated_files:
        log.info(
            "Migrated %d messages across %d chats to stamp origin='manual' on user-side legacy entries",
            migrated_messages,
            migrated_files,
        )
    if gated_skips:
        log.debug(
            "Skipped %d chats already past the user-origin-manual migration gate",
            gated_skips,
        )


def _migrate_missing_message_count() -> None:
    """One-shot migration: backfill ``Chat.message_count`` on chats whose
    ``chat.yaml`` predates the field.

    Walks each chat once, parses ``messages.yaml``, computes the active
    branch length, persists via ``atomic_write_yaml`` (bypassing
    ``save_chat`` so ``updated_at`` doesn't bump). The cached chat object
    is the same Python instance held in ``index.chats``, so mutating
    ``chat.message_count`` updates the in-memory cache too.

    Idempotent: subsequent boots find the key present and skip. Detection
    uses the raw YAML so we distinguish "missing on disk" from "stored as
    0 because the chat is empty".
    """
    migrated = 0
    for chat_id, path in list(index.chat_paths.items()):
        chat_yaml = path / "chat.yaml"
        raw = _read_yaml_raw(chat_yaml)
        if raw is None or "message_count" in raw:
            continue
        chat = index.chats.get(chat_id)
        if chat is None:
            continue
        messages = load_chat_messages(chat_id)
        chat.message_count = len(get_active_path(chat, messages.messages))
        atomic_write_yaml(chat_yaml, chat)
        migrated += 1
    if migrated:
        log.info("Migrated %d chats to add message_count", migrated)


def _migrate_missing_generic_block() -> None:
    """One-shot migration: persist defaults for ``provider_mode`` /
    ``generic`` on settings.yaml files predating the generic-provider
    settings block.

    Pydantic supplies defaults on load, so the in-memory ``Settings``
    object is correct without this migration. We still write the keys
    back so the YAML on disk reflects them — matters for users who
    hand-grep / hand-edit ``data/settings.yaml``.

    Idempotent: subsequent boots find both keys present and skip.

    Preset fields are intentionally NOT migrated. The three new
    ``Preset`` size fields (``max_context_tokens`` etc.) come in via
    Pydantic defaults on load; ``presets.yaml`` stays byte-identical
    until the user next saves a preset.
    """
    raw = _read_yaml_raw(SETTINGS_PATH)
    if raw is None:
        return
    if "provider_mode" in raw and "generic" in raw:
        return
    settings = load_settings()
    atomic_write_yaml(SETTINGS_PATH, settings)
    log.info("Migrated settings.yaml to add provider_mode/generic defaults")


def _warn_about_bak_dirs() -> None:
    """Log a warning for each ``*.bak`` directory under the entity dirs.

    These are left over from importer "replace" mode: the original entity
    directory was renamed to ``<dir>.bak`` while the new content was being
    staged, and either the import is mid-flight (server was restarted
    during an import) or it failed in a way that left the .bak behind.
    Don't auto-delete — the user should inspect and decide.
    """
    for parent in (CONTACTS_DIR, USERS_DIR, SCENARIOS_DIR, LIBRARIES_DIR, CHATS_DIR):
        if not parent.exists():
            continue
        for child in parent.iterdir():
            if child.is_dir() and child.name.endswith(".bak"):
                log.warning(
                    "Found leftover .bak directory: %s — this is recovery "
                    "data from an interrupted import. Inspect and remove "
                    "manually.", child,
                )


# ---------------------------------------------------------------------------
# Settings & presets
# ---------------------------------------------------------------------------


def load_settings() -> Settings:
    return load_yaml(SETTINGS_PATH, Settings) or Settings()


def save_settings(settings: Settings) -> None:
    atomic_write_yaml(SETTINGS_PATH, settings)


def load_presets() -> PresetLibrary:
    return load_yaml(PRESETS_PATH, PresetLibrary) or PresetLibrary()


def save_presets(lib: PresetLibrary) -> None:
    atomic_write_yaml(PRESETS_PATH, lib)


# ---------------------------------------------------------------------------
# Generic entity I/O helpers
# ---------------------------------------------------------------------------


def _list_entities(entities_idx: dict[str, T]) -> list[T]:
    """Return every parsed entity from the in-memory cache. No disk I/O."""
    return list(entities_idx.values())


def _get_entity(entities_idx: dict[str, T], entity_id: str) -> T | None:
    """Return the cached parsed entity. No disk I/O."""
    return entities_idx.get(entity_id)


def _resolve_collision(
    desired: Path, marker: str, model_cls: type[T], own_id: str, kind: str,
) -> None:
    """Make ``desired`` available for a directory rename.

    If ``desired`` already exists with a parseable marker file naming a
    different entity, raise — refusing to clobber a real entity (would
    silently destroy data). If it has no marker (stale from a prior
    partial run) or a marker that names the same entity, controlled-delete
    its known files and rmdir; user-stashed extras survive in place.
    """
    marker_path = desired / marker
    if marker_path.exists():
        try:
            existing_obj = load_yaml(marker_path, model_cls)
        except RuntimeError:
            existing_obj = None
        if existing_obj is not None and getattr(existing_obj, "id", None) != own_id:
            raise RuntimeError(
                f"Refusing to overwrite directory {desired} — it already "
                f"contains entity {getattr(existing_obj, 'id', '?')!r}."
            )
    # The collision is either a stale partial-run dir, or a same-id
    # leftover. Controlled-delete using the appropriate per-kind remover
    # so user files survive.
    remover = _REMOVERS.get(kind)
    if remover is not None:
        remover(desired)


def _save_entity(
    obj: BaseModel,
    entities_idx: dict[str, BaseModel],
    paths_idx: dict[str, Path],
    parent_dir: Path,
    marker: str,
    model_cls: type[BaseModel],
    kind: str,
    *,
    bump_version: bool = True,
) -> None:
    """Persist ``obj`` to ``parent_dir/{slug}-{id8}/{marker}`` atomically.

    On success, both ``entities_idx`` (parsed-model cache) and
    ``paths_idx`` (directory lookup) are updated. The model cache is
    updated AFTER the disk write so a failed write doesn't leave a stale
    cached version.

    ``bump_version=False`` lets file-upload routes and chat-tree mutations
    save without invalidating an open edit-view draft's ``version_id`` —
    optimistic concurrency in the entity PUT path stays in force, but
    sibling routes that mutate the same file (e.g. ``selected_child_id``
    on a chat) don't 409 a draft the user didn't intend to conflict with.
    """
    entity_id: str = obj.id  # type: ignore[attr-defined]
    if bump_version and hasattr(obj, "version_id"):
        # Reroll the version_id on every entity-level save so the entity
        # PUT route can detect concurrent edits via 409.
        obj.version_id = new_id()  # type: ignore[attr-defined]
    label = (
        getattr(obj, "name", None)
        or getattr(obj, "title", None)
        or "untitled"
    )
    desired = parent_dir / entity_dir_name(label, entity_id)
    existing = paths_idx.get(entity_id)

    if existing is not None and existing != desired:
        # Name (slug) changed — rename the directory. Resolve any collision
        # at the destination first; refuse to clobber a real different
        # entity, controlled-delete a stale leftover.
        if desired.exists():
            _resolve_collision(desired, marker, model_cls, entity_id, kind)
        if desired.exists():
            # _resolve_collision left user-stashed files behind; the rename
            # below would fail with EEXIST. Fall back to writing into the
            # existing dir + leaving the old slug behind for the user to
            # inspect, rather than mangling their files.
            log.warning(
                "Cannot rename %s → %s: destination still has files. "
                "Leaving old directory in place.", existing, desired,
            )
        else:
            existing.rename(desired)
            paths_idx[entity_id] = desired
            existing = desired
    elif existing is None:
        desired.mkdir(parents=True, exist_ok=True)
        paths_idx[entity_id] = desired
        existing = desired

    atomic_write_yaml(existing / marker, obj)
    # Update the parsed-entity cache after the disk write succeeds. A
    # failed atomic_write_yaml raises before this line, leaving the cache
    # consistent with what's on disk.
    entities_idx[entity_id] = obj


def _delete_entity(
    entities_idx: dict[str, BaseModel],
    paths_idx: dict[str, Path],
    entity_id: str,
    kind: str,
) -> bool:
    """Remove the entity directory using the per-kind controlled-delete.

    Marker file disappears so the indexer skips this directory on next
    boot; user-stashed files survive in place. Both the parsed cache and
    the path lookup drop ``entity_id``.
    """
    path = paths_idx.pop(entity_id, None)
    entities_idx.pop(entity_id, None)
    if path is None:
        return False
    remover = _REMOVERS.get(kind)
    if remover is None:
        log.error("No remover registered for kind %r", kind)
        return False
    remover(path)
    return True


# ---------------------------------------------------------------------------
# Contact CRUD
# ---------------------------------------------------------------------------


def list_contacts() -> list[Contact]:
    return _list_entities(index.contacts)


def get_contact(contact_id: str) -> Contact | None:
    return _get_entity(index.contacts, contact_id)


def save_contact(contact: Contact, *, bump_version: bool = True) -> Contact:
    contact.updated_at = now_seconds()
    _save_entity(
        contact, index.contacts, index.contact_paths,
        CONTACTS_DIR, "info.yaml", Contact, "contact",
        bump_version=bump_version,
    )
    return contact


def delete_contact(contact_id: str) -> bool:
    return _delete_entity(index.contacts, index.contact_paths, contact_id, "contact")


def contact_dir(contact_id: str) -> Path | None:
    return index.contact_paths.get(contact_id)


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


def list_users() -> list[User]:
    return _list_entities(index.users)


def get_user(user_id: str) -> User | None:
    return _get_entity(index.users, user_id)


def save_user(user: User, *, bump_version: bool = True) -> User:
    user.updated_at = now_seconds()
    _save_entity(
        user, index.users, index.user_paths,
        USERS_DIR, "info.yaml", User, "user",
        bump_version=bump_version,
    )
    return user


def delete_user(user_id: str) -> bool:
    return _delete_entity(index.users, index.user_paths, user_id, "user")


def user_dir(user_id: str) -> Path | None:
    return index.user_paths.get(user_id)


# ---------------------------------------------------------------------------
# Scenario CRUD
# ---------------------------------------------------------------------------


def list_scenarios() -> list[Scenario]:
    return _list_entities(index.scenarios)


def get_scenario(scenario_id: str) -> Scenario | None:
    return _get_entity(index.scenarios, scenario_id)


def save_scenario(scenario: Scenario, *, bump_version: bool = True) -> Scenario:
    scenario.updated_at = now_seconds()
    _save_entity(
        scenario, index.scenarios, index.scenario_paths,
        SCENARIOS_DIR, "info.yaml", Scenario, "scenario",
        bump_version=bump_version,
    )
    return scenario


def delete_scenario(scenario_id: str) -> bool:
    return _delete_entity(index.scenarios, index.scenario_paths, scenario_id, "scenario")


def scenario_dir(scenario_id: str) -> Path | None:
    return index.scenario_paths.get(scenario_id)


# ---------------------------------------------------------------------------
# Brain library CRUD
# ---------------------------------------------------------------------------


def list_brain_libraries() -> list[BrainLibrary]:
    return _list_entities(index.brain_libraries)


def get_brain_library(library_id: str) -> BrainLibrary | None:
    return _get_entity(index.brain_libraries, library_id)


def save_brain_library(library: BrainLibrary, *, bump_version: bool = True) -> BrainLibrary:
    library.updated_at = now_seconds()
    _save_entity(
        library, index.brain_libraries, index.brain_library_paths,
        LIBRARIES_DIR, "info.yaml", BrainLibrary, "brain_library",
        bump_version=bump_version,
    )
    return library


def delete_brain_library(library_id: str) -> bool:
    return _delete_entity(
        index.brain_libraries, index.brain_library_paths, library_id, "brain_library",
    )


def brain_library_dir(library_id: str) -> Path | None:
    return index.brain_library_paths.get(library_id)


# ---------------------------------------------------------------------------
# Context preset CRUD
# ---------------------------------------------------------------------------


def list_context_presets() -> list[ContextPreset]:
    return _list_entities(index.context_presets)


def get_context_preset(preset_id: str) -> ContextPreset | None:
    return _get_entity(index.context_presets, preset_id)


def save_context_preset(
    preset: ContextPreset, *, bump_version: bool = True,
) -> ContextPreset:
    preset.updated_at = now_seconds()
    _save_entity(
        preset, index.context_presets, index.context_preset_paths,
        CONTEXT_PRESETS_DIR, "info.yaml", ContextPreset, "context_preset",
        bump_version=bump_version,
    )
    return preset


def delete_context_preset(preset_id: str) -> bool:
    return _delete_entity(
        index.context_presets, index.context_preset_paths, preset_id, "context_preset",
    )


def context_preset_dir(preset_id: str) -> Path | None:
    return index.context_preset_paths.get(preset_id)


def chat_dir(chat_id: str) -> Path | None:
    """Return the on-disk directory for ``chat_id``, or None if unknown.

    Used by routes that need to read or write chat-scoped assets
    (e.g. the Generic-mode image proxy's per-chat ``images/`` subdir).
    """
    return index.chat_paths.get(chat_id)


# ---------------------------------------------------------------------------
# Chat CRUD (metadata + messages + bookmarks)
# ---------------------------------------------------------------------------


def list_chats() -> list[Chat]:
    return _list_entities(index.chats)


def get_chat(chat_id: str) -> Chat | None:
    return _get_entity(index.chats, chat_id)


def save_chat(chat: Chat, *, bump_version: bool = True) -> Chat:
    """Persist chat metadata.

    ``bump_version=False`` is the right choice for chat-tree mutations
    (creating messages, navigating branches, regenerating, bookmarking) so
    they don't 409 the chat info modal's open draft. The entity PUT path
    in ``routers/chats.update_chat`` keeps the default and rerolls the
    version_id every entity-level edit.
    """
    _save_entity(
        chat, index.chats, index.chat_paths,
        CHATS_DIR, "chat.yaml", Chat, "chat",
        bump_version=bump_version,
    )
    index.bump_last_used(chat)
    return chat


def delete_chat(chat_id: str) -> bool:
    ok = _delete_entity(index.chats, index.chat_paths, chat_id, "chat")
    if ok:
        # Last-used timestamps are derived from chats; rebuild the cache
        # over the remaining chats. Cheap (in-memory iteration).
        index._rebuild_last_used()
    return ok


def chat_dir(chat_id: str) -> Path | None:
    return index.chat_paths.get(chat_id)


def chat_attachments_dir(chat_id: str) -> Path | None:
    """Return ``data/chats/{slug}-{id8}/attachments/`` for ``chat_id``.

    Returns ``None`` if the chat is unknown. Created lazily by the
    upload route when an attachment is first stored.
    """
    d = index.chat_paths.get(chat_id)
    if d is None:
        return None
    return d / "attachments"


def chat_attachment_cache_dir(chat_id: str) -> Path | None:
    """Return ``attachments/.cached/`` for ``chat_id`` — the cache of
    JPEG-compressed attachment copies sent to multimodal providers.

    Dot-prefixed so it sits outside the ``{att_id}.*`` globs that locate
    originals (send-time lookup, export, message-create validation). It's a
    regenerable derivative: swept by :func:`remove_chat_dir` on chat
    deletion and never embedded in exports.
    """
    d = chat_attachments_dir(chat_id)
    if d is None:
        return None
    return d / ".cached"


def load_chat_messages(chat_id: str) -> ChatMessages:
    d = index.chat_paths.get(chat_id)
    if d is None:
        return ChatMessages()
    return load_yaml(d / "messages.yaml", ChatMessages) or ChatMessages()


def save_chat_messages(chat_id: str, messages: ChatMessages) -> None:
    d = index.chat_paths.get(chat_id)
    if d is None:
        raise KeyError(f"Chat {chat_id!r} not found")
    atomic_write_yaml(d / "messages.yaml", messages)
    # Refresh ``Chat.message_count`` on the cached chat so the very next
    # ``save_chat`` (which tree-mutation routes follow with) persists the
    # current count. The cache holds the same Python object the route is
    # mutating — including any in-progress ``selected_child_id`` change —
    # so this read sees up-to-date branching.
    chat = index.chats.get(chat_id)
    if chat is not None:
        chat.message_count = len(get_active_path(chat, messages.messages))


def recount_active_path(chat: Chat, messages: ChatMessages) -> None:
    """Mutate ``chat.message_count`` to match the active-branch length.

    Use this from routes that change ``chat.selected_child_id`` without
    also writing ``messages.yaml`` (delete_message, restore_message,
    select_child, restore_path, bookmark_jump). The route's subsequent
    ``save_chat(chat, bump_version=False)`` then persists the fresh count.
    """
    chat.message_count = len(get_active_path(chat, messages.messages))


def load_chat_bookmarks(chat_id: str) -> ChatBookmarks:
    d = index.chat_paths.get(chat_id)
    if d is None:
        return ChatBookmarks()
    return load_yaml(d / "bookmarks.yaml", ChatBookmarks) or ChatBookmarks()


def save_chat_bookmarks(chat_id: str, bookmarks: ChatBookmarks) -> None:
    d = index.chat_paths.get(chat_id)
    if d is None:
        raise KeyError(f"Chat {chat_id!r} not found")
    atomic_write_yaml(d / "bookmarks.yaml", bookmarks)


def save_chat_with_all(
    chat: Chat,
    messages: ChatMessages,
    bookmarks: ChatBookmarks,
    *,
    bump_version: bool = True,
) -> Chat:
    """Persist a chat plus its messages + bookmarks in safe order.

    Order: ``messages.yaml`` → ``bookmarks.yaml`` → ``chat.yaml``. The
    references go in last (``chat.selected_child_id`` points at messages,
    ``chat.last_deleted_child`` etc.) so a crash anywhere leaves a state
    where any reference in ``chat.yaml`` is guaranteed to resolve. The
    indexer keys off ``chat.yaml`` existing, so a partial directory with
    just messages + bookmarks but no ``chat.yaml`` is invisible to the
    rest of the app — self-healing on next save.

    Used by ``create_chat`` (where chat.yaml carries a greeting reference
    that must resolve) and the chat importer.
    """
    save_chat(chat, bump_version=bump_version)
    # save_chat ensures the directory exists and is indexed; now write the
    # other two files into it. We do this in the SAFE order even though
    # save_chat went first — because save_chat is the one creating the
    # directory and indexing it. Re-write chat.yaml at the end so the
    # references in chat.selected_child_id point at messages that have
    # actually been persisted.
    save_chat_messages(chat.id, messages)
    save_chat_bookmarks(chat.id, bookmarks)
    save_chat(chat, bump_version=False)
    return chat

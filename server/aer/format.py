"""AER context format builders: system prompt, message body, brain blocks,
style instruction."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from server.aer.macros import apply_macros
from server.models import (
    Brain,
    BrainLibrary,
    Chat,
    ChatMessage,
    Contact,
    ContactScenario,
    Emotion,
    ExampleChat,
    Intimacy,
    ResponseLength,
    Scenario,
    Settings,
    Style,
    User,
)


ENSP = "\u2002"  # EN SPACE — distinguishes example quotes from real chat history
SECTION_SEPARATOR = "----"


# ---------------------------------------------------------------------------
# Field formatting
# ---------------------------------------------------------------------------


def _format_field(name: str, value: str) -> str:
    """Format ``"{name}: value"``; continuation lines indented 4 spaces. Empty → ""."""
    value = (value or "").strip()
    if not value:
        return ""
    lines = value.split("\n")
    out = [f"{name}: {lines[0]}\n"]
    for cont in lines[1:]:
        out.append(f"    {cont}\n")
    return "".join(out)


# ---------------------------------------------------------------------------
# Example quotes
# ---------------------------------------------------------------------------


def _format_example_chat(example: ExampleChat, char_name: str, idx: int) -> str:
    char_name = (char_name or "").strip()
    user_name = (example.user_name or "").strip() or "User"
    parts: list[str] = [f"{idx}.\n"]
    for msg in example.messages:
        text = (msg.text or "").strip()
        if not text:
            continue
        sender = char_name if msg.is_contact else user_name
        text_lines = text.split("\n")
        parts.append(f"{ENSP}{sender}: {text_lines[0]}\n")
        for cont in text_lines[1:]:
            parts.append(f"{ENSP}    {cont}\n")
        if msg.is_contact:
            # Every contact bubble carries an Emotion line in context.
            # Imports that don't supply an emotion default to neutral
            # rather than skipping the line — omitting it would train
            # the model to drop the Emotion: prefix and break the
            # streaming parser's bubble boundaries.
            emotion = (msg.emotion or Emotion.NEUTRAL).value
            parts.append(f"{ENSP}  Emotion: {emotion}\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Contact / user / scenario sections
# ---------------------------------------------------------------------------


def _intimacy_line(intimacy: Intimacy, user_name: str) -> str:
    user_name = (user_name or "").strip()
    if intimacy == Intimacy.STRANGER:
        return f"Opinion of {user_name}: Doesn't know {user_name}.\n"
    return f"Relationship: {intimacy.value}\n"


def _character_section(persona, intimacy: Intimacy, addressee_name: str) -> str:
    """Render the "Type: contact" section for whichever persona plays the
    assistant role. In normal generation that's the Contact; in impersonate
    mode the personas are flipped and ``persona`` is the User.
    """
    name = (persona.name or "").strip()
    parts: list[str] = []
    parts.append(f"{name}\n")
    parts.append("Type: contact\n")
    parts.append(_format_field("Gender", persona.gender))
    parts.append(_format_field("Pronouns", persona.pronouns))
    parts.append(_format_field("Species", persona.species))
    parts.append(_format_field("Personality", persona.persona))
    parts.append(_format_field("Appearance", persona.appearance))
    parts.append(_intimacy_line(intimacy, addressee_name))
    parts.append(_format_field("Categories", persona.tags))

    example_chats = getattr(persona, "example_chats", None) or []
    if example_chats:
        parts.append("Example quotes:\n")
        for i, ex in enumerate(example_chats, start=1):
            parts.append(_format_example_chat(ex, name, i))

    return "".join(parts)


def _user_section(persona) -> str:
    """Render the "Type: user" section for whichever persona plays the user
    role. Flipped in impersonate mode (``persona`` is the Contact)."""
    name = (persona.name or "").strip()
    parts: list[str] = []
    parts.append(f"{name}\n")
    parts.append("Type: user\n")
    parts.append(_format_field("Gender", persona.gender))
    parts.append(_format_field("Pronouns", persona.pronouns))
    parts.append(_format_field("Species", persona.species))
    parts.append(_format_field("Personality", persona.persona))
    parts.append(_format_field("Appearance", persona.appearance))
    parts.append(_format_field("Categories", persona.tags))
    return "".join(parts)


def _scenario_section(scenario: Scenario | None, chat_tags: str = "") -> str:
    """Render the scenario section. Environment/Scene come from the scenario;
    Tags come from ``chat_tags`` (chat-level), since ``scenario.tags`` is only
    a seed value copied into ``Chat.tags`` at chat creation."""
    env = (scenario.environment if scenario else "") or ""
    scene = (scenario.scene if scenario else "") or ""
    chat_tags = chat_tags or ""
    if not (env.strip() or scene.strip() or chat_tags.strip()):
        return ""
    parts: list[str] = []
    parts.append(_format_field("Environment", env))
    parts.append(_format_field("Scene", scene))
    parts.append(_format_field("Tags", chat_tags))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Brain blocks
# ---------------------------------------------------------------------------


def _brain_block(brain: Brain) -> str:
    name = (brain.name or "").strip()
    content = (brain.content or "").strip()
    return f"{SECTION_SEPARATOR}\n{name}\n{content}\n"


def render_brain_block(brain: Brain) -> str:
    """Public alias for :func:`_brain_block`. Used by the ``{{global_brains}}``
    macro so AER's system-prompt path and Generic mode produce byte-identical
    brain blocks."""
    return _brain_block(brain)


def render_brain_blocks(brains: Iterable[Brain]) -> str:
    """Concatenated brain blocks. Each block already ends with ``\\n``; the
    caller may want to ``.rstrip`` if a wrapping tag (e.g. ``</LORE>``) sits
    on the next line."""
    return "".join(_brain_block(b) for b in brains)


def collect_global_brains(
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: Iterable[BrainLibrary] | None = None,
) -> list[Brain]:
    """All global brains attached to entities that participate in the system prompt.

    Order is contact → user → scenario → libraries (in attach order); preserved
    across the conditional split helpers below.
    """
    out: list[Brain] = []
    out.extend(b for b in contact.brains if not b.disabled)
    out.extend(b for b in user.brains if not b.disabled)
    if scenario is not None:
        out.extend(b for b in scenario.brains if not b.disabled)
    if libraries:
        for lib in libraries:
            out.extend(b for b in lib.brains if not b.disabled)
    return out


def _is_conditional(brain: Brain) -> bool:
    return bool(brain.keys) or brain.advanced is not None


def collect_unconditional_global_brains(
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: Iterable[BrainLibrary] | None = None,
) -> list[Brain]:
    """Subset of ``collect_global_brains`` that always fires — these stay
    baked into the AER system prompt at their natural location.
    ``collect_global_brains`` already drops ``disabled`` brains."""
    return [b for b in collect_global_brains(contact, user, scenario, libraries) if not _is_conditional(b)]


def collect_conditional_global_brains(
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    libraries: Iterable[BrainLibrary] | None = None,
) -> list[Brain]:
    """Subset of ``collect_global_brains`` that runs through the activation
    engine — these are spliced as a single relocated system message when (and
    only when) they activate. Disabled brains are already filtered out by
    ``collect_global_brains``."""
    return [b for b in collect_global_brains(contact, user, scenario, libraries) if _is_conditional(b)]


def build_local_brain_block(brains: Iterable[Brain]) -> str:
    """Combine multiple local brain entries into a single ``system`` message body."""
    return "".join(_brain_block(b) for b in brains).strip()


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


def build_system_prompt(
    contact: Contact,
    user: User,
    scenario: Scenario | None,
    intimacy: Intimacy,
    *,
    chat_tags: str = "",
    include_scenario: bool = True,
    libraries: Iterable[BrainLibrary] | None = None,
    now: datetime | None = None,
    chat: Chat | None = None,
    contact_scenario: ContactScenario | None = None,
    settings: Settings | None = None,
    active_path: Iterable[ChatMessage] | None = None,
    messages_tree: Iterable[ChatMessage] | None = None,
    rollover_cursor: int = 0,
    is_mobile: bool = False,
    generation_type: str | None = None,
    flip_personas: bool = False,
) -> str:
    """Build the ``[0]`` system message content.

    ``include_scenario=False`` is used for deletion-response contexts, where
    the contact is told the user has left rather than handed the active
    scene.

    ``chat_tags`` is the chat-level tag string; when non-empty it is
    rendered as the scenario section's Tags line, causing that section to
    be emitted even when ``scenario is None``. ``scenario.tags`` is *not*
    rendered here — it's only a seed value copied into ``Chat.tags`` at
    chat creation.

    ``libraries`` are global brain libraries attached to the chat. Their
    unconditional brains are appended after the contact/user/scenario
    unconditionals, in attach order.

    The remaining kwargs (``chat``, ``contact_scenario``, ``settings``,
    ``active_path``, ``messages_tree``, ``rollover_cursor``, ``is_mobile``,
    ``generation_type``) feed the macro registry — without them, history-
    aware / runtime-aware macros resolve to empty strings.
    """
    parts: list[str] = []
    parts.append("AetherRoom\n")
    parts.append(f"{SECTION_SEPARATOR}\n")
    if flip_personas:
        # Impersonate: the user persona plays the assistant role, the
        # contact persona is addressed as the user. Brain/scenario blocks
        # below stay sourced from the original entities.
        parts.append(_character_section(user, intimacy, contact.name))
        parts.append(f"{SECTION_SEPARATOR}\n")
        parts.append(_user_section(contact))
    else:
        parts.append(_character_section(contact, intimacy, user.name))
        parts.append(f"{SECTION_SEPARATOR}\n")
        parts.append(_user_section(user))

    if include_scenario:
        scen = _scenario_section(scenario, chat_tags=chat_tags)
        if scen:
            parts.append(f"{SECTION_SEPARATOR}\n")
            parts.append(scen)

    effective_scenario = scenario if include_scenario else None
    for brain in collect_unconditional_global_brains(contact, user, effective_scenario, libraries):
        parts.append(_brain_block(brain))

    raw = "".join(parts)
    # Impersonate flips contact ↔ user inside the macro context too so
    # ``{{contact.X}}`` / ``{{user.X}}`` references resolve to whichever
    # persona is playing each role in this turn. Contact-only fields
    # (greeting / reminder_brain / example_chats) fall back to empty via
    # ``getattr`` defaults inside the macros — User has no equivalent.
    macro_contact, macro_user = (user, contact) if flip_personas else (contact, user)
    return apply_macros(
        raw,
        macro_contact,
        macro_user,
        scenario=effective_scenario,
        contact_scenario=contact_scenario,
        chat=chat,
        active_path=active_path,
        messages_tree=messages_tree,
        rollover_cursor=rollover_cursor,
        settings=settings,
        is_mobile=is_mobile,
        generation_type=generation_type,
        now=now,
        libraries=libraries,
    ).strip()


# ---------------------------------------------------------------------------
# Message body
# ---------------------------------------------------------------------------


def format_message(
    message: ChatMessage,
    *,
    display_sender_name: str | None = None,
    emit_emotion: bool | None = None,
    default_neutral_emotion: bool = False,
) -> str:
    """Render a ``ChatMessage`` to its API content body (multi-bubble, optional emotion).

    Impersonate mode overrides:
        ``display_sender_name`` — render as a different speaker (e.g. the
            user persona's name on a contact bubble after role-flipping).
        ``emit_emotion`` — force-on or force-off the ``  Emotion: …`` line
            regardless of the message's stored sender.
        ``default_neutral_emotion`` — when emitting, default missing
            emotions to NEUTRAL (used for user-side bubbles that don't
            carry emotions in storage).
    """
    sender_name = (display_sender_name or message.sender_name or "").strip()
    if emit_emotion is None:
        emit_emotion = message.sender == "contact"
    parts: list[str] = []
    for bubble in message.body:
        text = (bubble.text or "").strip()
        if not text:
            continue
        lines = [line.rstrip() for line in text.split("\n")]
        while lines and lines[-1] == "":
            lines.pop()
        if not lines:
            continue
        parts.append(f"{sender_name}: {lines[0]}\n")
        for cont in lines[1:]:
            parts.append(f"    {cont}\n")
        if emit_emotion:
            # ``bubble.emotion`` can be ``None`` on bubbles authored by
            # Generic mode (which doesn't carry emotions). Default to
            # ``NEUTRAL`` so a mixed-history chat stays generatable on
            # AER without a NoneType crash.
            if bubble.emotion is None and default_neutral_emotion:
                emotion = Emotion.NEUTRAL
            else:
                emotion = bubble.emotion or Emotion.NEUTRAL
            parts.append(f"  Emotion: {emotion.value}\n")
    return "".join(parts).strip()


# ---------------------------------------------------------------------------
# Style instruction
# ---------------------------------------------------------------------------


def build_style_instruction(
    style: Style,
    response_length: ResponseLength | None,
    cjk: bool,
    *,
    is_greeting: bool = False,
) -> str:
    modifiers: list[str] = []
    if is_greeting:
        modifiers.append(", greeting")
    if response_length is not None:
        modifiers.append(f"; Response length: {response_length.value}")
    if cjk:
        modifiers.append("; CJK")
    return f"[ Style: {style.value}{''.join(modifiers)} ]"


def build_deletion_style_instruction(cjk: bool) -> str:
    """Style instruction used when the user has deleted the contact: emits
    ``[ Style: deletion response[; CJK] ]``."""
    suffix = "; CJK" if cjk else ""
    return f"[ Style: deletion response{suffix} ]"

"""Rollover splices conditional brains as a single system message ~2048 tokens
before the end of context, and counts them toward the brain budget."""
from __future__ import annotations

import pytest

from server.aer.rollover import (
    BrainBudgetExceeded,
    CONDITIONAL_BRAIN_TARGET_TOKENS,
    _solo_tokens,
    build_messages_for_generation,
)
from server.models import (
    Brain,
    BrainKey,
    Chat,
    ChatMessage,
    Contact,
    Emotion,
    Settings,
    SubMessage,
    User,
)


_FILLER = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor " * 8


def _linear_chat(contact, user, messages):
    chat = Chat(contact_id=contact.id, user_id=user.id)
    prev = ""
    for m in messages:
        chat.selected_child_id[prev] = m.id
        prev = m.id
    return chat


def _build_history(contact, user, n_pairs, last_user_text):
    msgs = []
    parent = None
    for i in range(n_pairs):
        m = ChatMessage(
            parent_id=parent, sender="user" if i % 2 == 0 else "contact",
            sender_name=user.name if i % 2 == 0 else contact.name,
            body=[SubMessage(text=_FILLER, emotion=Emotion.NEUTRAL)],
            timestamp=float(i),
        )
        msgs.append(m)
        parent = m.id
    tail = ChatMessage(
        parent_id=parent, sender="user", sender_name=user.name,
        body=[SubMessage(text=last_user_text, emotion=Emotion.NEUTRAL)],
        timestamp=float(n_pairs),
    )
    msgs.append(tail)
    return msgs


def _find_conditional_block(api_messages, marker):
    """Return the api_messages index of the conditional brain block that
    contains ``marker`` in its content, or None."""
    for i, m in enumerate(api_messages):
        if m["role"] == "system" and marker in m["content"] and m["content"].startswith("----"):
            return i
    return None


def test_conditional_brain_activates_and_relocates(tmp_storage):
    contact = Contact(
        name="Alice", persona="Baker.",
        brains=[
            Brain(name="AlwaysOn", content="Alice loves baking bread."),
            Brain(name="BananaLore", content="Bananas are tropical fruit.",
                  keys=[BrainKey(pattern="banana")]),
        ],
    )
    user = User(name="Bob")
    settings = Settings()

    # Long enough history that the splice target (2048 tokens) lands well
    # inside the conversation rather than against the system-prompt floor.
    messages = _build_history(contact, user, 40, "Time to eat a banana!")
    chat = _linear_chat(contact, user, messages)
    ctx = build_messages_for_generation(chat, messages, contact, user, None, settings)

    # AlwaysOn is in the system prompt (apex of the AER text).
    assert "AlwaysOn" in ctx.api_messages[0]["content"]
    # BananaLore is NOT in the system prompt — it relocates.
    assert "BananaLore" not in ctx.api_messages[0]["content"]

    idx = _find_conditional_block(ctx.api_messages, "BananaLore")
    assert idx is not None, "BananaLore should activate"
    # Tail tokens after the spliced block — should be ~CONDITIONAL_BRAIN_TARGET_TOKENS.
    tail = sum(
        _solo_tokens(m, is_first=(i == 0))
        for i, m in enumerate(ctx.api_messages)
        if i > idx
    )
    assert tail >= CONDITIONAL_BRAIN_TARGET_TOKENS, (
        f"tail after conditional block was {tail} (target {CONDITIONAL_BRAIN_TARGET_TOKENS})"
    )


def test_conditional_brain_does_not_activate_without_match(tmp_storage):
    contact = Contact(
        name="Alice", persona="Baker.",
        brains=[
            Brain(name="BananaLore", content="Bananas are tropical fruit.",
                  keys=[BrainKey(pattern="banana")]),
        ],
    )
    user = User(name="Bob")
    settings = Settings()
    messages = _build_history(contact, user, 14, "Just having a quiet day.")
    chat = _linear_chat(contact, user, messages)
    ctx = build_messages_for_generation(chat, messages, contact, user, None, settings)

    assert _find_conditional_block(ctx.api_messages, "BananaLore") is None
    assert "BananaLore" not in ctx.api_messages[0]["content"]


def test_conditional_brain_per_message(tmp_storage):
    contact = Contact(name="Alice", persona="Baker.")
    user = User(name="Bob")
    settings = Settings()

    # Per-message conditional brain on a mid-conversation user message.
    messages = _build_history(contact, user, 12, "We talked about the rabbit yesterday.")
    # Replace one of the middle messages with one that carries a conditional brain.
    target_idx = 6
    cond_brain = Brain(
        name="RabbitLore",
        content="Rabbits are small mammals.",
        keys=[BrainKey(pattern="rabbit")],
    )
    messages[target_idx] = messages[target_idx].model_copy(
        update={"brains": [cond_brain]}
    )
    chat = _linear_chat(contact, user, messages)
    ctx = build_messages_for_generation(chat, messages, contact, user, None, settings)

    # The conditional per-message brain should relocate (not appear inline).
    # Find all '----' system messages and ensure RabbitLore is in exactly one
    # of them (the spliced block).
    rabbit_blocks = [
        i for i, m in enumerate(ctx.api_messages)
        if m["role"] == "system" and "RabbitLore" in m["content"]
    ]
    assert len(rabbit_blocks) == 1


def test_unconditional_per_message_brain_stays_inline(tmp_storage):
    contact = Contact(name="Alice", persona="Baker.")
    user = User(name="Bob")
    settings = Settings()
    messages = _build_history(contact, user, 12, "Some text.")
    uncond = Brain(name="StayInline", content="Always-on per-message brain.")
    messages[6] = messages[6].model_copy(update={"brains": [uncond]})
    chat = _linear_chat(contact, user, messages)
    ctx = build_messages_for_generation(chat, messages, contact, user, None, settings)

    # The inline brain message must sit immediately *before* its owning turn.
    inline_indices = [
        i for i, m in enumerate(ctx.api_messages)
        if m["role"] == "system" and "StayInline" in m["content"]
    ]
    assert len(inline_indices) == 1
    inline_i = inline_indices[0]
    # The next message should be a user/assistant turn carrying the same text.
    nxt = ctx.api_messages[inline_i + 1]
    assert nxt["role"] in ("user", "assistant")


def test_brain_budget_exceeded_when_unconditional_brain_is_huge(tmp_storage):
    huge = "x" * 200000
    contact = Contact(
        name="Alice",
        brains=[Brain(name="HugeAlwaysOn", content=huge)],
    )
    user = User(name="Bob")
    settings = Settings(context_preset="tablet")  # smallest base — easier to hit cap
    messages = _build_history(contact, user, 2, "hi")
    chat = _linear_chat(contact, user, messages)
    with pytest.raises(BrainBudgetExceeded):
        build_messages_for_generation(chat, messages, contact, user, None, settings)


def test_activated_conditional_brain_dropped_silently_when_over_budget(tmp_storage):
    """Unlike unconditional brains, a conditional brain whose activation
    pushes the budget over the cap is silently dropped — the user can't
    predict which combinations will activate, and losing one lore entry
    beats losing the whole turn."""
    huge = "x" * 200000
    contact = Contact(
        name="Alice",
        brains=[Brain(
            name="HugeConditional", content=huge,
            keys=[BrainKey(pattern="trigger")],
        )],
    )
    user = User(name="Bob")
    settings = Settings(context_preset="tablet")
    messages = _build_history(contact, user, 2, "say the trigger word")
    chat = _linear_chat(contact, user, messages)
    ctx = build_messages_for_generation(chat, messages, contact, user, None, settings)
    # No exception, no spliced block.
    assert _find_conditional_block(ctx.api_messages, "HugeConditional") is None


def test_oversized_conditional_brain_drops_tail_first(tmp_storage):
    """When two conditional brains both activate but only the first fits the
    budget, the second is dropped (tail-first ordering)."""
    fits = "tight " * 50           # ~50 tokens
    huge = "x " * 50000            # well over the tablet cap
    contact = Contact(
        name="Alice",
        brains=[
            Brain(name="Fits", content=fits, keys=[BrainKey(pattern="trigger")]),
            Brain(name="Huge", content=huge, keys=[BrainKey(pattern="trigger")]),
        ],
    )
    user = User(name="Bob")
    settings = Settings(context_preset="tablet")
    messages = _build_history(contact, user, 2, "say the trigger word")
    chat = _linear_chat(contact, user, messages)
    ctx = build_messages_for_generation(chat, messages, contact, user, None, settings)
    assert _find_conditional_block(ctx.api_messages, "Fits") is not None
    assert _find_conditional_block(ctx.api_messages, "Huge") is None

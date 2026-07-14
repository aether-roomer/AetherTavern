"""Tests for the two-tier rollover algorithm."""
from __future__ import annotations

import pytest

from server.aer.rollover import (
    BrainBudgetExceeded,
    build_messages_for_generation,
    get_active_path,
)
from server.aer.template import render
from server.models import (
    Brain,
    Chat,
    ChatMessage,
    Contact,
    Emotion,
    Intimacy,
    Scenario,
    Settings,
    Style,
    SubMessage,
    User,
)


@pytest.fixture(scope="module", autouse=True)
def _ensure_tokenizer_loaded():
    # Eagerly load once per test module so individual tests don't pay the cold start.
    from server.aer.tokenizer import load_tokenizer

    load_tokenizer()


def _make_chat(contact_id: str, user_id: str, **kwargs) -> Chat:
    return Chat(
        contact_id=contact_id, user_id=user_id, intimacy=Intimacy.CLOSE, style=Style.CHAT, **kwargs
    )


def _make_contact(brains=None) -> Contact:
    return Contact(
        id="c1",
        name="Alice",
        persona="Cheerful and curious.",
        appearance="Short red hair, green eyes.",
        brains=brains or [],
    )


def _make_user() -> User:
    return User(id="u1", name="Bob", persona="Laid-back and witty.")


def _make_messages(n: int) -> list[ChatMessage]:
    """Build a linear path of N messages alternating user/contact."""
    out: list[ChatMessage] = []
    parent_id = None
    for i in range(n):
        sender = "user" if i % 2 == 0 else "contact"
        sender_name = "Bob" if sender == "user" else "Alice"
        text = f"Line {i}: " + "x " * 20  # a chunk of words to inflate tokens
        msg = ChatMessage(
            id=f"m{i}",
            parent_id=parent_id,
            sender=sender,
            sender_name=sender_name,
            body=[SubMessage(text=text)],
            timestamp=float(i),
        )
        out.append(msg)
        parent_id = msg.id
    return out


def _link_path_in_chat(chat: Chat, messages: list[ChatMessage]) -> Chat:
    sel: dict[str, str] = {}
    parent_key = ""  # ROOT_PARENT_KEY
    for m in messages:
        sel[parent_key] = m.id
        parent_key = m.id
    return chat.model_copy(update={"selected_child_id": sel})


def test_active_path_follows_selections():
    msgs = _make_messages(5)
    chat = _link_path_in_chat(_make_chat("c1", "u1"), msgs)
    path = get_active_path(chat, msgs)
    assert [m.id for m in path] == [f"m{i}" for i in range(5)]


def test_active_path_stops_on_empty_sentinel():
    msgs = _make_messages(3)
    chat = _link_path_in_chat(_make_chat("c1", "u1"), msgs)
    chat = chat.model_copy(update={"selected_child_id": {**chat.selected_child_id, "m1": "__empty__"}})
    path = get_active_path(chat, msgs)
    assert [m.id for m in path] == ["m0", "m1"]


def test_small_chat_no_rollover():
    contact = _make_contact()
    user = _make_user()
    msgs = _make_messages(2)
    chat = _link_path_in_chat(_make_chat(contact.id, user.id), msgs)
    settings = Settings()  # default 28k+8k limits — way more than enough
    ctx = build_messages_for_generation(chat, msgs, contact, user, None, settings)
    # Expected: system + start_of_chat + 2 history msgs + style
    assert ctx.api_messages[0]["role"] == "system"
    assert ctx.api_messages[1]["content"] == "Start of chat."
    assert ctx.api_messages[-1]["content"].startswith("[ Style: chat")
    assert ctx.new_cursor == 0
    assert ctx.new_rolled_over is False
    assert ctx.total_tokens > 0


def test_greeting_context_has_no_history():
    contact = _make_contact()
    user = _make_user()
    chat = _make_chat(contact.id, user.id)
    ctx = build_messages_for_generation(chat, [], contact, user, None, Settings(), is_greeting=True)
    # system + start_of_chat + style (greeting modifier)
    assert len(ctx.api_messages) == 3
    assert ctx.api_messages[0]["role"] == "system"
    assert ctx.api_messages[1]["content"] == "Start of chat."
    assert "greeting" in ctx.api_messages[2]["content"]


def test_deletion_context_has_no_scenario():
    contact = _make_contact()
    user = _make_user()
    scenario = Scenario(id="s1", name="apartment", environment="Alice's place.")
    chat = _make_chat(contact.id, user.id, scenario_id="s1")
    ctx = build_messages_for_generation(
        chat, [], contact, user, scenario, Settings(), is_deletion=True
    )
    # Scenario section (Environment line) should NOT appear in the system prompt.
    assert "Environment" not in ctx.api_messages[0]["content"]
    assert "[ Style: deletion response" in ctx.api_messages[-1]["content"]


def test_rollover_trims_oldest_history_to_soft_budget(monkeypatch):
    from server.aer import rollover as ro
    contact = _make_contact()
    user = _make_user()
    msgs = _make_messages(20)
    chat = _link_path_in_chat(_make_chat(contact.id, user.id), msgs)
    # Force a tiny budget so 20 messages overflow.
    monkeypatch.setitem(ro.CONTEXT_PRESETS, "opus",
                        {"base_context_size": 400, "rollover_window": 120})
    settings = Settings()
    ctx = build_messages_for_generation(
        chat, msgs, contact, user, None, settings, max_output_tokens=64
    )
    assert ctx.new_rolled_over is True
    assert ctx.new_cursor > 0
    assert all(m["content"] != "Start of chat." for m in ctx.api_messages)
    base, rw = ro.context_limits(settings)
    assert ctx.total_tokens <= base + rw


def test_brain_protection_during_trim(monkeypatch):
    from server.aer import rollover as ro
    contact = _make_contact(brains=[])  # brains attached to messages instead
    user = _make_user()
    msgs = _make_messages(20)
    msgs[0] = msgs[0].model_copy(update={
        "brains": [Brain(name="Mochi", content="Alice's fluffy orange tabby cat.")]
    })
    chat = _link_path_in_chat(_make_chat(contact.id, user.id), msgs)
    monkeypatch.setitem(ro.CONTEXT_PRESETS, "opus",
                        {"base_context_size": 400, "rollover_window": 120})
    settings = Settings()
    ctx = build_messages_for_generation(
        chat, msgs, contact, user, None, settings, max_output_tokens=64
    )
    assert ctx.new_rolled_over
    brain_msgs = [m for m in ctx.api_messages if m["role"] == "system" and m["content"].startswith("----")]
    assert any("Mochi" in m["content"] for m in brain_msgs)


def test_brain_budget_cap_raises():
    from server.aer import rollover as ro
    huge_brain = Brain(name="Lore", content="x " * 500)  # ~500 tokens
    contact = _make_contact(brains=[huge_brain] * 50)  # ~25_000 brain tokens
    user = _make_user()
    chat = _link_path_in_chat(_make_chat(contact.id, user.id), [])
    settings = Settings()  # context_preset=opus → base=28672, cap = 11468
    with pytest.raises(BrainBudgetExceeded) as excinfo:
        build_messages_for_generation(chat, [], contact, user, None, settings)
    base, _rw = ro.context_limits(settings)
    assert excinfo.value.cap == int(base / 2.5)
    assert excinfo.value.total > excinfo.value.cap


def test_new_path_ids_short_chat_matches_active_path():
    """Short chat that fits in context: ``new_path_ids`` is the full
    active path; ``new_cursor`` is 0 so every message is in context.
    The chat-stats counter on the frontend uses these to render
    ``msgs: N`` (no truncation indicator)."""
    contact = _make_contact()
    user = _make_user()
    msgs = _make_messages(8)
    chat = _link_path_in_chat(_make_chat(contact.id, user.id), msgs)
    ctx = build_messages_for_generation(chat, msgs, contact, user, None, Settings())
    assert ctx.new_rolled_over is False
    assert ctx.new_cursor == 0
    active = get_active_path(chat, msgs)
    assert ctx.new_path_ids == [m.id for m in active]
    # Frontend would compute: messages_in_context = len(path) - cursor.
    in_ctx = len(ctx.new_path_ids) - ctx.new_cursor
    assert in_ctx == len(active)


def test_new_path_ids_after_rollover_locates_oldest_in_context(monkeypatch):
    """Long chat triggers rollover; ``new_path_ids[new_cursor]`` is the
    oldest message still in context — the boundary the frontend uses to
    anchor the dashed divider above the in-context section."""
    from server.aer import rollover as ro
    contact = _make_contact()
    user = _make_user()
    msgs = _make_messages(20)
    chat = _link_path_in_chat(_make_chat(contact.id, user.id), msgs)
    monkeypatch.setitem(ro.CONTEXT_PRESETS, "opus",
                        {"base_context_size": 400, "rollover_window": 120})
    ctx = build_messages_for_generation(
        chat, msgs, contact, user, None, Settings(), max_output_tokens=64,
    )
    assert ctx.new_rolled_over is True
    assert ctx.new_cursor > 0
    active = get_active_path(chat, msgs)
    # Path itself is unchanged — full path is still there for navigation.
    assert ctx.new_path_ids == [m.id for m in active]
    # But the in-context window starts at ``cursor`` rather than 0.
    in_ctx = len(ctx.new_path_ids) - ctx.new_cursor
    assert 0 < in_ctx < len(active)
    oldest_in_ctx = ctx.new_path_ids[ctx.new_cursor]
    assert oldest_in_ctx == active[ctx.new_cursor].id


def test_new_path_ids_branch_swap_recomputes(monkeypatch):
    """Switching to a shorter sibling branch recomputes ``new_path_ids``
    fresh — the chat-stats counter and rollover divider must reflect
    the NEW branch's path, not the cached state from the previous one.
    Pins the chat-info / chat-stats display correctness when a branch
    swipe lands on a different-length path."""
    from server.aer import rollover as ro
    contact = _make_contact()
    user = _make_user()
    main = _make_messages(20)
    chat = _link_path_in_chat(_make_chat(contact.id, user.id), main)
    monkeypatch.setitem(ro.CONTEXT_PRESETS, "opus",
                        {"base_context_size": 400, "rollover_window": 120})

    # Long branch hits rollover.
    ctx_long = build_messages_for_generation(
        chat, main, contact, user, None, Settings(), max_output_tokens=64,
    )
    assert ctx_long.new_rolled_over is True
    assert ctx_long.new_cursor > 0

    # Add a short alt root and switch the path to it.
    sibling = ChatMessage(
        id="alt0",
        parent_id=None,
        sender="contact",
        sender_name="Alice",
        body=[SubMessage(text="Hi from the alt branch.", emotion="neutral")],
    )
    msgs_with_sibling = main + [sibling]
    chat2 = chat.model_copy(update={
        "selected_child_id": {**chat.selected_child_id, "": sibling.id},
    })
    ctx_short = build_messages_for_generation(
        chat2, msgs_with_sibling, contact, user, None,
        Settings(), max_output_tokens=64,
    )
    # 1-msg branch fits — no rollover, cursor at 0, single id in path.
    assert ctx_short.new_rolled_over is False
    assert ctx_short.new_cursor == 0
    assert ctx_short.new_path_ids == [sibling.id]
    # The chat would show ``msgs: 1`` rather than ``msgs: 8/20``.
    in_ctx = len(ctx_short.new_path_ids) - ctx_short.new_cursor
    assert in_ctx == 1


def test_rendered_prompt_strips_padding_around_nothink_and_emotion():
    """Whole-pipeline contract: messages constructed with trailing newlines /
    leading whitespace must render without those padding artifacts. In
    particular: ``/nothink`` sits flush against user content (no blank
    line before it), the contact's emotion tag sits flush against the
    next message boundary (no trailing blank line between them), and
    the system prompt body has no trailing whitespace."""
    contact = Contact(
        id="c1",
        name="  Alice\n",
        persona="\n  Cheerful.\n\n",
        brains=[Brain(name="  Mochi\n", content="\n  Cat.\n\n")],
    )
    user = User(id="u1", name="Bob \n", persona="Witty.\n")

    contact_msg = ChatMessage(
        id="m0",
        parent_id=None,
        sender="contact",
        sender_name="  Alice  ",
        body=[SubMessage(text="\nHi!\n\n", emotion=Emotion.HAPPY)],
        timestamp=0.0,
    )
    user_msg = ChatMessage(
        id="m1",
        parent_id="m0",
        sender="user",
        sender_name="Bob\n",
        body=[SubMessage(text="Sure.   \n\n")],
        timestamp=1.0,
    )
    chat = _link_path_in_chat(
        _make_chat(contact.id, user.id), [contact_msg, user_msg]
    )

    ctx = build_messages_for_generation(
        chat, [contact_msg, user_msg], contact, user, None, Settings()
    )
    text = render(ctx.api_messages)

    # /nothink follows user content with no blank line before it.
    assert "Bob: Sure./nothink" in text
    assert "\n/nothink" not in text

    # Emotion tag sits flush against the next marker — no trailing \n
    # after the contact's last bubble.
    assert "Emotion: happy<|user|>" in text

    # Brain content (last line of the system prompt) sits flush against
    # the next message marker.
    assert "Cat.<|system|>" in text

    # Padded sender names and field values were stripped before render.
    assert "Alice  :" not in text
    assert "Bob\n:" not in text
    assert "  Mochi" not in text
    assert "Mochi  " not in text

    # Whole prompt ends at the generation suffix with a trailing \n so the
    # model's first generated token is real content.
    assert text.endswith("<|assistant|>\n<think></think>\n")


def test_branch_change_resets_cursor():
    """Cached cursor is invalidated when the path prefix changes (branch switch)."""
    contact = _make_contact()
    user = _make_user()
    msgs = _make_messages(20)
    chat = _link_path_in_chat(_make_chat(contact.id, user.id), msgs)
    # Pretend we previously rolled over with a different path prefix.
    chat = chat.model_copy(update={
        "rollover_start_index": 5,
        "rolled_over": True,
        "rollover_path_ids": ["different-id-1", "different-id-2", "x", "y", "z"],
    })
    settings = Settings()  # default limits — no need to trim
    ctx = build_messages_for_generation(chat, msgs, contact, user, None, settings)
    # Cached prefix mismatch → reset to 0.
    assert ctx.new_cursor == 0
    assert ctx.new_rolled_over is False

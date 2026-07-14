"""Reminder-brain prompt assembly: placement, depth clamp, disabled
handling, brain-budget exclusion, adjacent-brain merge."""
from __future__ import annotations

import pytest

from server.aer.rollover import (
    BrainBudgetExceeded,
    build_messages_for_generation,
)
from server.models import (
    Brain,
    Chat,
    ChatMessage,
    Contact,
    Emotion,
    Intimacy,
    ReminderBrain,
    Settings,
    Style,
    SubMessage,
    User,
)


@pytest.fixture(scope="module", autouse=True)
def _ensure_tokenizer_loaded():
    from server.aer.tokenizer import load_tokenizer

    load_tokenizer()


def _make_chat(contact_id: str, user_id: str, **kwargs) -> Chat:
    return Chat(
        contact_id=contact_id, user_id=user_id, intimacy=Intimacy.CLOSE,
        style=Style.CHAT, **kwargs,
    )


def _make_messages(n: int) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    parent_id = None
    for i in range(n):
        sender = "user" if i % 2 == 0 else "contact"
        m = ChatMessage(
            id=f"m{i}",
            parent_id=parent_id,
            sender=sender,
            sender_name="Bob" if sender == "user" else "Alice",
            body=[SubMessage(text=f"msg {i}", emotion=Emotion.NEUTRAL)],
        )
        out.append(m)
        parent_id = m.id
    return out


def _contact(reminder: ReminderBrain | None = None, brains=None) -> Contact:
    return Contact(
        id="c1",
        name="Alice",
        persona="Cheerful.",
        brains=brains or [],
        reminder_brain=reminder,
    )


def _user() -> User:
    return User(id="u1", name="Bob")


def _chat_with_path(msgs: list[ChatMessage]) -> Chat:
    chat = _make_chat("c1", "u1")
    parent_id = None
    for m in msgs:
        chat.selected_child_id[parent_id or ""] = m.id
        parent_id = m.id
    return chat


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------


class TestReminderPlacement:
    def test_no_reminder_no_splice(self):
        msgs = _make_messages(4)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs, _contact(), _user(), None, Settings(),
        )
        assert ctx.reminder_tokens == 0
        # No system message contains the reminder content (there is none).
        assert all("Be terse" not in m["content"] for m in ctx.api_messages)

    def test_depth_zero_lands_right_above_style_marker(self):
        msgs = _make_messages(6)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs, _contact(ReminderBrain(name="N", content="Be terse.", depth=0)),
            _user(), None, Settings(),
        )
        # Style marker is the last system message.
        last = ctx.api_messages[-1]
        assert last["role"] == "system" and last["content"].startswith("[ Style:")
        # Reminder content is at the message immediately before the style marker.
        prev = ctx.api_messages[-2]
        assert prev["role"] == "system"
        assert "Be terse." in prev["content"]

    def test_depth_three_lands_three_user_assistant_turns_back(self):
        msgs = _make_messages(8)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs, _contact(ReminderBrain(name="N", content="Reminder!", depth=3)),
            _user(), None, Settings(),
        )
        # Depth counts only user/assistant turns: exactly 3 should sit
        # between the reminder and the style marker.
        rem_idx = next(i for i, m in enumerate(ctx.api_messages)
                       if "Reminder!" in m["content"])
        style_idx = next(i for i, m in enumerate(ctx.api_messages)
                         if m["content"].startswith("[ Style:"))
        ua_between = sum(
            1 for m in ctx.api_messages[rem_idx + 1 : style_idx]
            if m["role"] in ("user", "assistant")
        )
        assert ua_between == 3

    def test_disabled_reminder_not_inserted(self):
        msgs = _make_messages(4)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs,
            _contact(ReminderBrain(name="N", content="should not appear",
                                   depth=0, disabled=True)),
            _user(), None, Settings(),
        )
        assert ctx.reminder_tokens == 0
        assert all("should not appear" not in m["content"] for m in ctx.api_messages)

    def test_empty_content_skipped(self):
        msgs = _make_messages(4)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs, _contact(ReminderBrain(name="N", content="", depth=0)),
            _user(), None, Settings(),
        )
        assert ctx.reminder_tokens == 0

    def test_floor_at_header_for_huge_depth(self):
        # Cap at 10 + clamp to floor at 1 (above the AER system header at 0).
        # With a 2-message history, depth=10 would target a negative index; it
        # clamps to 1.
        msgs = _make_messages(2)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs, _contact(ReminderBrain(name="N", content="capped", depth=10)),
            _user(), None, Settings(),
        )
        # Reminder appears somewhere, but never below index 0 (which is the
        # AER system header).
        rem_idx = next(i for i, m in enumerate(ctx.api_messages)
                       if "capped" in m["content"])
        assert rem_idx >= 1


# ---------------------------------------------------------------------------
# Token bookkeeping
# ---------------------------------------------------------------------------


class TestReminderTokens:
    def test_tokens_counted_separately(self):
        msgs = _make_messages(4)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs, _contact(ReminderBrain(name="N", content="x" * 200, depth=0)),
            _user(), None, Settings(),
        )
        assert ctx.reminder_tokens > 0
        # brain_tokens tracks ordinary brain content; the reminder content
        # is NOT folded into that counter.
        assert ctx.reminder_tokens != ctx.brain_tokens

    def test_reminder_excluded_from_brain_budget_cap(self):
        # 10 kB of pure reminder content should NOT raise BrainBudgetExceeded.
        big_content = "X " * 8000  # ~16 kB
        msgs = _make_messages(2)
        chat = _chat_with_path(msgs)
        # Should NOT raise.
        ctx = build_messages_for_generation(
            chat, msgs,
            _contact(ReminderBrain(name="N", content=big_content, depth=0)),
            _user(), None, Settings(),
        )
        assert ctx.reminder_tokens > 0

    def test_regular_brain_budget_still_enforced(self):
        # Confirm the cap still trips for honest-to-god brain content.
        big_brain_content = "X " * 30000  # ~60 kB
        msgs = _make_messages(2)
        chat = _chat_with_path(msgs)
        contact = _contact(brains=[Brain(name="Huge", content=big_brain_content)])
        with pytest.raises(BrainBudgetExceeded):
            build_messages_for_generation(
                chat, msgs, contact, _user(), None, Settings(),
            )


# ---------------------------------------------------------------------------
# Adjacent-brain merge: the reminder folds into a neighbouring brain block
# when it lands directly next to one. Other adjacent system messages
# (style marker, "Start of chat.") are NOT merged into.
# ---------------------------------------------------------------------------


class TestReminderMergesWithBrainBlock:
    def test_lands_alone_when_no_adjacent_brain(self):
        msgs = _make_messages(4)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs,
            _contact(ReminderBrain(name="N", content="solo reminder", depth=0)),
            _user(), None, Settings(),
        )
        # Reminder is its own system message — content does NOT contain the
        # brain prefix "----".
        rem = next(m for m in ctx.api_messages if "solo reminder" in m["content"])
        assert not rem["content"].startswith("----")
        assert "----" not in rem["content"]

    def test_merges_into_adjacent_per_message_brain_block(self):
        # Per-message brains land as a system block immediately BEFORE
        # their owning message body. With msg3 carrying a brain, the
        # tail of api_messages is: …, brain_block, msg3_body, style.
        # depth=1 targets msg3_body's index — the brain at
        # ``target - 1`` triggers the merge, folding reminder content
        # into the brain block rather than emitting its own system
        # message.
        msgs = _make_messages(4)
        msgs[-1].brains = [Brain(name="Loc", content="local content")]
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs,
            _contact(ReminderBrain(name="N", content="merged reminder", depth=1)),
            _user(), None, Settings(),
        )
        # The brain block ("----" prefix) now also contains the reminder.
        brain_blocks = [m for m in ctx.api_messages
                        if m["role"] == "system" and m["content"].startswith("----")]
        assert any("merged reminder" in b["content"] for b in brain_blocks)
        # No standalone reminder message exists alongside it.
        standalone = [
            m for m in ctx.api_messages
            if m["role"] == "system"
            and "merged reminder" in m["content"]
            and not m["content"].startswith("----")
        ]
        assert not standalone

    def test_style_marker_never_absorbs_reminder(self):
        msgs = _make_messages(4)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs,
            _contact(ReminderBrain(name="N", content="not in style", depth=0)),
            _user(), None, Settings(),
        )
        # Style marker stays clean — it doesn't contain the reminder content.
        last = ctx.api_messages[-1]
        assert last["content"].startswith("[ Style:")
        assert "not in style" not in last["content"]


# ---------------------------------------------------------------------------
# Macro expansion: reminder content runs through the AER macro registry the
# same way the system prompt does.
# ---------------------------------------------------------------------------


class TestReminderMacros:
    def test_user_and_char_macros_expand(self):
        msgs = _make_messages(4)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs,
            _contact(ReminderBrain(
                name="N", content="Hi {{user}}, this is {{char}}.", depth=0,
            )),
            _user(), None, Settings(),
        )
        rem = next(m for m in ctx.api_messages if "this is" in m["content"])
        assert "Hi Bob, this is Alice." in rem["content"]
        assert "{{user}}" not in rem["content"]
        assert "{{char}}" not in rem["content"]

    def test_macro_expanding_to_empty_skips_splice(self):
        msgs = _make_messages(4)
        chat = _chat_with_path(msgs)
        # {{noop}} expands to "" — after strip the reminder is empty and
        # should not be spliced.
        ctx = build_messages_for_generation(
            chat, msgs,
            _contact(ReminderBrain(name="N", content="{{noop}}", depth=0)),
            _user(), None, Settings(),
        )
        assert ctx.reminder_tokens == 0
        assert all("{{noop}}" not in m["content"] for m in ctx.api_messages)

    def test_unknown_macro_passes_through_literally(self):
        msgs = _make_messages(4)
        chat = _chat_with_path(msgs)
        ctx = build_messages_for_generation(
            chat, msgs,
            _contact(ReminderBrain(
                name="N", content="literal {{notamacro}} token", depth=0,
            )),
            _user(), None, Settings(),
        )
        rem = next(m for m in ctx.api_messages if "literal" in m["content"])
        assert "{{notamacro}}" in rem["content"]

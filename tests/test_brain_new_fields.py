"""New Brain / BrainKey fields:
- Brain.disabled — soft-disable; skipped at every collection point.
- BrainKey.match_whole_words — literal-key word-boundary check.
- BrainKey.search_messages — message-count search window
  (model field round-trips; activation enforcement is BrainKey-level
  and shares the same `_scoped` window — covered separately).
"""
from __future__ import annotations

import pytest

from server.aer.activation import (
    CompiledKey,
    compile_key,
    key_matches,
)
from server.aer.rollover import build_messages_for_generation
from server.models import (
    Brain,
    BrainKey,
    Chat,
    ChatMessage,
    Contact,
    Emotion,
    Intimacy,
    Settings,
    Style,
    SubMessage,
    User,
)


@pytest.fixture(scope="module", autouse=True)
def _ensure_tokenizer_loaded():
    from server.aer.tokenizer import load_tokenizer

    load_tokenizer()


def _msgs(n: int) -> list[ChatMessage]:
    out, parent_id = [], None
    for i in range(n):
        sender = "user" if i % 2 == 0 else "contact"
        m = ChatMessage(
            id=f"m{i}", parent_id=parent_id, sender=sender,
            sender_name="Bob" if sender == "user" else "Alice",
            body=[SubMessage(text=f"msg {i}", emotion=Emotion.NEUTRAL)],
        )
        out.append(m)
        parent_id = m.id
    return out


def _chat(msgs) -> Chat:
    chat = Chat(contact_id="c1", user_id="u1",
                intimacy=Intimacy.CLOSE, style=Style.CHAT)
    parent_id = None
    for m in msgs:
        chat.selected_child_id[parent_id or ""] = m.id
        parent_id = m.id
    return chat


# ---------------------------------------------------------------------------
# Disabled
# ---------------------------------------------------------------------------


class TestDisabledBrain:
    def test_disabled_unconditional_brain_skipped(self):
        msgs = _msgs(2)
        chat = _chat(msgs)
        contact = Contact(
            id="c1", name="Alice",
            brains=[Brain(name="ON", content="on content"),
                    Brain(name="OFF", content="off content", disabled=True)],
        )
        user = User(id="u1", name="Bob")
        ctx = build_messages_for_generation(chat, msgs, contact, user, None, Settings())
        # System prompt content holds unconditional brains. The disabled
        # brain's content must not appear anywhere.
        full = "\n".join(m["content"] for m in ctx.api_messages)
        assert "on content" in full
        assert "off content" not in full

    def test_disabled_conditional_brain_skipped(self):
        msgs = _msgs(2)
        # Inject a keyword that would otherwise match.
        msgs[-1].body = [SubMessage(text="trigger me", emotion=Emotion.NEUTRAL)]
        chat = _chat(msgs)
        contact = Contact(
            id="c1", name="Alice",
            brains=[Brain(
                name="OFF", content="should not appear",
                keys=[BrainKey(pattern="trigger")],
                disabled=True,
            )],
        )
        user = User(id="u1", name="Bob")
        ctx = build_messages_for_generation(chat, msgs, contact, user, None, Settings())
        full = "\n".join(m["content"] for m in ctx.api_messages)
        assert "should not appear" not in full

    def test_disabled_per_message_brain_skipped(self):
        msgs = _msgs(2)
        msgs[-1].brains = [Brain(name="LocalOFF", content="local off content",
                                  disabled=True)]
        chat = _chat(msgs)
        contact = Contact(id="c1", name="Alice")
        user = User(id="u1", name="Bob")
        ctx = build_messages_for_generation(chat, msgs, contact, user, None, Settings())
        full = "\n".join(m["content"] for m in ctx.api_messages)
        assert "local off content" not in full


# ---------------------------------------------------------------------------
# match_whole_words
# ---------------------------------------------------------------------------


class TestMatchWholeWords:
    def _key(self, pattern: str, whole: bool) -> CompiledKey:
        return compile_key(BrainKey(
            pattern=pattern,
            is_regex=False,
            case_sensitive=False,
            match_whole_words=whole,
        ))

    def test_substring_match_when_off(self):
        # Substring match without whole-word — "the" inside "theatre" matches.
        ck = self._key("the", whole=False)
        assert key_matches("a theatre", ck)

    def test_word_boundary_match_when_on(self):
        ck = self._key("the", whole=True)
        assert key_matches("the cat", ck)
        assert not key_matches("theatre", ck)
        assert not key_matches("tether", ck)
        assert key_matches("ate the bird", ck)

    def test_word_boundary_with_punctuation(self):
        # Trailing punctuation still counts as a boundary.
        ck = self._key("hello", whole=True)
        assert key_matches("hello!", ck)
        assert key_matches("hello, world", ck)

    def test_word_boundary_no_match_inside_word(self):
        ck = self._key("ell", whole=True)
        assert not key_matches("hello", ck)


# ---------------------------------------------------------------------------
# search_messages — round-trip on the model.
# ---------------------------------------------------------------------------


class TestSearchMessagesField:
    def test_round_trip_default_none(self):
        k = BrainKey(pattern="x")
        assert k.search_messages is None

    def test_round_trip_value(self):
        k = BrainKey(pattern="x", search_messages=5)
        assert k.search_messages == 5

    def test_round_trip_via_brain(self):
        b = Brain(name="b", content="c",
                  keys=[BrainKey(pattern="x", search_messages=3, match_whole_words=True)])
        dumped = b.model_dump()
        restored = Brain.model_validate(dumped)
        assert restored.keys[0].search_messages == 3
        assert restored.keys[0].match_whole_words is True

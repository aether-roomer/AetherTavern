"""Tests for chat-creation behaviour: greeting auto-insert and selection
state. Storage-backed; uses the ``tmp_storage`` fixture from conftest."""
from __future__ import annotations

import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from server import storage
from server.aer.rollover import build_messages_for_generation, get_active_path
from server.main import app
from server.models import (
    EMPTY_SENTINEL,
    ROOT_PARENT_KEY,
    Chat,
    ChatMessage,
    ChatMessages,
    Contact,
    ContactScenario,
    Emotion,
    Intimacy,
    ResponseLength,
    Scenario,
    Settings,
    Style,
    SubMessage,
    User,
)
from server.routers import generate as gen_module
from server.routers.chats import CreateChatRequest, create_chat, update_chat
from server.routers.generate import context_tokens


@pytest.fixture(scope="module", autouse=True)
def _ensure_tokenizer_loaded():
    from server.aer.tokenizer import load_tokenizer

    load_tokenizer()


@pytest.fixture(autouse=True)
def _reset_inflight_slot():
    """The generation slot is module-level global state. Reset between tests so
    a failure mid-test can't lock subsequent tests out."""
    gen_module._inflight_label = None
    yield
    gen_module._inflight_label = None


@pytest.fixture
def client(tmp_storage):
    return TestClient(app)


@pytest.mark.asyncio
async def test_create_chat_seeds_greeting_message(tmp_storage):
    contact = storage.save_contact(Contact(
        id="c1", name="Alice", greeting="Hello, {{user}}!",
        greeting_emotion=Emotion.HAPPY,
    ))
    user = storage.save_user(User(id="u1", name="Bob"))

    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id,
    ))

    msgs = storage.load_chat_messages(chat.id).messages
    assert len(msgs) == 1
    seeded = msgs[0]
    assert seeded.sender == "contact"
    assert seeded.sender_name == "Alice"
    # Macros expanded against the chosen personas at creation time.
    assert seeded.body[0].text == "Hello, Bob!"
    assert seeded.body[0].emotion == Emotion.HAPPY
    # Greeting becomes the active root child.
    assert chat.selected_child_id[ROOT_PARENT_KEY] == seeded.id


@pytest.mark.asyncio
async def test_create_chat_no_greeting_leaves_chat_empty(tmp_storage):
    contact = storage.save_contact(Contact(id="c1", name="Alice"))
    user = storage.save_user(User(id="u1", name="Bob"))
    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id,
    ))
    assert storage.load_chat_messages(chat.id).messages == []


@pytest.mark.asyncio
async def test_create_chat_falls_back_to_neutral_emotion(tmp_storage):
    contact = storage.save_contact(Contact(
        id="c1", name="Alice", greeting="Hi", greeting_emotion=None,
    ))
    user = storage.save_user(User(id="u1", name="Bob"))
    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id,
    ))
    msgs = storage.load_chat_messages(chat.id).messages
    assert msgs[0].body[0].emotion == Emotion.NEUTRAL


# ---------------------------------------------------------------------------
# Root regen: with ``is_regen=True`` and no parent, the prior root message
# (the greeting we're rerolling) must NOT be carried into the prompt.
# ---------------------------------------------------------------------------


def _alice_bob():
    return (
        Contact(id="c1", name="Alice", persona="Curious."),
        User(id="u1", name="Bob", persona="Laid-back."),
    )


def test_root_regen_truncates_path_to_empty():
    contact, user = _alice_bob()
    chat = Chat(contact_id="c1", user_id="u1")
    greeting = ChatMessage(
        id="m1", parent_id=None, sender="contact",
        sender_name="Alice", body=[SubMessage(text="hi", emotion=Emotion.NEUTRAL)],
    )
    chat.selected_child_id[ROOT_PARENT_KEY] = "m1"
    ctx = build_messages_for_generation(
        chat=chat, messages_tree=[greeting],
        contact=contact, user=user, scenario=None,
        settings=Settings(),
        is_greeting=True, is_regen=True, regen_parent_id=None,
    )
    # No assistant or user messages — path was truncated to empty.
    bodies = [m["content"] for m in ctx.api_messages if m["role"] == "assistant"]
    assert bodies == []
    user_bodies = [m["content"] for m in ctx.api_messages if m["role"] == "user"]
    assert user_bodies == []


# ---------------------------------------------------------------------------
# CJK is a chat-level flag, seeded at creation from the OR of the participant
# defaults. After creation, only ``chat.cjk`` is consulted — flipping a
# participant's default flag doesn't retroactively change live chats.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_chat_cjk_seeded_from_user_default(tmp_storage):
    contact = storage.save_contact(Contact(id="c1", name="Alice", cjk=False))
    user = storage.save_user(User(id="u1", name="Bob", cjk=True))
    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id,
    ))
    assert chat.cjk is True


@pytest.mark.asyncio
async def test_create_chat_cjk_seeded_from_scenario_default(tmp_storage):
    contact = storage.save_contact(Contact(id="c1", name="Alice", cjk=False))
    user = storage.save_user(User(id="u1", name="Bob", cjk=False))
    scenario = storage.save_scenario(Scenario(id="s1", name="Tokyo cafe", cjk=True))
    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id, scenario_id=scenario.id,
    ))
    assert chat.cjk is True


@pytest.mark.asyncio
async def test_create_chat_cjk_off_when_no_participant_has_it(tmp_storage):
    contact = storage.save_contact(Contact(id="c1", name="Alice", cjk=False))
    user = storage.save_user(User(id="u1", name="Bob", cjk=False))
    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id,
    ))
    assert chat.cjk is False


@pytest.mark.asyncio
async def test_chat_cjk_not_recomputed_after_creation(tmp_storage):
    """Once a chat exists, flipping a participant's default cjk flag must not
    retroactively change the chat — the chat owns its own cjk state."""
    contact = storage.save_contact(Contact(id="c1", name="Alice", cjk=True))
    user = storage.save_user(User(id="u1", name="Bob", cjk=False))
    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id,
    ))
    assert chat.cjk is True

    # Flip the contact default off; the live chat keeps its own value.
    contact.cjk = False
    storage.save_contact(contact)
    reloaded = storage.get_chat(chat.id)
    assert reloaded.cjk is True


# ---------------------------------------------------------------------------
# /api/chats/{id}/context-tokens — non-streaming tokenisation endpoint used
# by the chat view to keep the ``ctx: N tokens`` stat fresh after edits.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_tokens_returns_count_breakdown(tmp_storage):
    contact = storage.save_contact(Contact(id="c1", name="Alice", greeting="Hi"))
    user = storage.save_user(User(id="u1", name="Bob"))
    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id,
    ))

    counts = await context_tokens(chat.id)

    assert counts["total_tokens"] > 0
    assert counts["system_tokens"] > 0
    # No user/assistant turns yet, so history-only count is just the
    # auto-inserted greeting; brain budget is empty.
    assert counts["history_tokens"] >= 0
    assert counts["brain_tokens"] == 0
    # The breakdown should be coherent: system + brain is part of the total.
    assert counts["total_tokens"] >= counts["system_tokens"]


# ---------------------------------------------------------------------------
# Contact-scoped scenarios — the per-contact alternative to global scenarios.
# Selected via ``contact_scenario_id``; mutually exclusive with ``scenario_id``.
# Carries optional overrides for style / intimacy / response_length and an
# optional greeting that supersedes ``contact.greeting``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_contact_scenario_seeds_tags_cjk_overrides(tmp_storage):
    cs = ContactScenario(
        name="Scenario A", tags="alpha, bravo", cjk=True,
        style=Style.ROLEPLAY, intimacy=Intimacy.ACQUAINTANCE,
        response_length=ResponseLength.SHORT,
    )
    contact = storage.save_contact(Contact(
        id="c1", name="Alice", default_intimacy=Intimacy.STRANGER,
        default_style=Style.CHAT, scenarios=[cs], default_scenario_id=cs.id,
    ))
    user = storage.save_user(User(id="u1", name="Bob"))

    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id, contact_scenario_id=cs.id,
    ))

    assert chat.contact_scenario_id == cs.id
    assert chat.scenario_id is None
    assert chat.tags == "alpha, bravo"
    assert chat.cjk is True
    assert chat.style == Style.ROLEPLAY
    assert chat.intimacy == Intimacy.ACQUAINTANCE
    assert chat.response_length == ResponseLength.SHORT


@pytest.mark.asyncio
async def test_contact_scenario_inherits_when_overrides_none(tmp_storage):
    """Leaving style / intimacy / response_length unset means inherit from
    contact defaults. Contact's ``default_response_length`` is None here, so
    the chat's ``response_length`` stays None too."""
    cs = ContactScenario(name="Scenario A", tags="")
    contact = storage.save_contact(Contact(
        id="c1", name="Alice", default_intimacy=Intimacy.CLOSE,
        default_style=Style.ROLEPLAY, scenarios=[cs],
    ))
    user = storage.save_user(User(id="u1", name="Bob"))

    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id, contact_scenario_id=cs.id,
    ))
    assert chat.intimacy == Intimacy.CLOSE
    assert chat.style == Style.ROLEPLAY
    assert chat.response_length is None


@pytest.mark.asyncio
async def test_contact_default_response_length_seeds_chat(tmp_storage):
    """A contact's ``default_response_length`` seeds the chat at creation,
    same as ``default_intimacy`` / ``default_style``."""
    contact = storage.save_contact(Contact(
        id="c1", name="Alice",
        default_response_length=ResponseLength.LONG,
    ))
    user = storage.save_user(User(id="u1", name="Bob"))

    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id,
    ))
    assert chat.response_length == ResponseLength.LONG


@pytest.mark.asyncio
async def test_contact_scenario_response_length_overrides_contact_default(tmp_storage):
    """When both are set, the contact-scenario's override wins over the
    contact-level default."""
    cs = ContactScenario(name="Scenario A", response_length=ResponseLength.SHORT)
    contact = storage.save_contact(Contact(
        id="c1", name="Alice",
        default_response_length=ResponseLength.LONG,
        scenarios=[cs],
    ))
    user = storage.save_user(User(id="u1", name="Bob"))

    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id, contact_scenario_id=cs.id,
    ))
    assert chat.response_length == ResponseLength.SHORT


@pytest.mark.asyncio
async def test_contact_scenario_greeting_overrides_contact_greeting(tmp_storage):
    cs = ContactScenario(
        name="Scenario A", greeting="Scenario greeting, {{user}}.",
        greeting_emotion=Emotion.PLAYFUL,
    )
    contact = storage.save_contact(Contact(
        id="c1", name="Alice", greeting="Default greeting, {{user}}.",
        greeting_emotion=Emotion.HAPPY, scenarios=[cs],
    ))
    user = storage.save_user(User(id="u1", name="Bob"))

    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id, contact_scenario_id=cs.id,
    ))
    msgs = storage.load_chat_messages(chat.id).messages
    assert len(msgs) == 1
    assert msgs[0].body[0].text == "Scenario greeting, Bob."
    assert msgs[0].body[0].emotion == Emotion.PLAYFUL


@pytest.mark.asyncio
async def test_contact_scenario_empty_greeting_falls_back_to_contact(tmp_storage):
    cs = ContactScenario(name="Scenario A")  # no greeting set
    contact = storage.save_contact(Contact(
        id="c1", name="Alice", greeting="Default greeting, {{user}}.",
        greeting_emotion=Emotion.HAPPY, scenarios=[cs],
    ))
    user = storage.save_user(User(id="u1", name="Bob"))

    chat = await create_chat(CreateChatRequest(
        contact_id=contact.id, user_id=user.id, contact_scenario_id=cs.id,
    ))
    msgs = storage.load_chat_messages(chat.id).messages
    assert msgs[0].body[0].text == "Default greeting, Bob."
    assert msgs[0].body[0].emotion == Emotion.HAPPY


@pytest.mark.asyncio
async def test_contact_scenario_and_scenario_id_mutually_exclusive(tmp_storage):
    cs = ContactScenario(name="Scenario A")
    contact = storage.save_contact(Contact(id="c1", name="Alice", scenarios=[cs]))
    user = storage.save_user(User(id="u1", name="Bob"))
    scen = storage.save_scenario(Scenario(id="s1", name="Global Scenario"))
    with pytest.raises(HTTPException) as exc:
        await create_chat(CreateChatRequest(
            contact_id=contact.id, user_id=user.id,
            scenario_id=scen.id, contact_scenario_id=cs.id,
        ))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_contact_scenario_must_belong_to_contact(tmp_storage):
    other_cs = ContactScenario(name="Foreign Scenario")
    storage.save_contact(Contact(id="c2", name="Other", scenarios=[other_cs]))
    contact = storage.save_contact(Contact(id="c1", name="Alice"))
    user = storage.save_user(User(id="u1", name="Bob"))
    with pytest.raises(HTTPException) as exc:
        await create_chat(CreateChatRequest(
            contact_id=contact.id, user_id=user.id,
            contact_scenario_id=other_cs.id,
        ))
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_update_chat_validates_contact_scenario(tmp_storage):
    cs = ContactScenario(name="Scenario A")
    contact = storage.save_contact(Contact(id="c1", name="Alice", scenarios=[cs]))
    user = storage.save_user(User(id="u1", name="Bob"))
    chat = await create_chat(CreateChatRequest(contact_id=contact.id, user_id=user.id))

    # Valid contact-scenario reassignment.
    chat.contact_scenario_id = cs.id
    chat.scenario_id = None
    out = await update_chat(chat.id, chat)
    assert out.contact_scenario_id == cs.id

    # Mutual exclusion: setting both fails.
    chat.scenario_id = "any"
    chat.contact_scenario_id = cs.id
    with pytest.raises(HTTPException) as exc:
        await update_chat(chat.id, chat)
    assert exc.value.status_code == 400


def test_extend_chat_keeps_full_active_path():
    """Sanity check: a normal extend (regen_parent_id=tip, is_regen=False or
    True) does include the prior message."""
    contact, user = _alice_bob()
    chat = Chat(contact_id="c1", user_id="u1")
    greeting = ChatMessage(
        id="m1", parent_id=None, sender="contact",
        sender_name="Alice", body=[SubMessage(text="hi", emotion=Emotion.NEUTRAL)],
    )
    chat.selected_child_id[ROOT_PARENT_KEY] = "m1"
    ctx = build_messages_for_generation(
        chat=chat, messages_tree=[greeting],
        contact=contact, user=user, scenario=None,
        settings=Settings(),
        regen_parent_id="m1",  # extend after the tip
    )
    bodies = [m["content"] for m in ctx.api_messages if m["role"] == "assistant"]
    assert any("hi" in b for b in bodies)


# ---------------------------------------------------------------------------
# Concurrency: AetherTavern is single-user / localhost, and the UI can only
# follow one stream at a time. /api/chats/{id}/generate and
# /api/contacts/{id}/deletion-response share a single global slot — when one
# is running, further requests are declined via a typed ``busy`` SSE error
# event (NOT an HTTP 4xx, which EventSource would surface as an opaque
# connection failure since it can't read response bodies on non-2xx).
# ---------------------------------------------------------------------------


def test_generate_declines_when_chat_generation_in_flight(client):
    gen_module._inflight_label = "chat:other"
    r = client.get("/api/chats/anything/generate")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert "event: error" in r.text
    assert '"kind": "busy"' in r.text
    assert "Another generation is already in progress" in r.text


def test_generate_declines_when_deletion_response_in_flight(client):
    """A deletion response counts as a generation — extending a chat while one
    is streaming would also thrash the upstream endpoint."""
    gen_module._inflight_label = "deletion:somebody"
    r = client.get("/api/chats/anything/generate")
    assert r.status_code == 200
    assert '"kind": "busy"' in r.text


def test_deletion_response_declines_when_chat_generation_in_flight(client):
    gen_module._inflight_label = "chat:other"
    r = client.get("/api/contacts/anything/deletion-response")
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    assert '"kind": "busy"' in r.text


# ---------------------------------------------------------------------------
# Path jump: when ``expected_tip_id`` differs from the server-side active
# tip, the server aligns ``selected_child_id`` along the client's claimed
# branch. Two-tabs-of-the-same-chat case — each generation extends from
# wherever its tab is looking, regardless of any cursor changes another
# tab made meanwhile.
# ---------------------------------------------------------------------------


def test_generate_jumps_active_path_to_expected_tip(client, tmp_storage):
    contact = storage.save_contact(Contact(id="c1", name="Alice"))
    user = storage.save_user(User(id="u1", name="Bob"))
    chat = storage.save_chat(Chat(id="ch1", contact_id=contact.id, user_id=user.id))

    # Branched tree:
    #   ROOT
    #     ├── m_a   (currently selected)
    #     └── m_b
    #          └── m_b_child
    m_a = ChatMessage(
        id="m_a", parent_id=None, sender="contact", sender_name="Alice",
        body=[SubMessage(text="branch A", emotion=Emotion.NEUTRAL)],
    )
    m_b = ChatMessage(
        id="m_b", parent_id=None, sender="contact", sender_name="Alice",
        body=[SubMessage(text="branch B", emotion=Emotion.NEUTRAL)],
    )
    m_b_child = ChatMessage(
        id="m_b_child", parent_id="m_b", sender="user", sender_name="Bob",
        body=[SubMessage(text="hi from B", emotion=Emotion.NEUTRAL)],
    )
    storage.save_chat_messages(
        chat.id, ChatMessages(messages=[m_a, m_b, m_b_child]),
    )
    chat.selected_child_id[ROOT_PARENT_KEY] = "m_a"
    storage.save_chat(chat)

    # ``client.stream`` so we don't consume the SSE body — the route's
    # synchronous prefix (which performs the jump and persists it) has
    # already run by the time we get the response object back.
    url = f"/api/chats/{chat.id}/generate?expected_tip_id=m_b_child"
    with client.stream("GET", url) as r:
        assert r.status_code == 200

    reloaded = storage.get_chat(chat.id)
    assert reloaded.selected_child_id[ROOT_PARENT_KEY] == "m_b"
    assert reloaded.selected_child_id["m_b"] == "m_b_child"


def test_generate_with_matching_tip_does_not_rewrite_selection(client, tmp_storage):
    """No-op when the client's view already matches the server's active tip —
    the jump shouldn't churn ``selected_child_id`` unnecessarily."""
    contact = storage.save_contact(Contact(id="c1", name="Alice"))
    user = storage.save_user(User(id="u1", name="Bob"))
    chat = storage.save_chat(Chat(id="ch1", contact_id=contact.id, user_id=user.id))
    m_a = ChatMessage(
        id="m_a", parent_id=None, sender="contact", sender_name="Alice",
        body=[SubMessage(text="branch A", emotion=Emotion.NEUTRAL)],
    )
    storage.save_chat_messages(chat.id, ChatMessages(messages=[m_a]))
    chat.selected_child_id[ROOT_PARENT_KEY] = "m_a"
    storage.save_chat(chat)
    pre_updated_at = storage.get_chat(chat.id).updated_at

    url = f"/api/chats/{chat.id}/generate?expected_tip_id=m_a"
    with client.stream("GET", url) as r:
        assert r.status_code == 200

    reloaded = storage.get_chat(chat.id)
    assert reloaded.selected_child_id[ROOT_PARENT_KEY] == "m_a"
    # The pre-stream code didn't bump updated_at (no save_chat call on the
    # no-jump path).
    assert reloaded.updated_at == pre_updated_at

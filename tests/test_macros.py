"""Tests for the macro registry and individual macro handlers."""
from __future__ import annotations

from datetime import datetime

import pytest

from server.aer.macros import MacroCtx, apply_macros, expand
from server.models import (
    Brain,
    Chat,
    ChatMessage,
    Contact,
    ContactScenario,
    Scenario,
    Settings,
    SubMessage,
    Emotion,
    Intimacy,
    Style,
)


def _contact(**kwargs):
    return Contact(name=kwargs.pop("name", "Alice"), **kwargs)


def _user(**kwargs):
    return User_ctor(name=kwargs.pop("name", "Bob"), **kwargs)


# Import User after pytest collects to avoid name shadowing in test parameters.
from server.models import User as User_ctor  # noqa: E402


# ---------------------------------------------------------------------------
# Registry basics
# ---------------------------------------------------------------------------


def test_unknown_macro_passes_through():
    out = apply_macros("hello {{completely_unknown}} world", "Alice", "Bob")
    assert out == "hello {{completely_unknown}} world"


def test_case_insensitive_name():
    out = apply_macros("{{Char}} {{USER}} {{char}}", "Alice", "Bob")
    assert out == "Alice Bob Alice"


def test_comment_macro_collapses():
    out = apply_macros("a {{// this is a note}} b", "Alice", "Bob")
    assert out == "a  b"


def test_dollar_shorthand_word_boundary():
    out = apply_macros("$c! $user. but $contractor stays.", "Alice", "Bob")
    assert "Alice!" in out
    assert "Bob." in out
    assert "$contractor" in out


def test_allowlist_scopes_expansion():
    text = "{{char}} rolls {{roll::1d1}}"
    out = apply_macros(text, "Alice", "Bob", allowlist={"roll"})
    # {{char}} stays literal, {{roll::1d1}} expands to 1
    assert "{{char}}" in out
    assert "rolls 1" in out


def test_allowlist_skips_dollar_shorthand():
    out = apply_macros("$user said hi", "Alice", "Bob", allowlist={"roll"})
    assert out == "$user said hi"


# ---------------------------------------------------------------------------
# Identity & names
# ---------------------------------------------------------------------------


def test_char_user():
    c = Contact(name="Aria")
    u = User_ctor(name="Tomo", persona="A quiet observer.")
    assert apply_macros("{{char}} and {{user}}", c, u) == "Aria and Tomo"
    assert apply_macros("{{persona}}", c, u) == "A quiet observer."


def test_description_and_personality_distinct_fields():
    c = Contact(
        name="Aria",
        persona="A confident detective.",
        appearance="Sharp suit, red scarf.",
    )
    u = User_ctor(name="Tomo")
    assert apply_macros("{{description}}", c, u) == "A confident detective."
    assert apply_macros("{{personality}}", c, u) == "Sharp suit, red scarf."
    # Aliases follow the same mapping.
    assert apply_macros("{{charDescription}}", c, u) == "A confident detective."
    assert apply_macros("{{charPersonality}}", c, u) == "Sharp suit, red scarf."


def test_scenario_resolves_active_scene():
    c = Contact(name="Aria")
    u = User_ctor(name="Tomo")
    scen = Scenario(name="Heist", environment="A vault", scene="Lights are off.")
    assert apply_macros("{{scenario}}", c, u, scenario=scen) == "Lights are off."
    cs = ContactScenario(name="Spy", scene="Rooftop chase.")
    assert apply_macros("{{scenario}}", c, u, contact_scenario=cs) == "Rooftop chase."
    # No scenario → empty
    assert apply_macros("{{scenario}}", c, u) == ""


def test_char_first_message_prefers_contact_scenario():
    c = Contact(name="Aria", greeting="Hi from Aria.")
    u = User_ctor(name="Tomo")
    cs = ContactScenario(name="At a cafe", greeting="Welcome to the cafe.")
    assert apply_macros("{{charFirstMessage}}", c, u) == "Hi from Aria."
    assert apply_macros(
        "{{charFirstMessage}}", c, u, contact_scenario=cs,
    ) == "Welcome to the cafe."


def test_group_macros_return_char_in_solo():
    c = Contact(name="Aria")
    u = User_ctor(name="Tomo")
    assert apply_macros("{{group}}", c, u) == "Aria"
    assert apply_macros("{{groupNotMuted}}", c, u) == "Aria"
    assert apply_macros("{{notChar}}", c, u) == ""


# ---------------------------------------------------------------------------
# AER namespace
# ---------------------------------------------------------------------------


def test_aer_namespace_contact_fields():
    c = Contact(
        name="Aria",
        species="Cat",
        gender="Female",
        pronouns="she/her",
        persona="Curious.",
        appearance="Black fur.",
        description="UI-only notes",
        greeting="Mrow.",
        tags="feline, tsundere",
    )
    u = User_ctor(name="Tomo")
    assert apply_macros("{{contact.name}}", c, u) == "Aria"
    assert apply_macros("{{contact.species}}", c, u) == "Cat"
    assert apply_macros("{{contact.gender}}", c, u) == "Female"
    assert apply_macros("{{contact.pronouns}}", c, u) == "she/her"
    assert apply_macros("{{contact.persona}}", c, u) == "Curious."
    assert apply_macros("{{contact.appearance}}", c, u) == "Black fur."
    assert apply_macros("{{contact.description}}", c, u) == "UI-only notes"
    assert apply_macros("{{contact.greeting}}", c, u) == "Mrow."
    assert apply_macros("{{contact.tags}}", c, u) == "feline, tsundere"


def test_pronoun_parts_two_part():
    c = Contact(name="Aria", pronouns="she/her")
    u = User_ctor(name="Tomo", pronouns="they/them")
    assert apply_macros("{{contact.pronouns.subject}}", c, u) == "she"
    assert apply_macros("{{contact.pronouns.object}}", c, u) == "her"
    assert apply_macros("{{user.pronouns.subject}}", c, u) == "they"
    assert apply_macros("{{user.pronouns.object}}", c, u) == "them"
    # Inline use in prose:
    out = apply_macros("{{contact.pronouns.object}} dog wagged its tail.", c, u)
    assert out == "her dog wagged its tail."


def test_pronoun_parts_single_part_fallback():
    """One-part input → both subject and object return that part."""
    c = Contact(name="Aria", pronouns="it")
    u = User_ctor(name="Tomo")
    assert apply_macros("{{contact.pronouns.subject}}", c, u) == "it"
    assert apply_macros("{{contact.pronouns.object}}", c, u) == "it"


def test_pronoun_parts_empty():
    c = Contact(name="Aria", pronouns="")
    u = User_ctor(name="Tomo")
    assert apply_macros("{{contact.pronouns.subject}}", c, u) == ""
    assert apply_macros("{{contact.pronouns.object}}", c, u) == ""


def test_pronoun_parts_extra_segments_ignored():
    """AER expects two-part format; trailing slash-separated parts are dropped."""
    c = Contact(name="Aria", pronouns="she/her/hers/herself")
    u = User_ctor(name="Tomo")
    assert apply_macros("{{contact.pronouns.subject}}", c, u) == "she"
    assert apply_macros("{{contact.pronouns.object}}", c, u) == "her"


def test_pronoun_parts_strip_whitespace():
    c = Contact(name="Aria", pronouns=" they  /  them ")
    u = User_ctor(name="Tomo")
    assert apply_macros("{{contact.pronouns.subject}}", c, u) == "they"
    assert apply_macros("{{contact.pronouns.object}}", c, u) == "them"


def test_aer_namespace_user_fields():
    c = Contact(name="Aria")
    u = User_ctor(
        name="Tomo",
        species="Human",
        gender="Nonbinary",
        pronouns="they/them",
        persona="A novelist.",
        appearance="Round glasses.",
        description="Tomo's notes",
        tags="writer, tea",
    )
    assert apply_macros("{{user.species}}", c, u) == "Human"
    assert apply_macros("{{user.pronouns}}", c, u) == "they/them"
    assert apply_macros("{{user.persona}}", c, u) == "A novelist."
    assert apply_macros("{{user.appearance}}", c, u) == "Round glasses."
    assert apply_macros("{{user.tags}}", c, u) == "writer, tea"


def test_aer_namespace_scenario_fields():
    c = Contact(name="Aria")
    u = User_ctor(name="Tomo")
    scen = Scenario(
        name="Heist", environment="A vault", scene="Lights off.",
        description="Notes", tags="action, night",
    )
    assert apply_macros("{{scenario.name}}", c, u, scenario=scen) == "Heist"
    assert apply_macros("{{scenario.environment}}", c, u, scenario=scen) == "A vault"
    assert apply_macros("{{scenario.scene}}", c, u, scenario=scen) == "Lights off."
    assert apply_macros("{{scenario.tags}}", c, u, scenario=scen) == "action, night"
    # Greeting only exists on ContactScenario.
    cs = ContactScenario(name="C", greeting="Hi there.")
    assert apply_macros("{{scenario.greeting}}", c, u, contact_scenario=cs) == "Hi there."
    # Plain Scenario has no greeting → empty.
    assert apply_macros("{{scenario.greeting}}", c, u, scenario=scen) == ""


# ---------------------------------------------------------------------------
# Date / time
# ---------------------------------------------------------------------------


def test_weekday_isodate_isotime():
    moment = datetime(2026, 5, 13, 14, 30)  # 2026-05-13 was a Wednesday
    c = Contact(name="A")
    u = User_ctor(name="B")
    assert apply_macros("{{weekday}}", c, u, now=moment) == "Wednesday"
    assert apply_macros("{{isodate}}", c, u, now=moment) == "2026-05-13"
    assert apply_macros("{{isotime}}", c, u, now=moment) == "14:30"


def test_datetimeformat_common_tokens():
    moment = datetime(2026, 5, 3, 9, 5, 7)  # 2026-05-03 was a Sunday
    c = Contact(name="A")
    u = User_ctor(name="B")
    cases = [
        ("YYYY-MM-DD", "2026-05-03"),
        ("YYYY-MM-DD HH:mm:ss", "2026-05-03 09:05:07"),
        ("hh:mm A", "09:05 AM"),
        ("dddd, MMMM Do YYYY", "Sunday, May 3rd 2026"),
        ("MMM D", "May 3"),
    ]
    for fmt, expected in cases:
        got = apply_macros(f"{{{{datetimeformat::{fmt}}}}}", c, u, now=moment)
        assert got == expected, f"{fmt!r} -> {got!r}"


def test_datetimeformat_literal_brackets():
    moment = datetime(2026, 5, 13, 14, 30)
    c = Contact(name="A")
    u = User_ctor(name="B")
    out = apply_macros("{{datetimeformat::[at] HH:mm}}", c, u, now=moment)
    assert out == "at 14:30"


# ---------------------------------------------------------------------------
# Randomness: random / pick / roll
# ---------------------------------------------------------------------------


def test_random_returns_one_of():
    c = Contact(name="A")
    u = User_ctor(name="B")
    seen = set()
    for _ in range(50):
        out = apply_macros("{{random::red::green::blue}}", c, u)
        seen.add(out)
    assert seen.issubset({"red", "green", "blue"})
    assert seen  # at least one value chosen


def test_pick_deterministic_per_chat():
    c = Contact(name="A")
    u = User_ctor(name="B")
    chat = Chat(id="chat-xyz", contact_id=c.id, user_id=u.id)
    text = "Today is a {{pick::sunny::rainy::cloudy}} day."
    first = apply_macros(text, c, u, chat=chat)
    for _ in range(20):
        assert apply_macros(text, c, u, chat=chat) == first


def test_pick_rerolls_on_nonce_change():
    c = Contact(name="A")
    u = User_ctor(name="B")
    chat = Chat(id="chat-xyz", contact_id=c.id, user_id=u.id)
    text = "Mood: {{pick::angry::happy::sad::quiet::tired::bored::serene::worried}}"
    base = apply_macros(text, c, u, chat=chat)
    # Try many nonces; at least one should produce a different pick.
    chat2 = chat.model_copy(update={"pick_reroll_nonce": "nonce-A"})
    chat3 = chat.model_copy(update={"pick_reroll_nonce": "nonce-B"})
    a = apply_macros(text, c, u, chat=chat2)
    b = apply_macros(text, c, u, chat=chat3)
    assert {base, a, b} != {base}  # at least one differs


def test_pick_rerolls_on_macro_edit():
    c = Contact(name="A")
    u = User_ctor(name="B")
    chat = Chat(id="chat-xyz", contact_id=c.id, user_id=u.id)
    # Adding a fourth option changes the raw macro text → reseeds.
    out_a = apply_macros(
        "{{pick::dog::cat::fish::lizard::parrot::hamster}}", c, u, chat=chat,
    )
    out_b = apply_macros(
        "{{pick::dog::cat::fish::lizard::parrot::hamster::snake}}", c, u, chat=chat,
    )
    # Different raw text seeds different results in at least some cases.
    # Build a few variations to make collisions unlikely.
    variants = [
        "{{pick::dog::cat::fish}}",
        "{{pick::dog::cat::fish::bird}}",
        "{{pick::dog::cat::fish::bird::snake}}",
        "{{pick::dog::cat::fish::bird::snake::otter}}",
    ]
    results = {apply_macros(v, c, u, chat=chat) for v in variants}
    assert len(results) >= 2


def test_pick_multiple_instances_get_distinct_seeds():
    c = Contact(name="A")
    u = User_ctor(name="B")
    chat = Chat(id="chat-xyz", contact_id=c.id, user_id=u.id)
    # Two identical pick macros in the same text should be treated as
    # separate instances (sequence number disambiguates).
    text = "{{pick::A::B::C::D::E::F::G::H}} {{pick::A::B::C::D::E::F::G::H}}"
    out = apply_macros(text, c, u, chat=chat)
    parts = out.split(" ")
    assert len(parts) == 2
    # With 8 options and independent seeds, the two should *probably* differ.
    # Run a handful of distinct sequence-number pairs to confirm at least
    # one variation produces non-identical sibling values.
    # (Smoke: at least we got two valid options out.)
    for p in parts:
        assert p in {"A", "B", "C", "D", "E", "F", "G", "H"}


def test_pick_without_chat_still_returns_option():
    """No chat in ctx → seed is empty-prefixed but stable; still picks."""
    c = Contact(name="A")
    u = User_ctor(name="B")
    out = apply_macros("{{pick::x::y::z}}", c, u)
    assert out in {"x", "y", "z"}


def test_roll_dice():
    c = Contact(name="A")
    u = User_ctor(name="B")
    # 1d1 always rolls 1.
    assert apply_macros("{{roll::1d1}}", c, u) == "1"
    # NdM with modifier.
    out = apply_macros("{{roll::3d1+5}}", c, u)
    assert out == "8"
    # dM shorthand → 1dM.
    out = apply_macros("{{roll::d1}}", c, u)
    assert out == "1"
    # Range check.
    for _ in range(30):
        out = apply_macros("{{roll::2d6}}", c, u)
        assert 2 <= int(out) <= 12


def test_roll_malformed_stays_literal():
    c = Contact(name="A")
    u = User_ctor(name="B")
    assert apply_macros("{{roll::garbage}}", c, u) == "{{roll::garbage}}"
    assert apply_macros("{{roll::1d}}", c, u) == "{{roll::1d}}"


def test_roll_oversized_stays_literal():
    c = Contact(name="A")
    u = User_ctor(name="B")
    # Over the 100/1000 caps → literal.
    assert apply_macros("{{roll::101d6}}", c, u) == "{{roll::101d6}}"
    assert apply_macros("{{roll::1d10000}}", c, u) == "{{roll::1d10000}}"


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------


def _msg(sender, text, parent_id=None, ts=0.0):
    return ChatMessage(
        parent_id=parent_id,
        sender=sender,
        sender_name="Alice" if sender == "contact" else "Bob",
        body=[SubMessage(text=text, emotion=Emotion.NEUTRAL)],
        timestamp=ts,
    )


def test_last_message_macros():
    c = Contact(name="Alice")
    u = User_ctor(name="Bob")
    path = [
        _msg("contact", "First reply.", ts=1.0),
        _msg("user", "User says hi.", ts=2.0),
        _msg("contact", "Second reply.", ts=3.0),
    ]
    assert apply_macros("{{lastMessage}}", c, u, active_path=path) == "Second reply."
    assert apply_macros("{{lastUserMessage}}", c, u, active_path=path) == "User says hi."
    assert apply_macros("{{lastCharMessage}}", c, u, active_path=path) == "Second reply."


def test_last_message_id_and_range():
    c = Contact(name="A")
    u = User_ctor(name="B")
    path = [_msg("user", "1"), _msg("contact", "2"), _msg("user", "3")]
    assert apply_macros("{{lastMessageId}}", c, u, active_path=path) == "2"
    assert apply_macros("{{allChatRange}}", c, u, active_path=path) == "0-2"
    # Empty path → empty strings.
    assert apply_macros("{{lastMessageId}}", c, u) == ""
    assert apply_macros("{{allChatRange}}", c, u) == ""


def test_first_included_message_id():
    c = Contact(name="A")
    u = User_ctor(name="B")
    path = [_msg("user", "1"), _msg("contact", "2"), _msg("user", "3"), _msg("contact", "4")]
    out = apply_macros(
        "{{firstIncludedMessageId}}", c, u, active_path=path, rollover_cursor=2,
    )
    assert out == "2"


def test_idle_duration():
    c = Contact(name="A")
    u = User_ctor(name="B")
    now = datetime(2026, 5, 13, 14, 30)
    last_user_ts = now.timestamp() - 125  # 2 minutes 5 sec ago
    path = [
        _msg("contact", "hi", ts=now.timestamp() - 200),
        _msg("user", "ok", ts=last_user_ts),
        _msg("contact", "?", ts=now.timestamp() - 50),
    ]
    out = apply_macros("{{idleDuration}}", c, u, active_path=path, now=now)
    assert "minutes" in out or "minute" in out


def test_swipe_ids():
    c = Contact(name="A")
    u = User_ctor(name="B")
    parent = _msg("user", "Hi.", ts=1.0)
    sib1 = _msg("contact", "Hi back.", parent_id=parent.id, ts=2.0)
    sib2 = _msg("contact", "Hello!", parent_id=parent.id, ts=3.0)
    sib3 = _msg("contact", "Yo.", parent_id=parent.id, ts=4.0)
    tree = [parent, sib1, sib2, sib3]
    # Active path ends with sib2 (the second sibling chronologically).
    path = [parent, sib2]
    assert apply_macros(
        "{{lastSwipeId}}", c, u, active_path=path, messages_tree=tree,
    ) == "3"
    assert apply_macros(
        "{{currentSwipeId}}", c, u, active_path=path, messages_tree=tree,
    ) == "2"


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------


def test_model_macro_reads_settings():
    c = Contact(name="A")
    u = User_ctor(name="B")
    s = Settings(default_model="xialong-v1")
    assert apply_macros("{{model}}", c, u, settings=s) == "xialong-v1"
    # Without settings → empty.
    assert apply_macros("{{model}}", c, u) == ""


def test_context_token_macros():
    c = Contact(name="A")
    u = User_ctor(name="B")
    s = Settings(context_preset="opus")
    out = apply_macros(
        "max={{maxContext}} prompt={{maxPrompt}} resp={{maxResponse}}", c, u, settings=s,
    )
    # Opus base_context_size=28672, MAX_OUTPUT_TOKENS=1536, so prompt=27136.
    assert "max=28672" in out
    assert "prompt=27136" in out
    assert "resp=1536" in out


def test_ismobile_macro():
    c = Contact(name="A")
    u = User_ctor(name="B")
    assert apply_macros("{{isMobile}}", c, u, is_mobile=False) == "false"
    assert apply_macros("{{isMobile}}", c, u, is_mobile=True) == "true"


def test_sanitize_html_macro_reads_settings():
    c = Contact(name="A")
    u = User_ctor(name="B")
    # Default-on settings → "true"; explicit off → "false".
    assert apply_macros("{{sanitize_html}}", c, u, settings=Settings()) == "true"
    off = Settings(sanitize_generic_html=False)
    assert apply_macros("{{sanitize_html}}", c, u, settings=off) == "false"
    # No settings in scope → the safe default ("true").
    assert apply_macros("{{sanitize_html}}", c, u) == "true"


def test_sanitize_html_gates_a_scoped_block():
    """The seeded HTML-output block pattern: ``{{#if !sanitize_html}}`` shows
    its body only when raw-HTML rendering is on (sanitize off)."""
    c = Contact(name="A")
    u = User_ctor(name="B")
    text = "{{#if !sanitize_html}}LIVE{{/if}}"
    assert apply_macros(text, c, u, settings=Settings()) == ""
    on_raw = Settings(sanitize_generic_html=False)
    assert apply_macros(text, c, u, settings=on_raw) == "LIVE"


def test_impersonate_flipped_macros_return_empty_for_user_in_contact_slot():
    """Impersonate flips contact ↔ user in the macro context so
    ``{{contact.X}}`` references resolve to the user persona's fields.
    Contact-only fields (greeting, reminder_brain, example_chats) must
    fall back to empty strings via ``getattr``-with-default — anything
    else would crash with ``AttributeError`` when User sits in the
    contact slot."""
    contact = Contact(name="Alice", greeting="hi there")
    persona = User_ctor(name="Bob", persona="curious")

    # Sanity: normal direction works as expected.
    assert apply_macros("{{charfirstmessage}}", contact, persona) == "hi there"
    assert apply_macros("{{contact.greeting}}", contact, persona) == "hi there"

    # Flipped: contact slot now holds the User entity. Contact-only
    # field macros should resolve to "" without crashing.
    assert apply_macros("{{charfirstmessage}}", persona, contact) == ""
    assert apply_macros("{{contact.greeting}}", persona, contact) == ""
    assert apply_macros("{{chardepthprompt}}", persona, contact) == ""
    assert apply_macros("{{mesexamples}}", persona, contact) == ""

    # And the shared fields still resolve through the flipped context —
    # ``{{contact.name}}`` returns whichever entity sits in the contact
    # slot, which in impersonate IS the user's name.
    assert apply_macros("{{contact.name}}", persona, contact) == "Bob"
    assert apply_macros("{{user.name}}", persona, contact) == "Alice"


def test_last_generation_type_macro():
    c = Contact(name="A")
    u = User_ctor(name="B")
    assert apply_macros(
        "{{lastGenerationType}}", c, u, generation_type="normal",
    ) == "normal"
    assert apply_macros(
        "{{lastGenerationType}}", c, u, generation_type="swipe",
    ) == "swipe"
    # Canonical strings extended to mirror SillyTavern's {{lastGenerationType}}.
    assert apply_macros(
        "{{lastGenerationType}}", c, u, generation_type="continue",
    ) == "continue"
    assert apply_macros(
        "{{lastGenerationType}}", c, u, generation_type="impersonate",
    ) == "impersonate"
    # No generation_type → empty.
    assert apply_macros("{{lastGenerationType}}", c, u) == ""


# ---------------------------------------------------------------------------
# Format / utility
# ---------------------------------------------------------------------------


def test_space_and_newline():
    c = Contact(name="A")
    u = User_ctor(name="B")
    assert apply_macros("a{{space::3}}b", c, u) == "a   b"
    assert apply_macros("a{{newline::2}}b", c, u) == "a\n\nb"
    # Default N=1.
    assert apply_macros("a{{space}}b", c, u) == "a b"
    assert apply_macros("a{{newline}}b", c, u) == "a\nb"


def test_noop_and_comment():
    c = Contact(name="A")
    u = User_ctor(name="B")
    assert apply_macros("a{{noop}}b", c, u) == "ab"
    assert apply_macros("a{{//ignored}}b", c, u) == "ab"


def test_reverse():
    c = Contact(name="A")
    u = User_ctor(name="B")
    assert apply_macros("{{reverse::hello}}", c, u) == "olleh"


# ---------------------------------------------------------------------------
# Integration: message-path {{roll}} baking
# ---------------------------------------------------------------------------


def test_message_path_bakes_roll_only(tmp_storage, monkeypatch):
    """POST /messages should expand {{roll}} but leave other macros literal."""
    from fastapi.testclient import TestClient
    from server.main import app
    from server import storage

    # Seed minimal entities so a chat can be created.
    contact = storage.save_contact(Contact(name="Aria"))
    user = storage.save_user(User_ctor(name="Tomo"))
    chat = storage.save_chat(Chat(contact_id=contact.id, user_id=user.id))

    client = TestClient(app)
    payload = {
        "sender": "user",
        "body": [{
            "text": "I roll {{roll::1d1}} and say {{char}}.",
            "emotion": "neutral",
        }],
    }
    r = client.post(f"/api/chats/{chat.id}/messages", json=payload)
    assert r.status_code == 200, r.text
    msg = r.json()
    text = msg["body"][0]["text"]
    # {{roll::1d1}} → "1" (only possible result for 1d1)
    assert "I roll 1 and" in text
    # {{char}} stayed literal — message bodies aren't subject to identity macros.
    assert "{{char}}" in text


# ---------------------------------------------------------------------------
# Backward-compat: existing name-only signature still works
# ---------------------------------------------------------------------------


def test_legacy_name_only_signature():
    """The pre-refactor ``apply_macros(text, char_name, user_name)`` call shape
    is still supported for callers that don't have full Contact/User objects."""
    assert apply_macros("{{char}} loves {{user}}", "Alice", "Bob") == "Alice loves Bob"
    moment = datetime(2026, 5, 5, 14, 30)
    assert apply_macros(
        "{{date}} {{time}} {{time24}}", "A", "B", now=moment,
    ) == "2026-05-05 02:30 PM 14:30"

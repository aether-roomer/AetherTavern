"""Byte-for-byte tests for the AER context-format builders: system prompt,
message body, brain blocks, style instruction, macro replacement."""
from __future__ import annotations

from datetime import datetime

from server.aer.format import (
    build_local_brain_block,
    build_style_instruction,
    build_system_prompt,
    format_message,
)
from server.aer.macros import apply_macros
from server.models import (
    Brain,
    ChatMessage,
    Contact,
    Emotion,
    ExampleChat,
    ExampleMessage,
    Intimacy,
    ResponseLength,
    Scenario,
    Style,
    SubMessage,
    User,
)


def _alice_bob_setup():
    contact = Contact(
        id="char1",
        name="Alice",
        persona="Cheerful and curious.",
        appearance="Short red hair, green eyes.",
        brains=[Brain(name="Mochi", content="Alice's fluffy orange tabby cat.")],
    )
    user = User(id="user1", name="Bob", persona="Laid-back and witty.")
    scenario = Scenario(id="scen1", name="Apartment", environment="Alice's apartment, evening.")
    return contact, user, scenario


def test_system_prompt_matches_spec_example():
    """A canonical Alice + Bob + apartment-scenario setup, with one global
    contact brain, rendered as a complete system prompt and compared
    byte-for-byte to the expected output."""
    contact, user, scenario = _alice_bob_setup()

    expected = (
        "AetherRoom\n"
        "----\n"
        "Alice\n"
        "Type: contact\n"
        "Personality: Cheerful and curious.\n"
        "Appearance: Short red hair, green eyes.\n"
        "Relationship: close\n"
        "----\n"
        "Bob\n"
        "Type: user\n"
        "Personality: Laid-back and witty.\n"
        "----\n"
        "Environment: Alice's apartment, evening.\n"
        "----\n"
        "Mochi\n"
        "Alice's fluffy orange tabby cat."
    )

    actual = build_system_prompt(contact, user, scenario, Intimacy.CLOSE)
    assert actual == expected


def test_message_body_format():
    msg = ChatMessage(
        id="m1",
        sender="contact",
        sender_name="Alice",
        body=[
            SubMessage(
                text="Hey Bob! Come on in.\nI just made some tea, want a cup?",
                emotion=Emotion.HAPPY,
            )
        ],
    )
    expected = (
        "Alice: Hey Bob! Come on in.\n"
        "    I just made some tea, want a cup?\n"
        "  Emotion: happy"
    )
    assert format_message(msg) == expected


def test_contact_message_with_none_emotion_defaults_to_neutral():
    """Generic-origin bubbles carry ``emotion=None``. Formatting such a
    bubble through AER falls back to NEUTRAL rather than dereferencing
    ``.value`` on the missing emotion."""
    msg = ChatMessage(
        id="m_none",
        sender="contact",
        sender_name="Alice",
        body=[
            SubMessage(text="Hi there.", emotion=None),
        ],
    )
    out = format_message(msg)
    assert "Hi there." in out
    assert "Emotion: neutral" in out


def test_user_message_no_emotion_tag():
    msg = ChatMessage(
        id="m2",
        sender="user",
        sender_name="Bob",
        body=[SubMessage(text="Sure, sounds great. Nice bookshelf by the way.")],
    )
    assert format_message(msg) == "Bob: Sure, sounds great. Nice bookshelf by the way."


def test_local_brain_block():
    brains = [Brain(name="The Bookshelf", content="A tall oak bookshelf filled with mystery novels.")]
    expected = "----\nThe Bookshelf\nA tall oak bookshelf filled with mystery novels."
    assert build_local_brain_block(brains) == expected


def test_style_instruction_variants():
    assert build_style_instruction(Style.CHAT, None, False) == "[ Style: chat ]"
    assert (
        build_style_instruction(Style.ROLEPLAY, None, False, is_greeting=True)
        == "[ Style: roleplay, greeting ]"
    )
    assert (
        build_style_instruction(Style.CHAT, ResponseLength.LONG, False)
        == "[ Style: chat; Response length: long ]"
    )
    assert (
        build_style_instruction(Style.NOVEL, ResponseLength.MEDIUM, True)
        == "[ Style: roleplay, novel style; Response length: medium; CJK ]"
    )
    assert (
        build_style_instruction(Style.CHAT, None, True, is_greeting=True)
        == "[ Style: chat, greeting; CJK ]"
    )


def test_chat_tags_emit_scenario_section_without_scenario():
    contact = Contact(id="c", name="Alice", persona="Friendly.")
    user = User(id="u", name="Bob")
    out = build_system_prompt(
        contact, user, None, Intimacy.STRANGER, chat_tags="comedy, slow burn"
    )
    assert out.endswith("----\nTags: comedy, slow burn")
    assert "Environment:" not in out
    assert "Scene:" not in out


def test_chat_tags_render_with_scenario_environment():
    contact = Contact(id="c", name="Alice", persona="Friendly.")
    user = User(id="u", name="Bob")
    scenario = Scenario(
        id="s", name="Apt", environment="Alice's apartment.", tags="ignored-seed"
    )
    out = build_system_prompt(
        contact, user, scenario, Intimacy.STRANGER, chat_tags="comedy"
    )
    # Scenario.tags is a seed for chat.tags at creation, not rendered here.
    assert out.endswith("Tags: comedy")
    assert "ignored-seed" not in out
    assert "Environment: Alice's apartment.\n" in out


def test_empty_chat_tags_with_scenario_does_not_leak_scenario_tags():
    contact = Contact(id="c", name="Alice", persona="Friendly.")
    user = User(id="u", name="Bob")
    scenario = Scenario(
        id="s",
        name="Apt",
        environment="Alice's apartment.",
        scene="Evening tea.",
        tags="should-not-appear",
    )
    out = build_system_prompt(contact, user, scenario, Intimacy.STRANGER, chat_tags="")
    assert "Environment: Alice's apartment.\n" in out
    assert out.endswith("Scene: Evening tea.")
    assert "Tags:" not in out
    assert "should-not-appear" not in out


def test_empty_chat_tags_and_no_scenario_emits_no_section():
    contact = Contact(id="c", name="Alice", persona="Friendly.")
    user = User(id="u", name="Bob")
    out = build_system_prompt(contact, user, None, Intimacy.STRANGER)
    assert "Environment:" not in out
    assert "Scene:" not in out
    assert "Tags:" not in out


def test_stranger_intimacy_uses_opinion_line():
    contact = Contact(id="c", name="Alice", persona="Friendly.")
    user = User(id="u", name="Bob")
    out = build_system_prompt(contact, user, None, Intimacy.STRANGER)
    assert "Opinion of Bob: Doesn't know Bob.\n" in out
    assert "Relationship:" not in out


def test_example_quotes_use_ensp():
    contact = Contact(
        id="c",
        name="Alice",
        persona="Friendly.",
        example_chats=[
            ExampleChat(
                name="example",
                style=Style.CHAT,
                user_name="Stranger",
                messages=[
                    ExampleMessage(is_contact=True, text="Hi!", emotion=Emotion.HAPPY),
                    ExampleMessage(is_contact=False, text="Hello."),
                ],
            )
        ],
    )
    user = User(id="u", name="Bob")
    out = build_system_prompt(contact, user, None, Intimacy.STRANGER)
    ensp = "\u2002"
    assert "Example quotes:\n1.\n" in out
    assert f"{ensp}Alice: Hi!\n{ensp}  Emotion: happy\n" in out
    assert f"{ensp}Stranger: Hello.\n" in out


def test_fields_with_padding_are_stripped_in_system_prompt():
    """Fields fed in with trailing newlines and leading whitespace must not
    leak that whitespace into the rendered system prompt. Pins the contract
    that ``build_system_prompt`` strips both each field and the final
    output, so no trailing ``\\n`` ends the prompt and no padded names
    appear inline."""
    contact = Contact(
        id="c",
        name="  Alice\n",
        persona="\n  Cheerful and curious.\n\n",
        appearance="Red hair.\n",
        brains=[Brain(name="  Mochi  \n", content="\n\nAlice's cat.\n\n")],
        example_chats=[
            ExampleChat(
                name="example",
                style=Style.CHAT,
                user_name="  Stranger\n",
                messages=[
                    ExampleMessage(is_contact=True, text="Hi!\n", emotion=Emotion.HAPPY),
                    ExampleMessage(is_contact=False, text="\n  Hello.\n"),
                ],
            )
        ],
    )
    user = User(id="u", name="Bob \n", persona="Witty.\n\n")

    out = build_system_prompt(contact, user, None, Intimacy.CLOSE)

    # No trailing whitespace at the end of the prompt.
    assert out == out.rstrip()
    assert not out.endswith("\n")
    # No trace of the un-stripped padded fields.
    assert "Alice\n" in out  # the rendered name line, but...
    assert "  Alice" not in out  # ...not with leading spaces
    assert "Alice  " not in out  # ...nor trailing spaces
    assert "Bob \n" not in out
    assert "Mochi  " not in out
    assert "  Mochi" not in out
    # Brain rendered with stripped name + content.
    assert "----\nMochi\nAlice's cat." in out
    # Personality + appearance fields stripped (no leading newlines / trailing blanks).
    assert "Personality: Cheerful and curious.\n" in out
    assert "Appearance: Red hair.\n" in out
    # Example quote sender names are stripped too.
    ensp = "\u2002"  # EN SPACE
    assert f"{ensp}Stranger: Hello." in out
    assert "Stranger\n" not in out  # the un-stripped trailing-newline form
    # The brain block ends the prompt — its content must not retain
    # the input's trailing blank line.
    assert out.endswith("Alice's cat.")


def test_format_message_strips_padding():
    """Message bodies fed in with trailing newlines and leading whitespace
    must not leak that whitespace before /nothink (user) or after the
    emotion tag (contact)."""
    contact_msg = ChatMessage(
        id="m1",
        sender="contact",
        sender_name="  Alice  ",
        body=[SubMessage(text="\n\n  Hi there!  \n\n", emotion=Emotion.HAPPY)],
    )
    out = format_message(contact_msg)
    # No trailing whitespace after the emotion tag.
    assert not out.endswith("\n")
    assert not out.endswith(" ")
    # Sender name stripped, text stripped.
    assert out == "Alice: Hi there!\n  Emotion: happy"

    user_msg = ChatMessage(
        id="m2",
        sender="user",
        sender_name="Bob\n",
        body=[SubMessage(text="Sure.   \n\n")],
    )
    uout = format_message(user_msg)
    assert not uout.endswith("\n")
    assert uout == "Bob: Sure."


def test_local_brain_block_strips_padding():
    bb = build_local_brain_block([
        Brain(name="  Note\n", content="\n  hello\n\n"),
    ])
    assert bb == "----\nNote\nhello"
    assert not bb.endswith("\n")


def test_macro_replacement_basic():
    text = "{{char}} loves {{user}}. Hi {{Char}}!"
    assert apply_macros(text, "Alice", "Bob") == "Alice loves Bob. Hi Alice!"


def test_macro_replacement_dollar_word_boundary():
    text = "Hi $c! Talk to $user. Don't match $contractor or $username."
    out = apply_macros(text, "Alice", "Bob")
    assert "Hi Alice!" in out
    assert "Talk to Bob." in out
    assert "$contractor" in out
    assert "$username" in out


def test_macro_replacement_date_time():
    moment = datetime(2026, 5, 5, 14, 30)
    out = apply_macros("{{date}} {{time}} {{time24}}", "A", "B", now=moment)
    assert out == "2026-05-05 02:30 PM 14:30"


def test_macro_replacement_time_morning():
    moment = datetime(2026, 5, 5, 9, 5)
    assert apply_macros("{{time}}", "A", "B", now=moment) == "09:05 AM"


def test_macro_replacement_midnight():
    moment = datetime(2026, 5, 5, 0, 30)
    assert apply_macros("{{time}}", "A", "B", now=moment) == "12:30 AM"

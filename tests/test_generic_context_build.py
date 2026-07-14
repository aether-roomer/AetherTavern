"""Generic-mode context builder contracts.

Pipeline: system prompt (preset blocks) → non-floating additional messages
→ history (AER-multi-bubble split into N API messages; Generic-origin
single-bubble) → floating additional messages at depth → conditional brain
splice → reminder brain splice → two-tier rollover.

Most contracts are testable against synthetic Contact/User/Scenario
fixtures; we avoid hitting any LLM upstream and exercise the builder's
own assembly logic. Brain budget / rollover are exercised through their
parametrised AER helpers — the test stubs those when needed.
"""
from __future__ import annotations

import pytest

from server.generic.context import (
    build_messages_for_generic,
    guesstimate_tokens,
)
from server.models import (
    Chat,
    ChatMessage,
    Contact,
    ContextPreset,
    ContextPresetAdditionalMessage,
    ContextPresetBlock,
    Preset,
    Settings,
    SubMessage,
    User,
)


def _make_chat(**kwargs):
    defaults = {"contact_id": "c", "user_id": "u"}
    defaults.update(kwargs)
    return Chat(**defaults)


def _simple_preset(*, prefix_names=True, blocks=None, additionals=None):
    return ContextPreset(
        name="T",
        prefix_names=prefix_names,
        system_prompt_blocks=blocks or [
            ContextPresetBlock(name="b", content="You are {{contact.name}}."),
        ],
        additional_messages=additionals or [],
    )


def test_minimal_system_prompt_renders():
    chat = _make_chat()
    contact = Contact(name="Roxy")
    user = User(name="Anon")
    preset = _simple_preset()
    gen_preset = Preset()
    settings = Settings()

    ctx = build_messages_for_generic(
        chat=chat, contact=contact, user=user, scenario=None,
        preset=preset, generation_preset=gen_preset,
        history=[], settings=settings,
    )
    assert len(ctx.api_messages) == 1
    assert ctx.api_messages[0]["role"] == "system"
    assert "You are Roxy." in ctx.api_messages[0]["content"]


def test_disabled_block_skipped():
    chat = _make_chat()
    contact = Contact(name="C")
    user = User(name="U")
    preset = ContextPreset(
        name="T",
        system_prompt_blocks=[
            ContextPresetBlock(name="on", content="ON", enabled=True),
            ContextPresetBlock(name="off", content="OFF", enabled=False),
        ],
    )
    ctx = build_messages_for_generic(
        chat=chat, contact=contact, user=user, scenario=None,
        preset=preset, generation_preset=Preset(),
        history=[], settings=Settings(),
    )
    sys_content = ctx.api_messages[0]["content"]
    assert "ON" in sys_content
    assert "OFF" not in sys_content


def test_disabled_additional_message_skipped():
    chat = _make_chat()
    preset = _simple_preset(additionals=[
        ContextPresetAdditionalMessage(
            name="on", role="assistant", mode="simple",
            simple_content="kept", enabled=True,
        ),
        ContextPresetAdditionalMessage(
            name="off", role="assistant", mode="simple",
            simple_content="dropped", enabled=False,
        ),
    ])
    ctx = build_messages_for_generic(
        chat=chat, contact=Contact(name="C"), user=User(name="U"),
        scenario=None,
        preset=preset, generation_preset=Preset(),
        history=[], settings=Settings(),
    )
    contents = [m["content"] for m in ctx.api_messages]
    assert any("kept" in c for c in contents)
    assert not any("dropped" in c for c in contents)


def test_aer_multi_bubble_merges_into_single_api_message():
    """AER-origin history messages can carry multi-bubble bodies (from
    the bubble-merging that happens at AER import). Generic mode has
    no bubble concept — the builder collapses them into ONE api_message
    per ChatMessage, joining bubble texts with ``\\n\\n``. Emitting one
    api_message per bubble (the prior behavior) caused consecutive
    same-role API messages which some providers reject + empty content
    for empty bubbles."""
    chat = _make_chat()
    contact = Contact(name="C")
    user = User(name="U")
    history = [
        ChatMessage(
            id="m1", parent_id=None, sender="contact", sender_name="C",
            origin="aer",
            body=[
                SubMessage(text="bubble one", emotion="neutral"),
                SubMessage(text="bubble two", emotion="happy"),
            ],
        ),
    ]
    ctx = build_messages_for_generic(
        chat=chat, contact=contact, user=user, scenario=None,
        preset=_simple_preset(prefix_names=False),
        generation_preset=Preset(),
        history=history, settings=Settings(),
    )
    # System + ONE assistant message containing both bubbles joined.
    assistant_msgs = [m for m in ctx.api_messages if m["role"] == "assistant"]
    assert len(assistant_msgs) == 1
    assert assistant_msgs[0]["content"] == "bubble one\n\nbubble two"


def test_empty_bubbles_are_skipped_in_generic_history():
    """A message whose bubbles all strip to empty is dropped from the
    api_messages list entirely — better than emitting ``{role: "user",
    content: ""}`` which some providers reject + accumulates badly on
    consecutive same-role messages."""
    chat = _make_chat()
    contact = Contact(name="C")
    user = User(name="U")
    history = [
        ChatMessage(
            id="m1", parent_id=None, sender="user", sender_name="U",
            origin="manual",
            body=[
                SubMessage(text="   ", emotion="neutral"),
                SubMessage(text="", emotion="neutral"),
            ],
        ),
        ChatMessage(
            id="m2", parent_id="m1", sender="contact", sender_name="C",
            origin="generic",
            body=[SubMessage(text="real reply", emotion=None)],
        ),
    ]
    ctx = build_messages_for_generic(
        chat=chat, contact=contact, user=user, scenario=None,
        preset=_simple_preset(prefix_names=False),
        generation_preset=Preset(),
        history=history, settings=Settings(),
    )
    # Only the contact's "real reply" lands; the empty user message
    # is silently skipped.
    history_msgs = [m for m in ctx.api_messages if m["role"] in ("user", "assistant")]
    assert len(history_msgs) == 1
    assert history_msgs[0]["role"] == "assistant"
    assert history_msgs[0]["content"] == "real reply"


def test_prefix_names_prepends_speaker_in_api_payload():
    chat = _make_chat()
    contact = Contact(name="Roxy")
    user = User(name="Anon")
    history = [
        ChatMessage(
            id="m1", parent_id=None, sender="user", sender_name="Anon",
            origin="manual",
            body=[SubMessage(text="hello", emotion="neutral")],
        ),
        ChatMessage(
            id="m2", parent_id="m1", sender="contact", sender_name="Roxy",
            origin="generic",
            body=[SubMessage(text="hi back", emotion=None)],
        ),
    ]
    ctx = build_messages_for_generic(
        chat=chat, contact=contact, user=user, scenario=None,
        preset=_simple_preset(prefix_names=True),
        generation_preset=Preset(),
        history=history, settings=Settings(),
    )
    history_msgs = [m for m in ctx.api_messages if m["role"] in ("user", "assistant")]
    assert len(history_msgs) == 2
    assert history_msgs[0]["content"].startswith("Anon:\n")
    assert "hello" in history_msgs[0]["content"]
    assert history_msgs[1]["content"].startswith("Roxy:\n")
    assert "hi back" in history_msgs[1]["content"]


def test_floating_additional_message_at_depth_zero_lands_at_tail():
    """``depth=0`` is the only guaranteed-at-tail position."""
    chat = _make_chat()
    contact = Contact(name="C")
    user = User(name="U")
    history = [
        ChatMessage(
            id="m1", parent_id=None, sender="user", sender_name="U",
            body=[SubMessage(text="msg-a", emotion="neutral")],
        ),
        ChatMessage(
            id="m2", parent_id="m1", sender="contact", sender_name="C",
            body=[SubMessage(text="msg-b", emotion="neutral")],
        ),
    ]
    preset = _simple_preset(prefix_names=False, additionals=[
        ContextPresetAdditionalMessage(
            name="prefill", role="assistant", mode="simple",
            simple_content="PREFILL", float_enabled=True, float_depth=0,
        ),
    ])
    ctx = build_messages_for_generic(
        chat=chat, contact=contact, user=user, scenario=None,
        preset=preset, generation_preset=Preset(),
        history=history, settings=Settings(),
    )
    # Last entry must be the depth-0 float.
    assert ctx.api_messages[-1]["content"] == "PREFILL"
    assert ctx.api_messages[-1]["role"] == "assistant"


def test_floating_messages_higher_depth_further_from_tail():
    """``depth=N`` lands N positions from the tail at the moment of
    insertion. Higher depths go first → lower depths land closer to
    the tail."""
    chat = _make_chat()
    contact = Contact(name="C")
    user = User(name="U")
    preset = _simple_preset(prefix_names=False, additionals=[
        ContextPresetAdditionalMessage(
            name="depth5", role="system", mode="simple",
            simple_content="DEPTH5", float_enabled=True, float_depth=5,
        ),
        ContextPresetAdditionalMessage(
            name="depth0", role="assistant", mode="simple",
            simple_content="DEPTH0", float_enabled=True, float_depth=0,
        ),
    ])
    history = [
        ChatMessage(
            id=f"m{i}", parent_id=f"m{i-1}" if i > 1 else None,
            sender="user" if i % 2 else "contact",
            sender_name="U" if i % 2 else "C",
            body=[SubMessage(text=f"history-{i}", emotion="neutral")],
        )
        for i in range(1, 4)
    ]
    ctx = build_messages_for_generic(
        chat=chat, contact=contact, user=user, scenario=None,
        preset=preset, generation_preset=Preset(),
        history=history, settings=Settings(),
    )
    # depth=0 is the absolute tail.
    assert ctx.api_messages[-1]["content"] == "DEPTH0"
    # depth=5 lands further up — verify it's somewhere BEFORE depth=0.
    depths = [i for i, m in enumerate(ctx.api_messages)
              if m["content"] in ("DEPTH5", "DEPTH0")]
    assert len(depths) == 2
    assert depths[0] < depths[1]


def test_guesstimate_matches_silly_tavern_formula():
    """SillyTavern's UTF-8/3.35 formula. Empty → 0; pure ASCII rounds
    up via math.ceil; CJK gets ~3 bytes per char."""
    assert guesstimate_tokens("") == 0
    # "hello world" → 11 bytes → ceil(11/3.35) = 4
    assert guesstimate_tokens("hello world") == 4
    # "こんにちは" → 15 bytes (3 per char × 5) → ceil(15/3.35) = 5
    assert guesstimate_tokens("こんにちは") == 5


def test_brain_message_role_threads_through_to_unconditional_inline_brain(monkeypatch):
    """When ``settings.generic.<provider>.brain_message_role`` is
    ``"user"``, per-message unconditional brains attached to history
    messages should be emitted with that role (instead of the default
    ``"system"``)."""
    chat = _make_chat()
    contact = Contact(name="C")
    user = User(name="U")
    from server.models import Brain
    brain = Brain(name="inline", content="brain content")
    history = [
        ChatMessage(
            id="m1", parent_id=None, sender="user", sender_name="U",
            origin="manual",
            body=[SubMessage(text="hi", emotion="neutral")],
            brains=[brain],
        ),
    ]
    settings = Settings(provider_mode="generic")
    settings.generic.provider = "novelai"
    settings.generic.novelai.brain_message_role = "user"
    settings.generic.novelai.api_token = "test"
    settings.generic.novelai.model_id = "test-model"

    ctx = build_messages_for_generic(
        chat=chat, contact=contact, user=user, scenario=None,
        preset=_simple_preset(prefix_names=False),
        generation_preset=Preset(),
        history=history, settings=settings,
    )
    # The inline brain message should carry the configured role.
    brain_messages = [m for m in ctx.api_messages
                      if m["content"].startswith("----")]
    assert brain_messages, "expected one inline brain message"
    assert brain_messages[0]["role"] == "user"

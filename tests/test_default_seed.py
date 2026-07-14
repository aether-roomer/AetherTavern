"""First-boot seed: default ContextPreset + backfill into Settings.generic."""
from __future__ import annotations

import pytest

from server import storage
from server.aer.context_preset_render import (
    render_additional_messages,
    render_system_prompt,
)
from server.aer.macros import MacroCtx
from server.models import (
    Brain,
    Chat,
    Contact,
    Intimacy,
    Scenario,
    Settings,
    Style,
    User,
)


# ---------------------------------------------------------------------------
# Seeding behaviour
# ---------------------------------------------------------------------------


def test_first_boot_seeds_one_default(tmp_storage):
    ps = storage.list_context_presets()
    assert len(ps) == 1
    p = ps[0]
    assert p.name == "Default"
    assert len(p.system_prompt_blocks) == 6
    assert [b.name for b in p.system_prompt_blocks] == [
        "Intro", "Character", "User", "Scenario", "Lore", "HTML output",
    ]
    # The HTML-output block ships enabled but is macro-gated; the on/off flag
    # isn't what hides it.
    assert p.system_prompt_blocks[-1].enabled is True
    assert len(p.additional_messages) == 1
    msg = p.additional_messages[0]
    assert msg.name == "Prefill"
    assert msg.role == "assistant"
    assert msg.mode == "simple"
    assert msg.float_enabled is True
    assert msg.float_depth == 0


def test_seed_backfills_provider_context_preset_ids(tmp_storage):
    p = storage.list_context_presets()[0]
    s = storage.load_settings()
    assert s.generic.novelai.context_preset_id == p.id
    assert s.generic.openrouter.context_preset_id == p.id
    assert s.generic.nanogpt.context_preset_id == p.id


def test_seed_does_not_overwrite_existing_provider_preset_id(tmp_storage):
    # First-boot seed already ran. Edit a provider id, then re-run init —
    # the seed branch skips because index already has a preset.
    s = storage.load_settings()
    s.generic.openrouter.context_preset_id = "user-chosen-id"
    storage.atomic_write_yaml(storage.SETTINGS_PATH, s)
    storage.initialize()
    s = storage.load_settings()
    assert s.generic.openrouter.context_preset_id == "user-chosen-id"


# ---------------------------------------------------------------------------
# Rendering — full-fields → spec-verbatim output
# ---------------------------------------------------------------------------


def _ctx(contact, user, scenario=None, chat=None, libraries=None):
    return MacroCtx(
        contact=contact, user=user, scenario=scenario, chat=chat,
        libraries=list(libraries or []),
    )


def _chat(**kwargs):
    base = dict(
        contact_id="c", user_id="u",
        tags="", intimacy=Intimacy.STRANGER, style=Style.CHAT, cjk=False,
    )
    base.update(kwargs)
    return Chat(**base)


def test_seed_renders_full_fields_verbatim(tmp_storage):
    p = storage.list_context_presets()[0]
    c = Contact(
        name="NAME", species="SPECIES", pronouns="PRONOUNS",
        persona="PERSONA", appearance="APPEARANCE", tags="TAGS",
    )
    u = User(
        name="UNAME", species="USPECIES", pronouns="UPRONOUNS",
        persona="UPERSONA", appearance="UAPPEARANCE", tags="UTAGS",
    )
    s = Scenario(name="S", environment="ENV", scene="SCENE")
    chat = _chat(tags="STAGS")
    c.brains = [Brain(name="Brain 1", content="content")]

    out = render_system_prompt(p, _ctx(c, u, scenario=s, chat=chat))

    expected = (
        "You are roleplaying a character that interacts with the user's "
        "character. The user is looking for an immersive experience and "
        "consents to all kinds of content, including darker and adult "
        "themes and NSFW. You are allowed to freely explore any theme "
        "that comes up within the context of this fictional and "
        "consensual roleplay.\n"
        "\n"
        "<CHARACTER>\n"
        "Name: NAME\n"
        "Species: SPECIES\n"
        "Pronouns: PRONOUNS\n"
        "\n"
        "Personality:\n"
        "PERSONA\n"
        "\n"
        "Appearance:\n"
        "APPEARANCE\n"
        "\n"
        "Tags: TAGS\n"
        "</CHARACTER>\n"
        "\n"
        "<USER>\n"
        "Name: UNAME\n"
        "Species: USPECIES\n"
        "Pronouns: UPRONOUNS\n"
        "\n"
        "Personality:\n"
        "UPERSONA\n"
        "\n"
        "Appearance:\n"
        "UAPPEARANCE\n"
        "\n"
        "Tags: UTAGS\n"
        "</USER>\n"
        "\n"
        "<SCENARIO>\n"
        "Environment:\n"
        "ENV\n"
        "\n"
        "Scene:\n"
        "SCENE\n"
        "\n"
        "Tags: STAGS\n"
        "</SCENARIO>\n"
        "\n"
        "<LORE>\n"
        "----\n"
        "Brain 1\n"
        "content\n"
        "</LORE>"
    )
    assert out == expected


def test_seed_html_output_block_gated_on_sanitize(tmp_storage):
    """The HTML-output block stays out of the prompt while Sanitize HTML is on
    (the default) and appears as the final section only when it's off."""
    p = storage.list_context_presets()[0]
    c = Contact(name="NAME")
    u = User(name="UNAME")
    chat = _chat()

    on = MacroCtx(contact=c, user=u, chat=chat, settings=Settings())
    assert "<FORMATTING>" not in render_system_prompt(p, on)

    off_ctx = MacroCtx(
        contact=c, user=u, chat=chat,
        settings=Settings(sanitize_generic_html=False),
    )
    out_off = render_system_prompt(p, off_ctx)
    assert "<FORMATTING>" in out_off
    assert "<|RAWHTML|> and <|/RAWHTML|>" in out_off
    assert "<script>" in out_off
    # Lands last, separated by a blank line from the preceding section.
    assert out_off.rstrip().endswith("</FORMATTING>")
    assert "\n\n<FORMATTING>" in out_off


# ---------------------------------------------------------------------------
# Partial-fill outputs — no orphan headers, no trailing blanks
# ---------------------------------------------------------------------------


def test_seed_contact_name_only(tmp_storage):
    p = storage.list_context_presets()[0]
    c = Contact(name="NAME")
    u = User(name="UNAME")
    out = render_system_prompt(p, _ctx(c, u, chat=_chat()))
    assert "<CHARACTER>\nName: NAME\n</CHARACTER>" in out
    assert "<USER>\nName: UNAME\n</USER>" in out
    assert "<SCENARIO>" not in out
    assert "<LORE>" not in out


def test_seed_contact_name_and_tags(tmp_storage):
    p = storage.list_context_presets()[0]
    c = Contact(name="NAME", tags="TAGS")
    u = User(name="UNAME")
    out = render_system_prompt(p, _ctx(c, u, chat=_chat()))
    # Tags-wart: flush continuation under Name when no multi-line field
    # precedes (so no blank line between Name and Tags).
    assert "<CHARACTER>\nName: NAME\nTags: TAGS\n</CHARACTER>" in out


def test_seed_contact_persona_then_tags_has_blank_above_tags(tmp_storage):
    p = storage.list_context_presets()[0]
    c = Contact(name="NAME", persona="PERSONA", tags="TAGS")
    u = User(name="UNAME")
    out = render_system_prompt(p, _ctx(c, u, chat=_chat()))
    # The Tags wart: when a multi-line field (Persona) preceded, Tags
    # gets a blank line above it.
    assert "Personality:\nPERSONA\n\nTags: TAGS\n</CHARACTER>" in out


def test_seed_scenario_scene_only(tmp_storage):
    p = storage.list_context_presets()[0]
    c = Contact(name="NAME")
    u = User(name="UNAME")
    s = Scenario(name="S", environment="", scene="SCENE")
    out = render_system_prompt(p, _ctx(c, u, scenario=s, chat=_chat()))
    assert "<SCENARIO>\nScene:\nSCENE\n</SCENARIO>" in out


def test_seed_scenario_env_and_chat_tags_no_orphan_scene(tmp_storage):
    p = storage.list_context_presets()[0]
    c = Contact(name="NAME")
    u = User(name="UNAME")
    s = Scenario(name="S", environment="ENV", scene="")
    out = render_system_prompt(p, _ctx(c, u, scenario=s, chat=_chat(tags="STAGS")))
    assert "<SCENARIO>\nEnvironment:\nENV\n\nTags: STAGS\n</SCENARIO>" in out
    assert "Scene:" not in out


def test_seed_scenario_all_empty_section_vanishes(tmp_storage):
    p = storage.list_context_presets()[0]
    c = Contact(name="NAME")
    u = User(name="UNAME")
    out = render_system_prompt(p, _ctx(c, u, chat=_chat()))
    assert "<SCENARIO>" not in out


def test_seed_lore_vanishes_without_global_brains(tmp_storage):
    p = storage.list_context_presets()[0]
    c = Contact(name="NAME")
    u = User(name="UNAME")
    out = render_system_prompt(p, _ctx(c, u, chat=_chat()))
    assert "<LORE>" not in out
    assert "----" not in out


def test_seed_prefill_renders_contact_name(tmp_storage):
    p = storage.list_context_presets()[0]
    c = Contact(name="Alice")
    u = User(name="Bob")
    msgs = render_additional_messages(p, _ctx(c, u, chat=_chat()))
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["float_enabled"] is True
    assert msgs[0]["float_depth"] == 0
    assert msgs[0]["content"].endswith("Alice:\n")
